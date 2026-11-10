# ruff: noqa: I001, E401, E402, E701, E702, E731, F401, F811
# fmt: off
from __future__ import annotations; import ast; import importlib.metadata; import importlib.util; import os; import re; import shlex; import shutil; import subprocess; import sys; import tomllib; from functools import cache; from pathlib import Path; import pytest; import yaml; _SPEC = importlib.util.spec_from_file_location( "truealpha_workflow_contract", Path(__file__).parent / "workflow_contract.py" ); assert _SPEC is not None and _SPEC.loader is not None; _contract = importlib.util.module_from_spec(_SPEC); sys.modules[_SPEC.name] = _contract; _SPEC.loader.exec_module(_contract); WorkflowContractError = _contract.WorkflowContractError; glob_matches = _contract.glob_matches; job = _contract.job; job_step = _contract.job_step; load = _contract.load; paths_filters = _contract.paths_filters; selected_lanes = _contract.selected_lanes; source = _contract.source; step = _contract.step; steps = _contract.steps; step_text = _contract.step_text; spec_text = _contract.spec_text; triggers = _contract.triggers; REPO_ROOT = Path(__file__).resolve().parents[3]; REPROOF = "mutation-reproof.yml"; MAIN_HEALTH = "main-health.yml"; CLOSE_GUARD = "issue-close-guard.yml"; REQUIRED = "ci-required.yml"; PYTHON = "ci-python.yml"; DB = "ci-db.yml"; RUNTIME = "ci-runtime.yml"; WEB = "ci-web.yml"; IMAGES = "release-images.yml"; SHARD_REFRESH = "shard-weight-refresh.yml"
def test_a_missing_step_names_what_it_was_looking_for() -> None:
    with pytest.raises(WorkflowContractError, match="has no step named"): step(REQUIRED, "A step nobody wrote")
def test_manual_image_release_is_explicit_and_waits_for_required_jobs() -> None:
    dispatch = triggers(REQUIRED)["workflow_dispatch"]["inputs"]["force_images"]; assert dispatch["description"] == "Publish all current-ref images after required checks."; assert dispatch["type"] == "boolean" and dispatch["default"] is False; release = job(REQUIRED, "images_release"); condition = str(release["if"]); assert "github.event_name == 'workflow_dispatch' &&" in condition; assert "github.ref == 'refs/heads/main' &&" in condition; assert "inputs.force_images" in condition; assert "github.event_name == 'push'" in condition
    for dependency in ("security", "db", "python", "runtime", "web"): assert f"needs.{dependency}.result == 'success' || needs.{dependency}.result == 'skipped'" in condition, ( f"images_release must wait for {dependency}" )
    assert release["with"]["publish"] is True
    for image in ("app_web", "llm_service", "data_engine"): assert release["with"][image] is True, ( f"{image}: images_release must publish every image on main push and manual force" )
    text = source(REQUIRED); assert text.index("  images_release:\n") < text.index("\n  required:\n"), ( "the required job must summarise images_release, so it comes after it" )
def test_images_build_beside_the_tests_and_publish_only_after_them() -> None:
    build = job(REQUIRED, "images_build"); assert build["needs"] == "changes", "images_build must depend on the paths filter and nothing slower"; assert "needs." not in str(build["if"]).replace("needs.changes.outputs", ""), ( "images_build waits for a lane's result — the build is back on the critical path" ); assert build["with"]["publish"] is False
    for event in ("pull_request", "merge_group", "push", "workflow_dispatch"): assert f"github.event_name == '{event}'" in str(build["if"]), f"images_build no longer runs on {event}"
    assert "prebuilt" not in build["with"], "the pre-build cannot itself be pre-built"; release = job(REQUIRED, "images_release"); assert "images_build" in release["needs"]; assert "needs.images_build.result == 'success'" in str(release["if"]), ( "images_release publishes without a successful build at this SHA" ); assert "needs.images_build.result == 'skipped'" not in str(release["if"]), ( "a skipped build under a true trigger is a build that never happened; it must not publish" ); assert release["with"]["prebuilt"] is True
    for image in ("app_web", "llm_service", "data_engine"):
        assert release["with"][image] is True
        for clause in ( f"needs.changes.outputs.image_{image} == 'true'", "github.event_name == 'push'", "inputs.force_images", ): assert clause in str(build["with"][image]), f"{image}: images_build must select image on {clause!r}"
    prebuilt = triggers(IMAGES)["workflow_call"]["inputs"]["prebuilt"]; assert prebuilt["type"] == "boolean" and prebuilt["default"] is False and prebuilt["required"] is False; assert "!inputs.prebuilt" in str(job(IMAGES, "build")["if"]), "the inner build runs a second time when pre-built"; publish = job(IMAGES, "publish"); condition = " ".join(str(publish["if"]).split()); assert condition.startswith("!cancelled() &&"), ( "publish needs a skipped build job; without !cancelled() it is skipped along with it" ); assert "inputs.publish" in condition and "needs.plan.outputs.has_images == 'true'" in condition; assert "needs.build.result == 'success'" in condition; assert "(inputs.prebuilt && needs.build.result == 'skipped')" in condition, ( "a skipped build opens publish only when the caller vouched for it" ); assert "needs.build.result == 'failure'" not in condition and "always()" not in condition; required = job(REQUIRED, "required"); assert "images_build" in required["needs"] and "images_check" not in required["needs"], ( "required must summarise images_build — a broken Dockerfile would otherwise merge green on main" )
def test_release_tags_trigger_ci_required_and_publish_semver_and_bare_sha_tags() -> None: push_trigger = triggers(REQUIRED)["push"]; assert push_trigger["branches"] == ["main"]; assert push_trigger["tags"] == ["v[0-9]+.[0-9]+.[0-9]+"]; changes = job(REQUIRED, "changes"); tag_guard = job_step(REQUIRED, "changes", "Verify release tag is on main"); assert tag_guard.get("if") == "github.ref_type == 'tag'"; assert "git fetch origin main" in str(tag_guard.get("run", "")); assert 'git merge-base --is-ancestor "$GITHUB_SHA" origin/main' in str(tag_guard.get("run", "")); assert "always()" not in str(job(REQUIRED, "images_build")["if"]); assert "needs.changes.result == 'success'" in str(job(REQUIRED, "images_release")["if"]); filter_step = next(s for s in changes["steps"] if s.get("id") == "filter"); assert "github.ref_type != 'tag'" in str(filter_step.get("if", "")), ( "paths-filter must skip on tag pushes so test lanes do not re-run against 0000000000000000000000000000000000000000" ); publish_steps = job(IMAGES, "publish")["steps"]; meta_step = next(s for s in publish_steps if s.get("id") == "meta"); meta_tags = [line.strip() for line in str(meta_step["with"]["tags"]).splitlines() if line.strip()]; assert "type=sha,format=short" in meta_tags; assert "type=sha,prefix=,format=short" in meta_tags; assert "type=semver,pattern=v{{version}}" in meta_tags
ESCALATE_TOOL = "tools/escalate_issue.py"
class _Context:
    def __init__(self, value: object) -> None: self._value = value
    def __getattr__(self, name: str) -> object: value = self._value.get(name) if isinstance(self._value, dict) else None; return _Context(value) if value is None or isinstance(value, dict) else value
    def __bool__(self) -> bool: return bool(self._value)
def _decides( condition: object, github: dict, *, succeeded: bool = True, needs: dict | None = None, inputs: dict | None = None, ) -> bool:
    expression = str(condition).strip() if condition is not None else "true"
    if expression.startswith("${{") and expression.endswith("}}"): expression = expression[3:-2].strip()
    expression = " ".join(expression.split()); functions = set(re.findall(r"([A-Za-z_]+)\(", expression)); assert functions <= {"success", "failure", "always", "cancelled"}, f"unsupported functions {functions}"; assert not re.search(r"!\s*\(", expression), "negating a group is not supported by this evaluator"; python = re.sub(r"!(?!=)\s*([A-Za-z_][\w.]*)", r"(not \1)", expression); python = python.replace("&&", " and ").replace("||", " or "); namespace = { "github": _Context(github), "needs": _Context(needs or {}), "inputs": _Context(inputs or {}), "success": lambda: succeeded, "failure": lambda: not succeeded, "always": lambda: True, "cancelled": lambda: False, "true": True, "false": False, "null": None, }; return bool(eval(python, {"__builtins__": {}}, namespace))
def _tool_calls(run: object, action: str) -> list[str]:
    return [ line.strip() for line in str(run).splitlines() if f"{ESCALATE_TOOL} {action} " in line and not line.strip().startswith("#") ]
def _assert_calls_the_tool(spec: dict, action: str, where: str) -> None: run = str(spec.get("run", "")); calls = _tool_calls(run, action); assert len(calls) == 1, f"{where}: expected one `{ESCALATE_TOOL} {action}` call, found {calls}"; assert '--title "$ESCALATION_TITLE"' in calls[0] and "--body-file" in calls[0], ( f'{where}: the {action} call does not use the shared title — the issue a red run opens would not be the issue a green run closes' ); assert "gh issue" not in run, f"{where}: an inline `gh issue` beside the tool is a second lifecycle implementation"; assert "${{" not in run, f"{where}: an expression is interpolated into the shell; pass it through env"
def test_the_reproof_alert_closes_only_on_a_full_run_of_main() -> None:
    workflow = yaml.safe_load(source(REPROOF)); assert workflow["env"]["ESCALATION_TITLE"] == "mutation-reproof is red: a declared guard is inert"; assert workflow["permissions"] == {"contents": "read"}, "the workflow-level grant reaches the PR-triggered steps"; assert "permissions" not in job(REPROOF, "reproof"), "the PR-triggered reproof job must not hold its own grant"; escalate, resolve = job(REPROOF, "escalate"), job(REPROOF, "resolve")
    for spec, action in ((escalate, "open"), (resolve, "resolve")): assert spec["needs"] == "reproof"; assert spec["permissions"] == {"contents": "read", "issues": "write"}, ( f"the {action} job's grant is {spec['permissions']}" ); writers = [item for item in spec["steps"] if ESCALATE_TOOL in str(item.get("run", ""))]; assert len(writers) == 1, f"the {action} job has {len(writers)} steps calling the tool"; _assert_calls_the_tool(writers[0], action, f"reproof {action}")
    main = "refs/heads/main"
    for github in ({"event_name": "schedule", "ref": main}, {"event_name": "workflow_dispatch", "ref": main}): assert _decides(resolve["if"], github), f"a green {github['event_name']} run on main no longer closes"; assert not _decides(resolve["if"], github, succeeded=False); assert _decides(escalate["if"], github, succeeded=False)
    for github in ( {"event_name": "pull_request", "ref": "refs/pull/1/merge"}, {"event_name": "pull_request", "ref": main}, {"event_name": "workflow_dispatch", "ref": "refs/heads/feature"}, ):
        assert not _decides(resolve["if"], github), f"{github} can close the reproof alert"
        if github["event_name"] == "pull_request": assert not _decides(escalate["if"], github, succeeded=False), "a red PR run files an issue"
