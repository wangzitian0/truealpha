"""Focused CI workflow and runtime-hooks manifest invariants (#1647, #1692)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
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
    from infra2_sdk.delivery import RuntimeHooksManifest

    return RuntimeHooksManifest.from_file(REPO_ROOT / "runtime-hooks.manifest.json")


def test_runtime_hooks_manifest_is_schema_3_and_maps_each_check_to_its_cells() -> None:
    from infra2_sdk.delivery import CELL_POLICIES, resolve_check_cells

    hooks = _load_hooks()
    assert hooks.schema_version == 3
    assert _chained_checks(hooks) == []
    resolved = {cell: [c.id for c in hooks.checks if cell in resolve_check_cells(c)] for cell in CELL_POLICIES}
    assert {cell: ids for cell, ids in resolved.items() if ids} == EXPECTED_CELL_CHECKS


def test_runtime_hooks_cells_run_their_checks_and_report_a_failed_check(tmp_path: Path) -> None:
    from infra2_sdk.delivery import CELL_POLICIES

    manifest = {
        "schema_version": 3,
        "service": "truealpha/app",
        "checks": [
            {
                "id": "health",
                "needs": ["running_service"],
                "argv": [sys.executable, "-c", "import sys; print('boom: first line', file=sys.stderr); sys.exit(1)"],
                "timeout_seconds": 30,
            },
            {
                "id": "walk_tree",
                "needs": ["running_service"],
                "argv": [sys.executable, "-c", "import sys; sys.exit(0)"],
                "timeout_seconds": 30,
            },
        ],
    }
    manifest_path = tmp_path / "runtime-hooks.manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    env = {
        **os.environ,
        "APP_URL": "https://fake.invalid",
        "APP_VERSION_REF": "v0.0.1",
        "ENVIRONMENT": "staging",
    }
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "infra2_sdk.delivery",
            "run-cell",
            str(manifest_path),
            "--cell",
            "staging.post",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1
    payload = json.loads(proc.stdout)
    assert payload["passed"] is False
    assert CELL_POLICIES["staging.post"].stop_on_fail is False
    assert [(v["id"], v["passed"]) for v in payload["verdicts"]] == [("health", False), ("walk_tree", True)]
    assert "boom: first line" in payload["verdicts"][0]["stderr"]


def test_runtime_hooks_manifest_refuses_schema_2_and_shell_chains() -> None:
    from infra2_sdk.delivery import RuntimeHooksManifest

    raw = json.loads((REPO_ROOT / "runtime-hooks.manifest.json").read_text(encoding="utf-8"))
    schema_2 = {**raw, "schema_version": 2}
    with pytest.raises(ValueError, match="schema_version"):
        RuntimeHooksManifest.model_validate(schema_2)

    with_chain = json.loads(json.dumps(raw))
    with_chain["checks"][0]["argv"] = ["sh", "-c", "first && second"]
    assert _chained_checks(RuntimeHooksManifest.model_validate(with_chain)) == ["smoke"]

    with_empty_checks = {**raw, "checks": []}
    with pytest.raises(ValueError, match="checks"):
        RuntimeHooksManifest.model_validate(with_empty_checks)


def test_browser_check_uses_headless_shell_and_each_cell_fits_its_budget() -> None:
    from infra2_sdk.delivery import CELL_POLICIES, resolve_check_cells

    hooks = _load_hooks()
    walk_tree = next(row for row in hooks.checks if row.id == "walk_tree")
    assert "--only-shell" in walk_tree.bootstrap
    for cell in EXPECTED_CELL_CHECKS:
        total = sum(row.timeout_seconds for row in hooks.checks if cell in resolve_check_cells(row))
        assert total <= CELL_POLICIES[cell].budget_seconds, (cell, total)


def test_checks_without_uv_sync_bootstrap_never_invoke_cold_uv_run() -> None:
    """Checks without `uv sync` bootstrap run in clean temp checkouts; cold `uv run` rebuilds 140+ packages and times out."""
    hooks = _load_hooks()
    for check in hooks.checks:
        cmd = " ".join(check.argv)
        if "uv sync" not in (check.bootstrap or ""):
            assert "uv run" not in cmd, f"{check.id} invokes `uv run` without `uv sync` bootstrap: {cmd}"

    prod_surface = next(row for row in hooks.checks if row.id == "prod_surface")
    res = subprocess.run(
        ["sh", "-c", f"{prod_surface.argv[-1]} --help"],
        cwd=REPO_ROOT,
        env={"PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin", "PYTHONSAFEPATH": "1"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res.returncode == 0, res.stderr
    assert "prod_surface" in res.stdout


def test_tag_and_main_runs_of_one_commit_do_not_cancel_each_other() -> None:
    group = yaml.safe_load((WORKFLOWS / "ci-required.yml").read_text(encoding="utf-8"))["concurrency"]["group"]
    assert "github.ref_type" in group


def test_a_tag_push_promotes_and_never_builds_and_push_builds_once() -> None:
    req = yaml.safe_load((WORKFLOWS / "ci-required.yml").read_text(encoding="utf-8"))["jobs"]
    images = yaml.safe_load((WORKFLOWS / "release-images.yml").read_text(encoding="utf-8"))["jobs"]
    for name in ("security", "images_build", "images_release"):
        assert "github.ref_type != 'tag'" in req[name]["if"], name
    build_job = req["images_build"]
    assert build_job["needs"] == "changes"
    assert build_job["permissions"] == {"contents": "read"}
    assert "github.event_name == 'push'" in build_job["if"]
    assert "github.ref == 'refs/heads/main'" in build_job["if"]
    assert build_job["with"]["publish"] is False
    for img_input in ("app_web", "llm_service", "data_engine"):
        expr = str(build_job["with"][img_input])
        assert "github.event_name == 'push'" in expr
        assert f"needs.changes.outputs.image_{img_input} == 'true'" in expr

    release_job = req["images_release"]
    assert release_job["needs"] == ["changes", "security", "db", "python", "web", "images_build"]
    assert "needs.images_build.result == 'success'" in release_job["if"]
    assert "needs.images_build.result == 'skipped'" not in release_job["if"]
    assert release_job["with"]["prebuilt"] == "${{ github.event_name == 'push' }}"
    assert release_job["with"]["publish"] is True

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


def test_python_test_durations_seed_and_lpt_sharding() -> None:
    durations_path = REPO_ROOT / "common" / "runtime" / "data" / "python-test-durations.json"
    durations = json.loads(durations_path.read_text(encoding="utf-8"))
    assert isinstance(durations, dict) and len(durations) >= 50
    for rel, sec in durations.items():
        assert isinstance(rel, str)
        assert isinstance(sec, (int, float)) and not isinstance(sec, bool) and sec >= 0.0
        assert (REPO_ROOT / rel).is_file(), f"stale duration entry for missing file: {rel}"

    ci_python = yaml.safe_load((WORKFLOWS / "ci-python.yml").read_text(encoding="utf-8"))
    wf_env = ci_python["env"]
    s3_declared = set(str(wf_env["S3_CORE_TEST_FILES"]).split()) | set(str(wf_env["S3_DATA_ENGINE_TEST_FILES"]).split())
    shard_step = next(
        s for s in ci_python["jobs"]["test-deps-smoke"]["steps"] if "Run tests (shard" in str(s.get("name", ""))
    )
    match = re.search(r"python3 -c '\n(.*?)\n\s*'\)", shard_step["run"], re.DOTALL)
    assert match is not None, "expected inline python3 sharding snippet in ci-python.yml"
    snippet = "\n".join(line[10:] if line.startswith("          ") else line for line in match.group(1).splitlines())

    for de_required in ("true", "false"):
        roots = ["common/contracts/tests", "common/factors/tests", "common/runtime/tests", "apps/llm-service/tests"]
        if de_required == "true":
            roots.append("apps/data-engine/tests")
        expected_files = {
            (REPO_ROOT / r / p).relative_to(REPO_ROOT).as_posix()
            for r in roots
            for pat in ("test_*.py", "*_test.py")
            for p in (REPO_ROOT / r).rglob(pat)
        } - s3_declared

        env = {
            **os.environ,
            "S3_CORE_TEST_FILES": str(wf_env["S3_CORE_TEST_FILES"]),
            "S3_DATA_ENGINE_TEST_FILES": str(wf_env["S3_DATA_ENGINE_TEST_FILES"]),
            "DATA_ENGINE_REQUIRED": de_required,
        }
        shards: list[list[str]] = []
        for idx in range(4):
            rendered = snippet.replace("${{ strategy.job-index }}", str(idx)).replace("${{ strategy.job-total }}", "4")
            proc = subprocess.run(
                [sys.executable, "-c", rendered],
                cwd=REPO_ROOT,
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            chosen = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
            assert chosen, f"empty shard {idx} for DATA_ENGINE_REQUIRED={de_required}"
            shards.append(chosen)

        flattened = [p for shard in shards for p in shard]
        assert len(flattened) == len(set(flattened)), "shards overlap"
        assert set(flattened) == expected_files
        assert not (set(flattened) & s3_declared)

        weights = {p: float(durations.get(p, (REPO_ROOT / p).stat().st_size / 1000.0)) for p in expected_files}
        shard_loads = [sum(weights[p] for p in shard) for shard in shards]
        assert max(shard_loads) - min(shard_loads) <= max(weights.values()) + 1e-9


def test_security_gate_runs_in_single_job() -> None:
    security = yaml.safe_load((WORKFLOWS / "security-gate.yml").read_text(encoding="utf-8"))
    assert list(security["jobs"]) == ["security-gate"]
    steps = security["jobs"]["security-gate"]["steps"]
    checkout = next(s for s in steps if str(s.get("uses", "")).startswith("actions/checkout@"))
    assert checkout["with"]["fetch-depth"] == 0
    assert any(str(s.get("uses", "")).startswith("gitleaks/gitleaks-action@") for s in steps)
    assert any("Forbid moomoo trading APIs" in str(s.get("name", "")) for s in steps)


def test_main_health_tracks_and_requires_all_failed_lanes(tmp_path: Path) -> None:
    mh = yaml.safe_load((WORKFLOWS / "main-health.yml").read_text(encoding="utf-8"))
    steps = mh["jobs"]["report"]["steps"]
    open_step = next(s for s in steps if s.get("name") == "Open or update the red-main issue")
    resolve_step = next(s for s in steps if s.get("name") == "Resolve the red-main issue")

    def extract_py(run_script: str) -> str:
        match = re.search(r"python3 - <<'PY'\n(.*?)\nPY", run_script, re.DOTALL)
        assert match is not None
        return match.group(1)

    open_py = extract_py(open_step["run"])
    resolve_py = extract_py(resolve_step["run"])

    (tmp_path / "run_jobs.json").write_text(
        json.dumps(
            {
                "jobs": [
                    {"name": "python / test-deps-smoke (0)", "conclusion": "failure"},
                    {"name": "web / browser", "conclusion": "failure"},
                    {"name": "ci-required", "conclusion": "failure"},
                    {"name": "security / security-gate", "conclusion": "success"},
                ]
            }
        ),
        encoding="utf-8",
    )
    subprocess.run([sys.executable, "-c", open_py], cwd=tmp_path, check=True)
    assert (tmp_path / "failed_lanes.txt").read_text(encoding="utf-8").strip() == "python, web"

    # Incident has python in body and db in a later comment; web-only green must stay open.
    (tmp_path / "issue.json").write_text(
        json.dumps(
            {
                "body": "ci-required failed on a push to main.\nfailed-lanes: python\n",
                "comments": [{"body": "ci-required failed on a push to main.\nfailed-lanes: db\n"}],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "run_jobs.json").write_text(
        json.dumps(
            {
                "jobs": [
                    {"name": "web / check", "conclusion": "success"},
                    {"name": "db / migrate", "conclusion": "success"},
                    {"name": "ci-required", "conclusion": "success"},
                ]
            }
        ),
        encoding="utf-8",
    )
    partial = subprocess.run([sys.executable, "-c", resolve_py], cwd=tmp_path, capture_output=True, text=True)
    assert partial.returncode != 0
    assert "python" in partial.stdout

    # Failing sub-job inside a required lane must keep incident open.
    (tmp_path / "run_jobs.json").write_text(
        json.dumps(
            {
                "jobs": [
                    {"name": "python / gates", "conclusion": "success"},
                    {"name": "python / test-deps-smoke (0)", "conclusion": "failure"},
                    {"name": "db / migrate", "conclusion": "success"},
                ]
            }
        ),
        encoding="utf-8",
    )
    red_sub = subprocess.run([sys.executable, "-c", resolve_py], cwd=tmp_path, capture_output=True, text=True)
    assert red_sub.returncode != 0

    # All recorded failed lanes ran and passed -> resolves cleanly.
    (tmp_path / "run_jobs.json").write_text(
        json.dumps(
            {
                "jobs": [
                    {"name": "python / gates", "conclusion": "success"},
                    {"name": "python / dagster-code-server-liveness", "conclusion": "skipped"},
                    {"name": "db / migrate", "conclusion": "success"},
                    {"name": "ci-required", "conclusion": "success"},
                ]
            }
        ),
        encoding="utf-8",
    )
    subprocess.run([sys.executable, "-c", resolve_py], cwd=tmp_path, check=True)
