"""Focused CI workflow and runtime-hooks manifest invariants (#1647, #1692)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"


def _chained_checks(hooks: object) -> list[str]:
    """Names of checks whose shell command chains two commands with `&&`."""
    return [row.id for row in getattr(hooks, "checks") if any("&&" in part for part in row.argv)]


def test_workflows_parse_and_ci_required_pins_tag_and_jobs() -> None:
    loaded = {p.name: yaml.safe_load(p.read_text(encoding="utf-8")) for p in sorted(WORKFLOWS.glob("*.yml"))}
    assert loaded, "expected .github/workflows/*.yml files"
    req = loaded["ci-required.yml"]["jobs"]
    req_needs = set(req["required"]["needs"])
    assert {"changes", "security", "db", "python", "runtime", "web", "images_build", "images_release"} <= req_needs
    changes_steps = "\n".join(str(step.get("run", "")) for step in req["changes"]["steps"])
    assert "merge-base --is-ancestor" in changes_steps
    assert loaded["ci-web.yml"]["jobs"]["browser"]["runs-on"] == "ubuntu-24.04"


def test_pyproject_and_lockfile_do_not_depend_on_qlib() -> None:
    assert "qlib" not in (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8").lower()
    assert 'name = "pyqlib"' not in (REPO_ROOT / "uv.lock").read_text(encoding="utf-8").lower()


EXPECTED_CELL_CHECKS = {
    "preview.operation": ["smoke"],
    "preview.post": ["health", "warm_surface", "walk_tree"],
    "staging.operation": ["smoke"],
    "staging.post": ["health", "warm_surface", "walk_tree"],
    "prod.operation": ["smoke"],
    "prod.post": ["health"],
    "scheduled_audit": ["nightly_verdicts"],
}


def _load_hooks():  # type: ignore[no-untyped-def]
    from infra2_sdk.manifests import load_hooks_manifest

    return load_hooks_manifest(REPO_ROOT / "runtime-hooks.manifest.json")


def test_runtime_hooks_manifest_is_schema_3_and_maps_each_check_to_its_cells() -> None:
    from infra2_sdk.manifests import CELL_POLICIES

    hooks = _load_hooks()
    assert hooks.schema_version == 3
    assert _chained_checks(hooks) == []
    resolved = {cell: [c.id for c in hooks.checks_for_cell(cell)] for cell in CELL_POLICIES}
    assert {cell: ids for cell, ids in resolved.items() if ids} == EXPECTED_CELL_CHECKS


def test_runtime_hooks_cells_run_their_checks_and_report_a_failed_check(tmp_path: Path) -> None:
    from infra2_sdk.manifests import run_cell

    hooks = _load_hooks()
    env = {key: "x" for row in hooks.checks for key in row.required_env}
    for cell in EXPECTED_CELL_CHECKS:
        cwd = tmp_path / f"pass_{cell}"
        cwd.mkdir()
        result = run_cell(
            hooks,
            cell,
            app_url="https://fake.invalid",
            version_ref="v0.0.1",
            cwd=cwd,
            ambient_env=env,
            runner=lambda argv, **_kw: subprocess.CompletedProcess(argv, 0, "", ""),
        )
        assert result.status == "pass"
        assert [c.name for c in result.checks] == EXPECTED_CELL_CHECKS[cell]

    ran: list[str] = []

    def fake(argv, **_kw):  # type: ignore[no-untyped-def]
        ran.append(argv[-1])
        if "warm_surface.sh" in argv[-1]:
            return subprocess.CompletedProcess(argv, 1, "", "boom: first line\nsecond line")
        return subprocess.CompletedProcess(argv, 0, "", "")

    cwd = tmp_path / "staging_post"
    cwd.mkdir()
    result = run_cell(
        hooks,
        "staging.post",
        app_url="https://fake.invalid",
        version_ref="v0.0.1",
        cwd=cwd,
        ambient_env=env,
        runner=fake,
    )
    assert result.status == "fail"
    from infra2_sdk.manifests import CELL_POLICIES

    assert CELL_POLICIES["staging.post"].stop_on_fail is False
    assert [(c.name, c.ok) for c in result.checks] == [("health", True), ("warm_surface", False), ("walk_tree", True)]
    assert any("walk-tree" in command for command in ran)


def test_runtime_hooks_manifest_refuses_schema_2_and_shell_chains() -> None:
    from infra2_sdk.manifests import RuntimeHooksManifest

    raw = json.loads((REPO_ROOT / "runtime-hooks.manifest.json").read_text(encoding="utf-8"))
    schema_2 = {**raw, "schema_version": 2}
    with pytest.raises(ValueError, match="schema_version"):
        RuntimeHooksManifest.from_dict(schema_2)

    with_chain = json.loads(json.dumps(raw))
    with_chain["checks"][0]["argv"] = ["sh", "-c", "first && second"]
    assert _chained_checks(RuntimeHooksManifest.from_dict(with_chain)) == ["smoke"]

    with_empty_checks = {**raw, "checks": []}
    with pytest.raises(ValueError, match="checks"):
        RuntimeHooksManifest.from_dict(with_empty_checks)


def test_browser_check_uses_headless_shell_and_fits_every_cell_budget() -> None:
    from infra2_sdk.manifests import CELL_POLICIES

    hooks = _load_hooks()
    walk_tree = next(row for row in hooks.checks if row.id == "walk_tree")
    assert "--only-shell" in walk_tree.bootstrap
    for cell in EXPECTED_CELL_CHECKS:
        for row in hooks.checks_for_cell(cell):
            assert row.timeout_seconds <= CELL_POLICIES[cell].budget_seconds, (cell, row.id)