def test_a_red_main_push_files_an_issue_and_a_green_one_closes_it() -> None:
    on = triggers(MAIN_HEALTH); assert set(on) == {"workflow_run"}, f"main-health triggers on {sorted(on)}; it reports on ci-required only"; required_name = yaml.safe_load(source(REQUIRED))["name"]; assert on["workflow_run"] == {"workflows": [required_name], "types": ["completed"], "branches": ["main"]}, ( f"main-health listens to {on['workflow_run']}, not to completed {required_name!r} runs on main" ); workflow = yaml.safe_load(source(MAIN_HEALTH)); assert workflow["permissions"] == {"contents": "read", "actions": "read", "issues": "write"}; assert workflow["env"]["ESCALATION_TITLE"] == "main is red: ci-required failed on push"; assert workflow["concurrency"]["cancel-in-progress"] is False, "an open and a close must not cancel each other"; jobs = workflow["jobs"]; assert list(jobs) == ["report"], f"main-health has jobs {sorted(jobs)}"; report = jobs["report"]; assert "permissions" not in report, "a job-level grant would replace the pinned workflow grant"
    for event in ("pull_request", "workflow_dispatch", "merge_group", "schedule"): github = {"event": {"workflow_run": {"event": event, "head_branch": "main"}}}; assert not _decides(report.get("if"), github), ( f"a {event} run of ci-required on a branch named main files or closes the red-main issue" )
    assert _decides(report.get("if"), {"event": {"workflow_run": {"event": "push", "head_branch": "main"}}}); checkout = [item for item in report["steps"] if "actions/checkout" in str(item.get("uses", ""))]; assert len(checkout) == 1 and "ref" not in (checkout[0].get("with") or {}), ( "the privileged job must run the default branch's tool, never the triggering commit's" ); assert checkout[0]["with"].get("persist-credentials") is False
    for item in report["steps"]: assert "${{" not in str(item.get("run", "")), ( f"step {item.get('name')!r} interpolates an expression into its shell — commit text is attacker-shaped; pass it through env" )
    event_env = {key: value for key, value in report["env"].items() if "github.event" in str(value)}; assert {"HEAD_SHA", "COMMIT_MESSAGE", "RUN_URL"} <= set(event_env), ( f"the body must name the head SHA, the commit and the run, through env; env carries {sorted(event_env)}" ); open_step = step(MAIN_HEALTH, "File the red-main issue"); resolve_step = step(MAIN_HEALTH, "Resolve the red-main issue"); _assert_calls_the_tool(open_step, "open", "main-health open"); _assert_calls_the_tool(resolve_step, "resolve", "main-health resolve")
    for conclusion in ("failure", "success", "cancelled", "skipped", "timed_out", "neutral"): github = {"event": {"workflow_run": {"event": "push", "head_branch": "main", "conclusion": conclusion}}}; assert _decides(open_step["if"], github) is (conclusion == "failure"), f"open on {conclusion}"; assert _decides(resolve_step["if"], github) is (conclusion == "success"), f"resolve on {conclusion}"
def test_the_condition_evaluator_keeps_githubs_precedence() -> None:
    assert _decides("!github.x == 'y'", {"x": ""}) is False; assert _decides("!github.x", {}) is True; assert _decides("github.a != 'b'", {"a": "c"}) is True; assert _decides("${{ success() && github.a == 'b' }}", {"a": "b"}) is True; assert _decides(None, {}) is True
    with pytest.raises(AssertionError, match="unsupported"): _decides("contains(github.a, 'b')", {})
def test_the_close_guard_can_reopen_and_sees_full_history() -> None: workflow = source(CLOSE_GUARD); assert "issues: write" in workflow, "the guard cannot reopen anything without it"; assert "fetch-depth: 0" in workflow and "fetch --tags" in workflow; assert triggers(CLOSE_GUARD)["issues"]["types"] == ["closed"]
def test_abolished_vision_close_workflows_and_guard_remain_deleted() -> None:
    for rel_path in ( ".github/workflows/vision-close-check.yml", ".github/workflows/vision-close-reopen.yml", "tools/vision_close_guard.py", "common/runtime/tests/test_vision_close_guard.py", ): target = REPO_ROOT / rel_path; assert not target.exists(), f"{rel_path} was abolished by ADR A8 and must not be restored"
def test_legacy_cd_and_sre_workflows_and_scripts_remain_deleted() -> None:
    for rel_path in ( ".github/workflows/deploy-release.yml", ".github/workflows/auto-release-staging.yml", ".github/workflows/walk-release.yml", ".github/workflows/delivery-report.yml", ".github/workflows/scheduler-liveness.yml", ".github/workflows/nightly-dagster-liveness.yml", "tools/cut_release.sh", "tools/owner_approval_gate.sh", "tools/production_evidence_policy.json", "tools/auto_release.py", "tools/staging_evidence.py", "tools/delivery_report.py", "tools/walk_evidence.py", "tools/app_deploy_request.py", "tools/app_deploy_transport.py", "tools/scheduler_liveness.py", "tools/verified_sha_images.py", "common/runtime/src/truealpha_tools/auto_release.py", "common/runtime/src/truealpha_tools/staging_evidence.py", "common/runtime/src/truealpha_tools/delivery_report.py", "common/runtime/src/truealpha_tools/walk_evidence.py", "common/runtime/src/truealpha_tools/release_runs.py", "common/runtime/src/truealpha_tools/app_deploy_request.py", "common/runtime/src/truealpha_tools/app_deploy_transport.py", "common/runtime/src/truealpha_tools/scheduler_liveness.py", "common/runtime/src/truealpha_tools/verified_sha_images.py", ):
        target = REPO_ROOT / rel_path
        assert not target.exists(), f"{rel_path} was removed in #1588 and must not be restored"
def test_runtime_hooks_manifest_declares_the_four_verification_slots() -> None:
    from infra2_sdk.manifests import load_hooks_manifest; manifest_path = REPO_ROOT / "runtime-hooks.manifest.json"; assert manifest_path.is_file(), "runtime-hooks.manifest.json must exist at repository root"; hooks = load_hooks_manifest(manifest_path); assert hooks.schema_version == 1; assert hooks.service == "truealpha/app"; assert set(hooks.slots) == {"smoke", "preview_gate", "staging_gate", "scheduled_audit"}
    for gate_slot in ("preview_gate", "staging_gate"): spec = hooks.slots[gate_slot]; assert spec.run.index("tools/warm_surface.sh") < spec.run.index("node e2e/walk-tree.mjs"), ( f"{gate_slot} must warm the surface before running walk-tree.mjs" ); assert "tools/surface_contract.py --authenticated" in spec.run; assert set(spec.required_env) == { "APP_URL", "APP_VERSION_REF", "TA_EMAIL", "TA_PASSWORD", "TA_MEMBER_EMAIL", "TA_MCP_TOKEN", }
REFRESH_PAT_GUARD = "Require a credential that can push a branch and open a pull request"; REFRESH_HARVEST = "Harvest the newest green main run that carries durations"; REFRESH_DECIDE = "Decide whether one refresh pull request is worth opening"; REFRESH_OPEN = "Open one refresh pull request"
def test_shard_weight_refresh_has_a_schedule_and_a_manual_replay_and_no_merge_trigger() -> None: on = triggers(SHARD_REFRESH); assert set(on) == {"schedule", "workflow_dispatch"}, f"shard-weight-refresh triggers on {sorted(on)}"; assert on["schedule"] == [{"cron": "23 4 * * 0"}]; assert job(SHARD_REFRESH, "refresh")["if"] == "github.ref == 'refs/heads/main'", ( "a dispatch on another branch would open a pull request from that branch's tool" )
def test_shard_weight_refresh_has_read_grants_only_and_pushes_with_the_pat() -> None:
    workflow = yaml.safe_load(source(SHARD_REFRESH)); assert workflow["permissions"] == {"contents": "read", "actions": "read", "pull-requests": "read"}; assert "permissions" not in workflow["jobs"]["refresh"], "a job-level grant would replace the workflow grant"; refresh_steps = job(SHARD_REFRESH, "refresh")["steps"]; names = [spec.get("name") for spec in refresh_steps]; checkout_index = next( index for index, spec in enumerate(refresh_steps) if str(spec.get("uses", "")).startswith("actions/checkout") ); assert names.index(REFRESH_PAT_GUARD) < checkout_index, "the guard must run before checkout"; guard = step(SHARD_REFRESH, REFRESH_PAT_GUARD); assert guard["env"] == {"INFRA2_PAT": "${{ secrets.INFRA2_PAT }}"}; assert "exit 1" in str(guard["run"]); assert refresh_steps[checkout_index]["with"]["token"] == "${{ secrets.INFRA2_PAT }}"; assert refresh_steps[checkout_index]["with"]["ref"] == "main"; holders = [spec.get("name") or spec.get("uses") for spec in refresh_steps if "INFRA2_PAT" in spec_text(spec)]; assert holders == [REFRESH_PAT_GUARD, "actions/checkout@v4", REFRESH_OPEN], holders; assert step(SHARD_REFRESH, REFRESH_OPEN)["env"]["GH_TOKEN"] == "${{ secrets.INFRA2_PAT }}"
    for reader in (REFRESH_HARVEST, REFRESH_DECIDE): assert step(SHARD_REFRESH, reader)["env"]["GH_TOKEN"] == "${{ github.token }}"
    for spec in refresh_steps: assert "${{" not in str(spec.get("run", "")), f"{spec.get('name')} interpolates an expression into the shell"
def test_shard_weight_refresh_opens_at_most_one_pull_request_and_only_when_the_tool_says_so() -> None:
    decide = step(SHARD_REFRESH, REFRESH_DECIDE); run = str(decide["run"]); assert 'gh pr list --head "$REFRESH_BRANCH" --state open' in run, "the count must cover the refresh branch"; assert "tools/shard_weight_refresh.py" in run and '--open-prs "$open_prs"' in run; opener = step(SHARD_REFRESH, REFRESH_OPEN); assert opener["if"] == "steps.decide.outputs.open_pr == 'true'"; script = str(opener["run"]); assert 'git push --force origin "$REFRESH_BRANCH"' in script; assert "git add tools/pytest_shard_weights.json" in script, "the pull request holds the weights file only"
    assert "Refs #1503" in script and "Closes #1503" not in script
    assert yaml.safe_load(source(SHARD_REFRESH))["env"]["REFRESH_BRANCH"] == "ci/refresh-shard-weights"
