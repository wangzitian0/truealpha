"""Focused CI workflow and runtime-hooks manifest invariants (#1647, #1692)."""

from __future__ import annotations

import ast
import json
import re
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.gate

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


def _declared_pytest_markers(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets):
            continue
        elts = node.value.elts if isinstance(node.value, (ast.List, ast.Tuple)) else [node.value]
        for elt in elts:
            if (
                isinstance(elt, ast.Attribute)
                and isinstance(elt.value, ast.Attribute)
                and isinstance(elt.value.value, ast.Name)
                and elt.value.value.id == "pytest"
                and elt.value.attr == "mark"
            ):
                found.add(elt.attr)
    return found


def test_ci_python_shards_cover_all_tests_and_isolate_s3() -> None:
    ci_python = yaml.safe_load((WORKFLOWS / "ci-python.yml").read_text(encoding="utf-8"))
    jobs = ci_python["jobs"]
    assert set(jobs) == {"gates", "test-s3", "test-deps-smoke", "dagster-code-server-liveness"}

    wf_env = ci_python["env"]
    s3_core = set(str(wf_env["S3_CORE_TEST_FILES"]).split())
    s3_data_engine = set(str(wf_env["S3_DATA_ENGINE_TEST_FILES"]).split())
    s3_declared = s3_core | s3_data_engine
    assert s3_core and s3_data_engine
    assert not (s3_core & s3_data_engine)
    for rel in sorted(s3_declared):
        assert (REPO_ROOT / rel).is_file(), f"declared S3 test file missing: {rel}"

    s3_runs = "\n".join(str(step.get("run", "")) for step in jobs["test-s3"]["steps"])
    assert "docker compose up -d --wait s3" in s3_runs
    assert "S3_CORE_TEST_FILES" in s3_runs and "S3_DATA_ENGINE_TEST_FILES" in s3_runs

    deps_job = jobs["test-deps-smoke"]
    assert deps_job["strategy"]["matrix"]["shard"] == [0, 1, 2, 3]
    deps_runs = "\n".join(str(step.get("run", "")) for step in deps_job["steps"])
    assert "docker compose up" not in deps_runs
    assert "S3_CORE_TEST_FILES" in deps_runs and "S3_DATA_ENGINE_TEST_FILES" in deps_runs

    roots = (
        "common/contracts/tests",
        "common/factors/tests",
        "common/runtime/tests",
        "apps/llm-service/tests",
        "apps/data-engine/tests",
    )
    all_tests = {
        p.relative_to(REPO_ROOT).as_posix()
        for r in roots
        for pat in ("test_*.py", "*_test.py")
        for p in (REPO_ROOT / r).rglob(pat)
    }
    deps_selected = all_tests - s3_declared
    assert s3_declared <= all_tests
    assert not (s3_declared & deps_selected)
    assert (s3_declared | deps_selected) == all_tests

    allowed_markers = {"gate", "deps", "smoke", "contract"}
    unmarked = [p for p in sorted(all_tests) if not (_declared_pytest_markers(REPO_ROOT / p) & allowed_markers)]
    assert unmarked == [], f"tests missing Class 1/2 pytestmark: {unmarked}"


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
        record = json.loads((cwd / hooks.artifacts_dir / "checks.jsonl").read_text().splitlines()[-1])
        assert len(record["checks"]) >= 1
        assert all(
            isinstance(check.get("seconds"), (int, float)) and check["seconds"] >= 0 for check in record["checks"]
        )

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
    record = json.loads((cwd / hooks.artifacts_dir / "checks.jsonl").read_text().splitlines()[-1])
    assert record["checks"][1]["detail"] == "boom: first line"


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


def test_browser_check_uses_headless_shell_and_each_cell_fits_its_budget() -> None:
    from infra2_sdk.manifests import CELL_POLICIES

    hooks = _load_hooks()
    walk_tree = next(row for row in hooks.checks if row.id == "walk_tree")
    assert "--only-shell" in walk_tree.bootstrap
    for cell in EXPECTED_CELL_CHECKS:
        total = sum(row.timeout_seconds for row in hooks.checks_for_cell(cell))
        assert total <= CELL_POLICIES[cell].budget_seconds, (cell, total)


# Words that install or upgrade OS packages on the runner. SDK 4.5 separates hook bootstrap into
# a 120s `bootstrap_cap_seconds` (`BOOTSTRAP_CAP_SECONDS`) outside the cell budget; before that
# separation, on infra2 run 45331134994 the apt part alone took about 260 s of the 360 s
# `staging.post` cell (#1843).
_OS_PACKAGE_TOKENS = frozenset({"--with-deps", "install-deps", "apt", "apt-get", "aptitude", "dpkg"})


def _os_package_tokens(command: str) -> set[str]:
    """OS package words in a shell command, also inside quotes, pipes and absolute paths."""
    words = re.split(r"[\s;&|()\"'`]+", command)
    return {word.rsplit("/", 1)[-1] for word in words} & _OS_PACKAGE_TOKENS


def test_browser_installs_change_no_os_package() -> None:
    """#1843: no hook bootstrap and no step of the CI browser job runs apt.

    Measured on ubuntu-24.04: the headless shell has no missing library; apt added fonts
    and upgraded mesa. The staging walk and the CI walk must run on the same OS packages.
    """
    hooks = _load_hooks()
    bootstraps = {row.id: row.bootstrap for row in hooks.checks if row.bootstrap.strip()}
    assert "walk_tree" in bootstraps
    assert {cid: _os_package_tokens(cmd) for cid, cmd in bootstraps.items() if _os_package_tokens(cmd)} == {}

    browser_steps = yaml.safe_load((WORKFLOWS / "ci-web.yml").read_text(encoding="utf-8"))["jobs"]["browser"]["steps"]
    runs = [str(step["run"]) for step in browser_steps if "run" in step]
    assert sum("playwright install" in run for run in runs) == 1, runs
    assert [_os_package_tokens(run) for run in runs if _os_package_tokens(run)] == []

    # The predicate finds the form that the manifest used before #1843, and the usual others.
    with_deps = bootstraps["walk_tree"].replace("playwright install", "playwright install --with-deps")
    assert _os_package_tokens(with_deps) == {"--with-deps"}
    assert _os_package_tokens("bunx playwright install-deps chromium") == {"install-deps"}
    assert _os_package_tokens("sudo apt-get install -y fonts-unifont") == {"apt-get"}
    assert _os_package_tokens('bash -c "x|/usr/bin/apt install -y fonts-unifont"') == {"apt"}
    assert _os_package_tokens("sudo chown runner ~/.cache") == set()
