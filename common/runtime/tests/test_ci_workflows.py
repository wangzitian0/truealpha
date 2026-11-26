"""Focused CI workflow and runtime-hooks manifest invariants (#1647, #1692)."""

from __future__ import annotations

import json
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
    assert {"changes", "security", "db", "python", "web", "images_build", "images_release"} <= req_needs
    changes_steps = "\n".join(str(step.get("run", "")) for step in req["changes"]["steps"])
    assert "merge-base --is-ancestor" in changes_steps
    assert loaded["ci-web.yml"]["jobs"]["browser"]["runs-on"] == "ubuntu-24.04"


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
    assert deps_job["strategy"]["matrix"]["shard"] == [0, 1]
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


EXPECTED_CELL_CHECKS = {
    "preview.operation": ["smoke"],
    "preview.post": ["health", "walk_tree"],
    "staging.operation": ["smoke"],
    "staging.post": ["health", "walk_tree"],
    "prod.operation": ["smoke"],
    "prod.post": ["health", "prod_surface"],
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
        if "infra2_sdk.deploy" in argv[-1]:
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
    assert [(c.name, c.ok) for c in result.checks] == [("health", False), ("walk_tree", True)]
    assert any("walk-tree" in command for command in ran)
    record = json.loads((cwd / hooks.artifacts_dir / "checks.jsonl").read_text().splitlines()[-1])
    assert record["checks"][0]["detail"] == "boom: first line"


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


def test_tag_and_main_runs_of_one_commit_do_not_cancel_each_other() -> None:
    group = yaml.safe_load((WORKFLOWS / "ci-required.yml").read_text(encoding="utf-8"))["concurrency"]["group"]
    assert "github.ref_type" in group


def test_a_tag_push_promotes_and_never_builds_and_push_builds_once() -> None:
    req = yaml.safe_load((WORKFLOWS / "ci-required.yml").read_text(encoding="utf-8"))["jobs"]
    images = yaml.safe_load((WORKFLOWS / "release-images.yml").read_text(encoding="utf-8"))["jobs"]
    for name in ("security", "images_build", "images_release"):
        assert "github.ref_type != 'tag'" in req[name]["if"], name
    assert "github.event_name == 'push'" not in req["images_build"]["if"]
    assert "needs.images_build.result == 'skipped'" in req["images_release"]["if"]
    assert req["images_release"]["with"]["prebuilt"] is False
    assert req["images_release"]["with"]["publish"] is True

    promote_job = req["images_promote"]
    assert "github.ref_type == 'tag'" in promote_job["if"]
    assert promote_job["with"]["promote"] is True
    assert "images_promote" in req["required"]["needs"]
    for name in ("build", "publish"):
        assert "!inputs.promote" in images[name]["if"], name
    assert "inputs.promote" in images["promote"]["if"]
    promote_runs = "\n".join(str(step.get("run", "")) for step in images["promote"]["steps"])
    assert "docker buildx imagetools create" in promote_runs
    assert "docker buildx imagetools inspect" in promote_runs
    assert "promote_release_image.py" not in promote_runs
    assert not [s for s in images["promote"]["steps"] if "build-push-action" in str(s.get("uses", ""))]
    publish_tags = "\n".join(str(step.get("with", {}).get("tags", "")) for step in images["publish"]["steps"])
    assert "type=semver" not in publish_tags


def test_no_called_job_asks_for_more_than_any_caller_grants() -> None:
    """A called job above its caller grant fails the run at startup (#1878, 2026-11-25)."""
    req = yaml.safe_load((WORKFLOWS / "ci-required.yml").read_text(encoding="utf-8"))["jobs"]
    images = yaml.safe_load((WORKFLOWS / "release-images.yml").read_text(encoding="utf-8"))["jobs"]
    rank = {"none": 0, "read": 1, "write": 2}
    callers = [job for job in req.values() if job.get("uses") == "./.github/workflows/release-images.yml"]
    assert len(callers) == 3
    for name, job in images.items():
        for scope, level in (job.get("permissions") or {}).items():
            for caller in callers:
                granted = caller.get("permissions", {}).get(scope, "none")
                assert rank[level] <= rank[granted], (name, scope, level, granted)


def test_pr_image_filters_are_scoped_and_ci_web_and_python_use_speedups() -> None:
    req = yaml.safe_load((WORKFLOWS / "ci-required.yml").read_text(encoding="utf-8"))["jobs"]
    filter_step = next(s for s in req["changes"]["steps"] if s.get("id") == "filter")
    filters = yaml.safe_load(filter_step["with"]["filters"])
    assert "apps/data-engine/**" not in filters["image_data_engine"]
    assert "apps/app-web/**" not in filters["image_app_web"]
    assert "apps/llm-service/**" not in filters["image_llm_service"]
    assert "apps/data-engine/Dockerfile*" in filters["image_data_engine"]

    ci_web = yaml.safe_load((WORKFLOWS / "ci-web.yml").read_text(encoding="utf-8"))
    for job_name in ("check", "browser"):
        steps = ci_web["jobs"][job_name]["steps"]
        assert any("~/.bun/install/cache" in str(s.get("with", {}).get("path", "")) for s in steps), job_name

    ci_python = yaml.safe_load((WORKFLOWS / "ci-python.yml").read_text(encoding="utf-8"))
    shard_run = "\n".join(str(s.get("run", "")) for s in ci_python["jobs"]["test-deps-smoke"]["steps"])
    assert "os.path.getsize" in shard_run