def test_shard_weight_refresh_tries_the_next_run_when_one_run_has_no_durations() -> None: run = str(step(SHARD_REFRESH, REFRESH_HARVEST)["run"]); assert "--event push --status success" in run and "--limit 10" in run; assert "for run_id in $run_ids; do" in run; assert "tools/harvest_shard_weights.py" in run; assert run.count("exit 1") == 2, "no green run and no run with durations must each fail the job"
_STUB_GH = '#!/bin/bash\necho "$*" >> "$STUB_LOG"\ncase "$1 $2" in\n  "run list")\n    case "$*" in\n      *"--workflow ci-required.yml"*"--branch main"*"--event push"*"--status success"*) printf \'%s\' "$STUB_RUN_IDS" ;;\n      *) echo "unexpected run list arguments: $*" >&2; exit 9 ;;\n    esac ;;\n  "pr create") ;;\n  *) echo "unexpected gh call: $*" >&2; exit 9 ;;\nesac\n'
_STUB_HARVEST = 'import os\nimport sys\nfrom pathlib import Path\n\nrun_id = sys.argv[1]\nout = sys.argv[sys.argv.index("--out") + 1]\nwith open(os.environ["STUB_LOG"], "a") as log:\n    log.write(f"harvest {run_id}\\n")\nif run_id in os.environ.get("STUB_BAD_RUNS", "").split():\n    print(f"harvest: run {run_id} yielded no durations", file=sys.stderr)\n    raise SystemExit(1)\nPath(out).write_text("{}")\n'
def _stub_environment(tmp_path: Path) -> dict[str, str]: bin_dir = tmp_path / "bin"; bin_dir.mkdir(exist_ok=True); gh = bin_dir / "gh"; gh.write_text(_STUB_GH, encoding="utf-8"); gh.chmod(0o755); (tmp_path / "runner").mkdir(exist_ok=True); return { "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "HOME": str(tmp_path), "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "RUNNER_TEMP": str(tmp_path / "runner"), "GITHUB_OUTPUT": str(tmp_path / "github_output"), "GH_TOKEN": "unused", "STUB_LOG": str(tmp_path / "stub.log"), }
def _run_refresh_harvest_step(tmp_path: Path, run_ids: str, bad_runs: str) -> subprocess.CompletedProcess[str]: (tmp_path / "tools").mkdir(exist_ok=True); (tmp_path / "tools" / "harvest_shard_weights.py").write_text(_STUB_HARVEST, encoding="utf-8"); environment = _stub_environment(tmp_path) | {"STUB_RUN_IDS": run_ids, "STUB_BAD_RUNS": bad_runs}; return subprocess.run( ["bash", "-c", str(step(SHARD_REFRESH, REFRESH_HARVEST)["run"])], cwd=tmp_path, capture_output=True, text=True, check=False, timeout=60, env=environment, )
def test_the_refresh_harvest_takes_the_newest_run_that_carries_durations(tmp_path: Path) -> None: result = _run_refresh_harvest_step(tmp_path, run_ids="13\n12\n11", bad_runs="13 12"); assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"; assert (tmp_path / "github_output").read_text(encoding="utf-8") == "run_id=11\n"; assert (tmp_path / "stub.log").read_text(encoding="utf-8").splitlines()[1:] == [ "harvest 13", "harvest 12", "harvest 11", ], "the step must try the runs from the newest to the oldest"
def test_the_refresh_harvest_stops_at_the_first_run_that_works(tmp_path: Path) -> None: result = _run_refresh_harvest_step(tmp_path, run_ids="13\n12\n11", bad_runs=""); assert result.returncode == 0; assert (tmp_path / "github_output").read_text(encoding="utf-8") == "run_id=13\n"; assert "harvest 12" not in (tmp_path / "stub.log").read_text(encoding="utf-8")
@pytest.mark.parametrize( ("run_ids", "bad_runs", "message"), [ ("13\n12", "13 12", "none of the newest 10 green main runs carries pytest durations"), ("", "", "no green ci-required run on main"), ], )
def test_the_refresh_harvest_fails_the_job_instead_of_reporting_nothing_to_do( tmp_path: Path, run_ids: str, bad_runs: str, message: str ) -> None: result = _run_refresh_harvest_step(tmp_path, run_ids=run_ids, bad_runs=bad_runs); assert result.returncode != 0; assert message in result.stdout; assert not (tmp_path / "github_output").exists(), "a failed harvest must publish no run id"
def _repo_with_a_weights_change(tmp_path: Path) -> tuple[Path, Path]:
    origin = tmp_path / "origin.git"; work = tmp_path / "work"; environment = _stub_environment(tmp_path); identity = ["-c", "user.name=probe", "-c", "user.email=probe@example.invalid"]
    def git(cwd: Path, *arguments: str) -> None: subprocess.run(["git", *identity, *arguments], cwd=cwd, check=True, capture_output=True, env=environment)
    origin.mkdir(); git(origin, "init", "-q", "--bare", "-b", "main"); work.mkdir(); git(work, "init", "-q", "-b", "main"); weights = work / "tools" / "pytest_shard_weights.json"; weights.parent.mkdir(); weights.write_text('{"seconds_by_file": {}}\n', encoding="utf-8"); git(work, "add", "."); git(work, "commit", "-q", "-m", "base"); git(work, "remote", "add", "origin", str(origin)); git(work, "push", "-q", "origin", "main"); weights.write_text('{"seconds_by_file": {"tests/test_new.py": 3.0}}\n', encoding="utf-8"); (work / "scratch.txt").write_text("not part of the refresh\n", encoding="utf-8"); return origin, work
def _run_refresh_open_step(tmp_path: Path, work: Path) -> subprocess.CompletedProcess[str]: environment = _stub_environment(tmp_path) | { "REFRESH_BRANCH": yaml.safe_load(source(SHARD_REFRESH))["env"]["REFRESH_BRANCH"], "HARVEST_RUN_ID": "77", "REASON": "the harvest gives weights to 1 test files that lack one", "JOB_URL": "https://example.invalid/runs/1", "GITHUB_REPOSITORY": "owner/repo", }; return subprocess.run( ["bash", "-c", str(step(SHARD_REFRESH, REFRESH_OPEN)["run"])], cwd=work, capture_output=True, text=True, check=False, timeout=60, env=environment, )
def _origin_git(tmp_path: Path, origin: Path, *arguments: str) -> str: return subprocess.run( ["git", *arguments], cwd=origin, check=True, capture_output=True, text=True, env=_stub_environment(tmp_path), ).stdout.strip()
def test_the_refresh_pushes_one_commit_of_the_weights_file_and_opens_the_pull_request(tmp_path: Path) -> None:
    origin, work = _repo_with_a_weights_change(tmp_path); result = _run_refresh_open_step(tmp_path, work); assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    title = "[truealpha-ci] Refresh the shard weights from run 77 (#1503)"
    assert ( _origin_git(tmp_path, origin, "log", "main..ci/refresh-shard-weights", "--format=%s%n%an") == f"{title}\ngithub-actions[bot]" ); changed = _origin_git(tmp_path, origin, "diff", "--name-only", "main", "ci/refresh-shard-weights"); assert changed == "tools/pytest_shard_weights.json", "the commit must hold the weights file and nothing else"; gh_calls = (tmp_path / "stub.log").read_text(encoding="utf-8").splitlines(); assert gh_calls == [ f"pr create --repo owner/repo --base main --head ci/refresh-shard-weights --title {title} --body-file {tmp_path}/runner/pr_body.md" ]; body = (tmp_path / "runner" / "pr_body.md").read_text(encoding="utf-8"); assert "Decision: the harvest gives weights to 1 test files that lack one." in body
    assert "Refs #1503" in body and "Closes" not in body
def test_the_refresh_replaces_a_stale_branch_left_by_an_earlier_pull_request(tmp_path: Path) -> None:
    origin, work = _repo_with_a_weights_change(tmp_path); identity = ["-c", "user.name=probe", "-c", "user.email=probe@example.invalid"]; environment = _stub_environment(tmp_path)
    for arguments in ( ["switch", "-q", "-c", "ci/refresh-shard-weights"], ["commit", "-q", "--allow-empty", "-m", "stale refresh"], ["push", "-q", "origin", "ci/refresh-shard-weights"], ["switch", "-q", "main"], ["branch", "-q", "-D", "ci/refresh-shard-weights"], ): subprocess.run(["git", *identity, *arguments], cwd=work, check=True, capture_output=True, env=environment)
    result = _run_refresh_open_step(tmp_path, work); assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"; subjects = _origin_git(tmp_path, origin, "log", "ci/refresh-shard-weights", "--format=%s"); assert subjects.splitlines()[0].startswith("[truealpha-ci] Refresh the shard weights"); assert "stale refresh" not in subjects
MIGRATION_CHAIN_STEPS = ( (PYTHON, "test-core", "Apply the migration chain"), (PYTHON, "test-data-engine", "Apply the migration chain"), (PYTHON, "dagster-code-server-liveness", "Apply the migration chain"), (DB, "migrate", "Apply the migration chain"), (DB, "migrate", "Re-apply the migration chain (idempotency)"), (DB, "migrate", "Re-apply the migration chain over real rows (replay-with-data)"), (WEB, "check", "Apply the migration chain (populated and empty)"), (WEB, "browser", "Apply the migration chain (populated and empty)"), )
@pytest.mark.parametrize(("workflow", "job_id", "name"), MIGRATION_CHAIN_STEPS)
def test_every_ci_job_applies_the_chain_through_the_one_applier(workflow: str, job_id: str, name: str) -> None: assert "db/apply_migrations.sh" in job_step(workflow, job_id, name).get("run", "")
def test_no_ci_job_reaches_a_schema_by_any_other_route() -> None:
    strays = []
    for workflow in (PYTHON, DB, WEB, RUNTIME):
        for job_id, spec in yaml.safe_load(source(workflow))["jobs"].items():
            for candidate in spec.get("steps", []):
                run = str(candidate.get("run", ""))
                if "db/migrations" not in run and "db/roles.sql" not in run: continue
                if "db/apply_migrations.sh" in run: continue
                strays.append(f"{workflow}:{job_id}:{candidate.get('name')}")
    assert not strays, ( f'{strays} reach the migration files without going through db/apply_migrations.sh — that is how the seven appliers diverged (#984)' )
def test_no_other_test_reads_a_workflow_directly() -> None:
    offenders = []
    for path in sorted((REPO_ROOT / "common/runtime/tests").glob("test_*.py")):
        if path.name == Path(__file__).name: continue
        if ".github/workflows" in path.read_text(encoding="utf-8"): offenders.append(path.name)
    assert not offenders, ( f'{offenders} read a workflow directly. Workflow-shape assertions live in {Path(__file__).name} and resolve steps through tests/workflow_contract.py (#583)' )
def test_no_test_bootstraps_a_tools_script_by_hand() -> None:
    offenders = []
    for path in sorted((REPO_ROOT / "common/runtime/tests").glob("test_*.py")):
        source = path.read_text(encoding="utf-8"); tree = ast.parse(source); bound = { target.id: ast.get_source_segment(source, node.value) or "" for node in ast.walk(tree) if isinstance(node, ast.Assign) for target in node.targets if isinstance(target, ast.Name) }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call): continue
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            if name != "spec_from_file_location": continue
            reachable = [ast.get_source_segment(source, node) or ""]; reachable += [bound.get(argument.id, "") for argument in ast.walk(node) if isinstance(argument, ast.Name)]
            if any("tools" in text for text in reachable): offenders.append(path.name)
    assert not offenders, ( f'{offenders} bootstrap a tools/ script by hand. Use `truealpha_runtime.testing.load_tool(name)` — it registers the module in sys.modules before exec, which a hand copy forgets and a dataclass in the tool then fails on (#583)' )
