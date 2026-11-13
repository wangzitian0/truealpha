"""Every assertion about the SHAPE of a workflow, in one place — #583.

Six test files each opened `.github/workflows/*.yml` and asserted a substring,
every one of them locating its region by splitting the file on a step name.
That is fragile in a demonstrated way: when the surface-walk credential check
moved into its own probe step, one such split silently selected a different
region and the assertion failed for a reason unrelated to its property. Its
neighbours had the same fragility; nobody noticed, because each file had
invented the technique separately.

These tests now resolve workflows, jobs and steps by name through
`tests/workflow_contract.py` — test infrastructure, beside the tests rather than
in the runtime package, since nothing the application or the deployers run reads
a workflow file. It raises when the thing a test is about no longer exists — a better failure than an assertion over the wrong
slice. The tool-behaviour tests stay in their own files; a test about a
workflow's shape does not belong beside one about a function's return value.
"""

from __future__ import annotations

import ast
import importlib.metadata
import importlib.util
import os
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from functools import cache
from pathlib import Path

import pytest
import yaml

# Loaded the way every test in this directory loads its subject — the tests are
# not a package, and pytest's importlib mode does not put this directory on the
# path.
_SPEC = importlib.util.spec_from_file_location(
    "truealpha_workflow_contract", Path(__file__).parent / "workflow_contract.py"
)
assert _SPEC is not None and _SPEC.loader is not None
_contract = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _contract
_SPEC.loader.exec_module(_contract)

WorkflowContractError = _contract.WorkflowContractError
glob_matches = _contract.glob_matches
job = _contract.job
job_step = _contract.job_step
load = _contract.load
paths_filters = _contract.paths_filters
selected_lanes = _contract.selected_lanes
source = _contract.source
step = _contract.step
steps = _contract.steps
step_text = _contract.step_text
spec_text = _contract.spec_text
triggers = _contract.triggers

REPO_ROOT = Path(__file__).resolve().parents[3]

REPROOF = "mutation-reproof.yml"
MAIN_HEALTH = "main-health.yml"
CLOSE_GUARD = "issue-close-guard.yml"
REQUIRED = "ci-required.yml"
PYTHON = "ci-python.yml"
DB = "ci-db.yml"
RUNTIME = "ci-runtime.yml"
WEB = "ci-web.yml"
IMAGES = "release-images.yml"
SHARD_REFRESH = "shard-weight-refresh.yml"


# --- the locator itself ------------------------------------------------------


def test_a_missing_step_names_what_it_was_looking_for() -> None:
    """The property that makes the rest of this file trustworthy: a renamed step
    fails loudly here instead of quietly re-pointing an assertion."""
    with pytest.raises(WorkflowContractError, match="has no step named"):
        step(REQUIRED, "A step nobody wrote")


# --- ci-required -------------------------------------------------------------


def test_manual_image_release_is_explicit_and_waits_for_required_jobs() -> None:
    dispatch = triggers(REQUIRED)["workflow_dispatch"]["inputs"]["force_images"]
    assert dispatch["description"] == "Publish all current-ref images after required checks."
    assert dispatch["type"] == "boolean" and dispatch["default"] is False

    release = job(REQUIRED, "images_release")
    condition = str(release["if"])
    assert "github.event_name == 'workflow_dispatch' &&" in condition
    assert "github.ref == 'refs/heads/main' &&" in condition
    assert "inputs.force_images" in condition
    assert "github.event_name == 'push'" in condition
    for dependency in ("security", "db", "python", "runtime", "web"):
        assert f"needs.{dependency}.result == 'success' || needs.{dependency}.result == 'skipped'" in condition, (
            f"images_release must wait for {dependency}"
        )
    assert release["with"]["publish"] is True
    for image in ("app_web", "llm_service", "data_engine"):
        assert release["with"][image] is True, (
            f"{image}: images_release must publish every image on main push and manual force"
        )

    text = source(REQUIRED)
    assert text.index("  images_release:\n") < text.index("\n  required:\n"), (
        "the required job must summarise images_release, so it comes after it"
    )


def test_images_build_beside_the_tests_and_publish_only_after_them() -> None:
    """#860: a main push measured 6.0 min because images_release `needs:` every test
    lane and the 89 s build + 60 s publish started only after the 2.6 min test wave. A
    build depends on nothing the tests prove, so images_build runs beside them on every
    event (it absorbed the PR-time images_check) and images_release keeps the wait, since
    only PUBLISHING must: a red test never publishes, and neither does a red build.

    - images_build needs `changes` alone and waits for no lane's result;
    - images_release still waits for every lane (asserted above) AND requires images_build
      to have succeeded -- strictly, not "or skipped", because its own trigger clause is a
      subset of images_build's, so a skipped build under a true trigger is a build that
      never happened, and nothing publishes what was not built at this SHA;
    - images_release passes `prebuilt`, and release-images honours it exactly once: the
      build job stands down, publish is let through past the skipped dependency with
      `!cancelled()`, and only a success or a vouched-for skip opens the door;
    - the two callers select the same images, so what publishes is what was built;
    - `required` summarises images_build, so a broken Dockerfile is red on main.
    """
    build = job(REQUIRED, "images_build")
    assert build["needs"] == "changes", "images_build must depend on the paths filter and nothing slower"
    assert "needs." not in str(build["if"]).replace("needs.changes.outputs", ""), (
        "images_build waits for a lane's result — the build is back on the critical path"
    )
    assert build["with"]["publish"] is False
    for event in ("pull_request", "merge_group", "push", "workflow_dispatch"):
        assert f"github.event_name == '{event}'" in str(build["if"]), f"images_build no longer runs on {event}"
    assert "prebuilt" not in build["with"], "the pre-build cannot itself be pre-built"

    release = job(REQUIRED, "images_release")
    assert "images_build" in release["needs"]
    assert "needs.images_build.result == 'success'" in str(release["if"]), (
        "images_release publishes without a successful build at this SHA"
    )
    assert "needs.images_build.result == 'skipped'" not in str(release["if"]), (
        "a skipped build under a true trigger is a build that never happened; it must not publish"
    )
    assert release["with"]["prebuilt"] is True
    for image in ("app_web", "llm_service", "data_engine"):
        assert release["with"][image] is True
        for clause in (
            f"needs.changes.outputs.image_{image} == 'true'",
            "github.event_name == 'push'",
            "inputs.force_images",
        ):
            assert clause in str(build["with"][image]), f"{image}: images_build must select image on {clause!r}"

    prebuilt = triggers(IMAGES)["workflow_call"]["inputs"]["prebuilt"]
    assert prebuilt["type"] == "boolean" and prebuilt["default"] is False and prebuilt["required"] is False
    assert "!inputs.prebuilt" in str(job(IMAGES, "build")["if"]), "the inner build runs a second time when pre-built"
    publish = job(IMAGES, "publish")
    condition = " ".join(str(publish["if"]).split())
    assert condition.startswith("!cancelled() &&"), (
        "publish needs a skipped build job; without !cancelled() it is skipped along with it"
    )
    assert "inputs.publish" in condition and "needs.plan.outputs.has_images == 'true'" in condition
    assert "needs.build.result == 'success'" in condition
    assert "(inputs.prebuilt && needs.build.result == 'skipped')" in condition, (
        "a skipped build opens publish only when the caller vouched for it"
    )
    assert "needs.build.result == 'failure'" not in condition and "always()" not in condition

    required = job(REQUIRED, "required")
    assert "images_build" in required["needs"] and "images_check" not in required["needs"], (
        "required must summarise images_build — a broken Dockerfile would otherwise merge green on main"
    )


def test_release_tags_trigger_ci_required_and_publish_semver_and_bare_sha_tags() -> None:
    """#1588: release tag pushes (`vX.Y.Z`) trigger ci-required, skip paths-filter so test lanes
    skip cleanly while images_build and images_release build and publish all three images with
    `:vX.Y.Z`, `:sha-<short>`, and bare `:<short>` (`sha[:7]`)."""
    push_trigger = triggers(REQUIRED)["push"]
    assert push_trigger["branches"] == ["main"]
    assert push_trigger["tags"] == ["v[0-9]+.[0-9]+.[0-9]+"]

    changes = job(REQUIRED, "changes")
    tag_guard = job_step(REQUIRED, "changes", "Verify release tag is on main")
    assert tag_guard.get("if") == "github.ref_type == 'tag'"
    assert "git fetch origin main" in str(tag_guard.get("run", ""))
    assert 'git merge-base --is-ancestor "$GITHUB_SHA" origin/main' in str(tag_guard.get("run", ""))
    assert "always()" not in str(job(REQUIRED, "images_build")["if"])
    assert "needs.changes.result == 'success'" in str(job(REQUIRED, "images_release")["if"])

    filter_step = next(s for s in changes["steps"] if s.get("id") == "filter")
    assert "github.ref_type != 'tag'" in str(filter_step.get("if", "")), (
        "paths-filter must skip on tag pushes so test lanes do not re-run against 0000000000000000000000000000000000000000"
    )

    publish_steps = job(IMAGES, "publish")["steps"]
    meta_step = next(s for s in publish_steps if s.get("id") == "meta")
    meta_tags = [line.strip() for line in str(meta_step["with"]["tags"]).splitlines() if line.strip()]
    assert "type=sha,format=short" in meta_tags
    assert "type=sha,prefix=,format=short" in meta_tags
    assert "type=semver,pattern=v{{version}}" in meta_tags


# --- escalation lifecycle: open on red, resolve on green (#876) ---------------

ESCALATE_TOOL = "tools/escalate_issue.py"


class _Context:
    """Attribute access over a dict with GitHub's null propagation: a missing
    property is null (falsy), never an error — `github.event.inputs.x` on a
    schedule event is null, and a condition must be judged on exactly that."""

    def __init__(self, value: object) -> None:
        self._value = value

    def __getattr__(self, name: str) -> object:
        value = self._value.get(name) if isinstance(self._value, dict) else None
        return _Context(value) if value is None or isinstance(value, dict) else value

    def __bool__(self) -> bool:
        return bool(self._value)


def _decides(
    condition: object,
    github: dict,
    *,
    succeeded: bool = True,
    needs: dict | None = None,
    inputs: dict | None = None,
) -> bool:
    """Evaluate a job/step `if:` the way the runner would, for the operators these
    watchdog conditions use: `&&`, `||`, `!`, `==`, `!=`, string literals and the
    status functions. Anything else is refused rather than guessed at. `needs` and
    `inputs` are the other two contexts a job gate reads (#1412).

    Asserting substrings of a condition proves the text; evaluating it proves the
    decision — a PR run, a branch dispatch or a loosened override must come out
    False however the expression is spelled. A missing `if:` is `true`, which is
    what the runner does with it.
    """
    expression = str(condition).strip() if condition is not None else "true"
    if expression.startswith("${{") and expression.endswith("}}"):
        expression = expression[3:-2].strip()
    expression = " ".join(expression.split())  # a folded `>-` condition spans lines
    functions = set(re.findall(r"([A-Za-z_]+)\(", expression))
    assert functions <= {"success", "failure", "always", "cancelled"}, f"unsupported functions {functions}"
    assert not re.search(r"!\s*\(", expression), "negating a group is not supported by this evaluator"
    # `!x` binds tighter than `==` in GitHub's grammar and looser in Python's;
    # parenthesising the operand keeps GitHub's meaning.
    python = re.sub(r"!(?!=)\s*([A-Za-z_][\w.]*)", r"(not \1)", expression)
    python = python.replace("&&", " and ").replace("||", " or ")
    namespace = {
        "github": _Context(github),
        "needs": _Context(needs or {}),
        "inputs": _Context(inputs or {}),
        "success": lambda: succeeded,
        "failure": lambda: not succeeded,
        "always": lambda: True,
        "cancelled": lambda: False,
        "true": True,
        "false": False,
        "null": None,
    }
    return bool(eval(python, {"__builtins__": {}}, namespace))  # repo-controlled workflow text


def _tool_calls(run: object, action: str) -> list[str]:
    return [
        line.strip()
        for line in str(run).splitlines()
        if f"{ESCALATE_TOOL} {action} " in line and not line.strip().startswith("#")
    ]


def _assert_calls_the_tool(spec: dict, action: str, where: str) -> None:
    """One invocation of the shared tool with the shared title, no second
    implementation beside it, and no `${{ }}` text spliced into the shell."""
    run = str(spec.get("run", ""))
    calls = _tool_calls(run, action)
    assert len(calls) == 1, f"{where}: expected one `{ESCALATE_TOOL} {action}` call, found {calls}"
    assert '--title "$ESCALATION_TITLE"' in calls[0] and "--body-file" in calls[0], (
        f"{where}: the {action} call does not use the shared title — the issue a red run opens would not be "
        f"the issue a green run closes"
    )
    assert "gh issue" not in run, f"{where}: an inline `gh issue` beside the tool is a second lifecycle implementation"
    assert "${{" not in run, f"{where}: an expression is interpolated into the shell; pass it through env"


def test_the_reproof_alert_closes_only_on_a_full_run_of_main() -> None:
    """#876 W3, mutation-reproof's half. The workflow also runs on pull_request,
    where it re-proves only what the PR touched on a branch: that run must never
    touch issues, and the `issues: write` grant must never reach its steps."""
    workflow = yaml.safe_load(source(REPROOF))
    assert workflow["env"]["ESCALATION_TITLE"] == "mutation-reproof is red: a declared guard is inert"
    assert workflow["permissions"] == {"contents": "read"}, "the workflow-level grant reaches the PR-triggered steps"
    assert "permissions" not in job(REPROOF, "reproof"), "the PR-triggered reproof job must not hold its own grant"

    escalate, resolve = job(REPROOF, "escalate"), job(REPROOF, "resolve")
    for spec, action in ((escalate, "open"), (resolve, "resolve")):
        assert spec["needs"] == "reproof"
        assert spec["permissions"] == {"contents": "read", "issues": "write"}, (
            f"the {action} job's grant is {spec['permissions']}"
        )
        writers = [item for item in spec["steps"] if ESCALATE_TOOL in str(item.get("run", ""))]
        assert len(writers) == 1, f"the {action} job has {len(writers)} steps calling the tool"
        _assert_calls_the_tool(writers[0], action, f"reproof {action}")

    main = "refs/heads/main"
    for github in ({"event_name": "schedule", "ref": main}, {"event_name": "workflow_dispatch", "ref": main}):
        assert _decides(resolve["if"], github), f"a green {github['event_name']} run on main no longer closes"
        assert not _decides(resolve["if"], github, succeeded=False)
        assert _decides(escalate["if"], github, succeeded=False)
    for github in (
        {"event_name": "pull_request", "ref": "refs/pull/1/merge"},
        # The event guard must hold on its own, not lean on the ref guard.
        {"event_name": "pull_request", "ref": main},
        {"event_name": "workflow_dispatch", "ref": "refs/heads/feature"},
    ):
        assert not _decides(resolve["if"], github), f"{github} can close the reproof alert"
        if github["event_name"] == "pull_request":
            assert not _decides(escalate["if"], github, succeeded=False), "a red PR run files an issue"


def test_a_red_main_push_files_an_issue_and_a_green_one_closes_it() -> None:
    """#876 W12: a red ci-required on main alerted nobody until the next release
    attempt (2026-09-16: #868 on top of #869, fixed by #873).

    workflow_run runs with a write token, so the shape is a security property as
    much as a behavioural one: only a completed ci-required run on main, only a
    push event (a fork PR from its own `main` matches the branch filter too),
    nothing from the triggering run spliced into a shell, and no grant beyond
    reading, reading runs, and writing issues.
    """
    on = triggers(MAIN_HEALTH)
    assert set(on) == {"workflow_run"}, f"main-health triggers on {sorted(on)}; it reports on ci-required only"
    required_name = yaml.safe_load(source(REQUIRED))["name"]
    assert on["workflow_run"] == {"workflows": [required_name], "types": ["completed"], "branches": ["main"]}, (
        f"main-health listens to {on['workflow_run']}, not to completed {required_name!r} runs on main"
    )

    workflow = yaml.safe_load(source(MAIN_HEALTH))
    assert workflow["permissions"] == {"contents": "read", "actions": "read", "issues": "write"}
    assert workflow["env"]["ESCALATION_TITLE"] == "main is red: ci-required failed on push"
    assert workflow["concurrency"]["cancel-in-progress"] is False, "an open and a close must not cancel each other"
    jobs = workflow["jobs"]
    assert list(jobs) == ["report"], f"main-health has jobs {sorted(jobs)}"
    report = jobs["report"]
    assert "permissions" not in report, "a job-level grant would replace the pinned workflow grant"

    # Every case carries head_branch "main": that is what the trigger's branch
    # filter already guaranteed, so the job guard must decide on the EVENT.
    for event in ("pull_request", "workflow_dispatch", "merge_group", "schedule"):
        github = {"event": {"workflow_run": {"event": event, "head_branch": "main"}}}
        assert not _decides(report.get("if"), github), (
            f"a {event} run of ci-required on a branch named main files or closes the red-main issue"
        )
    assert _decides(report.get("if"), {"event": {"workflow_run": {"event": "push", "head_branch": "main"}}})

    checkout = [item for item in report["steps"] if "actions/checkout" in str(item.get("uses", ""))]
    assert len(checkout) == 1 and "ref" not in (checkout[0].get("with") or {}), (
        "the privileged job must run the default branch's tool, never the triggering commit's"
    )
    assert checkout[0]["with"].get("persist-credentials") is False

    for item in report["steps"]:
        assert "${{" not in str(item.get("run", "")), (
            f"step {item.get('name')!r} interpolates an expression into its shell — commit text is attacker-shaped; "
            f"pass it through env"
        )
    event_env = {key: value for key, value in report["env"].items() if "github.event" in str(value)}
    assert {"HEAD_SHA", "COMMIT_MESSAGE", "RUN_URL"} <= set(event_env), (
        f"the body must name the head SHA, the commit and the run, through env; env carries {sorted(event_env)}"
    )

    open_step = step(MAIN_HEALTH, "File the red-main issue")
    resolve_step = step(MAIN_HEALTH, "Resolve the red-main issue")
    _assert_calls_the_tool(open_step, "open", "main-health open")
    _assert_calls_the_tool(resolve_step, "resolve", "main-health resolve")
    for conclusion in ("failure", "success", "cancelled", "skipped", "timed_out", "neutral"):
        github = {"event": {"workflow_run": {"event": "push", "head_branch": "main", "conclusion": conclusion}}}
        assert _decides(open_step["if"], github) is (conclusion == "failure"), f"open on {conclusion}"
        assert _decides(resolve_step["if"], github) is (conclusion == "success"), f"resolve on {conclusion}"


