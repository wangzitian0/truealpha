"""Focused CI workflow and runtime-hooks manifest invariants (#1647, #1692)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"


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


def test_runtime_hooks_manifest_and_fail_fast_slots(tmp_path: Path) -> None:
    from infra2_sdk.manifests import load_hooks_manifest, run_hook_slot

    hooks = load_hooks_manifest(REPO_ROOT / "runtime-hooks.manifest.json")
    assert hooks.slots["preview_gate"].checks
    assert hooks.slots["preview_gate"].checks == hooks.slots["staging_gate"].checks
    env = {key: "x" for spec in hooks.slots.values() for key in spec.required_env}
    checks = hooks.slots["staging_gate"].checks
    assert len(checks) >= 2
    fail_argv = tuple(checks[1].argv)
    ran: list[str] = []

    def fake(argv, **_kw):  # type: ignore[no-untyped-def]
        ran.append(argv[-1])
        if tuple(argv) == fail_argv:
            return subprocess.CompletedProcess(argv, 1, "", "boom: first line\nsecond line")
        return subprocess.CompletedProcess(argv, 0, "", "")

    cwd = tmp_path / "staging_gate"
    cwd.mkdir()
    run_hook_slot(hooks, "staging_gate", app_url="https://fake.invalid", cwd=cwd, ambient_env=env, runner=fake)
    record = json.loads((cwd / hooks.slots["staging_gate"].artifacts_dir / "checks.jsonl").read_text().splitlines()[-1])
    assert [(c["name"], c["ok"]) for c in record["checks"]] == [(checks[0].id, True), (checks[1].id, False)]
    assert record["checks"][1]["detail"] == "boom: first line"
    if len(checks) > 2:
        assert checks[2].argv[-1] not in ran