def test_every_workflow_installs_what_the_tools_it_runs_import() -> None:
    workspace_modules = { member.rsplit("/", 1)[-1].replace("-", "_") for member in ("apps/data-engine", "apps/llm-service", "common/contracts", "common/factors", "common/runtime") } | {"truealpha_runtime", "truealpha_tools", "truealpha_contracts", "data_engine", "factors"}; tools_src = REPO_ROOT / "common/runtime/src/truealpha_tools"
    def imports(script: Path, seen: set[Path] | None = None) -> set[str]:
        seen = set() if seen is None else seen; seen.add(script); tree = ast.parse(script.read_text(encoding="utf-8")); names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]; names.add(top)
                    if top == "truealpha_tools" and "." in alias.name:
                        sub = tools_src / f"{alias.name.split('.')[1]}.py"
                        if sub.is_file() and sub not in seen: names |= imports(sub, seen)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                top = node.module.split(".")[0]; names.add(top)
                if top == "truealpha_tools":
                    parts = node.module.split("."); candidates = [parts[1]] if len(parts) > 1 else [alias.name for alias in node.names]
                    for cand in candidates:
                        sub = tools_src / f"{cand}.py"
                        if sub.is_file() and sub not in seen: names |= imports(sub, seen)
        return names
    offenders = []
    for workflow in sorted((REPO_ROOT / ".github/workflows").glob("*.yml")):
        text = workflow.read_text(encoding="utf-8"); scripts = {name for name in re.findall(r"python (tools/[a-z_]+\.py)", text)}
        if not scripts: continue
        needs_workspace = {script for script in scripts if imports(REPO_ROOT / script) & workspace_modules}
        if not needs_workspace: continue
        installs = re.findall(r"uv sync[^\n]*", text); assert installs, f"{workflow.name} runs {sorted(scripts)} without any `uv sync`"; installs_workspace = [ line for line in installs if "--no-install-workspace" not in line and "--only-group" not in line ]
        if not installs_workspace: offenders.append((workflow.name, sorted(needs_workspace), installs))
    assert not offenders, ( f'these workflows never install the workspace and then run tools that import it: {offenders}. The script dies at import, before it can judge anything, and every later step in the job is skipped (#616)' )
_BARE_TOOL_CALL = re.compile(r"(?<![\w./-])python(?:3(?:\.\d+)?)?(?:\s+-\w+)*\s+tools/(\w+)\.py\b"); _STARTED_BY_UV_RUN = re.compile(r"\buv\s+run\b[^;&|()`]*$"); _IMPORT_ERRORS = {"ImportError", "ModuleNotFoundError", "Exception", "BaseException"}
def _bare_tool_calls(run: str) -> list[str]:
    calls = []
    for line in run.replace("\\\n", " ").splitlines():
        if line.lstrip().startswith("#"):
            continue
        for match in _BARE_TOOL_CALL.finditer(line):
            if not _STARTED_BY_UV_RUN.search(line[: match.start()]): calls.append(match.group(1))
    return calls
def _module_level_imports(tool: str, seen: set[str] | None = None) -> set[str]:
    seen = set() if seen is None else seen; seen.add(tool); tools_dir = REPO_ROOT / "tools"; tools_src = REPO_ROOT / "common/runtime/src/truealpha_tools"; names: set[str] = set(); subtools: set[str] = set()
    def visit(body: list[ast.stmt]) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)): continue
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    if top == "truealpha_tools" and "." in alias.name: subtools.add(alias.name.split(".")[1])
                    else: names.add(top)
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    top = node.module.split(".")[0]
                    if top == "truealpha_tools":
                        parts = node.module.split(".")
                        if len(parts) > 1: subtools.add(parts[1])
                        else: subtools.update(alias.name for alias in node.names)
                    else: names.add(top)
            elif isinstance(node, ast.If) and "TYPE_CHECKING" in { getattr(node.test, "id", None), getattr(node.test, "attr", None), }: visit(node.orelse)
            elif isinstance(node, (ast.Try, ast.TryStar)):
                caught = { name for handler in node.handlers for name in (re.findall(r"\w+", ast.unparse(handler.type)) if handler.type else ["BaseException"]) }
                if not caught & _IMPORT_ERRORS: visit(node.body)
                for handler in node.handlers: visit(handler.body)
                visit(node.orelse); visit(node.finalbody)
            else:
                for field in ("body", "orelse", "finalbody"):
                    child = getattr(node, field, None)
                    if isinstance(child, list): visit(child)
    source_file = tools_dir / f"{tool}.py"
    if not source_file.is_file(): source_file = tools_src / f"{tool}.py"
    visit(ast.parse(source_file.read_text(encoding="utf-8")).body); needed: set[str] = set(); walked: set[str] = set()
    while pending := sorted(subtools - walked):
        for sub in pending:
            walked.add(sub)
            if (tools_src / f"{sub}.py").is_file() and f"truealpha_tools.{sub}" not in seen: seen.add(f"truealpha_tools.{sub}"); visit(ast.parse((tools_src / f"{sub}.py").read_text(encoding="utf-8")).body)
    for name in names:
        if (tools_dir / f"{name}.py").is_file() or (tools_dir / name / "__init__.py").is_file():
            if name not in seen: needed |= _module_level_imports(name, seen)
        elif name not in sys.stdlib_module_names: needed.add(name)
    return needed
def _installs_for_the_path_interpreter(earlier: list[dict]) -> bool: setup_uv = [ index for index, spec in enumerate(earlier) if re.fullmatch(r"\./\.github/actions/setup-uv|astral-sh/setup-uv@\S+", str(spec.get("uses", ""))) ]; runs = [str(spec.get("run", "")) for spec in earlier]; synced = bool(setup_uv) and any(re.search(r"\buv\s+sync\b", run) for run in runs[setup_uv[0] + 1 :]); on_path = any(re.search(r"\.venv/bin\"?\s*>>\s*\"?\$\{?GITHUB_PATH\b", run) for run in runs); pip = any(re.search(r"(?<!uv )\bpip3?\s+install\b|\buv\s+pip\s+install\b[^\n]*--system", run) for run in runs); return (synced and on_path) or pip
def test_a_bare_python_runs_a_tool_only_after_its_job_installs_what_the_tool_imports() -> None:
    calls = 0; offenders = []
    for path in sorted((REPO_ROOT / ".github/workflows").glob("*.yml")):
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job_id, spec in (workflow.get("jobs") or {}).items():
            job_steps = spec.get("steps") or []
            for index, item in enumerate(job_steps):
                for tool in _bare_tool_calls(str(item.get("run", ""))):
                    calls += 1; imported = _module_level_imports(tool)
                    if imported and not _installs_for_the_path_interpreter(job_steps[:index]):
                        label = item.get("name") or item.get("id") or f"#{index + 1}"
                        offenders.append( f'{path.name}: job {job_id!r}, step {label!r} runs tools/{tool}.py with a bare python, and the tool imports {sorted(imported)} at module level' )
    assert calls, "no workflow step runs a tools/ script with a bare python: this check has lost its subject"
    assert not offenders, ( f'no earlier step of the job installs these imports for the PATH interpreter: {offenders}. The step fails with ModuleNotFoundError on a runner without them (#1369). Before the step, add setup-uv, then `uv sync`, then `echo "$PWD/.venv/bin" >> "$GITHUB_PATH"`. Or start the call with `uv run --no-sync` after a `uv sync`' )
def test_anything_that_caches_can_actually_save() -> None:
    workflows = { path.name: yaml.safe_load(path.read_text(encoding="utf-8")) for path in sorted((REPO_ROOT / ".github/workflows").glob("*.yml")) }
    def caches(job: dict) -> bool:
        for step in job.get("steps") or []:
            if "actions/cache" in str(step.get("uses", "")): return True
            with_block = step.get("with") or {}
            if str(with_block.get("enable-cache", "")).lower() == "true" or "cache" in with_block: return True
        return False
    def granted(workflow: dict, job: dict) -> dict: return {**(workflow.get("permissions") or {}), **(job.get("permissions") or {})}
    def only_reusable(workflow: dict) -> bool: triggers = workflow.get(True) or workflow.get("on") or {}; return set(triggers) == {"workflow_call"} if isinstance(triggers, dict) else triggers == "workflow_call"
    offenders = []
    for name, workflow in workflows.items():
        if only_reusable(workflow): continue
        for job_name, job in (workflow.get("jobs") or {}).items():
            called = str(job.get("uses", ""))
            if called.startswith("./.github/workflows/"):
                child = workflows.get(called.rsplit("/", 1)[-1])
                if child and any(caches(inner) for inner in (child.get("jobs") or {}).values()):
                    if granted(workflow, job).get("actions") != "write": offenders.append(f"{name}:{job_name} -> {called.rsplit('/', 1)[-1]}")
                continue
            if caches(job) and granted(workflow, job).get("actions") != "write": offenders.append(f"{name}:{job_name}")
    assert not offenders, ( f'these jobs cache without `actions: write`: {offenders}. The cache restores, never saves, and the job fails outright the first time its key changes' )
def test_the_split_python_jobs_cover_every_testpath() -> None:
    import tomllib; testpaths = set( tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["pytest"]["ini_options"][ "testpaths" ] ); workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/ci-python.yml").read_text(encoding="utf-8")); covered: set[str] = set()
    for job in workflow["jobs"].values():
        for step in job.get("steps") or []:
            run = str(step.get("run", ""))
            for match in re.finditer(r"pytest_shard\.py\s+(.+?)\s+--shard", run):
                for arg in shlex.split(match.group(1)): covered.add(arg)
            if run.startswith("uv run pytest "): tokens = shlex.split(run); covered.update(t for t in tokens[3:] if not t.startswith("-"))
    assert covered == testpaths, ( f'the split ci-python jobs run pytest over {sorted(covered)} but pyproject declares testpaths {sorted(testpaths)} — anything in the difference merges green with no CI coverage at all (#673)' )
def test_the_pr_trigger_covers_every_file_the_manifest_names() -> None:
    import json; workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/mutation-reproof.yml").read_text(encoding="utf-8")); triggers_block = workflow.get(True) or workflow.get("on") or {}; trigger_paths = set(triggers_block["pull_request"]["paths"]); manifest = json.loads((REPO_ROOT / "tools/mutations.json").read_text(encoding="utf-8")); needed = {m["file"] for m in manifest["mutations"]} | {m["guard"] for m in manifest["mutations"]}; needed |= {"tools/mutations.json", "tools/mutation_reproof.py"}; missing = sorted(needed - trigger_paths)
    assert not missing, ( f"tools/mutations.json names {missing} and mutation-reproof.yml's pull_request paths do not include them — a PR editing those files merges with no re-proof and the dead-guard latency regresses to the weekly run (#673)" )
def _changes_filters() -> dict[str, list[str]]:
    workflow = yaml.safe_load(source(REQUIRED)); changes_job = (workflow.get("jobs") or {})["changes"]; filter_step = next( (step for step in changes_job["steps"] if "filters" in (step.get("with") or {})), None, )
    assert filter_step is not None, ( "ci-required's changes job no longer carries a paths-filter step; every lane decision reads from it (#673)" )
    return yaml.safe_load(filter_step["with"]["filters"])
def test_the_changes_filter_reaches_the_sweep_over_published_agent_files() -> None:
    published = [ f"{root}/probe/file.md" for root in ("skills", *_MIRROR_ONLY_EXCLUDED_ROOTS, _RECORD_ONLY_EXCLUDED_ROOT) ] + ["AGENTS.md"]; missing = [path for path in published if "python" not in selected_lanes(REQUIRED, [path])]
    assert not missing, ( f'the python filter does not select {missing}: a PR touching only those paths skips ci-python, and `required` reads the skip as success (#1018)' )