def test_the_condition_evaluator_keeps_githubs_precedence() -> None:
    """The evaluator above is what the three lifecycle tests trust, so its one
    translation hazard is pinned: `!x == 'y'` is `(!x) == 'y'` in GitHub."""
    assert _decides("!github.x == 'y'", {"x": ""}) is False
    assert _decides("!github.x", {}) is True
    assert _decides("github.a != 'b'", {"a": "c"}) is True
    assert _decides("${{ success() && github.a == 'b' }}", {"a": "b"}) is True
    assert _decides(None, {}) is True
    with pytest.raises(AssertionError, match="unsupported"):
        _decides("contains(github.a, 'b')", {})


# --- issue-close-guard -------------------------------------------------------


def test_the_close_guard_can_reopen_and_sees_full_history() -> None:
    """#562: two ways this ships inert — no `issues: write`, or a shallow
    checkout that makes every commit look unreleased."""
    workflow = source(CLOSE_GUARD)
    assert "issues: write" in workflow, "the guard cannot reopen anything without it"
    assert "fetch-depth: 0" in workflow and "fetch --tags" in workflow
    assert triggers(CLOSE_GUARD)["issues"]["types"] == ["closed"]


# --- abolished vision-close-check and vision-close-reopen (ADR A8) -----------


def test_abolished_vision_close_workflows_and_guard_remain_deleted() -> None:
    """ADR A8 Decision 1: mechanical text policing and regex bot reopening are abolished.
    These files must remain deleted from the repository.
    """
    for rel_path in (
        ".github/workflows/vision-close-check.yml",
        ".github/workflows/vision-close-reopen.yml",
        "tools/vision_close_guard.py",
        "common/runtime/tests/test_vision_close_guard.py",
    ):
        target = REPO_ROOT / rel_path
        assert not target.exists(), f"{rel_path} was abolished by ADR A8 and must not be restored"


# --- #1588: legacy CD/SRE workflows and runtime-hooks.manifest.json ----------


def test_legacy_cd_and_sre_workflows_and_scripts_remain_deleted() -> None:
    """#1588: CD, surface walk, delivery report, and scheduler liveness moved to infra2.
    The legacy workflows and scripts must remain deleted from the repository.
    """
    for rel_path in (
        ".github/workflows/deploy-release.yml",
        ".github/workflows/auto-release-staging.yml",
        ".github/workflows/walk-release.yml",
        ".github/workflows/delivery-report.yml",
        ".github/workflows/scheduler-liveness.yml",
        ".github/workflows/nightly-dagster-liveness.yml",
        "tools/cut_release.sh",
        "tools/owner_approval_gate.sh",
        "tools/production_evidence_policy.json",
        "tools/auto_release.py",
        "tools/staging_evidence.py",
        "tools/delivery_report.py",
        "tools/walk_evidence.py",
        "tools/app_deploy_request.py",
        "tools/app_deploy_transport.py",
        "tools/scheduler_liveness.py",
        "tools/verified_sha_images.py",
        "common/runtime/src/truealpha_tools/auto_release.py",
        "common/runtime/src/truealpha_tools/staging_evidence.py",
        "common/runtime/src/truealpha_tools/delivery_report.py",
        "common/runtime/src/truealpha_tools/walk_evidence.py",
        "common/runtime/src/truealpha_tools/release_runs.py",
        "common/runtime/src/truealpha_tools/app_deploy_request.py",
        "common/runtime/src/truealpha_tools/app_deploy_transport.py",
        "common/runtime/src/truealpha_tools/scheduler_liveness.py",
        "common/runtime/src/truealpha_tools/verified_sha_images.py",
    ):
        target = REPO_ROOT / rel_path
        assert not target.exists(), f"{rel_path} was removed in #1588 and must not be restored"


def test_runtime_hooks_manifest_declares_the_five_v2_verification_slots() -> None:
    """#1588, #1675: runtime-hooks.manifest.json v2 is the single contract for infra2 release-train hooks."""
    from infra2_sdk.manifests import HOOK_SLOTS, load_hooks_manifest

    manifest_path = REPO_ROOT / "runtime-hooks.manifest.json"
    assert manifest_path.is_file(), "runtime-hooks.manifest.json must exist at repository root"
    hooks = load_hooks_manifest(manifest_path)
    assert hooks.schema_version == 2
    assert hooks.service == "truealpha/app"
    assert set(hooks.slots) == set(HOOK_SLOTS)

    assert hooks.slots["scheduled_audit"].checkout_lineage is True
    for non_lineage_slot in ("smoke", "preview_gate", "staging_gate", "prod_gate"):
        assert hooks.slots[non_lineage_slot].checkout_lineage is False

    assert hooks.slots["smoke"].bootstrap == ""
    assert "tools/health_check.py" in hooks.slots["smoke"].run
    assert hooks.slots["prod_gate"].bootstrap == ""
    assert "tools/surface_contract.py" in hooks.slots["prod_gate"].run

    for slot_name, spec in hooks.slots.items():
        assert "&&" not in spec.run, f"{slot_name} must list its steps in checks, not chain them with &&"
    assert [c.id for c in hooks.slots["scheduled_audit"].checks] == ["nightly_verdicts", "surface_contract"]

    for gate_slot in ("preview_gate", "staging_gate"):
        spec = hooks.slots[gate_slot]
        assert [c.id for c in spec.checks] == ["health", "warm_surface", "walk_tree", "surface_contract"]
        assert set(spec.required_env) == {
            "APP_URL",
            "APP_VERSION_REF",
            "TA_EMAIL",
            "TA_PASSWORD",
            "TA_MEMBER_EMAIL",
            "TA_MCP_TOKEN",
        }


# --- shard-weight refresh (#1503) ---------------------------------------------

REFRESH_PAT_GUARD = "Require a credential that can push a branch and open a pull request"
REFRESH_HARVEST = "Harvest the newest green main run that carries durations"
REFRESH_DECIDE = "Decide whether one refresh pull request is worth opening"
REFRESH_OPEN = "Open one refresh pull request"


def test_shard_weight_refresh_has_a_schedule_and_a_manual_replay_and_no_merge_trigger() -> None:
    """Every background job needs two triggers. A pull_request or push trigger would make it a gate."""
    on = triggers(SHARD_REFRESH)
    assert set(on) == {"schedule", "workflow_dispatch"}, f"shard-weight-refresh triggers on {sorted(on)}"
    assert on["schedule"] == [{"cron": "23 4 * * 0"}]
    assert job(SHARD_REFRESH, "refresh")["if"] == "github.ref == 'refs/heads/main'", (
        "a dispatch on another branch would open a pull request from that branch's tool"
    )