def test_the_changes_filter_reaches_every_test_that_guards_a_tool() -> None:
    import tomllib; filters = _changes_filters()
    for lane in ("python", "db", "web"):
        assert "tools/**" in filters[lane], ( f'the {lane} filter no longer includes tools/**; a tools-only PR skips that lane and `required` reads the skip as success (#673)' )
    assert ".github/workflows/**" in filters["python"], ( "a workflow-only PR skips ci-python, and this file's workflow-shape tests are exactly what does not run (#673)" )
    testpaths = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["pytest"][ "ini_options" ]["testpaths"]
    for path in testpaths:
        top = "/".join(path.split("/")[:2])
        assert any(entry.startswith(top) for entry in filters["python"]), ( f'pytest testpath {path!r} is outside every python filter entry — PRs touching it skip ci-python and merge with that suite never running (#673)' )
PICOMATCH_GOLDEN = ( ("docs/**", "docs", True), ("docs/**", "docs/a.md", True), ("docs/**", "docs/x/y.md", True), ("docs/**", "docsx/a.md", False), ("docs/**", ".docs/a.md", False), ("docs/**", "docs/.hidden/x", True), (".claude/**", ".claude/skills/prr/SKILL.md", True), (".claude/**", "claude/x", False), ("*.md", "README.md", True), ("*.md", "docs/a.md", False), ("*.md", ".hidden.md", True), ("**/*.md", "a/.b/c.md", True), ("apps/**/*.md", "apps/README.md", True), ("apps/**/*.md", "apps/.x/README.md", True), ("a/*/b", "a/b", False), ("a/**/b", "a/b", True), ("a/**/b", "a/.x/b", True), ("a?c", "a/c", False), ("a?c", "a.c", True), ("*", ".env.example", True), ("*", "a/b", False), ("**", ".a/b", True), ("apps/*", "apps", False), ("apps/*", "apps/.x", True), ("common/runtime/**", "common/runtime", True), ("common/runtime/**", "common/runtime2/x", False), ("uv.lock", "uv.lock.bak", False), ("!apps/app-web/tests/**", "docs/a.md", True), ("!apps/app-web/tests/**", "apps/app-web/tests/a.ts", False), )
def test_the_glob_matcher_agrees_with_picomatch_including_dot_paths() -> None:
    wrong = [ (pattern, path, want) for pattern, path, want in PICOMATCH_GOLDEN if glob_matches(pattern, path) is not want ]; assert not wrong, f"glob_matches differs from picomatch 2.3.1 on (pattern, path, picomatch result): {wrong}"; outcomes = {want for *_, want in PICOMATCH_GOLDEN}; assert outcomes == {True, False}, "the golden pairs must hold a match and a non-match"
    for unsupported in ("{a,b}/**", "a/[bc]", "a/+(b|c)", "a/!(b)", "a**b", "!!a/**", "/a/**", "./a/**", ""):
        with pytest.raises(ValueError): glob_matches(unsupported, "a/b")
    for path in ("", "/a", "a/"):
        with pytest.raises(ValueError): glob_matches("a/**", path)
def test_a_negated_filter_entry_stands_only_under_the_every_quantifier() -> None:
    negated = { lane: [entry for entry in patterns if entry.startswith("!")] for quantifier, lanes in paths_filters(REQUIRED) if quantifier == "some" for lane, patterns in lanes.items() }; negated = {lane: entries for lane, entries in negated.items() if entries}
    assert not negated, ( f'these lanes hold `!` entries under the default `some` quantifier: {negated}. Each entry selects every path it does not name, so the lane starts on every PR (#1412)' )
def test_every_lane_a_job_reads_is_a_lane_the_filter_defines() -> None: defined = {lane for _, lanes in paths_filters(REQUIRED) for lane in lanes}; published = set(job(REQUIRED, "changes")["outputs"]); read = set(re.findall(r"needs\.changes\.outputs\.(\w+)", source(REQUIRED))); assert read, "no job reads a changes output, so this test checks nothing"; assert read <= defined, f"jobs read lanes the filter does not define: {sorted(read - defined)}"; assert read <= published, f"jobs read outputs the changes job does not publish: {sorted(read - published)}"
_PULL_REQUEST = {"event_name": "pull_request", "ref_type": "branch", "ref": "refs/pull/1/merge"}
def _jobs_started_by(paths: list[str]) -> set[str]: selected = selected_lanes(REQUIRED, paths); defined = {lane for _, lanes in paths_filters(REQUIRED) for lane in lanes}; outputs = {lane: "true" if lane in selected else "false" for lane in defined}; jobs = load(REQUIRED)["jobs"]; needs = {name: {"result": "success", "outputs": outputs if name == "changes" else {}} for name in jobs}; return {name for name, spec in jobs.items() if _decides(spec.get("if"), _PULL_REQUEST, needs=needs)}
def _run_required_summary(started: set[str], failed: str | None = None) -> subprocess.CompletedProcess[str]: import json; assert shutil.which("jq"), "the required summary calls jq; the CI runner has it, so install it here"; results = { name: {"result": "failure" if name == failed else "success" if name in started else "skipped"} for name in job(REQUIRED, "required")["needs"] }; script = str(job_step(REQUIRED, "required", "Reject any failed, cancelled, or missing dependency")["run"]); return subprocess.run( ["bash", "-e", "-c", script], env={**os.environ, "RESULTS": json.dumps(results)}, capture_output=True, text=True, check=False, )
@cache
def _tracked_files() -> tuple[str, ...]: listing = subprocess.run( ["git", "-C", str(REPO_ROOT), "ls-files", "-z"], capture_output=True, text=True, check=True ).stdout; tracked = tuple(entry for entry in listing.split("\0") if entry); assert len(tracked) > 100, f"git reports only {len(tracked)} tracked files (GREEN-WHILE-EMPTY)"; return tracked
@cache
def _markdown_named_by_tests() -> dict[str, set[str]]:
    markdown = {path for path in _tracked_files() if path.endswith(".md")}; testpaths = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["pytest"][ "ini_options" ]["testpaths"]; this_file = Path(__file__).resolve().relative_to(REPO_ROOT).as_posix(); named: dict[str, set[str]] = {}
    for testpath in testpaths:
        for test_file in sorted((REPO_ROOT / testpath).rglob("*.py")):
            relative = test_file.relative_to(REPO_ROOT).as_posix()
            if relative == this_file: continue
            for node in ast.walk(ast.parse(test_file.read_text(encoding="utf-8"))):
                if not (isinstance(node, ast.Constant) and isinstance(node.value, str)): continue
                if not re.fullmatch(r"[\w./-]+\.md", node.value): continue
                if node.value in markdown: hits = [node.value]
                elif "/" in node.value: hits = []
                else: hits = [path for path in markdown if path.rsplit("/", 1)[-1] == node.value]
                for hit in hits: named.setdefault(hit, set()).add(relative)
    return named
def _lanes_that_read(path: str, named: dict[str, set[str]]) -> set[str]:
    roots = {"skills", *_MIRROR_ONLY_EXCLUDED_ROOTS, _RECORD_ONLY_EXCLUDED_ROOT}; readers = named.get(path, set()); lanes: set[str] = set()
    if path.split("/")[0] in roots or "/" not in path or readers: lanes.add("python")
    if any(reader.startswith("apps/data-engine/tests/") for reader in readers): lanes.add("data_engine")
    return lanes
def _documents() -> list[str]: roots = {"skills", *_MIRROR_ONLY_EXCLUDED_ROOTS, _RECORD_ONLY_EXCLUDED_ROOT}; return [ path for path in _tracked_files() if path.split("/")[0] in roots or (path.endswith(".md") and (path.startswith("docs/") or "/" not in path)) ]
def _unread_documents() -> list[str]: named = _markdown_named_by_tests(); return [path for path in _documents() if not _lanes_that_read(path, named)]
def test_a_document_selects_the_lanes_that_read_it_and_no_other() -> None:
    named = _markdown_named_by_tests(); anchor = "docs/architecture-decisions/A3-source-governance.md"; assert "common/runtime/tests/test_init_principles.py" in named.get(anchor, set()), ( f"the scan no longer sees test_init_principles.py read {anchor}, so it cannot find readers (GREEN-WHILE-EMPTY)" ); documents = _documents(); assert len(_unread_documents()) >= 10, f"fewer than 10 unread documents; the document list drifted: {documents}"; wrong = {}
    for path in documents:
        selected = selected_lanes(REQUIRED, [path]); wanted = _lanes_that_read(path, named)
        if selected != wanted: wrong[path] = f"filter selects {sorted(selected)}; its readers need {sorted(wanted)}"
    assert not wrong, f"documents whose lanes differ from the lanes of their readers: {wrong}"
def test_a_pull_request_that_changes_only_unread_documents_starts_no_test_lane() -> None:
    unread = _unread_documents(); assert unread, "no unread document exists, so this test checks nothing"; started = _jobs_started_by(unread)
    assert started == {"changes", "security", "required"}, ( f'a PR of {len(unread)} unread documents starts {sorted(started)}; only the filter, the secret scan and the summary may start (#1412)' )
    summary = _run_required_summary(started); assert summary.returncode == 0, f"required reports failure for a docs-only PR: {summary.stderr}"; assert _run_required_summary(started, failed="security").returncode != 0
@pytest.mark.parametrize( ("code_path", "needed"), [ ("common/runtime/src/truealpha_runtime/sample.py", {"python", "runtime", "data_engine"}), ("common/runtime/src/truealpha_tools/output_invariants.py", {"db", "python", "runtime", "data_engine"}), ("common/runtime/src/truealpha_tools/schema_drift.py", {"db", "python", "runtime", "data_engine"}), (".github/workflows/ci-web.yml", {"python", "web"}), ("db/migrations/20261020T0000_sample.sql", {"db", "python", "runtime", "data_engine"}), ("uv.lock", {"python", "runtime", "data_engine"}), ("apps/app-web/package.json", {"web", "image_app_web"}), ], )
def test_code_keeps_its_lanes_when_it_travels_with_unread_documents(code_path: str, needed: set[str]) -> None: unread = _unread_documents(); alone = selected_lanes(REQUIRED, [code_path]); assert needed <= alone, f"{code_path} selects {sorted(alone)} and lacks {sorted(needed - alone)}"; assert selected_lanes(REQUIRED, [code_path, *unread]) == alone, "unread documents changed the lanes of a code PR"; jobs_for_lane = { "db": "db", "python": "python", "runtime": "runtime", "web": "web", "image_app_web": "images_build", }; started = _jobs_started_by([code_path, *unread]); missing = {jobs_for_lane[lane] for lane in needed if lane in jobs_for_lane} - started; assert not missing, f"a PR changing {code_path} does not start {sorted(missing)}"
@pytest.mark.parametrize( ("path", "images"), [ ("apps/app-web/src/app/page.tsx", {"image_app_web"}), ("apps/llm-service/src/llm_service/main.py", {"image_llm_service"}), ("apps/data-engine/src/data_engine/dagster_defs.py", {"image_data_engine"}), ("common/contracts/src/truealpha_contracts/ast.py", {"image_llm_service", "image_data_engine"}), ("uv.lock", {"image_llm_service", "image_data_engine"}), ], )
def test_each_image_lane_selects_only_the_paths_its_image_is_built_from(path: str, images: set[str]) -> None: image_lanes = {"image_app_web", "image_llm_service", "image_data_engine"}; assert selected_lanes(REQUIRED, [path]) & image_lanes == images
def test_a_failing_shard_lane_fails_instead_of_running_the_whole_suite() -> None:
    lane = job(PYTHON, "test-data-engine"); shard_steps = [str(spec.get("run", "")) for spec in lane["steps"] if "pytest_shard.py" in str(spec.get("run", ""))]; assert len(shard_steps) == 1, f"expected exactly one sharding step, found {len(shard_steps)}"; snippet = shard_steps[0]; assert "$(python3 tools/pytest_shard.py" in snippet, ( 'the shard tool is no longer called in a command substitution — its exit status is invisible again, and a failed shard runs the entire suite instead of failing' ); assert "< <(python3 tools/pytest_shard.py" not in snippet, ( "process substitution is back: `set -e` cannot see the shard tool fail" ); assert "refusing to run bare pytest" in snippet, ( 'the empty-selection check is gone — the second layer that keeps an empty FILES array from expanding into a whole-suite run' )
    assert "--of ${{ strategy.job-total }}" in snippet, ( 'the lane count is hard-coded again — a `--of N` that drifts from the matrix length leaves every file at position kN+(N-1) assigned to a shard nobody runs, green forever (the #527 green-while-empty shape this job exists to avoid)' )
    assert "--shard ${{ strategy.job-index }}" in snippet, ( "the shard index no longer comes from the matrix position it is running at" ); assert lane["strategy"]["fail-fast"] is False, ( "fail-fast is on: one red shard would cancel the other two and hide their failures" )
def test_the_liveness_window_is_one_churn_cycle_plus_margin() -> None:
    lane = job(PYTHON, "dagster-code-server-liveness"); observe = [spec for spec in lane["steps"] if "No heartbeat received" in str(spec.get("run", ""))]; assert len(observe) == 1, f"expected exactly one churn-assertion step in the liveness job, found {len(observe)}"; spec = observe[0]; sleeps = re.findall(r"^\s*sleep (\d+)\s*$", str(spec["run"]), flags=re.MULTILINE); assert len(sleeps) == 1, f"the observation is one fixed sleep, found {sleeps}"; window = int(sleeps[0]); cadences = re.findall(r"every ~(\d+)-(\d+)s", source(PYTHON)); assert cadences, "ci-python.yml no longer documents the ~68-70s churn cadence the window is sized from"; far_end = max(int(high) for _, high in cadences); assert window > far_end, ( f"a {window} s window cannot be sure to contain a churn event that arrives every ~{far_end} s" )
    assert window <= far_end + 10, ( f"the {window} s window is more than 10 s past the ~{far_end} s cadence — the margin is creeping back toward the second cycle that cost 45 s on every run's critical path (#860)" )
    assert f"{window}s" in str(spec["name"]), "the step's name must say how long it observes"
def test_dagster_liveness_filter_is_narrow_and_a_subset_of_python() -> None:
    workflow = yaml.safe_load(source(REQUIRED)); filter_step = next( (step for step in workflow["jobs"]["changes"]["steps"] if "filters" in (step.get("with") or {})), None, ); assert filter_step is not None, "ci-required's changes job no longer carries a paths-filter step"; filters = yaml.safe_load(filter_step["with"]["filters"])
    assert "dagster_liveness" in filters, ( "the changes job no longer declares a dagster_liveness filter — #855 A4's gating has nothing to read and ci-python.yml falls back to running liveness on every PR again" )
    liveness_paths = set(filters["dagster_liveness"]); expected = { "apps/data-engine/src/data_engine/dagster_defs.py", "apps/data-engine/src/data_engine/lanes/**", "apps/data-engine/Dockerfile", "docker-compose.yml", ".github/workflows/ci-python.yml", }; assert liveness_paths == expected, ( f'dagster_liveness filter paths are {liveness_paths}, expected {expected} — the job it gates only reproduces through the composition root, its lanes, the image, the compose surface, and this file itself' )
    def covered(path: str, patterns: set[str]) -> bool:
        for pattern in patterns:
            if pattern == path: return True
            if pattern.endswith("/**") and path.startswith(pattern[: -len("/**")] + "/"): return True
        return False
    python_paths = set(filters["python"]); uncovered = {path for path in liveness_paths if not covered(path, python_paths)}; assert not uncovered, ( f'{uncovered} is in dagster_liveness but no python filter entry covers it — a PR that touches only that path sets dagster_liveness true while ci-python.yml is never invoked at all, so the liveness job silently never runs' ); outputs = workflow["jobs"]["changes"]["outputs"]; assert outputs.get("dagster_liveness") == ( "${{ github.event_name == 'merge_group' && 'true' || steps.filter.outputs.dagster_liveness }}" ), "dagster_liveness must be wired the same way as every other changes output (merge_group forces true)"
def test_dagster_liveness_job_is_gated_off_a_pr_and_unconditional_elsewhere() -> None:
    caller = job(REQUIRED, "python"); decision = str(caller["with"]["dagster_liveness_required"]); assert "github.event_name != 'pull_request'" in decision, ( f"the caller's decision is {decision!r} — it no longer runs liveness unconditionally off pull_request (main push, merge_group, workflow_dispatch)" ); assert "needs.changes.outputs.dagster_liveness == 'true'" in decision, ( f"the caller's decision is {decision!r} — it no longer reads the narrow filter, so a PR that touches the dagster surface would never run the liveness job either" ); liveness_input = triggers(PYTHON)["workflow_call"]["inputs"]["dagster_liveness_required"]; assert liveness_input["type"] == "boolean" and liveness_input["default"] is False, ( "dagster_liveness_required must default closed — an unset input on a direct call must not silently change what runs" )
    assert set(triggers(PYTHON)) == {"workflow_call"}, ( f'ci-python.yml declares {sorted(triggers(PYTHON))} — a reusable workflow with its own triggers silently gets no PR run (#878)' )
    liveness = job(PYTHON, "dagster-code-server-liveness"); condition = str(liveness["if"]); assert "inputs.dagster_liveness_required" in condition, ( f"the liveness job's if is {condition!r} — it no longer reads the caller's decision, so it either always runs (back on the PR critical path) or never does" ); assert "github.event_name" not in condition, ( f"the liveness job's if is {condition!r} — inside a reusable workflow that context is the caller's; the contract is the named inputs" ); de_cond = str(job(PYTHON, "test-data-engine")["if"]); assert "inputs.data_engine_required" in de_cond, ( f"test-data-engine condition {de_cond!r} must gate on data_engine_required" )
def test_the_data_engine_required_gate_and_filter() -> None:
    caller = job(REQUIRED, "python"); decision = str(caller["with"]["data_engine_required"]); assert "github.event_name != 'pull_request'" in decision; assert "needs.changes.outputs.data_engine == 'true'" in decision; de_input = triggers(PYTHON)["workflow_call"]["inputs"]["data_engine_required"]; assert de_input["type"] == "boolean" and de_input["default"] is True; de_paths = _paths_filter("data_engine")
    for required_prefix in ( "apps/data-engine/**", "common/contracts/**", "common/factors/**", "common/runtime/**", "db/**", "pyproject.toml", "uv.lock", ): assert required_prefix in de_paths, f"data_engine filter misses {required_prefix}"
POLARS_CROSS_CHECK_TESTS = { "common/factors/tests/test_gross_profit_per_employee.py", "common/factors/tests/test_peg.py", "common/factors/tests/test_price_to_sales.py", }; POLARS_CROSS_CHECK_SOURCES = { "common/factors/src/factors/base/gross_profit_per_employee.py", "common/factors/src/factors/base/peg.py", "common/factors/src/factors/base/price_to_sales.py", "common/factors/src/factors/panel.py", }
def _paths_filter(name: str) -> set[str]: workflow = yaml.safe_load(source(REQUIRED)); filter_step = next( (step for step in workflow["jobs"]["changes"]["steps"] if "filters" in (step.get("with") or {})), None, ); assert filter_step is not None, "ci-required's changes job no longer carries a paths-filter step"; filters = yaml.safe_load(filter_step["with"]["filters"]); assert name in filters, f"the changes job no longer declares a {name} filter"; return set(filters[name])
def _covered_by(path: str, patterns: set[str]) -> bool:
    for pattern in patterns:
        if pattern == path: return True
        if pattern.endswith("/**"):
            prefix = pattern[: -len("/**")]
            if path == prefix or path.startswith(prefix + "/"): return True
    return False
def test_the_changes_filter_reaches_the_shared_uv_action() -> None:
    action_path = ".github/actions/setup-uv/action.yml"; lanes = {"db": DB, "python": PYTHON, "data_engine": PYTHON, "runtime": RUNTIME}
    for lane, workflow in lanes.items(): assert any(spec.get("uses") == "./.github/actions/setup-uv" for spec in steps(workflow)), ( f"{workflow} no longer calls the shared setup-uv action; update the lane map of this test" ); assert _covered_by(action_path, _paths_filter(lane)), ( f'the {lane} filter in ci-required.yml misses {action_path}: a PR that edits only the action skips {workflow} and `required` reads the skip as success' )
def test_the_polars_cross_check_proofs_are_armed_in_ci_python() -> None:
    python_paths = _paths_filter("python")
    for relative in sorted(POLARS_CROSS_CHECK_TESTS | POLARS_CROSS_CHECK_SOURCES): assert (REPO_ROOT / relative).exists(), ( f"{relative} does not exist — the cross-check inventory has drifted from the tree" )
    uncovered = { relative for relative in POLARS_CROSS_CHECK_TESTS | POLARS_CROSS_CHECK_SOURCES if not _covered_by(relative, python_paths) }
    assert not uncovered, ( f"{sorted(uncovered)} are not covered by ci-required.yml's python paths filter — a PR touching only these files would skip ci-python, and `required` reads a skipped job as success, leaving the factor reproducibility proofs unarmed (#956, #969)" )
    runs = [ str(step.get("run", "")) for step in yaml.safe_load(source(PYTHON))["jobs"]["test-core"]["steps"] if "pytest" in str(step.get("run", "")) ]; assert any("common/factors/tests" in run for run in runs), ( f"ci-python's test-core job no longer runs common/factors/tests — the proofs are covered by the filter but nothing executes them. pytest invocations found: {runs!r}" )
    for relative in sorted(POLARS_CROSS_CHECK_TESTS):
        text = (REPO_ROOT / relative).read_text(encoding="utf-8")
        assert "importorskip" not in text, ( f'{relative} gates on pytest.importorskip again — the job runs, the test reports as passed, and the assertion never executes. That is exactly the state #969 migrated off Qlib to escape (11 of 12 ci-python runs skipped it).' )
        assert "factors.panel" in text and "pl.DataFrame" in text, ( f'{relative} no longer evaluates the native Polars panel expression — the cross-check would assert the Decimal path against itself' )