def test_shard_weight_refresh_has_read_grants_only_and_pushes_with_the_pat() -> None:
    """The default token cannot create pull requests here, and its pushes start no run.
    The PAT does both. Only the guard, the checkout and the opening step may hold it."""
    workflow = yaml.safe_load(source(SHARD_REFRESH))
    assert workflow["permissions"] == {"contents": "read", "actions": "read", "pull-requests": "read"}
    assert "permissions" not in workflow["jobs"]["refresh"], "a job-level grant would replace the workflow grant"

    refresh_steps = job(SHARD_REFRESH, "refresh")["steps"]
    names = [spec.get("name") for spec in refresh_steps]
    checkout_index = next(
        index for index, spec in enumerate(refresh_steps) if str(spec.get("uses", "")).startswith("actions/checkout")
    )
    assert names.index(REFRESH_PAT_GUARD) < checkout_index, "the guard must run before checkout"
    guard = step(SHARD_REFRESH, REFRESH_PAT_GUARD)
    assert guard["env"] == {"INFRA2_PAT": "${{ secrets.INFRA2_PAT }}"}
    assert "exit 1" in str(guard["run"])
    assert refresh_steps[checkout_index]["with"]["token"] == "${{ secrets.INFRA2_PAT }}"
    assert refresh_steps[checkout_index]["with"]["ref"] == "main"

    holders = [spec.get("name") or spec.get("uses") for spec in refresh_steps if "INFRA2_PAT" in spec_text(spec)]
    assert holders == [REFRESH_PAT_GUARD, "actions/checkout@v4", REFRESH_OPEN], holders
    assert step(SHARD_REFRESH, REFRESH_OPEN)["env"]["GH_TOKEN"] == "${{ secrets.INFRA2_PAT }}"
    assert step(SHARD_REFRESH, REFRESH_HARVEST)["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert "env" not in step(SHARD_REFRESH, REFRESH_DECIDE), "the decision step calls no gh and holds no token"
    for spec in refresh_steps:
        assert "${{" not in str(spec.get("run", "")), f"{spec.get('name')} interpolates an expression into the shell"


def test_shard_weight_refresh_opens_at_most_one_pull_request_and_only_when_the_tool_says_so() -> None:
    decide = step(SHARD_REFRESH, REFRESH_DECIDE)
    run = str(decide["run"])
    assert "tools/shard_weight_refresh.py" in run and "--open-prs" not in run, "the open-PR veto is back"
    opener = step(SHARD_REFRESH, REFRESH_OPEN)
    assert opener["if"] == "steps.decide.outputs.open_pr == 'true'"
    script = str(opener["run"])
    assert 'git push --force origin "$REFRESH_BRANCH"' in script
    assert 'gh pr list --repo "$GITHUB_REPOSITORY" --head "$REFRESH_BRANCH" --state open' in script
    assert script.index("gh pr list") < script.index("gh pr create") < script.index("gh pr merge"), (
        "the open-PR check must guard the create, and auto-merge must follow both"
    )
    assert 'if [ "$open_prs" = "0" ]; then' in script, "gh pr create must run only when no pull request is open"
    assert "gh pr merge" in script and "--auto" in script, "the pull request must enable auto-merge"
    assert "git add tools/pytest_shard_weights.json" in script, "the pull request holds the weights file only"
    assert "Refs #1503" in script and "Closes #1503" not in script
    assert yaml.safe_load(source(SHARD_REFRESH))["env"]["REFRESH_BRANCH"] == "ci/refresh-shard-weights"


def test_shard_weight_refresh_tries_the_next_run_when_one_run_has_no_durations() -> None:
    """A green main run can skip the Python lanes. The harvest tool fails closed on its log."""
    run = str(step(SHARD_REFRESH, REFRESH_HARVEST)["run"])
    assert "--event push --status success" in run and "--limit 10" in run
    assert "for run_id in $run_ids; do" in run
    assert "tools/harvest_shard_weights.py" in run
    assert run.count("exit 1") == 2, "no green run and no run with durations must each fail the job"


# The next tests run the step's own script text against stub `gh` and harvest programs. A workflow
# edit that loosens the step changes what these tests execute.

_STUB_GH = """#!/bin/bash
echo "$*" >> "$STUB_LOG"
case "$1 $2" in
  "run list")
    case "$*" in
      *"--workflow ci-required.yml"*"--branch main"*"--event push"*"--status success"*) printf '%s' "$STUB_RUN_IDS" ;;
      *) echo "unexpected run list arguments: $*" >&2; exit 9 ;;
    esac ;;
  "pr list") printf '%s' "${STUB_OPEN_PRS:-0}" ;;
  "pr create") ;;
  "pr merge") ;;
  *) echo "unexpected gh call: $*" >&2; exit 9 ;;
esac
"""

_STUB_HARVEST = """import os
import sys
from pathlib import Path

run_id = sys.argv[1]
out = sys.argv[sys.argv.index("--out") + 1]
with open(os.environ["STUB_LOG"], "a") as log:
    log.write(f"harvest {run_id}\\n")
if run_id in os.environ.get("STUB_BAD_RUNS", "").split():
    print(f"harvest: run {run_id} yielded no durations", file=sys.stderr)
    raise SystemExit(1)
Path(out).write_text("{}")
"""


def _stub_environment(tmp_path: Path) -> dict[str, str]:
    """An environment whose `gh` is a recorder. `git` is the real one with no user configuration."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(_STUB_GH, encoding="utf-8")
    gh.chmod(0o755)
    (tmp_path / "runner").mkdir(exist_ok=True)
    return {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "RUNNER_TEMP": str(tmp_path / "runner"),
        "GITHUB_OUTPUT": str(tmp_path / "github_output"),
        "GH_TOKEN": "unused",
        "STUB_LOG": str(tmp_path / "stub.log"),
    }


def _run_refresh_harvest_step(tmp_path: Path, run_ids: str, bad_runs: str) -> subprocess.CompletedProcess[str]:
    (tmp_path / "tools").mkdir(exist_ok=True)
    (tmp_path / "tools" / "harvest_shard_weights.py").write_text(_STUB_HARVEST, encoding="utf-8")
    environment = _stub_environment(tmp_path) | {"STUB_RUN_IDS": run_ids, "STUB_BAD_RUNS": bad_runs}
    return subprocess.run(
        ["bash", "-c", str(step(SHARD_REFRESH, REFRESH_HARVEST)["run"])],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        env=environment,
    )


def test_the_refresh_harvest_takes_the_newest_run_that_carries_durations(tmp_path: Path) -> None:
    result = _run_refresh_harvest_step(tmp_path, run_ids="13\n12\n11", bad_runs="13 12")
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert (tmp_path / "github_output").read_text(encoding="utf-8") == "run_id=11\n"
    assert (tmp_path / "stub.log").read_text(encoding="utf-8").splitlines()[1:] == [
        "harvest 13",
        "harvest 12",
        "harvest 11",
    ], "the step must try the runs from the newest to the oldest"


def test_the_refresh_harvest_stops_at_the_first_run_that_works(tmp_path: Path) -> None:
    result = _run_refresh_harvest_step(tmp_path, run_ids="13\n12\n11", bad_runs="")
    assert result.returncode == 0
    assert (tmp_path / "github_output").read_text(encoding="utf-8") == "run_id=13\n"
    assert "harvest 12" not in (tmp_path / "stub.log").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("run_ids", "bad_runs", "message"),
    [
        ("13\n12", "13 12", "none of the newest 10 green main runs carries pytest durations"),
        ("", "", "no green ci-required run on main"),
    ],
)
def test_the_refresh_harvest_fails_the_job_instead_of_reporting_nothing_to_do(
    tmp_path: Path, run_ids: str, bad_runs: str, message: str
) -> None:
    result = _run_refresh_harvest_step(tmp_path, run_ids=run_ids, bad_runs=bad_runs)
    assert result.returncode != 0
    assert message in result.stdout
    assert not (tmp_path / "github_output").exists(), "a failed harvest must publish no run id"


def _repo_with_a_weights_change(tmp_path: Path) -> tuple[Path, Path]:
    """A bare `origin`, and a checkout of its `main` whose weights file the tool has rewritten."""
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    environment = _stub_environment(tmp_path)
    identity = ["-c", "user.name=probe", "-c", "user.email=probe@example.invalid"]

    def git(cwd: Path, *arguments: str) -> None:
        subprocess.run(["git", *identity, *arguments], cwd=cwd, check=True, capture_output=True, env=environment)

    origin.mkdir()
    git(origin, "init", "-q", "--bare", "-b", "main")
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    weights = work / "tools" / "pytest_shard_weights.json"
    weights.parent.mkdir()
    weights.write_text('{"seconds_by_file": {}}\n', encoding="utf-8")
    git(work, "add", ".")
    git(work, "commit", "-q", "-m", "base")
    git(work, "remote", "add", "origin", str(origin))
    git(work, "push", "-q", "origin", "main")
    weights.write_text('{"seconds_by_file": {"tests/test_new.py": 3.0}}\n', encoding="utf-8")
    (work / "scratch.txt").write_text("not part of the refresh\n", encoding="utf-8")
    return origin, work


def _run_refresh_open_step(tmp_path: Path, work: Path, open_prs: int = 0) -> subprocess.CompletedProcess[str]:
    environment = _stub_environment(tmp_path) | {
        "STUB_OPEN_PRS": str(open_prs),
        "REFRESH_BRANCH": yaml.safe_load(source(SHARD_REFRESH))["env"]["REFRESH_BRANCH"],
        "HARVEST_RUN_ID": "77",
        "REASON": "the harvest gives weights to 1 test files that lack one",
        "JOB_URL": "https://example.invalid/runs/1",
        "GITHUB_REPOSITORY": "owner/repo",
    }
    return subprocess.run(
        ["bash", "-c", str(step(SHARD_REFRESH, REFRESH_OPEN)["run"])],
        cwd=work,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        env=environment,
    )


def _origin_git(tmp_path: Path, origin: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=origin,
        check=True,
        capture_output=True,
        text=True,
        env=_stub_environment(tmp_path),
    ).stdout.strip()


def test_the_refresh_pushes_one_commit_of_the_weights_file_and_opens_the_pull_request(tmp_path: Path) -> None:
    origin, work = _repo_with_a_weights_change(tmp_path)
    result = _run_refresh_open_step(tmp_path, work)
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"

    title = "[truealpha-ci] Refresh the shard weights from run 77 (#1503)"
    assert (
        _origin_git(tmp_path, origin, "log", "main..ci/refresh-shard-weights", "--format=%s%n%an")
        == f"{title}\ngithub-actions[bot]"
    )
    changed = _origin_git(tmp_path, origin, "diff", "--name-only", "main", "ci/refresh-shard-weights")
    assert changed == "tools/pytest_shard_weights.json", "the commit must hold the weights file and nothing else"

    gh_calls = (tmp_path / "stub.log").read_text(encoding="utf-8").splitlines()
    assert gh_calls == [
        "pr list --repo owner/repo --head ci/refresh-shard-weights --state open --json number --jq length",
        f"pr create --repo owner/repo --base main --head ci/refresh-shard-weights --title {title} --body-file {tmp_path}/runner/pr_body.md",
        "pr merge ci/refresh-shard-weights --repo owner/repo --auto --squash",
    ]
    body = (tmp_path / "runner" / "pr_body.md").read_text(encoding="utf-8")
    assert "Decision: the harvest gives weights to 1 test files that lack one." in body
    assert "Refs #1503" in body and "Closes" not in body


def test_the_refresh_pushes_to_an_open_pull_request_without_opening_a_second_one(tmp_path: Path) -> None:
    """An open refresh pull request takes the new head. No second pull request is created."""
    origin, work = _repo_with_a_weights_change(tmp_path)
    result = _run_refresh_open_step(tmp_path, work, open_prs=1)
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    gh_calls = (tmp_path / "stub.log").read_text(encoding="utf-8").splitlines()
    assert [call.split(" --")[0] for call in gh_calls] == ["pr list", "pr merge ci/refresh-shard-weights"]
    changed = _origin_git(tmp_path, origin, "diff", "--name-only", "main", "ci/refresh-shard-weights")
    assert changed == "tools/pytest_shard_weights.json"


def test_no_document_restates_the_pr_ci_budget() -> None:
    """The PR CI budget lives in the Workspace tier only. A document that restates it drifts."""
    restated = re.compile(r"PR CI\W{0,40}(?:≤|<=)\s*3\s*min")
    holders = [
        path
        for path in _tracked_files()
        if path.endswith(".md") and restated.search((REPO_ROOT / path).read_text(encoding="utf-8"))
    ]
    assert holders == [], f"these documents restate the PR CI budget; name the Workspace tier instead: {holders}"


def test_the_refresh_replaces_a_stale_branch_left_by_an_earlier_pull_request(tmp_path: Path) -> None:
    """A merged or closed refresh leaves its branch on origin. The next push must replace it."""
    origin, work = _repo_with_a_weights_change(tmp_path)
    identity = ["-c", "user.name=probe", "-c", "user.email=probe@example.invalid"]
    environment = _stub_environment(tmp_path)
    for arguments in (
        ["switch", "-q", "-c", "ci/refresh-shard-weights"],
        ["commit", "-q", "--allow-empty", "-m", "stale refresh"],
        ["push", "-q", "origin", "ci/refresh-shard-weights"],
        ["switch", "-q", "main"],
        ["branch", "-q", "-D", "ci/refresh-shard-weights"],
    ):
        subprocess.run(["git", *identity, *arguments], cwd=work, check=True, capture_output=True, env=environment)

    result = _run_refresh_open_step(tmp_path, work)
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    subjects = _origin_git(tmp_path, origin, "log", "ci/refresh-shard-weights", "--format=%s")
    assert subjects.splitlines()[0].startswith("[truealpha-ci] Refresh the shard weights")
    assert "stale refresh" not in subjects


# --- one migration applier (#984) --------------------------------------------

#: (workflow, job, step). Every CI job that needs a schema applies it by running
#: `db/apply_migrations.sh`, the single implementation of "apply db/migrations/*.sql in
#: glob order, then db/roles.sql, stopping on the first error". These eight steps used to
#: be three hand-written copies of that loop, and they had drifted from the one the
#: llm-service image boots with: no lock bound, no statement timeout, no
#: MIGRATIONS_DATABASE_URL. `common/runtime/tests/test_migration_applier.py` holds the rest
#: of the tree to the same rule; this file holds the workflows, because workflow-shape
#: assertions live here and resolve steps by name (#583).
MIGRATION_CHAIN_STEPS = (
    (PYTHON, "test-core", "Apply the migration chain"),
    (PYTHON, "test-data-engine", "Apply the migration chain"),
    (PYTHON, "dagster-code-server-liveness", "Apply the migration chain"),
    (DB, "migrate", "Apply the migration chain"),
    (DB, "migrate", "Re-apply the migration chain (idempotency)"),
    (DB, "migrate", "Re-apply the migration chain over real rows (replay-with-data)"),
    (WEB, "check", "Apply the migration chain (populated and empty)"),
    (WEB, "browser", "Apply the migration chain (populated and empty)"),
)


@pytest.mark.parametrize(("workflow", "job_id", "name"), MIGRATION_CHAIN_STEPS)
def test_every_ci_job_applies_the_chain_through_the_one_applier(workflow: str, job_id: str, name: str) -> None:
    assert "db/apply_migrations.sh" in job_step(workflow, job_id, name).get("run", "")


def test_no_ci_job_reaches_a_schema_by_any_other_route() -> None:
    """The half a list of known steps cannot cover: a NEW step that migrates a database
    its own way. Any step that runs psql against a migration file is one of the eight
    above or it is a ninth copy.

    ci-runtime is in scope with no step of its own on purpose: it brings the database up
    through docker compose on a fresh volume, so the postgres image's initdb hook runs
    db/docker-init.sh — the applier again. A migration step appearing there would mean
    that path had stopped working and someone had patched around it."""
    strays = []
    for workflow in (PYTHON, DB, WEB, RUNTIME):
        for job_id, spec in yaml.safe_load(source(workflow))["jobs"].items():
            for candidate in spec.get("steps", []):
                run = str(candidate.get("run", ""))
                if "db/migrations" not in run and "db/roles.sql" not in run:
                    continue
                if "db/apply_migrations.sh" in run:
                    continue
                strays.append(f"{workflow}:{job_id}:{candidate.get('name')}")
    assert not strays, (
        f"{strays} reach the migration files without going through db/apply_migrations.sh — "
        f"that is how the seven appliers diverged (#984)"
    )


# --- the boundary this file exists to hold -----------------------------------


def test_no_other_test_reads_a_workflow_directly() -> None:
    """The reason all of the above are here. Six files had grown their own
    conventions for locating a step; a seventh would grow a seventh."""
    offenders = []
    for path in sorted((REPO_ROOT / "common/runtime/tests").glob("test_*.py")):
        if path.name == Path(__file__).name:
            continue
        if ".github/workflows" in path.read_text(encoding="utf-8"):
            offenders.append(path.name)
    assert not offenders, (
        f"{offenders} read a workflow directly. Workflow-shape assertions live in "
        f"{Path(__file__).name} and resolve steps through tests/workflow_contract.py "
        f"(#583)"
    )


def test_no_test_bootstraps_a_tools_script_by_hand() -> None:
    """The second copy-paste the same six files carried.

    Deleting nine copies is a run that happened; this is the check that runs
    again (rule 7). Without it a tenth copy lands silently — verified by adding
    one, which the scan above did not notice because it looks for a different
    string.

    Scoped to `tools/` on purpose. This file's own `spec_from_file_location`
    loads a tests-directory sibling, which is a different problem with a
    different right answer, so forbidding the call outright would push a
    correct use into an exemption.

    Parsed rather than grepped, because the first version matched "tools"
    anywhere in the file and the first thing it flagged was this test — the
    word appears in the failure message three lines down. A scanner that reads
    prose as code is the defect `source-contracts.test.ts` already learned once.

    Arguments are resolved through the file's assignments, because the second
    version read only the call's own text and every one of the nine copies it
    was written to prevent would have walked past it (review): they all bound
    `MODULE_PATH = REPO_ROOT / "tools/<name>.py"` first and passed the NAME.
    The red case used an inline path, so it proved the scanner ran, not that it
    covered the pattern — a guard tested only against a shape nobody writes.
    """
    offenders = []
    for path in sorted((REPO_ROOT / "common/runtime/tests").glob("test_*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        bound = {
            target.id: ast.get_source_segment(source, node.value) or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            if name != "spec_from_file_location":
                continue
            reachable = [ast.get_source_segment(source, node) or ""]
            reachable += [bound.get(argument.id, "") for argument in ast.walk(node) if isinstance(argument, ast.Name)]
            if any("tools" in text for text in reachable):
                offenders.append(path.name)
    assert not offenders, (
        f"{offenders} bootstrap a tools/ script by hand. Use "
        f"`truealpha_runtime.testing.load_tool(name)` — it registers the module in "
        f"sys.modules before exec, which a hand copy forgets and a dataclass in the "
        f"tool then fails on (#583)"
    )


def test_every_workflow_installs_what_the_tools_it_runs_import() -> None:
    """A workflow that runs a tool must install what that tool imports — #616.

    `deploy-release.yml` installed with `--no-install-workspace` and then ran
    `tools/health_check.py`, which imports `truealpha_runtime` — the workspace
    package #585 moved the shared release read into. The step died at IMPORT
    time on the v0.0.22 production deploy, before it read or compared anything,
    and because the job stops at the first failure the surface walk was skipped
    too. So the release went out with no walk evidence, and the gate that exists
    to confirm production is serving the release never reached the question.

    Nothing above caught it: the assertions in this file check the shape of the
    command, never that the command's interpreter can load its own module. #526
    was "the gate compared the wrong thing"; this was "the gate never got as far
    as comparing".
    """
    workspace_modules = {
        member.rsplit("/", 1)[-1].replace("-", "_")
        for member in ("apps/data-engine", "apps/llm-service", "common/contracts", "common/factors", "common/runtime")
    } | {"truealpha_runtime", "truealpha_tools", "truealpha_contracts", "data_engine", "factors"}

    tools_src = REPO_ROOT / "common/runtime/src/truealpha_tools"

    def imports(script: Path, seen: set[Path] | None = None) -> set[str]:
        seen = set() if seen is None else seen
        seen.add(script)
        tree = ast.parse(script.read_text(encoding="utf-8"))
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    names.add(top)
                    if top == "truealpha_tools" and "." in alias.name:
                        sub = tools_src / f"{alias.name.split('.')[1]}.py"
                        if sub.is_file() and sub not in seen:
                            names |= imports(sub, seen)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                top = node.module.split(".")[0]
                names.add(top)
                if top == "truealpha_tools":
                    parts = node.module.split(".")
                    candidates = [parts[1]] if len(parts) > 1 else [alias.name for alias in node.names]
                    for cand in candidates:
                        sub = tools_src / f"{cand}.py"
                        if sub.is_file() and sub not in seen:
                            names |= imports(sub, seen)
        return names

    offenders = []
    for workflow in sorted((REPO_ROOT / ".github/workflows").glob("*.yml")):
        text = workflow.read_text(encoding="utf-8")
        scripts = {name for name in re.findall(r"python (tools/[a-z_]+\.py)", text)}
        if not scripts:
            continue
        needs_workspace = {script for script in scripts if imports(REPO_ROOT / script) & workspace_modules}
        if not needs_workspace:
            continue
        installs = re.findall(r"uv sync[^\n]*", text)
        assert installs, f"{workflow.name} runs {sorted(scripts)} without any `uv sync`"
        # A line counts as installing the workspace only when it excludes
        # neither way. The first version tested `--no-install-workspace` alone,
        # and the incident it was written from names `--only-group dev` as the
        # ACTUAL blocker — measured: `uv sync --frozen --only-group dev` leaves
        # truealpha_runtime unimportable. So the guard covered a spelling of the
        # defect rather than the defect, which is the third time that shape has
        # shown up this week (review).
        installs_workspace = [
            line for line in installs if "--no-install-workspace" not in line and "--only-group" not in line
        ]
        if not installs_workspace:
            offenders.append((workflow.name, sorted(needs_workspace), installs))

    assert not offenders, (
        f"these workflows never install the workspace and then run tools that import it: "
        f"{offenders}. The script dies at import, before it can judge anything, and every "
        f"later step in the job is skipped (#616)"
    )


#: `python3 tools/<name>.py` or `python tools/<name>.py`: the shell finds the interpreter through PATH.
_BARE_TOOL_CALL = re.compile(r"(?<![\w./-])python(?:3(?:\.\d+)?)?(?:\s+-\w+)*\s+tools/(\w+)\.py\b")
#: `uv run` starts the call when no shell separator stands between `uv run` and the call.
_STARTED_BY_UV_RUN = re.compile(r"\buv\s+run\b[^;&|()`]*$")
_IMPORT_ERRORS = {"ImportError", "ModuleNotFoundError", "Exception", "BaseException"}


def _bare_tool_calls(run: str) -> list[str]:
    """The names of the tools that a `run` script starts with the PATH interpreter."""
    calls = []
    for line in run.replace("\\\n", " ").splitlines():
        if line.lstrip().startswith("#"):
            continue
        for match in _BARE_TOOL_CALL.finditer(line):
            if not _STARTED_BY_UV_RUN.search(line[: match.start()]):
                calls.append(match.group(1))
    return calls


def _module_level_imports(tool: str, seen: set[str] | None = None) -> set[str]:
    """The non-stdlib top-level modules that `tools/<tool>.py` imports when it starts.

    The walk reads the code that runs at start: top-level statements and the `__main__` block.
    It skips function bodies and `if TYPE_CHECKING:` bodies, because that code does not run at start.
    It skips a `try` body whose handler catches an import failure, because that import is optional.
    A module in `tools/` or `common/runtime/src/truealpha_tools/` is not an external install.
    The walk follows it into its own imports.
    """
    seen = set() if seen is None else seen
    seen.add(tool)
    tools_dir = REPO_ROOT / "tools"
    tools_src = REPO_ROOT / "common/runtime/src/truealpha_tools"
    names: set[str] = set()
    subtools: set[str] = set()

    def visit(body: list[ast.stmt]) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    if top == "truealpha_tools" and "." in alias.name:
                        subtools.add(alias.name.split(".")[1])
                    else:
                        names.add(top)
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    top = node.module.split(".")[0]
                    if top == "truealpha_tools":
                        parts = node.module.split(".")
                        if len(parts) > 1:
                            subtools.add(parts[1])
                        else:
                            subtools.update(alias.name for alias in node.names)
                    else:
                        names.add(top)
            elif isinstance(node, ast.If) and "TYPE_CHECKING" in {
                getattr(node.test, "id", None),
                getattr(node.test, "attr", None),
            }:
                visit(node.orelse)
            elif isinstance(node, (ast.Try, ast.TryStar)):
                caught = {
                    name
                    for handler in node.handlers
                    for name in (re.findall(r"\w+", ast.unparse(handler.type)) if handler.type else ["BaseException"])
                }
                if not caught & _IMPORT_ERRORS:
                    visit(node.body)
                for handler in node.handlers:
                    visit(handler.body)
                visit(node.orelse)
                visit(node.finalbody)
            else:
                for field in ("body", "orelse", "finalbody"):
                    child = getattr(node, field, None)
                    if isinstance(child, list):
                        visit(child)

    source_file = tools_dir / f"{tool}.py"
    if not source_file.is_file():
        source_file = tools_src / f"{tool}.py"
    visit(ast.parse(source_file.read_text(encoding="utf-8")).body)
    needed: set[str] = set()
    # A worklist, not `for sub in subtools`: a visited module can import another
    # truealpha_tools module, which adds to `subtools` while the walk reads it (#1412:
    # reachability_ratchet -> import_graph raised "Set changed size during iteration").
    walked: set[str] = set()
    while pending := sorted(subtools - walked):
        for sub in pending:
            walked.add(sub)
            if (tools_src / f"{sub}.py").is_file() and f"truealpha_tools.{sub}" not in seen:
                seen.add(f"truealpha_tools.{sub}")
                visit(ast.parse((tools_src / f"{sub}.py").read_text(encoding="utf-8")).body)
    for name in names:
        if (tools_dir / f"{name}.py").is_file() or (tools_dir / name / "__init__.py").is_file():
            if name not in seen:
                needed |= _module_level_imports(name, seen)
        elif name not in sys.stdlib_module_names:
            needed.add(name)
    return needed


def _installs_for_the_path_interpreter(earlier: list[dict]) -> bool:
    """True when an earlier step puts packages where a bare `python3` finds them.

    Two forms count. One: setup-uv, then `uv sync`, and a `.venv/bin` line into `$GITHUB_PATH`.
    Two: `pip install` (not `uv pip install`), which installs into the interpreter that PATH finds.
    `uv sync` alone does not count. It fills `.venv`, and a bare `python3` does not read `.venv`.
    """
    setup_uv = [
        index
        for index, spec in enumerate(earlier)
        if re.fullmatch(r"\./\.github/actions/setup-uv|astral-sh/setup-uv@\S+", str(spec.get("uses", "")))
    ]
    runs = [str(spec.get("run", "")) for spec in earlier]
    synced = bool(setup_uv) and any(re.search(r"\buv\s+sync\b", run) for run in runs[setup_uv[0] + 1 :])
    on_path = any(re.search(r"\.venv/bin\"?\s*>>\s*\"?\$\{?GITHUB_PATH\b", run) for run in runs)
    pip = any(re.search(r"(?<!uv )\bpip3?\s+install\b|\buv\s+pip\s+install\b[^\n]*--system", run) for run in runs)
    return (synced and on_path) or pip


def test_a_bare_python_runs_a_tool_only_after_its_job_installs_what_the_tool_imports() -> None:
    """A tool that a bare `python3` runs must find its imports on PATH — #1369.

    #1357 made `tools/verified_sha_images.py` import `infra2_sdk` at module level.
    The `plan` job of release-images.yml ran that tool after a checkout and nothing else.
    So every tag run failed with `ModuleNotFoundError: No module named 'infra2_sdk'`.
    Only a tag sets `reuse_verified_sha`, so no pull request and no main push ran the tool.
    CI stayed green, and each automatic release deleted its own tag.

    This is the general form of the check, not the narrow `infra2_sdk` form.
    It reads every step of every workflow, `workflow_call` files too.
    A bare call is `python3 tools/<name>.py` or `python tools/<name>.py` that no `uv run` starts.
    `ast` gives the imports that run at start, and `sys.stdlib_module_names` removes the stdlib.
    The general form was exact on the tree of #1369: of 20 bare calls, it flagged only the one that failed.
    For each such import, an earlier step of the same job must install for the PATH interpreter.
    `_installs_for_the_path_interpreter` gives the two forms that count.

    Limits. The check does not prove that the install holds each module.
    It does not read step `if:` conditions.
    `test_the_plan_job_installs_the_sdk_on_each_run_that_asks_the_registry` pins both for release-images.
    `test_every_workflow_installs_what_the_tools_it_runs_import` covers the calls that `uv run` starts.
    """
    calls = 0
    offenders = []
    for path in sorted((REPO_ROOT / ".github/workflows").glob("*.yml")):
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job_id, spec in (workflow.get("jobs") or {}).items():
            job_steps = spec.get("steps") or []
            for index, item in enumerate(job_steps):
                for tool in _bare_tool_calls(str(item.get("run", ""))):
                    calls += 1
                    imported = _module_level_imports(tool)
                    if imported and not _installs_for_the_path_interpreter(job_steps[:index]):
                        label = item.get("name") or item.get("id") or f"#{index + 1}"
                        offenders.append(
                            f"{path.name}: job {job_id!r}, step {label!r} runs tools/{tool}.py with a bare "
                            f"python, and the tool imports {sorted(imported)} at module level"
                        )
    assert calls, "no workflow step runs a tools/ script with a bare python: this check has lost its subject"
    assert not offenders, (
        f"no earlier step of the job installs these imports for the PATH interpreter: {offenders}. "
        f"The step fails with ModuleNotFoundError on a runner without them (#1369). Before the step, add "
        f'setup-uv, then `uv sync`, then `echo "$PWD/.venv/bin" >> "$GITHUB_PATH"`. Or start the call '
        f"with `uv run --no-sync` after a `uv sync`"
    )


def test_anything_that_caches_can_actually_save() -> None:
    """`actions/cache` needs `actions: write` to populate. With read-only it
    restores, misses every time, and reports success — an optimisation that
    measures as working and does nothing.

    The first version of this check scanned only `uses: actions/cache`, and the
    half it missed is the half that broke: `setup-uv` with `enable-cache: true`
    caches internally, `ci-required` granted no `actions` scope at all, and a
    called workflow cannot exceed its caller's grant. That stayed invisible
    while the cache key kept hitting. The moment #645 changed uv.lock, every
    run failed in `Post Run astral-sh/setup-uv` with every real step green.

    So the scan asks what a job DOES, not which action it names, and it
    resolves the grant through `workflow_call` to the caller.
    """
    workflows = {
        path.name: yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in sorted((REPO_ROOT / ".github/workflows").glob("*.yml"))
    }

    def caches(job: dict) -> bool:
        for step in job.get("steps") or []:
            if "actions/cache" in str(step.get("uses", "")):
                return True
            with_block = step.get("with") or {}
            if str(with_block.get("enable-cache", "")).lower() == "true" or "cache" in with_block:
                return True
        return False

    def granted(workflow: dict, job: dict) -> dict:
        return {**(workflow.get("permissions") or {}), **(job.get("permissions") or {})}

    def only_reusable(workflow: dict) -> bool:
        """A workflow_call-only workflow has no permissions of its own — the
        caller's grant is the effective one, and that is what gets checked. Its
        jobs looked like offenders in the first version of this scan, which is a
        false positive on the design working as intended."""
        triggers = workflow.get(True) or workflow.get("on") or {}
        return set(triggers) == {"workflow_call"} if isinstance(triggers, dict) else triggers == "workflow_call"

    offenders = []
    for name, workflow in workflows.items():
        if only_reusable(workflow):
            continue
        for job_name, job in (workflow.get("jobs") or {}).items():
            called = str(job.get("uses", ""))
            if called.startswith("./.github/workflows/"):
                # The caller caps what the called workflow can have, so the
                # grant that matters is the caller's.
                child = workflows.get(called.rsplit("/", 1)[-1])
                if child and any(caches(inner) for inner in (child.get("jobs") or {}).values()):
                    if granted(workflow, job).get("actions") != "write":
                        offenders.append(f"{name}:{job_name} -> {called.rsplit('/', 1)[-1]}")
                continue
            if caches(job) and granted(workflow, job).get("actions") != "write":
                offenders.append(f"{name}:{job_name}")

    assert not offenders, (
        f"these jobs cache without `actions: write`: {offenders}. The cache restores, never "
        f"saves, and the job fails outright the first time its key changes"
    )


def test_the_split_python_jobs_cover_every_testpath() -> None:
    """A4 D3 (#673): `python / check` ran one serial `uv run pytest` (259 s
    measured); the split runs explicit path lists in parallel jobs. Explicit
    lists can rot: a sixth package added to pyproject's testpaths would run in
    nobody's job and merge green while covered by nothing — the #472 shape,
    coverage that exists on paper. So the union of the split jobs' pytest paths
    must equal pyproject's testpaths exactly.
    """
    import tomllib

    testpaths = set(
        tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["pytest"]["ini_options"][
            "testpaths"
        ]
    )
    workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/ci-python.yml").read_text(encoding="utf-8"))
    covered: set[str] = set()
    for job in workflow["jobs"].values():
        for step in job.get("steps") or []:
            run = str(step.get("run", ""))
            # A sharded lane (A4 B1) names its root(s) on the pytest_shard.py line
            # instead of on the pytest line, whose paths come from a shell
            # array. The root counts as covered here because
            # test_pytest_shard.py separately proves the shards partition that
            # root exactly — neither check alone would be enough.
            for match in re.finditer(r"pytest_shard\.py\s+(.+?)\s+--shard", run):
                for arg in shlex.split(match.group(1)):
                    covered.add(arg)
            if run.startswith("uv run pytest "):
                # shlex, and flags filtered: a `-q` or `-k expr` token counted
                # as a path would make the equality fail on correct workflows —
                # or worse, mask a genuinely dropped package behind a flag
                # token that happens to balance the set sizes (review).
                tokens = shlex.split(run)
                covered.update(t for t in tokens[3:] if not t.startswith("-"))
    assert covered == testpaths, (
        f"the split ci-python jobs run pytest over {sorted(covered)} but pyproject declares "
        f"testpaths {sorted(testpaths)} — anything in the difference merges green with no CI "
        f"coverage at all (#673)"
    )


def test_the_pr_trigger_covers_every_file_the_manifest_names() -> None:
    """A4 D4a (#673): the reproof runs on PRs path-filtered to the exact union
    of the manifest's file+guard sets — deliberately not a directory glob, which
    would put a 2-4 minute job on most PRs' critical path and undo D3's 3-minute
    wall. The cost of exactness is rot: a new mutation naming a file outside the
    list would merge with no PR-time re-proof and fall back to the weekly run,
    which is the 7-day latency this trigger exists to remove. So the list is
    pinned to the manifest here.

    Lives in THIS file, not test_mutation_reproof.py, because #583's boundary
    scan forbids any other test from opening a workflow — it fired on the first
    placement of this test, which is that guard doing its job.
    """
    import json

    workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/mutation-reproof.yml").read_text(encoding="utf-8"))
    # YAML 1.1 parses bare `on:` as boolean True; a YAML 1.2 loader keeps "on".
    # The same dual lookup the reusable-workflow check above already uses.
    triggers_block = workflow.get(True) or workflow.get("on") or {}
    trigger_paths = set(triggers_block["pull_request"]["paths"])
    manifest = json.loads((REPO_ROOT / "tools/mutations.json").read_text(encoding="utf-8"))
    needed = {m["file"] for m in manifest["mutations"]} | {m["guard"] for m in manifest["mutations"]}
    needed |= {"tools/mutations.json", "tools/mutation_reproof.py"}
    missing = sorted(needed - trigger_paths)
    assert not missing, (
        f"tools/mutations.json names {missing} and mutation-reproof.yml's pull_request "
        f"paths do not include them — a PR editing those files merges with no re-proof "
        f"and the dead-guard latency regresses to the weekly run (#673)"
    )


def _changes_filters() -> dict[str, list[str]]:
    workflow = yaml.safe_load(source(REQUIRED))
    changes_job = (workflow.get("jobs") or {})["changes"]
    # Default + assertion so a restructured changes job fails by naming the
    # missing contract, not as a bare StopIteration.
    filter_step = next(
        (step for step in changes_job["steps"] if "filters" in (step.get("with") or {})),
        None,
    )
    assert filter_step is not None, (
        "ci-required's changes job no longer carries a paths-filter step; every lane decision reads from it (#673)"
    )
    return yaml.safe_load(filter_step["with"]["filters"])


def test_the_changes_filter_reaches_the_sweep_over_published_agent_files() -> None:
    """#1018 merged with test_every_top_level_directory_is_scanned_or_explicitly_excluded
    red on main. It added `.ws-publish/` and touched nothing the python filter matched,
    so ci-python never ran and `required` read the skip as success: #673's class, one
    directory further over.

    The shared rule source publishes this repository's `AGENTS.md` and skills, so a PR
    that touches only those paths is the normal case, not an edge. A test in this file
    reads every one of them: `skills/` is a scan root, the mirror-only roots and the
    publish record are excluded on premises asserted here, and `AGENTS.md` is a root file.

    A PR that adds some other new top-level directory still skips ci-python; no filter
    entry can name a directory that does not exist yet.
    """
    # Selection, not list membership (#1412): a filter entry can name a root and still miss
    # a dot-directory file, or reach it through a broader entry such as `*.md`.
    published = [
        f"{root}/probe/file.md" for root in ("skills", *_MIRROR_ONLY_EXCLUDED_ROOTS, _RECORD_ONLY_EXCLUDED_ROOT)
    ] + ["AGENTS.md"]
    missing = [path for path in published if "python" not in selected_lanes(REQUIRED, [path])]
    assert not missing, (
        f"the python filter does not select {missing}: a PR touching only those paths skips ci-python, "
        f"and `required` reads the skip as success (#1018)"
    )


def test_the_changes_filter_reaches_every_test_that_guards_a_tool() -> None:
    """A tools-only PR used to run ZERO tests — A4 review finding (#673).

    The `changes` job's python filter did not include `tools/**`, and the
    `required` summariser treats a skipped job as success. Every deploy-gate
    tool (health_check, walk_evidence, output_invariants, issue_close_guard,
    mutation_reproof, ...) is tested from common/runtime/tests via load_tool,
    which only ci-python executes — so the gate tooling itself could merge
    with no test running. `tools/` appeared exactly once in ci-required.yml
    before the fix: inside a comment.

    Pins three memberships (python/db/web each execute or read tools/) and
    that the python filter covers the directory of every pytest testpath, so a
    sixth package cannot land outside the filter the way tools/ did.
    """
    import tomllib

    filters = _changes_filters()

    for lane in ("python", "db", "web"):
        assert "tools/**" in filters[lane], (
            f"the {lane} filter no longer includes tools/**; a tools-only PR skips that lane "
            f"and `required` reads the skip as success (#673)"
        )

    # The same class one directory over, found by #689's own check run: this
    # very file pins the shape of deploy-release, delivery-report,
    # issue-close-guard and mutation-reproof, and a PR editing those workflows
    # matched no python filter — so the tests pinning them were exactly the
    # ones that did not run.
    assert ".github/workflows/**" in filters["python"], (
        "a workflow-only PR skips ci-python, and this file's workflow-shape tests are exactly what does not run (#673)"
    )

    testpaths = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["pytest"][
        "ini_options"
    ]["testpaths"]
    for path in testpaths:
        top = "/".join(path.split("/")[:2])
        assert any(entry.startswith(top) for entry in filters["python"]), (
            f"pytest testpath {path!r} is outside every python filter entry — PRs touching it "
            f"skip ci-python and merge with that suite never running (#673)"
        )


# --- #1412: a pull request that changes only unread documents starts no test lane ------
#
# Measured on #1409, a rules-only PR (AGENTS.md, skills/, the two agent mirrors and the
# publish record): ci-python ran, and so did all three image builds. The image lanes held
# `!` entries (#1168) that never excluded anything. The paths-filter default quantifier
# is `some`, and picomatch returns true for every path a negated pattern does not match,
# so each `!` entry selected the whole repository.
#
# The tests below pin four facts:
#   1. the glob matcher follows picomatch, dot paths included;
#   2. no `!` entry stands under the default quantifier;
#   3. a document selects the lanes that run a test which reads it, and no other lane;
#   4. a PR of unread documents starts only `changes`, `security` and `required`, and the
#      `required` summary still reports success.

#: What `picomatch(pattern, {dot: true})` returned for each pair. Produced with picomatch
#: 2.3.1, the version that dorny/paths-filter v3 depends on. Both outcomes are present.
PICOMATCH_GOLDEN = (
    ("docs/**", "docs", True),
    ("docs/**", "docs/a.md", True),
    ("docs/**", "docs/x/y.md", True),
    ("docs/**", "docsx/a.md", False),
    ("docs/**", ".docs/a.md", False),
    ("docs/**", "docs/.hidden/x", True),
    (".claude/**", ".claude/skills/prr/SKILL.md", True),
    (".claude/**", "claude/x", False),
    ("*.md", "README.md", True),
    ("*.md", "docs/a.md", False),
    ("*.md", ".hidden.md", True),
    ("**/*.md", "a/.b/c.md", True),
    ("apps/**/*.md", "apps/README.md", True),
    ("apps/**/*.md", "apps/.x/README.md", True),
    ("a/*/b", "a/b", False),
    ("a/**/b", "a/b", True),
    ("a/**/b", "a/.x/b", True),
    ("a?c", "a/c", False),
    ("a?c", "a.c", True),
    ("*", ".env.example", True),
    ("*", "a/b", False),
    ("**", ".a/b", True),
    ("apps/*", "apps", False),
    ("apps/*", "apps/.x", True),
    ("common/runtime/**", "common/runtime", True),
    ("common/runtime/**", "common/runtime2/x", False),
    ("uv.lock", "uv.lock.bak", False),
    ("!apps/app-web/tests/**", "docs/a.md", True),
    ("!apps/app-web/tests/**", "apps/app-web/tests/a.ts", False),
)


def test_the_glob_matcher_agrees_with_picomatch_including_dot_paths() -> None:
    """The gate tests below judge a filter with `glob_matches`. If it differs from picomatch,
    they pass or fail for the wrong reason. The expected values come from picomatch itself,
    and the pairs include a dot directory, a dot file and a negated pattern."""
    wrong = [
        (pattern, path, want) for pattern, path, want in PICOMATCH_GOLDEN if glob_matches(pattern, path) is not want
    ]
    assert not wrong, f"glob_matches differs from picomatch 2.3.1 on (pattern, path, picomatch result): {wrong}"
    outcomes = {want for *_, want in PICOMATCH_GOLDEN}
    assert outcomes == {True, False}, "the golden pairs must hold a match and a non-match"
    for unsupported in ("{a,b}/**", "a/[bc]", "a/+(b|c)", "a/!(b)", "a**b", "!!a/**", "/a/**", "./a/**", ""):
        with pytest.raises(ValueError):
            glob_matches(unsupported, "a/b")
    for path in ("", "/a", "a/"):
        with pytest.raises(ValueError):
            glob_matches("a/**", path)


def test_a_negated_filter_entry_stands_only_under_the_every_quantifier() -> None:
    """#1168 added `!apps/app-web/tests/**`-style entries to the image lanes so a test-only
    or markdown-only PR would build no image. The step kept the default quantifier `some`:
    a lane is selected when ANY entry matches ANY changed file. A `!` entry matches every
    path it does not name, so it selected every PR. Measured on #1409: a rules-only PR built
    all three images. `predicate-quantifier: every` makes the entries a conjunction, which a
    union of scopes cannot use. Exclusions therefore do not belong in a `some` step."""
    negated = {
        lane: [entry for entry in patterns if entry.startswith("!")]
        for quantifier, lanes in paths_filters(REQUIRED)
        if quantifier == "some"
        for lane, patterns in lanes.items()
    }
    negated = {lane: entries for lane, entries in negated.items() if entries}
    assert not negated, (
        f"these lanes hold `!` entries under the default `some` quantifier: {negated}. Each entry "
        f"selects every path it does not name, so the lane starts on every PR (#1412)"
    )


def test_every_lane_a_job_reads_is_a_lane_the_filter_defines() -> None:
    """A job gate that reads a lane the filter does not define reads the empty string, and
    the lane never starts. The tests below start jobs from filter output, so a misspelled
    lane would make them pass over a job that can no longer run."""
    defined = {lane for _, lanes in paths_filters(REQUIRED) for lane in lanes}
    published = set(job(REQUIRED, "changes")["outputs"])
    read = set(re.findall(r"needs\.changes\.outputs\.(\w+)", source(REQUIRED)))
    assert read, "no job reads a changes output, so this test checks nothing"
    assert read <= defined, f"jobs read lanes the filter does not define: {sorted(read - defined)}"
    assert read <= published, f"jobs read outputs the changes job does not publish: {sorted(read - published)}"


_PULL_REQUEST = {"event_name": "pull_request", "ref_type": "branch", "ref": "refs/pull/1/merge"}


def _jobs_started_by(paths: list[str]) -> set[str]:
    """The ci-required jobs that a pull request changing exactly these paths starts.

    Lane outputs come from the real filter. Each job `if:` is judged by `_decides`, so the
    result follows the workflow text. A merge-queue run forces every lane and is not modelled.
    """
    selected = selected_lanes(REQUIRED, paths)
    defined = {lane for _, lanes in paths_filters(REQUIRED) for lane in lanes}
    outputs = {lane: "true" if lane in selected else "false" for lane in defined}
    jobs = load(REQUIRED)["jobs"]
    needs = {name: {"result": "success", "outputs": outputs if name == "changes" else {}} for name in jobs}
    return {name for name, spec in jobs.items() if _decides(spec.get("if"), _PULL_REQUEST, needs=needs)}


def _run_required_summary(started: set[str], failed: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run the `required` job's own script on the results that a PR run would hand it."""
    import json

    assert shutil.which("jq"), "the required summary calls jq; the CI runner has it, so install it here"
    results = {
        name: {"result": "failure" if name == failed else "success" if name in started else "skipped"}
        for name in job(REQUIRED, "required")["needs"]
    }
    script = str(job_step(REQUIRED, "required", "Reject any failed, cancelled, or missing dependency")["run"])
    return subprocess.run(
        ["bash", "-e", "-c", script],
        env={**os.environ, "RESULTS": json.dumps(results)},
        capture_output=True,
        text=True,
        check=False,
    )


@cache
def _tracked_files() -> tuple[str, ...]:
    listing = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z"], capture_output=True, text=True, check=True
    ).stdout
    tracked = tuple(entry for entry in listing.split("\0") if entry)
    assert len(tracked) > 100, f"git reports only {len(tracked)} tracked files (GREEN-WHILE-EMPTY)"
    return tracked


@cache
def _markdown_named_by_tests() -> dict[str, set[str]]:
    """`{tracked markdown file: the test files that name it in a string constant}`.

    A bare name such as `init.md` means the root file when one is tracked, and otherwise
    every tracked file of that name. This sees a name written out in the test. It does not
    see a name built at run time or a tree walk. The walks are listed in `_lanes_that_read`.
    The test file you are reading is skipped: its constants name files only to assert on
    them (the Qlib sweep does the same for itself).
    """
    markdown = {path for path in _tracked_files() if path.endswith(".md")}
    testpaths = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["pytest"][
        "ini_options"
    ]["testpaths"]
    this_file = Path(__file__).resolve().relative_to(REPO_ROOT).as_posix()
    named: dict[str, set[str]] = {}
    for testpath in testpaths:
        for test_file in sorted((REPO_ROOT / testpath).rglob("*.py")):
            relative = test_file.relative_to(REPO_ROOT).as_posix()
            if relative == this_file:
                continue
            for node in ast.walk(ast.parse(test_file.read_text(encoding="utf-8"))):
                if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                    continue
                if not re.fullmatch(r"[\w./-]+\.md", node.value):
                    continue
                if node.value in markdown:
                    hits = [node.value]
                elif "/" in node.value:
                    hits = []
                else:
                    hits = [path for path in markdown if path.rsplit("/", 1)[-1] == node.value]
                for hit in hits:
                    named.setdefault(hit, set()).add(relative)
    return named


def _lanes_that_read(path: str, named: dict[str, set[str]]) -> set[str]:
    """The lanes that run a test which reads this document. Empty: no test reads it.

    - Four roots are read by tree walks in this file: the Qlib text sweep reads `skills/`,
      and the mirror and record tests read `.claude/`, `.agents/` and `.ws-publish/`.
    - The Qlib sweep reads every top-level file but `init.md`, and tests name that file.
    - A test that names a document reads it. A test under `apps/data-engine/tests` runs in
      the data_engine job as well.
    """
    roots = {"skills", *_MIRROR_ONLY_EXCLUDED_ROOTS, _RECORD_ONLY_EXCLUDED_ROOT}
    readers = named.get(path, set())
    lanes: set[str] = set()
    if path.split("/")[0] in roots or "/" not in path or readers:
        lanes.add("python")
    if any(reader.startswith("apps/data-engine/tests/") for reader in readers):
        lanes.add("data_engine")
    return lanes


def _documents() -> list[str]:
    """Every tracked file that is a document or a mirror of one: `docs/`, the top-level
    markdown files, and the four roots that are published copies of the rule source."""
    roots = {"skills", *_MIRROR_ONLY_EXCLUDED_ROOTS, _RECORD_ONLY_EXCLUDED_ROOT}
    return [
        path
        for path in _tracked_files()
        if path.split("/")[0] in roots or (path.endswith(".md") and (path.startswith("docs/") or "/" not in path))
    ]


def _unread_documents() -> list[str]:
    named = _markdown_named_by_tests()
    return [path for path in _documents() if not _lanes_that_read(path, named)]


def test_a_document_selects_the_lanes_that_read_it_and_no_other() -> None:
    """#1412: a document that a test reads keeps the lane that runs that test, or a PR
    could change it, skip the test and break main (#1018). A document that no test reads
    selects no lane. Named readers today: init.md, README.md, AGENTS.md,
    docs/datahub-quality-report.md and docs/architecture-decisions/A3-source-governance.md.
    """
    named = _markdown_named_by_tests()
    anchor = "docs/architecture-decisions/A3-source-governance.md"
    assert "common/runtime/tests/test_init_principles.py" in named.get(anchor, set()), (
        f"the scan no longer sees test_init_principles.py read {anchor}, so it cannot find readers (GREEN-WHILE-EMPTY)"
    )
    documents = _documents()
    assert len(_unread_documents()) >= 10, f"fewer than 10 unread documents; the document list drifted: {documents}"
    wrong = {}
    for path in documents:
        selected = selected_lanes(REQUIRED, [path])
        wanted = _lanes_that_read(path, named)
        if selected != wanted:
            wrong[path] = f"filter selects {sorted(selected)}; its readers need {sorted(wanted)}"
    assert not wrong, f"documents whose lanes differ from the lanes of their readers: {wrong}"


def test_a_pull_request_that_changes_only_unread_documents_starts_no_test_lane() -> None:
    """The `required` check stays green: the summary reads a skipped job as success."""
    unread = _unread_documents()
    assert unread, "no unread document exists, so this test checks nothing"
    started = _jobs_started_by(unread)
    assert started == {"changes", "security", "required"}, (
        f"a PR of {len(unread)} unread documents starts {sorted(started)}; only the filter, the "
        f"secret scan and the summary may start (#1412)"
    )
    summary = _run_required_summary(started)
    assert summary.returncode == 0, f"required reports failure for a docs-only PR: {summary.stderr}"
    # The same script must fail for a failed dependency, or the exit code above proves nothing.
    assert _run_required_summary(started, failed="security").returncode != 0


@pytest.mark.parametrize(
    ("code_path", "needed"),
    [
        ("common/runtime/src/truealpha_runtime/sample.py", {"python", "runtime", "data_engine"}),
        ("common/runtime/src/truealpha_tools/output_invariants.py", {"db", "python", "runtime", "data_engine"}),
        ("common/runtime/src/truealpha_tools/schema_drift.py", {"db", "python", "runtime", "data_engine"}),
        (".github/workflows/ci-web.yml", {"python", "web"}),
        ("db/migrations/20261020T0000_sample.sql", {"db", "python", "runtime", "data_engine"}),
        ("uv.lock", {"python", "runtime", "data_engine"}),
        ("apps/app-web/package.json", {"web", "image_app_web"}),
    ],
)
def test_code_keeps_its_lanes_when_it_travels_with_unread_documents(code_path: str, needed: set[str]) -> None:
    unread = _unread_documents()
    alone = selected_lanes(REQUIRED, [code_path])
    assert needed <= alone, f"{code_path} selects {sorted(alone)} and lacks {sorted(needed - alone)}"
    assert selected_lanes(REQUIRED, [code_path, *unread]) == alone, "unread documents changed the lanes of a code PR"
    jobs_for_lane = {
        "db": "db",
        "python": "python",
        "runtime": "runtime",
        "web": "web",
        "image_app_web": "images_build",
    }
    started = _jobs_started_by([code_path, *unread])
    missing = {jobs_for_lane[lane] for lane in needed if lane in jobs_for_lane} - started
    assert not missing, f"a PR changing {code_path} does not start {sorted(missing)}"


@pytest.mark.parametrize(
    ("path", "images"),
    [
        ("apps/app-web/src/app/page.tsx", {"image_app_web"}),
        ("apps/llm-service/src/llm_service/main.py", {"image_llm_service"}),
        ("apps/data-engine/src/data_engine/dagster_defs.py", {"image_data_engine"}),
        ("common/contracts/src/truealpha_contracts/ast.py", {"image_llm_service", "image_data_engine"}),
        ("uv.lock", {"image_llm_service", "image_data_engine"}),
    ],
)
def test_each_image_lane_selects_only_the_paths_its_image_is_built_from(path: str, images: set[str]) -> None:
    """The opposite failure to a lane that always starts: a lane that never starts. Each
    image lane must select its own sources and must leave the other images alone."""
    image_lanes = {"image_app_web", "image_llm_service", "image_data_engine"}
    assert selected_lanes(REQUIRED, [path]) & image_lanes == images


def test_a_failing_shard_lane_fails_instead_of_running_the_whole_suite() -> None:
    """A4 B1 (#673): the degraded path of the sharded data-engine lane.

    Bare `pytest` with no path arguments falls back to pyproject's testpaths, so
    an empty FILES array would make each of the three lanes run the ENTIRE suite
    and still report green — the failure would be invisible and the split would
    silently un-do itself. Two layers keep that impossible, and both are pinned
    here: the shard tool is called in a command substitution (whose failure
    `set -e` sees, unlike a process substitution — demonstrated executably in
    test_pytest_shard.py), and an explicit count check refuses an empty
    selection.
    """
    lane = job(PYTHON, "test-data-engine")
    shard_steps = [str(spec.get("run", "")) for spec in lane["steps"] if "pytest_shard.py" in str(spec.get("run", ""))]
    assert len(shard_steps) == 1, f"expected exactly one sharding step, found {len(shard_steps)}"
    snippet = shard_steps[0]
    assert "$(python3 tools/pytest_shard.py" in snippet, (
        "the shard tool is no longer called in a command substitution — its exit status is "
        "invisible again, and a failed shard runs the entire suite instead of failing"
    )
    assert "< <(python3 tools/pytest_shard.py" not in snippet, (
        "process substitution is back: `set -e` cannot see the shard tool fail"
    )
    assert "refusing to run bare pytest" in snippet, (
        "the empty-selection check is gone — the second layer that keeps an empty FILES array "
        "from expanding into a whole-suite run"
    )
    assert "--of ${{ strategy.job-total }}" in snippet, (
        "the lane count is hard-coded again — a `--of N` that drifts from the matrix length "
        "leaves every file at position kN+(N-1) assigned to a shard nobody runs, green forever "
        "(the #527 green-while-empty shape this job exists to avoid)"
    )
    assert "--shard ${{ strategy.job-index }}" in snippet, (
        "the shard index no longer comes from the matrix position it is running at"
    )
    assert lane["strategy"]["fail-fast"] is False, (
        "fail-fast is on: one red shard would cancel the other two and hide their failures"
    )


def test_the_liveness_window_is_one_churn_cycle_plus_margin() -> None:
    """#860: the liveness job (#454) slept a fixed 120 s and was the pole of every
    PR's and every main push's critical path at 2.5 min. The regression it guards
    against announced itself every ~68-70 s, and the job's own step name called
    120 s "over one cycle" -- so the window was one cycle with 50 s of unsized
    margin, not two cycles. It is now the documented cadence plus a few seconds.

    Pinned from the cadence the job documents, not from a literal: a window at or
    below the cadence's far end can miss the one event it waits for, and a window
    that creeps back past a modest margin puts the 45 s back on every run. Whoever
    re-measures the cycle longer must raise the comment, and this test then makes
    them raise the sleep with it.
    """
    lane = job(PYTHON, "dagster-code-server-liveness")
    observe = [spec for spec in lane["steps"] if "No heartbeat received" in str(spec.get("run", ""))]
    assert len(observe) == 1, f"expected exactly one churn-assertion step in the liveness job, found {len(observe)}"
    spec = observe[0]
    sleeps = re.findall(r"^\s*sleep (\d+)\s*$", str(spec["run"]), flags=re.MULTILINE)
    assert len(sleeps) == 1, f"the observation is one fixed sleep, found {sleeps}"
    window = int(sleeps[0])

    cadences = re.findall(r"every ~(\d+)-(\d+)s", source(PYTHON))
    assert cadences, "ci-python.yml no longer documents the ~68-70s churn cadence the window is sized from"
    far_end = max(int(high) for _, high in cadences)
    assert window > far_end, (
        f"a {window} s window cannot be sure to contain a churn event that arrives every ~{far_end} s"
    )
    assert window <= far_end + 10, (
        f"the {window} s window is more than 10 s past the ~{far_end} s cadence — the margin is creeping "
        f"back toward the second cycle that cost 45 s on every run's critical path (#860)"
    )
    assert f"{window}s" in str(spec["name"]), "the step's name must say how long it observes"


def test_dagster_liveness_filter_is_narrow_and_a_subset_of_python() -> None:
    """#855 A4: dagster-code-server-liveness (truealpha#454's daemon self-termination
    class) measured 151-160 s of every PR's and every main push's critical path for a
    regression that reproduces only through the dagster composition root, its lanes,
    the image it runs in, and its own job definition -- a surface ~95% of PR diffs
    never touch. `dagster_liveness` is that narrow filter.

    It must be a SUBSET of `python`'s own filter, not merely narrower: `python`'s own
    `if` still gates the whole `uses: ./.github/workflows/ci-python.yml` call in
    ci-required.yml, so a path that sets `dagster_liveness` true without also setting
    `python` true would compute a filter output ci-python.yml — and the liveness job
    inside it — never has a chance to read, silently skipping a run this filter says
    should happen.
    """
    workflow = yaml.safe_load(source(REQUIRED))
    filter_step = next(
        (step for step in workflow["jobs"]["changes"]["steps"] if "filters" in (step.get("with") or {})),
        None,
    )
    assert filter_step is not None, "ci-required's changes job no longer carries a paths-filter step"
    filters = yaml.safe_load(filter_step["with"]["filters"])

    assert "dagster_liveness" in filters, (
        "the changes job no longer declares a dagster_liveness filter — #855 A4's gating has "
        "nothing to read and ci-python.yml falls back to running liveness on every PR again"
    )
    liveness_paths = set(filters["dagster_liveness"])
    expected = {
        "apps/data-engine/src/data_engine/dagster_defs.py",
        "apps/data-engine/src/data_engine/lanes/**",
        "apps/data-engine/Dockerfile",
        "docker-compose.yml",
        ".github/workflows/ci-python.yml",
    }
    assert liveness_paths == expected, (
        f"dagster_liveness filter paths are {liveness_paths}, expected {expected} — the job it "
        f"gates only reproduces through the composition root, its lanes, the image, the compose "
        f"surface, and this file itself"
    )

    def covered(path: str, patterns: set[str]) -> bool:
        for pattern in patterns:
            if pattern == path:
                return True
            if pattern.endswith("/**") and path.startswith(pattern[: -len("/**")] + "/"):
                return True
        return False

    python_paths = set(filters["python"])
    uncovered = {path for path in liveness_paths if not covered(path, python_paths)}
    assert not uncovered, (
        f"{uncovered} is in dagster_liveness but no python filter entry covers it — a PR that "
        f"touches only that path sets dagster_liveness true while ci-python.yml is never invoked "
        f"at all, so the liveness job silently never runs"
    )

    outputs = workflow["jobs"]["changes"]["outputs"]
    assert outputs.get("dagster_liveness") == (
        "${{ github.event_name == 'merge_group' && 'true' || steps.filter.outputs.dagster_liveness }}"
    ), "dagster_liveness must be wired the same way as every other changes output (merge_group forces true)"


def test_dagster_liveness_job_is_gated_off_a_pr_and_unconditional_elsewhere() -> None:
    """#855 A4, the other half of the property above: what actually reads the filter.

    ci-required.yml's `python` job computes the decision (not `github.event_name` read
    inside the called workflow, whose value in a called workflow is the CALLER's and not
    this repo's to assume) and passes it as a `workflow_call` input; ci-python.yml's
    liveness job is gated on that input. Every gate here is an input the caller names,
    never a read of the caller's event.
    """
    caller = job(REQUIRED, "python")
    decision = str(caller["with"]["dagster_liveness_required"])
    assert "github.event_name != 'pull_request'" in decision, (
        f"the caller's decision is {decision!r} — it no longer runs liveness unconditionally "
        f"off pull_request (main push, merge_group, workflow_dispatch)"
    )
    assert "needs.changes.outputs.dagster_liveness == 'true'" in decision, (
        f"the caller's decision is {decision!r} — it no longer reads the narrow filter, so a "
        f"PR that touches the dagster surface would never run the liveness job either"
    )

    liveness_input = triggers(PYTHON)["workflow_call"]["inputs"]["dagster_liveness_required"]
    assert liveness_input["type"] == "boolean" and liveness_input["default"] is False, (
        "dagster_liveness_required must default closed — an unset input on a direct call must not silently change what runs"
    )

    # ci-python.yml stays `workflow_call`-only: a reusable workflow that also declares its
    # own triggers and permissions gets NO run on a PR — #878's first push never started
    # ci-required at all, with no error anywhere.
    assert set(triggers(PYTHON)) == {"workflow_call"}, (
        f"ci-python.yml declares {sorted(triggers(PYTHON))} — a reusable workflow with its own "
        f"triggers silently gets no PR run (#878)"
    )

    liveness = job(PYTHON, "dagster-code-server-liveness")
    condition = str(liveness["if"])
    assert "inputs.dagster_liveness_required" in condition, (
        f"the liveness job's if is {condition!r} — it no longer reads the caller's decision, so "
        f"it either always runs (back on the PR critical path) or never does"
    )
    assert "github.event_name" not in condition, (
        f"the liveness job's if is {condition!r} — inside a reusable workflow that context is the "
        f"caller's; the contract is the named inputs"
    )

    de_cond = str(job(PYTHON, "test-data-engine")["if"])
    assert "inputs.data_engine_required" in de_cond, (
        f"test-data-engine condition {de_cond!r} must gate on data_engine_required"
    )


def test_the_data_engine_required_gate_and_filter() -> None:
    """Decouple CI: test-data-engine shards run on data_engine changes, skipping for isolated LLM service PRs."""
    caller = job(REQUIRED, "python")
    decision = str(caller["with"]["data_engine_required"])
    assert "github.event_name != 'pull_request'" in decision
    assert "needs.changes.outputs.data_engine == 'true'" in decision

    de_input = triggers(PYTHON)["workflow_call"]["inputs"]["data_engine_required"]
    assert de_input["type"] == "boolean" and de_input["default"] is True

    de_paths = _paths_filter("data_engine")
    for required_prefix in (
        "apps/data-engine/**",
        "common/contracts/**",
        "common/factors/**",
        "common/runtime/**",
        "db/**",
        "pyproject.toml",
        "uv.lock",
    ):
        assert required_prefix in de_paths, f"data_engine filter misses {required_prefix}"


#: The three factor-expression cross-check proofs #969 migrated from pinned Qlib to the
#: Polars AST, and the sources each one proves. Kept as data so the two tests below
#: disagree loudly if one is edited without the other.
POLARS_CROSS_CHECK_TESTS = {
    "common/factors/tests/test_gross_profit_per_employee.py",
    "common/factors/tests/test_peg.py",
    "common/factors/tests/test_price_to_sales.py",
}
POLARS_CROSS_CHECK_SOURCES = {
    "common/factors/src/factors/base/gross_profit_per_employee.py",
    "common/factors/src/factors/base/peg.py",
    "common/factors/src/factors/base/price_to_sales.py",
    "common/factors/src/factors/panel.py",
}


def _paths_filter(name: str) -> set[str]:
    """One named paths-filter from ci-required's changes job."""
    workflow = yaml.safe_load(source(REQUIRED))
    filter_step = next(
        (step for step in workflow["jobs"]["changes"]["steps"] if "filters" in (step.get("with") or {})),
        None,
    )
    assert filter_step is not None, "ci-required's changes job no longer carries a paths-filter step"
    filters = yaml.safe_load(filter_step["with"]["filters"])
    assert name in filters, f"the changes job no longer declares a {name} filter"
    return set(filters[name])


def _covered_by(path: str, patterns: set[str]) -> bool:
    for pattern in patterns:
        if pattern == path:
            return True
        if pattern.endswith("/**"):
            prefix = pattern[: -len("/**")]
            if path == prefix or path.startswith(prefix + "/"):
                return True
    return False


def test_the_changes_filter_reaches_the_shared_uv_action() -> None:
    """#1248: a PR that edits only the shared setup-uv action must re-run each lane that calls it.

    `required` reads a skipped job as success (#673). A filter that misses the action lets a
    broken install step merge while no lane runs it. The tests of the action are in
    `test_setup_uv_retry.py`.
    """
    action_path = ".github/actions/setup-uv/action.yml"
    lanes = {"db": DB, "python": PYTHON, "data_engine": PYTHON, "runtime": RUNTIME}
    for lane, workflow in lanes.items():
        assert any(spec.get("uses") == "./.github/actions/setup-uv" for spec in steps(workflow)), (
            f"{workflow} no longer calls the shared setup-uv action; update the lane map of this test"
        )
        assert _covered_by(action_path, _paths_filter(lane)), (
            f"the {lane} filter in ci-required.yml misses {action_path}: a PR that edits only "
            f"the action skips {workflow} and `required` reads the skip as success"
        )


def test_the_polars_cross_check_proofs_are_armed_in_ci_python() -> None:
    """#956's failure class, carried across #969's engine migration.

    #956 found the three factor reproducibility proofs unarmed: they lived in
    ci-qlib.yml, they `pytest.importorskip("qlib")`'d under ci-python, and ci-required's
    qlib paths filter did not cover them — so a PR editing exactly those files ran the
    proofs nowhere, and `required` treats a skipped job as success. Measured at the time
    of the migration: the Qlib cross-checks had skipped in 11 of the last 12 ci-python
    runs.

    #969 deleted ci-qlib.yml and moved the proofs onto `polars`, a first-class
    dependency. That removes the optional-import half BY CONSTRUCTION, and this test is
    what keeps it removed: the same class cannot reappear under a new name (a new
    optional engine, a new lane) without turning this red. Three properties, all of
    which have to hold for the proof to actually execute:

    1. the proofs and the sources they prove are covered by the `python` paths filter,
       so a PR touching only those files still runs ci-python;
    2. ci-python's test-core job actually runs `common/factors/tests`;
    3. no proof is gated behind `pytest.importorskip`, which would let the job run and
       the assertion never execute.
    """
    python_paths = _paths_filter("python")

    for relative in sorted(POLARS_CROSS_CHECK_TESTS | POLARS_CROSS_CHECK_SOURCES):
        assert (REPO_ROOT / relative).exists(), (
            f"{relative} does not exist — the cross-check inventory has drifted from the tree"
        )

    uncovered = {
        relative
        for relative in POLARS_CROSS_CHECK_TESTS | POLARS_CROSS_CHECK_SOURCES
        if not _covered_by(relative, python_paths)
    }
    assert not uncovered, (
        f"{sorted(uncovered)} are not covered by ci-required.yml's python paths filter — a PR "
        f"touching only these files would skip ci-python, and `required` reads a skipped job as "
        f"success, leaving the factor reproducibility proofs unarmed (#956, #969)"
    )

    runs = [
        str(step.get("run", ""))
        for step in yaml.safe_load(source(PYTHON))["jobs"]["test-core"]["steps"]
        if "pytest" in str(step.get("run", ""))
    ]
    assert any("common/factors/tests" in run for run in runs), (
        "ci-python's test-core job no longer runs common/factors/tests — the proofs are covered by "
        f"the filter but nothing executes them. pytest invocations found: {runs!r}"
    )

    for relative in sorted(POLARS_CROSS_CHECK_TESTS):
        text = (REPO_ROOT / relative).read_text(encoding="utf-8")
        assert "importorskip" not in text, (
            f"{relative} gates on pytest.importorskip again — the job runs, the test reports as "
            f"passed, and the assertion never executes. That is exactly the state #969 migrated "
            f"off Qlib to escape (11 of 12 ci-python runs skipped it)."
        )
        assert "factors.panel" in text and "pl.DataFrame" in text, (
            f"{relative} no longer evaluates the native Polars panel expression — the cross-check would "
            f"assert the Decimal path against itself"
        )


# --- Qlib non-reintroduction: two bounded checks, then a best-effort sweep ------------
#
# #969 migrated the factor-expression engine from Qlib to the Polars AST. Keeping it
# migrated needs a standing check, and the first four attempts at one were all text
# walks — each round of review found another path shape the walk could not reach (an
# extensionless Dockerfile, a repository-root file, an unscanned top-level directory, a
# waived dot-directory, a bare-name skip matching at any depth). That is not four
# careless implementations; it is what proving a negative over an unbounded surface
# looks like. A walk has to be right about every path shape that exists and every one
# that will exist.
#
# So the guarantee lives in the two checks below, over a surface of every uv.lock in
# the tree (one today) and the interpreter running this suite. A dependency cannot
# reach any environment without passing through a lockfile, and code cannot call a
# module that will not resolve. The text sweep that follows is kept for what those two
# cannot see — a workflow lane, a path filter, a stale instruction in a skill file, a
# Dockerfile line not yet built — and is documented as best-effort, not as a guarantee.

#: Matched as a substring against distribution names, so it covers `pyqlib` (the
#: distribution), `qlib` (the import name) and anything like `qlib-server`.
QLIB_DISTRIBUTION_MARKER = "qlib"


def test_no_lockfile_resolves_a_qlib_distribution() -> None:
    """Primary guard, part 1: the dependency cannot come back through a lockfile.

    Bounded by construction — every `uv.lock` in the repository is parsed, not walked.
    Nothing reaches a virtualenv, a CI runner or a production image without being
    resolved here first, so this is the narrowest place the question can be asked.

    Every lockfile, not just the root one: `common/factors/qlib-runtime/` was a
    workspace-EXCLUDED project with its own `uv.lock`, which is exactly how the Qlib
    dependency was isolated from the root environment before #969 deleted it. A second
    lockfile reappearing is the shape this has to catch, so the glob is the check.
    """
    lockfiles = [path for path in REPO_ROOT.rglob("uv.lock") if not set(path.parts) & {".venv", ".git", "node_modules"}]
    assert lockfiles, "no uv.lock found at all — this check would pass by having nothing to read"

    for lockfile in lockfiles:
        relative = lockfile.relative_to(REPO_ROOT).as_posix()
        packages = tomllib.loads(lockfile.read_text(encoding="utf-8")).get("package", [])
        assert len(packages) > 20, (
            f"{relative} resolves only {len(packages)} packages — that is not a populated lockfile, "
            f"so a clean result would mean nothing (GREEN-WHILE-EMPTY)"
        )
        names = sorted({str(package.get("name", "")).lower() for package in packages})
        offenders = [name for name in names if QLIB_DISTRIBUTION_MARKER in name]
        assert not offenders, (
            f"{relative} resolves {offenders} — the Qlib dependency is back. #969 removed the "
            f"factor-expression engine and the workspace-excluded `common/factors/qlib-runtime` "
            f"project that carried it; a lockfile entry puts it into every environment that "
            f"installs from this file."
        )


def test_qlib_is_not_resolvable_in_this_environment() -> None:
    """Primary guard, part 2: the module cannot be imported, whatever text sits where.

    This is the property that actually matters. A text scan asks whether five letters
    appear in a file it managed to reach; this asks whether the interpreter running the
    test suite can load the thing — and if it cannot, no code path can use it.

    Both halves are checked because they fail differently: a distribution can be
    installed without being imported anywhere yet (`importlib.metadata`), and a module
    can be importable from a path entry that no distribution declares
    (`importlib.util.find_spec`).
    """
    installed = sorted(
        {
            name.lower()
            for distribution in importlib.metadata.distributions()
            if (name := distribution.metadata["Name"] or "")
        }
    )
    assert len(installed) > 20, (
        f"only {len(installed)} distributions are visible — the environment is not the one the "
        f"suite runs in, so a clean result would mean nothing (GREEN-WHILE-EMPTY)"
    )
    offenders = [name for name in installed if QLIB_DISTRIBUTION_MARKER in name]
    assert not offenders, (
        f"{offenders} {'is' if len(offenders) == 1 else 'are'} installed in the environment this "
        f"suite runs in — #969 removed the Qlib engine, and an installed distribution is a code "
        f"path whatever the repository's text says"
    )

    assert importlib.util.find_spec("qlib") is None, (
        "`import qlib` resolves in this environment — the deleted engine is importable again. "
        "It need not be declared anywhere for this to be true: a path entry, an editable "
        "install or a vendored directory is enough, which is why this is checked separately "
        "from the distribution list above."
    )


#: Scanned for a Qlib reintroduction: every top-level directory that holds code, CI or
#: an executed procedure. `db/` is production code, not data — `apps/llm-service`'s
#: Dockerfile COPYs it and its CMD runs `db/apply_migrations.sh` on every container boot.
#: `.agents/` holds procedures an agent executes against this repository, one of which is
#: `factor-acceptance`, the very procedure this migration changed.
QLIB_SCAN_ROOTS = (".github", ".agents", "apps", "db", "common", "tools")
#: Top-level directories deliberately NOT scanned. Every other top-level directory must
#: appear in QLIB_SCAN_ROOTS — asserted below, because the two holes this guard has
#: already had were both "a place the walk cannot reach", found by review rather than by
#: the guard. A new top-level directory now fails this test until someone classifies it.
#: The excluded roots whose exclusion rests on a premise rather than on their contents
#: being out of scope outright: each is safe only while everything in it is swept through
#: another path -- a symlink into one of QLIB_SCAN_ROOTS, or a published copy that repeats a
#: file there byte for byte. Used BY the tuple below, not merely named beside it, so the
#: exclusion and the assertion cannot drift apart (#1006 review).
_MIRROR_ONLY_EXCLUDED_ROOTS = (".claude",)
#: Excluded on the same kind of premise: it holds one JSON record and nothing else.
_RECORD_ONLY_EXCLUDED_ROOT = ".ws-publish"

QLIB_SCAN_EXCLUDED_ROOT_DIRS = (
    # The ADR for this migration (A5-polars-vectorbt-engine.md) and A0's amendment note
    # live here; init.md rule 25 points at them. Recording the migration is the opposite
    # of reintroducing it.
    "docs",
    # Frozen history. Accepted records pin these files' hashes, so editing the prose
    # inside one breaks the record rather than removing a dependency.
    "governance",
    # Where agent hosts look for this repository's skills: `.claude/skills` (Claude Code,
    # #1003) and `.agents/skills` (Codex). Every entry under `.claude/` is either a symlink
    # resolving INTO A SCANNED ROOT, or a published copy of a file under `.agents/skills/`
    # (dev_env #348 unified skill delivery into `.agents/skills`). The safety argument is
    # the general one, and it is the general one that
    # test_every_mirror_only_root_entry_is_scanned_through_its_source asserts. The content
    # is therefore swept through its source, once rather than twice. That premise is the
    # whole reason the exclusion is safe: a file added here that no scanned file repeats
    # would otherwise be silently out of scope, which is the exact shape of the two holes
    # this guard has already had.
    *_MIRROR_ONLY_EXCLUDED_ROOTS,
    # The record the shared rule source writes when it publishes `AGENTS.md` and the
    # skills (#1018): output paths and their hashes. It names files and runs nothing; the
    # files it names are swept where they live -- `AGENTS.md` as a root file, the skills
    # under `.agents/skills/`. Safe only while the record is all it holds, which
    # test_the_publish_record_root_holds_only_the_record asserts.
    _RECORD_ONLY_EXCLUDED_ROOT,
)
#: The repository's own top-level FILES are scanned too, non-recursively. `rglob` from a
#: scan root cannot reach a file sitting at the repository root, and the root
#: `pyproject.toml` is exactly where a `common/factors/qlib-runtime` workspace member would
#: come back — #969 removed one from it. Non-recursive on purpose: recursing the root
#: would pull in the excluded directories above.
QLIB_SCAN_REPOSITORY_ROOT_FILES = True
#: Records of what a contract asserted at the time, not live references. Their bytes are
#: hashed by accepted records (`governance/batches/D4-datahub-interface.v1.json` pins
#: `datahub_interface.v2.json`'s whole-file sha256) or by their own versioned successors,
#: so editing the prose inside them breaks the record rather than removing a dependency.
#: The live statement of the same red line is in docs/architecture-decisions/.
QLIB_SCAN_EXCLUDED_DIRS = (
    "common/contracts/tests/fixtures",
    "apps/app-web/node_modules",
    "apps/app-web/.next",
)
#: Top-level documents that carry the migration's own history. `init.md` rule 25 points at
#: the ADR, so the rest of this exclusion set already covers it in spirit.
QLIB_SCAN_EXCLUDED_FILES = ("init.md",)
#: Files a deployed or tested code path is written in whose NAME carries the type rather
#: than an extension. `Path("Dockerfile").suffix` is `""`, so a suffix allowlist alone
#: drops every Dockerfile in the tree — including the three that build the production
#: images, where `RUN pip install pyqlib` would reinstall the deleted engine.
_QLIB_SCAN_TEXT_NAMES = {"Dockerfile", "Makefile", "makefile", "GNUmakefile", "Containerfile"}
_QLIB_SCAN_TEXT_NAME_PREFIXES = ("Dockerfile.", "Makefile.")
#: Directory names skipped wherever they appear, at any depth. Deliberately only names
#: that are never source by convention — a VCS directory, a virtualenv, a tool cache, a
#: vendored package tree. `dist`, `build` and `.next` USED to be here and were removed:
#: those are ordinary words a source directory can legitimately be called, and matching
#: them by bare name made `common/factors/build/` and its whole subtree invisible. None of
#: the three exists in this tree; `apps/app-web/.next` is excluded by resolved path in
#: QLIB_SCAN_EXCLUDED_DIRS instead, which is how an ambiguous name should be handled.
#:
#: The residual is honest and stated: a source directory deliberately named
#: `__pycache__` or `node_modules` would still be skipped. That is what the lockfile and
#: import checks above are for — they do not care what a directory is called.
_QLIB_SCAN_GENERATED_DIR_NAMES = {
    "__pycache__",
    ".git",
    ".venv",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
}
#: Deliberately absent: a bare `.env`. `Path(".env").suffix` is `""`, so listing `.env`
#: here would never have matched it anyway — and matching it by NAME is not wanted, since
#: a real `.env` is git-ignored secrets and this test prints the lines it objects to.
#: The tracked `.env.example` is matched by `.example`.
_QLIB_SCAN_TEXT_SUFFIXES = {
    ".cfg",
    ".css",
    ".dockerfile",
    ".example",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".jsx",
    ".lock",
    ".md",
    ".mjs",
    ".py",
    ".sh",
    ".sql",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".yaml",
    ".yml",
}
#: Files the sweep must actually have READ — not merely selected. An allowlist that
#: silently stops matching is the failure mode this sweep has had repeatedly: the first
#: version visited 782 files and could see none of the first five below, so a `pyqlib`
#: line in a production Dockerfile or a re-added qlib-runtime workspace member would have
#: left it green; the second could not enter `db/` at all. Naming them turns "the walk
#: still reaches the risky files" into an assertion instead of an assumption — for these
#: files. It says nothing about the ones nobody has thought of, which is why the binding
#: checks are the lockfile and import ones above.
QLIB_SCAN_MUST_REACH = (
    "pyproject.toml",
    "Makefile",
    "docker-compose.yml",
    "apps/data-engine/Dockerfile",
    "apps/llm-service/Dockerfile",
    "apps/app-web/Dockerfile",
    # Runs on every production container boot (llm-service CMD), and is the shell script
    # a `pip install pyqlib` would be easiest to hide in.
    "db/apply_migrations.sh",
    ".agents/skills/factor-acceptance/SKILL.md",
    # A dot-directory that holds code, and the reason the classification rule below
    # waives no directory for being dot-prefixed.
    ".github/workflows/ci-required.yml",
)


def _is_scannable_text_file(path: Path) -> bool:
    """True when the file's type is one this scan can read as text.

    An allowlist, not a denylist, so a binary is never read by accident — but keyed on
    the NAME as well as the extension, because a Dockerfile has no extension.
    """
    if path.suffix.lower() in _QLIB_SCAN_TEXT_SUFFIXES:
        return True
    if path.name in _QLIB_SCAN_TEXT_NAMES:
        return True
    return path.name.startswith(_QLIB_SCAN_TEXT_NAME_PREFIXES)


def _qlib_scan_candidates() -> list[Path]:
    """Every file the reintroduction scan reads, roots and repository-root files alike."""
    excluded_dirs = tuple((REPO_ROOT / relative).resolve() for relative in QLIB_SCAN_EXCLUDED_DIRS)
    excluded_files = {(REPO_ROOT / name).resolve() for name in QLIB_SCAN_EXCLUDED_FILES}
    # A check for a name has to name it. This one file is exempt, by exact path rather
    # than by directory, so a real reference anywhere else in common/runtime/tests still
    # trips the scan.
    excluded_files.add(Path(__file__).resolve())

    found: list[Path] = []
    walked: list[Path] = []
    if QLIB_SCAN_REPOSITORY_ROOT_FILES:
        walked.extend(REPO_ROOT.glob("*"))
    for root_name in QLIB_SCAN_ROOTS:
        root = REPO_ROOT / root_name
        assert root.is_dir(), f"{root_name} is not a directory — the scan roots have drifted"
        walked.extend(root.rglob("*"))

    for path in walked:
        if not path.is_file() or path.is_symlink():
            continue
        if set(path.parts) & _QLIB_SCAN_GENERATED_DIR_NAMES:
            continue
        resolved = path.resolve()
        if resolved in excluded_files:
            continue
        if any(directory in resolved.parents for directory in excluded_dirs):
            continue
        if not _is_scannable_text_file(path):
            continue
        found.append(path)
    return found


def test_every_top_level_directory_is_scanned_or_explicitly_excluded() -> None:
    """Keeps the text sweep's scope EXPLICIT — it does not make the sweep complete.

    `REPO_ROOT.glob("*")` yields directories as well as files, and they die at
    `path.is_file()`. So a top-level directory outside QLIB_SCAN_ROOTS is invisible to
    the sweep silently: that is how `db/` (production code — the llm-service image COPYs
    it and its CMD runs `db/apply_migrations.sh` on every boot) and `skills/` went
    unscanned. Listing the roots does not catch that; comparing the list against what git
    TRACKS does, so a new committed top-level directory is a decision someone writes down
    in one of the two tuples with a reason beside it.

    What this does NOT do is make the sweep sound. Directory scope was one of several
    path shapes the sweep has been wrong about, and the next one is not enumerable in
    advance. The guarantee is test_no_lockfile_resolves_a_qlib_distribution and
    test_qlib_is_not_resolvable_in_this_environment; this keeps a best-effort check from
    quietly getting narrower.
    """
    classified = set(QLIB_SCAN_ROOTS) | set(QLIB_SCAN_EXCLUDED_ROOT_DIRS) | _QLIB_SCAN_GENERATED_DIR_NAMES
    # Git-TRACKED top-level directories, dot-prefixed included. Trackedness is the
    # discriminator, and getting it wrong has now failed in both directions:
    #
    #   - waiving every dot-prefixed name (to avoid churn when a new linter cache
    #     appears) let a committed `.circleci/` or `.buildkite/` carrying
    #     `pip install pyqlib` bypass both the sweep and this test;
    #   - demanding every directory on disk be classified made the test fail on whatever
    #     untracked scratch directory a local tool had created — `.claude/`, `.codex/`,
    #     `.pi/` — which is a machine's state, not the repository's.
    #
    # Tracked is exactly the property that matters: an untracked directory cannot reach a
    # PR, a CI runner or an image, so it cannot reintroduce anything. A tracked one
    # always can, whatever it is called. `.github` is the standing proof that a
    # dot-directory holds code.
    listing = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    tracked_paths = [entry for entry in listing.split("\0") if entry]
    assert len(tracked_paths) > 100, (
        f"git reports only {len(tracked_paths)} tracked files — this is not the repository, so a "
        f"clean result would mean nothing (GREEN-WHILE-EMPTY)"
    )
    present = {entry.split("/", 1)[0] for entry in tracked_paths if "/" in entry}
    unclassified = present - classified
    assert not unclassified, (
        f"top-level director{'y' if len(unclassified) == 1 else 'ies'} {sorted(unclassified)} "
        f"{'is' if len(unclassified) == 1 else 'are'} neither scanned for a Qlib reintroduction nor "
        f"explicitly excluded, so a reference there would leave test_no_code_path_reintroduces_qlib "
        f"green. Add to QLIB_SCAN_ROOTS to scan it, or to QLIB_SCAN_EXCLUDED_ROOT_DIRS with the "
        f"reason it is out of scope — the way docs/ and governance/ are."
    )
    for root_name in QLIB_SCAN_ROOTS:
        assert (REPO_ROOT / root_name).is_dir(), f"{root_name} is a declared scan root but is not a directory"
    for root_name in QLIB_SCAN_EXCLUDED_ROOT_DIRS:
        assert (REPO_ROOT / root_name).is_dir(), (
            f"{root_name} is excluded from the scan but no longer exists — drop the exclusion rather "
            f"than leaving a rule about a directory that is gone"
        )


def test_no_code_path_reintroduces_qlib() -> None:
    """BEST-EFFORT text sweep for Qlib references. Not the guarantee — the backstop.

    Scope claim, stated plainly because four rounds of review proved the confident
    version wrong: this walks the code surfaces named in QLIB_SCAN_ROOTS plus the
    repository's top-level files, reading the file types in the two allowlists. A
    reference in a path shape it does not reach will not be caught here. The shapes it
    missed and now covers were an extensionless Dockerfile, a repository-root file, an
    unscanned top-level directory, a waived dot-directory and a bare-name skip matching
    at any depth — five, found by review rather than by this test, which is the evidence
    for why it is not trusted as the primary control.

    What it is genuinely good for is the part the bounded checks cannot see: a ci-qlib
    workflow lane, a `qlib` path-filter entry, a stale instruction in a skill file, a
    `pip install pyqlib` in a Dockerfile that has not been rebuilt yet. Those are text,
    not resolved dependencies, and they are worth catching early.

    What actually guarantees the migration stays migrated is upstream of this:
    `test_no_lockfile_resolves_a_qlib_distribution` (a dependency cannot reach any
    environment without passing through a lockfile) and
    `test_qlib_is_not_resolvable_in_this_environment` (code cannot call a module that
    will not resolve). Both are bounded — the tree's lockfiles and one interpreter — so
    neither has a path shape to miss.

    Out of scope deliberately, never by omission (see
    test_every_top_level_directory_is_scanned_or_explicitly_excluded):

    - `docs/` records the migration itself — `A5-polars-vectorbt-engine.md` is the ADR
      for this change, and `A0-governed-research-access.md` carries an amendment note;
    - `init.md` rule 25 keeps a one-clause historical pointer to A5 (QLIB_SCAN_EXCLUDED_FILES);
    - `governance/` is frozen history and its files' hashes are pinned by accepted tests;
    - `common/contracts/tests/fixtures/` holds versioned accepted-contract artifacts whose
      whole-file hashes are recorded elsewhere (see QLIB_SCAN_EXCLUDED_DIRS).
    """
    offenders: list[str] = []
    # Recorded from inside the read loop, not from the candidate list: a candidate that
    # raises on decode is skipped, and a coverage claim built before the read would still
    # count it. What this guard promises is that these files were SEARCHED.
    read: set[str] = set()
    for path in _qlib_scan_candidates():
        relative = path.relative_to(REPO_ROOT).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):  # pragma: no cover - binary or unreadable
            continue
        read.add(relative)
        for number, line in enumerate(text.splitlines(), start=1):
            if "qlib" in line.lower():
                offenders.append(f"{relative}:{number}: {line.strip()[:120]}")

    assert len(read) > 500, (
        f"the scan only read {len(read)} files — the walk is not reaching the tree, so a clean "
        f"result would mean nothing (GREEN-WHILE-EMPTY)"
    )
    for relative in QLIB_SCAN_MUST_REACH:
        assert (REPO_ROOT / relative).is_file(), (
            f"{relative} no longer exists, so this guard's coverage list is describing a tree that "
            f"is gone — re-derive QLIB_SCAN_MUST_REACH rather than dropping the entry"
        )
        assert relative in read, (
            f"the scan never read {relative} — a Qlib reference there would leave this test green. "
            f"That is the exact hole this guard has had twice: a suffix allowlist drops every "
            f"extensionless Dockerfile, rglob from a scan root cannot reach a repository-root file, "
            f"and a top-level directory that is not a scan root is never entered at all."
        )

    assert not offenders, (
        "Qlib is referenced again under "
        + ", ".join((*QLIB_SCAN_ROOTS, "the repository root"))
        + f" ({len(offenders)} line(s)). #969 migrated the factor-expression engine to the Polars "
        "AST and deleted the Qlib side; a code path naming it again is a reintroduction, not a "
        "leftover. If the reference is a historical record rather than a dependency, it belongs in "
        "docs/ or governance/, not here. (This sweep is best-effort — the binding checks are "
        "test_no_lockfile_resolves_a_qlib_distribution and "
        "test_qlib_is_not_resolvable_in_this_environment.)\n" + "\n".join(sorted(offenders)[:40])
    )


def test_main_pushes_never_collide_and_a_tag_run_can_never_be_cancelled() -> None:
    """#708/#860: each main push keys its concurrency group by `github.sha` so two
    main commits never share a group and `cancel-in-progress` never kills a main-branch
    image build/publish mid-flight, while PR runs still collapse to their newest push.
    """
    concurrency = yaml.safe_load(source(REQUIRED))["concurrency"]
    group = str(concurrency["group"])
    assert concurrency["cancel-in-progress"] is True, (
        "PR runs no longer collapse to their newest push — every superseded PR run now queues "
        "behind the one that replaced it"
    )
    assert "github.event_name == 'push'" in group, (
        f"the concurrency key is {group!r}: a main push is no longer routed to its own "
        f"SHA-keyed group, so two main commits can land in the same group again"
    )
    assert "github.sha" in group, (
        f"the concurrency key is {group!r}: it no longer uses github.sha, so a main push "
        f"cannot get its own group and main pushes go back to colliding (measured 6/27, #860)"
    )
    assert "github.ref" in group
    assert "github.event_name" in group


def test_the_split_web_jobs_still_run_every_web_check() -> None:
    """A4 (#673): ci-web was one 168 s job; it is now `check` (typecheck +
    DB-backed tests, ~70 s) and `browser` (build + the two-state walk).

    A split is where coverage goes missing — the python split needed a guard
    for exactly this (#472's coverage-on-paper), and here the risk is sharper
    because the halves are named for their tools rather than their subjects.
    So: every check the single job performed must still be performed by
    someone, and the browser walk must still be the thing that runs LAST in
    its own job, after the build it depends on.
    """
    jobs = yaml.safe_load(source(WEB))["jobs"]
    assert set(jobs) == {"check", "browser"}, f"ci-web's jobs changed to {sorted(jobs)}"

    # Every field a check can live in, not just `run`: the arming variable sits
    # in a step's `env`, and the first version of this scan reported it missing
    # for that reason alone. A scan reading fewer fields than a check can
    # occupy both cries wolf and, worse, can pass on a fragment found in the
    # wrong one.
    everything = "\n".join(spec_text(spec) for job in jobs.values() for spec in job["steps"])
    for fragment, what in (
        ("make typecheck-web FULL=1", "the TypeScript typecheck"),
        ("make test-web FULL=1", "the DB-backed test loop (#373: 13 suites once ran nowhere)"),
        ("bun run build", "the production build"),
        ("walk-tree.mjs", "the frozen route-tree walk (#494)"),
        ("check_route_manifest.py", "the route-namespace manifest (#463)"),
        ("truealpha_empty", "the absent-state database (#580)"),
        ("TRUEALPHA_REQUIRE_RUNTIME", "the arming variable that turns a skip into a failure (#468)"),
    ):
        assert fragment in everything, (
            f"{what} runs in neither ci-web job — the split dropped it, and a dropped check merges green forever"
        )

    browser = "\n".join(str(spec.get("run", "")) for spec in jobs["browser"]["steps"])
    assert browser.index("bun run build") < browser.index("walk-tree.mjs"), "the walk runs before the build it walks"


def test_the_chromium_cache_can_actually_save() -> None:
    """The 19 s playwright download is cached, and a cache without `actions:
    write` restores, misses, and reports success — an optimisation that
    measures as working and does nothing (#645's shape). The grant lives on the
    CALLER because a called workflow cannot exceed it.
    """
    web_caller = yaml.safe_load(source(REQUIRED))["jobs"]["web"]
    assert web_caller.get("permissions", {}).get("actions") == "write", (
        "ci-required's web job does not grant actions: write, so ci-web's chromium cache can "
        "never save — it will miss on every run and report success"
    )
    cache = step(WEB, "Restore chromium")
    assert "hashFiles(" in str(cache["with"]["key"]), (
        "the chromium cache key is not derived from the lockfile — a constant key serves a stale "
        "browser forever, and a parsed version string is one rename from becoming constant"
    )
    install = step(WEB, "Install chromium for the walk-tree suite")
    assert "steps.chromium_cache.outputs.cache-hit != 'true'" in install.get("if", ""), (
        "the browser job installs chromium unconditionally even when cached (#1208)"
    )


def test_no_job_holds_a_cache_grant_it_does_not_use() -> None:
    """The mirror of test_anything_that_caches_can_actually_save, and the half
    that was missing.

    A caller's `permissions:` is a ceiling for the ENTIRE called workflow, not
    for the job that needs it. ci-required grants ci-web `actions: write` so the
    browser job can save the chromium cache — and without a narrowing block the
    same token is held by a job whose main act is installing packages from a
    lockfile, which is where a supply-chain compromise would land (review).
    ci-python/ci-runtime never exposed this because they are single-job.

    So: in a called workflow whose caller grants `actions: write`, every job
    that does NOT cache must pin its own permissions without it.
    """
    workflows = {
        path.name: yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in sorted((REPO_ROOT / ".github/workflows").glob("*.yml"))
    }
    granted = {
        str(spec["uses"]).rsplit("/", 1)[-1]
        for workflow in workflows.values()
        for spec in (workflow.get("jobs") or {}).values()
        if isinstance(spec, dict)
        and str(spec.get("uses", "")).startswith("./.github/workflows/")
        and (spec.get("permissions") or {}).get("actions") == "write"
    }
    assert granted, "no workflow_call grants actions: write any more — this check has lost its subject"

    offenders = []
    for name in sorted(granted):
        for job_id, spec in (workflows[name].get("jobs") or {}).items():
            uses_cache = any(
                "actions/cache" in str(item.get("uses", "")) or "enable-cache" in str(item.get("with", ""))
                for item in spec.get("steps") or []
            )
            holds_write = (spec.get("permissions") or {}).get("actions") == "write"
            if not uses_cache and holds_write:
                offenders.append(f"{name}:{job_id}")
            if not uses_cache and spec.get("permissions") is None:
                offenders.append(f"{name}:{job_id} (unpinned, so it inherits the caller's grant)")
    assert not offenders, (
        f"these jobs hold a cache-write token they never use: {offenders}. A caller grant is a "
        f"ceiling for the whole called workflow; narrow it per job."
    )


def test_every_buildx_setup_retries_once_after_a_docker_hub_failure() -> None:
    """2026-09-16, run 35076694921: `docker/setup-buildx-action` pulls moby/buildkit from
    Docker Hub, a TLS handshake timeout there failed main's data-engine publish with every
    test green, and the re-run passed. Each job that sets up buildx does so twice: once with
    `continue-on-error`, then — only if that failed — after a pause. A job that drops the
    retry is back to one flaky pull deciding main's colour."""
    for name in ("build", "publish"):
        steps = job(IMAGES, name)["steps"]
        setups = [i for i, s in enumerate(steps) if str(s.get("uses", "")).startswith("docker/setup-buildx-action@")]
        assert len(setups) == 2, f"{name} sets up buildx {len(setups)} time(s); expected a first try and one retry"
        first, retry = (steps[i] for i in setups)
        assert first.get("id") == "buildx" and first.get("continue-on-error") is True, (
            f"{name}'s first buildx setup must be `id: buildx` with continue-on-error, or its failure ends the job"
        )
        assert retry.get("if") == "steps.buildx.outcome == 'failure'", (
            f"{name}'s retry runs {retry.get('if')!r}; it must run only when the first try failed"
        )
        pause = steps[setups[1] - 1]
        assert pause.get("if") == "steps.buildx.outcome == 'failure'" and "sleep" in str(pause.get("run", "")), (
            f"{name} retries without a pause — an immediate retry meets the same timeout"
        )


@pytest.mark.parametrize("excluded_root", _MIRROR_ONLY_EXCLUDED_ROOTS)
def test_every_mirror_only_root_entry_is_scanned_through_its_source(excluded_root: str) -> None:
    """Each of these roots is excluded on one premise: everything in it is swept through
    another path. An entry is a symlink that resolves into one of QLIB_SCAN_ROOTS, or a
    published copy: the shared rule source writes `<root>/skills/<path>` as a byte copy of
    `skills/<path>` (dev_env #319, #1336). This asserts the premise, in those terms.

    Without it the exclusion is a hole waiting for its first real file, which is how both of
    this guard's previous holes were shaped -- a place the walk could not reach, found by
    review rather than by the guard. Confirmed red by pointing one of the symlinks at a
    directory outside the scan roots, by changing one byte of a copy, by adding a copy that
    has no source, and by adding a file outside `<root>/skills/`.
    """
    assert ".agents" in QLIB_SCAN_ROOTS, (
        "published copies are swept through .agents/skills/, so .agents must stay a scan root"
    )
    listing = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z", "--", excluded_root],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    entries = [entry for entry in listing.split("\0") if entry]
    assert entries, (
        f"{excluded_root}/ is excluded from the Qlib sweep but git tracks nothing "
        f"there, so this assertion is checking nothing (GREEN-WHILE-EMPTY). Remove the exclusion "
        f"or remove this test."
    )

    scanned = {(REPO_ROOT / root).resolve() for root in QLIB_SCAN_ROOTS}
    copy_prefix = f"{excluded_root}/skills/"
    escapes: list[str] = []
    for entry in entries:
        path = REPO_ROOT / entry
        if path.is_symlink():
            target = path.resolve()
            if not any(target == root or root in target.parents for root in scanned):
                escapes.append(f"{entry} -> {target} resolves outside every scanned root")
            continue
        source = REPO_ROOT / ".agents" / "skills" / entry[len(copy_prefix) :] if entry.startswith(copy_prefix) else None
        if source is None or not source.is_file() or source.read_bytes() != path.read_bytes():
            escapes.append(f"{entry} is a real file that no file under .agents/skills/ repeats byte for byte")
    assert not escapes, (
        f"{excluded_root}/ is excluded from the Qlib sweep only because everything "
        f"in it is scanned through its source. These are not: {escapes}. Either move them under "
        f"a scan root or move {excluded_root} into QLIB_SCAN_ROOTS."
    )


def test_the_publish_record_root_holds_only_the_record() -> None:
    """The publish record's directory is excluded because it holds one JSON file that
    names other files and runs nothing. A script written there later would sit outside
    the sweep with nothing to say so; this asserts the premise instead of trusting it.
    """
    listing = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z", "--", _RECORD_ONLY_EXCLUDED_ROOT],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    entries = sorted(entry for entry in listing.split("\0") if entry)
    assert entries == [f"{_RECORD_ONLY_EXCLUDED_ROOT}/manifest.json"], (
        f"{_RECORD_ONLY_EXCLUDED_ROOT}/ is excluded from the Qlib sweep only because it holds the "
        f"publish record and nothing else; git tracks {entries}. Scan what was added, or remove "
        f"the exclusion."
    )


def test_gates_runs_complexity_gate() -> None:
    """The gates job in ci-python.yml must execute tools/complexity_gate.py --check."""
    workflow = yaml.safe_load(source(PYTHON))
    gate_steps = [str(s.get("run", "")) for s in workflow["jobs"]["gates"]["steps"]]
    assert any("tools/complexity_gate.py --check" in s for s in gate_steps), (
        "ci-python.yml gates job must run tools/complexity_gate.py --check"
    )


def test_test_core_checks_out_full_git_history_for_release_tag_checks() -> None:
    """#1412 R4: `test_release_replays.py` runs inside `test-core` and compares HEAD against the
    newest reachable `vX.Y.Z` release tag. A shallow `fetch-depth: 1` checkout has no tags."""
    checkout = next(
        spec for spec in job(PYTHON, "test-core")["steps"] if str(spec.get("uses", "")).startswith("actions/checkout")
    )
    assert checkout.get("with", {}).get("fetch-depth") == 0, (
        "ci-python.yml test-core must check out full history (`fetch-depth: 0`) so release-tag replay checks see vX.Y.Z tags"
    )


def test_ci_python_survives_a_github_504_on_the_infra2_wheel() -> None:
    """#1248: each ci-python job that runs uv sync gets uv from the retrying shared action first.

    uv.lock pins the infra2-sdk wheel by URL. uv ignores UV_FIND_LINKS for such a wheel, so a
    cache of the wheel did not stop the 504 failure, and its pre-step added a second download.
    The retry now lives in .github/actions/setup-uv, which is the only place that sets it.
    """
    synced = []
    for job_name, job_spec in yaml.safe_load(source(PYTHON))["jobs"].items():
        job_steps = job_spec.get("steps", [])
        sync_at = next((i for i, s in enumerate(job_steps) if "uv sync" in str(s.get("run", ""))), None)
        if sync_at is None:
            continue
        synced.append(job_name)
        assert any(s.get("uses") == "./.github/actions/setup-uv" for s in job_steps[:sync_at]), (
            f"ci-python.yml job {job_name} runs uv sync without ./.github/actions/setup-uv before it: "
            f"the wheel download then has only the 3 default retries of uv"
        )
        removed = [
            s.get("name") or s.get("uses") or s.get("run")
            for s in job_steps
            if any(mark in spec_text(s) for mark in ("infra2_wheel_cache", "UV_FIND_LINKS", ".cache/infra2-sdk"))
        ]
        assert not removed, (
            f"ci-python.yml job {job_name} has the wheel cache steps {removed} again. uv ignores "
            f"UV_FIND_LINKS for a wheel that uv.lock pins by URL, so these steps do not stop a 504"
        )
    # GREEN-WHILE-EMPTY guard: four ci-python jobs ran uv sync when #1248 was fixed.
    assert len(synced) >= 4, f"only {synced} run uv sync; four jobs did when #1248 was fixed"


# --- #1412 L1: CI runs the make targets the laptop runs -----------------------

#: (workflow, job, exact `run` line). One definition of each check: tools/diff_scope.py
#: behind the Makefile. The Makefile once named libs/, which no longer existed, and
#: nothing noticed, because CI defined lint separately and never ran the Makefile.
CI_MAKE_TARGETS = (
    (PYTHON, "gates", "make lint FULL=1"),
    (PYTHON, "gates", "make typecheck-python FULL=1"),
    (PYTHON, "gates", "make contract-conformance FULL=1"),
    (WEB, "check", "make typecheck-web FULL=1"),
)

#: Commands that only the Makefile may spell out. A CI step that runs one of them
#: directly is a second definition that can drift from what the laptop runs.
DIRECT_COMMANDS = (
    "ruff check",
    "ruff format",
    "uv run mypy",
    "bun run typecheck",
    "export_issue58.py",
    "uv run pytest",
)


@pytest.mark.parametrize(("workflow", "job_id", "command"), CI_MAKE_TARGETS)
def test_ci_runs_the_make_target_the_laptop_runs(workflow: str, job_id: str, command: str) -> None:
    runs = [str(spec.get("run", "")).strip() for spec in job(workflow, job_id).get("steps", [])]
    assert command in runs, f"{workflow}:{job_id} does not run `{command}`; it runs {runs}"


def test_every_test_shard_and_the_web_tests_run_through_make() -> None:
    shard_jobs = [job_id for job_id, spec in load(PYTHON)["jobs"].items() if "pytest_shard.py" in str(spec)]
    assert len(shard_jobs) >= 2, f"expected the core and data-engine shard jobs, found {shard_jobs}"
    for job_id in shard_jobs:
        text = "\n".join(spec_text(spec) for spec in job(PYTHON, job_id)["steps"])
        assert 'make test-python FULL=1 FILES="${FILES[*]}"' in text, f"{PYTHON}:{job_id} runs its shard without make"
    web_tests = spec_text(job_step(WEB, "check", "Run tests (DB-backed tests armed)"))
    assert "make test-web FULL=1" in web_tests, web_tests


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


@pytest.mark.parametrize(
    "target", sorted({command.split()[1] for _, _, command in CI_MAKE_TARGETS} | {"test-python", "test-web"})
)
def test_each_ci_make_target_runs_the_whole_repository_with_full(target: str) -> None:
    env = {**os.environ, "DIFF_SCOPE_DRY_RUN": "1"}
    env.pop("BASE", None)
    result = subprocess.run(
        ["make", "-s", target, "FULL=1"], cwd=REPO_ROOT, capture_output=True, text=True, env=env, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "diff-scope: FULL=1, whole repository" in result.stdout, result.stdout
    planned = [line for line in result.stdout.splitlines() if line.startswith("== [")]
    assert planned, f"`make {target} FULL=1` planned no command: {result.stdout}"
    for line in planned:
        command = line.split("] ", 1)[1].split("  (in ", 1)[0].split()
        if command[:4] == ["uv", "run", "ruff", "check"]:
            missing = [p for p in command[4:] if not (REPO_ROOT / p).is_dir()]
            assert not missing, f"`make {target} FULL=1` lints paths that do not exist: {missing} (the libs/ rot)"


def test_a_job_that_installs_a_playwright_browser_does_not_ride_the_moving_runner_label() -> None:
    """On 2026-10-21 `ubuntu-latest` moved to Ubuntu 26.04. Playwright 1.58.2 cannot install
    chromium there ("does not support chromium on ubuntu26.04-x64"), so the release walk of
    production v0.2.30 and of staging v0.2.32 failed before it touched the app. A job that
    installs a browser or its system dependencies pins `ubuntu-24.04` as a plain string. The
    test also names the jobs it expects to find, so a renamed command cannot empty the scan."""
    installs = re.compile(r"playwright\s+(i|install)\b|--with-deps|apt(-get)?\s+install")
    found = {}
    for path in sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml")):
        for job_id, spec in yaml.safe_load(path.read_text())["jobs"].items():
            if any(installs.search(str(item.get("run", ""))) for item in spec.get("steps", [])):
                found[f"{path.name}:{job_id}"] = spec.get("runs-on")
    assert set(found) == {"ci-web.yml:browser"}, found
    assert {job: runner for job, runner in found.items() if runner != "ubuntu-24.04"} == {}, found


def test_preview_and_staging_gate_share_one_check_list() -> None:
    """#1692: preview_gate and staging_gate run the same checks in the same order."""
    from infra2_sdk.manifests import load_hooks_manifest

    hooks = load_hooks_manifest(REPO_ROOT / "runtime-hooks.manifest.json")
    assert hooks.slots["preview_gate"].checks, "preview_gate must list its checks"
    assert hooks.slots["preview_gate"].checks == hooks.slots["staging_gate"].checks


def _run_slots_with_fake_runner(tmp_path: Path, fail_argv: tuple[str, ...] | None) -> dict[str, tuple[dict, list[str]]]:
    """Run each manifest slot with a fake runner; return the last checks.jsonl record and the argv log."""
    import json
    import subprocess

    from infra2_sdk.manifests import load_hooks_manifest, run_hook_slot

    hooks = load_hooks_manifest(REPO_ROOT / "runtime-hooks.manifest.json")
    env = {key: "x" for spec in hooks.slots.values() for key in spec.required_env}
    out: dict[str, tuple[dict, list[str]]] = {}
    for slot, spec in hooks.slots.items():
        ran: list[str] = []

        def fake(argv, **_kw):  # type: ignore[no-untyped-def]
            ran.append(argv[-1])
            if fail_argv is not None and tuple(argv) == fail_argv:
                return subprocess.CompletedProcess(argv, 1, "", "boom: first line\nsecond line")
            return subprocess.CompletedProcess(argv, 0, "", "")

        cwd = tmp_path / slot
        cwd.mkdir()
        run_hook_slot(hooks, slot, app_url="https://fake.invalid", cwd=cwd, ambient_env=env, runner=fake)
        lines = (cwd / spec.artifacts_dir / "checks.jsonl").read_text().splitlines()
        out[slot] = (json.loads(lines[-1]), ran)
    return out


def test_every_hook_slot_writes_per_check_results_with_seconds(tmp_path: Path) -> None:
    """#1692 AC2: each slot reports at least one check id with seconds."""
    for slot, (record, _ran) in _run_slots_with_fake_runner(tmp_path, None).items():
        assert record["source"] == slot
        assert record["checks"], f"{slot} wrote no check"
        assert all(c["ok"] is True and isinstance(c["seconds"], float) for c in record["checks"]), slot


def test_a_failing_check_is_reported_and_stops_the_slot(tmp_path: Path) -> None:
    """#1692 AC2, AC3: the second check of staging_gate fails; the third does not run."""
    from infra2_sdk.manifests import load_hooks_manifest

    checks = load_hooks_manifest(REPO_ROOT / "runtime-hooks.manifest.json").slots["staging_gate"].checks
    assert len(checks) >= 3, "staging_gate needs three checks to show the stop"
    record, ran = _run_slots_with_fake_runner(tmp_path, tuple(checks[1].argv))["staging_gate"]
    assert [(c["name"], c["ok"]) for c in record["checks"]] == [(checks[0].id, True), (checks[1].id, False)]
    assert record["checks"][1]["detail"] == "boom: first line"
    assert checks[2].argv[-1] not in ran