QLIB_DISTRIBUTION_MARKER = "qlib"
def test_no_lockfile_resolves_a_qlib_distribution() -> None:
    lockfiles = [path for path in REPO_ROOT.rglob("uv.lock") if not set(path.parts) & {".venv", ".git", "node_modules"}]; assert lockfiles, "no uv.lock found at all — this check would pass by having nothing to read"
    for lockfile in lockfiles:
        relative = lockfile.relative_to(REPO_ROOT).as_posix(); packages = tomllib.loads(lockfile.read_text(encoding="utf-8")).get("package", []); assert len(packages) > 20, ( f'{relative} resolves only {len(packages)} packages — that is not a populated lockfile, so a clean result would mean nothing (GREEN-WHILE-EMPTY)' ); names = sorted({str(package.get("name", "")).lower() for package in packages}); offenders = [name for name in names if QLIB_DISTRIBUTION_MARKER in name]
        assert not offenders, ( f'{relative} resolves {offenders} — the Qlib dependency is back. #969 removed the factor-expression engine and the workspace-excluded `common/factors/qlib-runtime` project that carried it; a lockfile entry puts it into every environment that installs from this file.' )
def test_qlib_is_not_resolvable_in_this_environment() -> None:
    installed = sorted( { name.lower() for distribution in importlib.metadata.distributions() if (name := distribution.metadata["Name"] or "") } ); assert len(installed) > 20, ( f'only {len(installed)} distributions are visible — the environment is not the one the suite runs in, so a clean result would mean nothing (GREEN-WHILE-EMPTY)' ); offenders = [name for name in installed if QLIB_DISTRIBUTION_MARKER in name]
    assert not offenders, ( f"{offenders} {('is' if len(offenders) == 1 else 'are')} installed in the environment this suite runs in — #969 removed the Qlib engine, and an installed distribution is a code path whatever the repository's text says" )
    assert importlib.util.find_spec("qlib") is None, ( '`import qlib` resolves in this environment — the deleted engine is importable again. It need not be declared anywhere for this to be true: a path entry, an editable install or a vendored directory is enough, which is why this is checked separately from the distribution list above.' )
QLIB_SCAN_ROOTS = (".github", ".agents", "apps", "db", "common", "tools"); _MIRROR_ONLY_EXCLUDED_ROOTS = (".claude",); _RECORD_ONLY_EXCLUDED_ROOT = ".ws-publish"; QLIB_SCAN_EXCLUDED_ROOT_DIRS = ( "docs", "governance", *_MIRROR_ONLY_EXCLUDED_ROOTS, _RECORD_ONLY_EXCLUDED_ROOT, ); QLIB_SCAN_REPOSITORY_ROOT_FILES = True; QLIB_SCAN_EXCLUDED_DIRS = ( "common/contracts/tests/fixtures", "apps/app-web/node_modules", "apps/app-web/.next", ); QLIB_SCAN_EXCLUDED_FILES = ("init.md",); _QLIB_SCAN_TEXT_NAMES = {"Dockerfile", "Makefile", "makefile", "GNUmakefile", "Containerfile"}; _QLIB_SCAN_TEXT_NAME_PREFIXES = ("Dockerfile.", "Makefile."); _QLIB_SCAN_GENERATED_DIR_NAMES = { "__pycache__", ".git", ".venv", ".mypy_cache", ".pytest_cache", ".ruff_cache", "node_modules", }; _QLIB_SCAN_TEXT_SUFFIXES = { ".cfg", ".css", ".dockerfile", ".example", ".html", ".ini", ".js", ".json", ".jsx", ".lock", ".md", ".mjs", ".py", ".sh", ".sql", ".toml", ".ts", ".tsx", ".txt", ".yaml", ".yml", }; QLIB_SCAN_MUST_REACH = ( "pyproject.toml", "Makefile", "docker-compose.yml", "apps/data-engine/Dockerfile", "apps/llm-service/Dockerfile", "apps/app-web/Dockerfile", "db/apply_migrations.sh", ".agents/skills/factor-acceptance/SKILL.md", ".github/workflows/ci-required.yml", )
def _is_scannable_text_file(path: Path) -> bool:
    if path.suffix.lower() in _QLIB_SCAN_TEXT_SUFFIXES: return True
    if path.name in _QLIB_SCAN_TEXT_NAMES: return True
    return path.name.startswith(_QLIB_SCAN_TEXT_NAME_PREFIXES)
def _qlib_scan_candidates() -> list[Path]:
    excluded_dirs = tuple((REPO_ROOT / relative).resolve() for relative in QLIB_SCAN_EXCLUDED_DIRS); excluded_files = {(REPO_ROOT / name).resolve() for name in QLIB_SCAN_EXCLUDED_FILES}; excluded_files.add(Path(__file__).resolve()); found: list[Path] = []; walked: list[Path] = []
    if QLIB_SCAN_REPOSITORY_ROOT_FILES: walked.extend(REPO_ROOT.glob("*"))
    for root_name in QLIB_SCAN_ROOTS: root = REPO_ROOT / root_name; assert root.is_dir(), f"{root_name} is not a directory — the scan roots have drifted"; walked.extend(root.rglob("*"))
    for path in walked:
        if not path.is_file() or path.is_symlink(): continue
        if set(path.parts) & _QLIB_SCAN_GENERATED_DIR_NAMES: continue
        resolved = path.resolve()
        if resolved in excluded_files: continue
        if any(directory in resolved.parents for directory in excluded_dirs): continue
        if not _is_scannable_text_file(path): continue
        found.append(path)
    return found
def test_every_top_level_directory_is_scanned_or_explicitly_excluded() -> None:
    classified = set(QLIB_SCAN_ROOTS) | set(QLIB_SCAN_EXCLUDED_ROOT_DIRS) | _QLIB_SCAN_GENERATED_DIR_NAMES; listing = subprocess.run( ["git", "-C", str(REPO_ROOT), "ls-files", "-z"], capture_output=True, text=True, check=True, ).stdout; tracked_paths = [entry for entry in listing.split("\0") if entry]; assert len(tracked_paths) > 100, ( f'git reports only {len(tracked_paths)} tracked files — this is not the repository, so a clean result would mean nothing (GREEN-WHILE-EMPTY)' ); present = {entry.split("/", 1)[0] for entry in tracked_paths if "/" in entry}; unclassified = present - classified; assert not unclassified, ( f"top-level director{('y' if len(unclassified) == 1 else 'ies')} {sorted(unclassified)} {('is' if len(unclassified) == 1 else 'are')} neither scanned for a Qlib reintroduction nor explicitly excluded, so a reference there would leave test_no_code_path_reintroduces_qlib green. Add to QLIB_SCAN_ROOTS to scan it, or to QLIB_SCAN_EXCLUDED_ROOT_DIRS with the reason it is out of scope — the way docs/ and governance/ are."   )
    for root_name in QLIB_SCAN_ROOTS: assert (REPO_ROOT / root_name).is_dir(), f"{root_name} is a declared scan root but is not a directory"
    for root_name in QLIB_SCAN_EXCLUDED_ROOT_DIRS: assert (REPO_ROOT / root_name).is_dir(), ( f'{root_name} is excluded from the scan but no longer exists — drop the exclusion rather than leaving a rule about a directory that is gone' )
def test_no_code_path_reintroduces_qlib() -> None:
    offenders: list[str] = []; read: set[str] = set()
    for path in _qlib_scan_candidates():
        relative = path.relative_to(REPO_ROOT).as_posix()
        try: text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):  # pragma: no cover - binary or unreadable
            continue
        read.add(relative)
        for number, line in enumerate(text.splitlines(), start=1):
            if "qlib" in line.lower(): offenders.append(f"{relative}:{number}: {line.strip()[:120]}")
    assert len(read) > 500, ( f'the scan only read {len(read)} files — the walk is not reaching the tree, so a clean result would mean nothing (GREEN-WHILE-EMPTY)' )
    for relative in QLIB_SCAN_MUST_REACH: assert (REPO_ROOT / relative).is_file(), ( f"{relative} no longer exists, so this guard's coverage list is describing a tree that is gone — re-derive QLIB_SCAN_MUST_REACH rather than dropping the entry"       ); assert relative in read, ( f'the scan never read {relative} — a Qlib reference there would leave this test green. That is the exact hole this guard has had twice: a suffix allowlist drops every extensionless Dockerfile, rglob from a scan root cannot reach a repository-root file, and a top-level directory that is not a scan root is never entered at all.' )
    assert not offenders, ( "Qlib is referenced again under " + ", ".join((*QLIB_SCAN_ROOTS, "the repository root")) + f' ({len(offenders)} line(s)). #969 migrated the factor-expression engine to the Polars AST and deleted the Qlib side; a code path naming it again is a reintroduction, not a leftover. If the reference is a historical record rather than a dependency, it belongs in docs/ or governance/, not here. (This sweep is best-effort — the binding checks are test_no_lockfile_resolves_a_qlib_distribution and test_qlib_is_not_resolvable_in_this_environment.)\n' + "\n".join(sorted(offenders)[:40]) )
def test_main_pushes_never_collide_and_a_tag_run_can_never_be_cancelled() -> None:
    concurrency = yaml.safe_load(source(REQUIRED))["concurrency"]; group = str(concurrency["group"]); assert concurrency["cancel-in-progress"] is True, ( 'PR runs no longer collapse to their newest push — every superseded PR run now queues behind the one that replaced it' ); assert "github.event_name == 'push'" in group, ( f'the concurrency key is {group!r}: a main push is no longer routed to its own SHA-keyed group, so two main commits can land in the same group again' )
    assert "github.sha" in group, ( f'the concurrency key is {group!r}: it no longer uses github.sha, so a main push cannot get its own group and main pushes go back to colliding (measured 6/27, #860)' )
    assert "github.ref" in group; assert "github.event_name" in group
def test_the_split_web_jobs_still_run_every_web_check() -> None:
    jobs = yaml.safe_load(source(WEB))["jobs"]; assert set(jobs) == {"check", "browser"}, f"ci-web's jobs changed to {sorted(jobs)}"; everything = "\n".join(spec_text(spec) for job in jobs.values() for spec in job["steps"])
    for fragment, what in ( ("make typecheck-web FULL=1", "the TypeScript typecheck"), ("make test-web FULL=1", "the DB-backed test loop (#373: 13 suites once ran nowhere)"), ("bun run build", "the production build"), ("walk-tree.mjs", "the frozen route-tree walk (#494)"), ("check_route_manifest.py", "the route-namespace manifest (#463)"), ("truealpha_empty", "the absent-state database (#580)"), ("TRUEALPHA_REQUIRE_RUNTIME", "the arming variable that turns a skip into a failure (#468)"), ):
        assert fragment in everything, ( f"{what} runs in neither ci-web job — the split dropped it, and a dropped check merges green forever" )
    browser = "\n".join(str(spec.get("run", "")) for spec in jobs["browser"]["steps"]); assert browser.index("bun run build") < browser.index("walk-tree.mjs"), "the walk runs before the build it walks"
def test_the_chromium_cache_can_actually_save() -> None:
    web_caller = yaml.safe_load(source(REQUIRED))["jobs"]["web"]; assert web_caller.get("permissions", {}).get("actions") == "write", ( "ci-required's web job does not grant actions: write, so ci-web's chromium cache can never save — it will miss on every run and report success"   ); cache = step(WEB, "Restore chromium"); assert "hashFiles(" in str(cache["with"]["key"]), ( 'the chromium cache key is not derived from the lockfile — a constant key serves a stale browser forever, and a parsed version string is one rename from becoming constant' ); install = step(WEB, "Install chromium for the walk-tree suite")
    assert "steps.chromium_cache.outputs.cache-hit != 'true'" in install.get("if", ""), ( "the browser job installs chromium unconditionally even when cached (#1208)" )
def test_no_job_holds_a_cache_grant_it_does_not_use() -> None:
    workflows = { path.name: yaml.safe_load(path.read_text(encoding="utf-8")) for path in sorted((REPO_ROOT / ".github/workflows").glob("*.yml")) }; granted = { str(spec["uses"]).rsplit("/", 1)[-1] for workflow in workflows.values() for spec in (workflow.get("jobs") or {}).values() if isinstance(spec, dict) and str(spec.get("uses", "")).startswith("./.github/workflows/") and (spec.get("permissions") or {}).get("actions") == "write" }; assert granted, "no workflow_call grants actions: write any more — this check has lost its subject"; offenders = []
    for name in sorted(granted):
        for job_id, spec in (workflows[name].get("jobs") or {}).items():
            uses_cache = any( "actions/cache" in str(item.get("uses", "")) or "enable-cache" in str(item.get("with", "")) for item in spec.get("steps") or [] ); holds_write = (spec.get("permissions") or {}).get("actions") == "write"
            if not uses_cache and holds_write: offenders.append(f"{name}:{job_id}")
            if not uses_cache and spec.get("permissions") is None: offenders.append(f"{name}:{job_id} (unpinned, so it inherits the caller's grant)")
    assert not offenders, ( f'these jobs hold a cache-write token they never use: {offenders}. A caller grant is a ceiling for the whole called workflow; narrow it per job.' )
def test_every_buildx_setup_retries_once_after_a_docker_hub_failure() -> None:
    for name in ("build", "publish"): steps = job(IMAGES, name)["steps"]; setups = [i for i, s in enumerate(steps) if str(s.get("uses", "")).startswith("docker/setup-buildx-action@")]; assert len(setups) == 2, f"{name} sets up buildx {len(setups)} time(s); expected a first try and one retry"; first, retry = (steps[i] for i in setups); assert first.get("id") == "buildx" and first.get("continue-on-error") is True, ( f"{name}'s first buildx setup must be `id: buildx` with continue-on-error, or its failure ends the job" ); assert retry.get("if") == "steps.buildx.outcome == 'failure'", ( f"{name}'s retry runs {retry.get('if')!r}; it must run only when the first try failed" ); pause = steps[setups[1] - 1]; assert pause.get("if") == "steps.buildx.outcome == 'failure'" and "sleep" in str(pause.get("run", "")), ( f"{name} retries without a pause — an immediate retry meets the same timeout" )
@pytest.mark.parametrize("excluded_root", _MIRROR_ONLY_EXCLUDED_ROOTS)
def test_every_mirror_only_root_entry_is_scanned_through_its_source(excluded_root: str) -> None:
    assert ".agents" in QLIB_SCAN_ROOTS, ( "published copies are swept through .agents/skills/, so .agents must stay a scan root" ); listing = subprocess.run( ["git", "-C", str(REPO_ROOT), "ls-files", "-z", "--", excluded_root], capture_output=True, text=True, check=True, ).stdout; entries = [entry for entry in listing.split("\0") if entry]; assert entries, ( f'{excluded_root}/ is excluded from the Qlib sweep but git tracks nothing there, so this assertion is checking nothing (GREEN-WHILE-EMPTY). Remove the exclusion or remove this test.' ); scanned = {(REPO_ROOT / root).resolve() for root in QLIB_SCAN_ROOTS}; copy_prefix = f"{excluded_root}/skills/"; escapes: list[str] = []
    for entry in entries:
        path = REPO_ROOT / entry
        if path.is_symlink():
            target = path.resolve()
            if not any(target == root or root in target.parents for root in scanned): escapes.append(f"{entry} -> {target} resolves outside every scanned root")
            continue
        source = REPO_ROOT / ".agents" / "skills" / entry[len(copy_prefix) :] if entry.startswith(copy_prefix) else None
        if source is None or not source.is_file() or source.read_bytes() != path.read_bytes(): escapes.append(f"{entry} is a real file that no file under .agents/skills/ repeats byte for byte")
    assert not escapes, ( f'{excluded_root}/ is excluded from the Qlib sweep only because everything in it is scanned through its source. These are not: {escapes}. Either move them under a scan root or move {excluded_root} into QLIB_SCAN_ROOTS.' )
def test_the_publish_record_root_holds_only_the_record() -> None: listing = subprocess.run( ["git", "-C", str(REPO_ROOT), "ls-files", "-z", "--", _RECORD_ONLY_EXCLUDED_ROOT], capture_output=True, text=True, check=True, ).stdout; entries = sorted(entry for entry in listing.split("\0") if entry); assert entries == [f"{_RECORD_ONLY_EXCLUDED_ROOT}/manifest.json"], ( f'{_RECORD_ONLY_EXCLUDED_ROOT}/ is excluded from the Qlib sweep only because it holds the publish record and nothing else; git tracks {entries}. Scan what was added, or remove the exclusion.' )
def test_gates_runs_complexity_gate() -> None: workflow = yaml.safe_load(source(PYTHON)); gate_steps = [str(s.get("run", "")) for s in workflow["jobs"]["gates"]["steps"]]; assert any("tools/complexity_gate.py --check" in s for s in gate_steps), ( "ci-python.yml gates job must run tools/complexity_gate.py --check" )
def test_test_core_checks_out_full_git_history_for_release_tag_checks() -> None: checkout = next( spec for spec in job(PYTHON, "test-core")["steps"] if str(spec.get("uses", "")).startswith("actions/checkout") ); assert checkout.get("with", {}).get("fetch-depth") == 0, ( "ci-python.yml test-core must check out full history (`fetch-depth: 0`) so release-tag replay checks see vX.Y.Z tags" )
def test_ci_python_survives_a_github_504_on_the_infra2_wheel() -> None:
    synced = []
    for job_name, job_spec in yaml.safe_load(source(PYTHON))["jobs"].items():
        job_steps = job_spec.get("steps", []); sync_at = next((i for i, s in enumerate(job_steps) if "uv sync" in str(s.get("run", ""))), None)
        if sync_at is None: continue
        synced.append(job_name); assert any(s.get("uses") == "./.github/actions/setup-uv" for s in job_steps[:sync_at]), ( f'ci-python.yml job {job_name} runs uv sync without ./.github/actions/setup-uv before it: the wheel download then has only the 3 default retries of uv' ); removed = [ s.get("name") or s.get("uses") or s.get("run") for s in job_steps if any(mark in spec_text(s) for mark in ("infra2_wheel_cache", "UV_FIND_LINKS", ".cache/infra2-sdk")) ]; assert not removed, ( f'ci-python.yml job {job_name} has the wheel cache steps {removed} again. uv ignores UV_FIND_LINKS for a wheel that uv.lock pins by URL, so these steps do not stop a 504' )
    assert len(synced) >= 4, f"only {synced} run uv sync; four jobs did when #1248 was fixed"
CI_MAKE_TARGETS = ( (PYTHON, "gates", "make lint FULL=1"), (PYTHON, "gates", "make typecheck-python FULL=1"), (PYTHON, "gates", "make contract-conformance FULL=1"), (WEB, "check", "make typecheck-web FULL=1"), ); DIRECT_COMMANDS = ( "ruff check", "ruff format", "uv run mypy", "bun run typecheck", "export_issue58.py", "uv run pytest", )
@pytest.mark.parametrize(("workflow", "job_id", "command"), CI_MAKE_TARGETS)
def test_ci_runs_the_make_target_the_laptop_runs(workflow: str, job_id: str, command: str) -> None: runs = [str(spec.get("run", "")).strip() for spec in job(workflow, job_id).get("steps", [])]; assert command in runs, f"{workflow}:{job_id} does not run `{command}`; it runs {runs}"
def test_every_test_shard_and_the_web_tests_run_through_make() -> None:
    shard_jobs = [job_id for job_id, spec in load(PYTHON)["jobs"].items() if "pytest_shard.py" in str(spec)]; assert len(shard_jobs) >= 2, f"expected the core and data-engine shard jobs, found {shard_jobs}"
    for job_id in shard_jobs: text = "\n".join(spec_text(spec) for spec in job(PYTHON, job_id)["steps"]); assert 'make test-python FULL=1 FILES="${FILES[*]}"' in text, f"{PYTHON}:{job_id} runs its shard without make"
    web_tests = spec_text(job_step(WEB, "check", "Run tests (DB-backed tests armed)")); assert "make test-web FULL=1" in web_tests, web_tests
def test_no_ci_step_runs_a_check_that_the_makefile_owns() -> None:
    offenders = []
    for path in sorted((REPO_ROOT / ".github/workflows").glob("ci-*.yml")):
        for job_id, spec in load(path.name)["jobs"].items():
            for index, item in enumerate(spec.get("steps") or []):
                lines = [line.strip() for line in str(item.get("run", "")).splitlines()]
                for line in lines:
                    if line.startswith("#"):
                        continue
                    offenders += [f"{path.name}:{job_id}:{index}: {line}" for cmd in DIRECT_COMMANDS if cmd in line]
    assert not offenders, f"CI defines these checks a second time instead of calling make: {offenders}"
@pytest.mark.parametrize( "target", sorted({command.split()[1] for _, _, command in CI_MAKE_TARGETS} | {"test-python", "test-web"}) )
def test_each_ci_make_target_runs_the_whole_repository_with_full(target: str) -> None:
    env = {**os.environ, "DIFF_SCOPE_DRY_RUN": "1"}; env.pop("BASE", None); result = subprocess.run( ["make", "-s", target, "FULL=1"], cwd=REPO_ROOT, capture_output=True, text=True, env=env, check=False ); assert result.returncode == 0, result.stdout + result.stderr; assert "diff-scope: FULL=1, whole repository" in result.stdout, result.stdout; planned = [line for line in result.stdout.splitlines() if line.startswith("== [")]; assert planned, f"`make {target} FULL=1` planned no command: {result.stdout}"
    for line in planned:
        command = line.split("] ", 1)[1].split("  (in ", 1)[0].split()
        if command[:4] == ["uv", "run", "ruff", "check"]: missing = [p for p in command[4:] if not (REPO_ROOT / p).is_dir()]; assert not missing, f"`make {target} FULL=1` lints paths that do not exist: {missing} (the libs/ rot)"
def test_a_job_that_installs_a_playwright_browser_does_not_ride_the_moving_runner_label() -> None:
    installs = re.compile(r"playwright\s+(i|install)\b|--with-deps|apt(-get)?\s+install"); found = {}
    for path in sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml")):
        for job_id, spec in yaml.safe_load(path.read_text())["jobs"].items():
            if any(installs.search(str(item.get("run", ""))) for item in spec.get("steps", [])): found[f"{path.name}:{job_id}"] = spec.get("runs-on")
    assert set(found) == {"ci-web.yml:browser"}, found; assert {job: runner for job, runner in found.items() if runner != "ubuntu-24.04"} == {}, found
