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
import re
import shlex
import subprocess
import sys
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from truealpha_runtime.testing import load_tool

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
job = _contract.job
job_step = _contract.job_step
source = _contract.source
step = _contract.step
steps = _contract.steps
step_text = _contract.step_text
spec_text = _contract.spec_text
triggers = _contract.triggers

REPO_ROOT = Path(__file__).resolve().parents[3]

RELEASE = "deploy-release.yml"
WALK = "walk-release.yml"
FRESHNESS = "deploy-freshness.yml"
REPROOF = "mutation-reproof.yml"
MAIN_HEALTH = "main-health.yml"
CLOSE_GUARD = "issue-close-guard.yml"
REQUIRED = "ci-required.yml"
PYTHON = "ci-python.yml"
DB = "ci-db.yml"
RUNTIME = "ci-runtime.yml"
NIGHTLY = "nightly-dagster-liveness.yml"
WEB = "ci-web.yml"
IMAGES = "release-images.yml"
LIVENESS = "scheduler-liveness.yml"
AUTO_RELEASE = "auto-release-staging.yml"


# --- the locator itself ------------------------------------------------------


def test_a_missing_step_names_what_it_was_looking_for() -> None:
    """The property that makes the rest of this file trustworthy: a renamed step
    fails loudly here instead of quietly re-pointing an assertion."""
    with pytest.raises(WorkflowContractError, match="has no step named"):
        step(RELEASE, "A step nobody wrote")


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
    # #731: a main push publishes only the images whose filter matched; a tag and
    # the manual force publish all three. Before this, every uv.lock or libs/**
    # merge republished app-web for a dependency it does not use.
    for image in ("app_web", "llm_service", "data_engine"):
        expression = str(release["with"][image])
        assert "github.ref_type == 'tag'" in expression, f"{image}: a tag must publish every image"
        assert "inputs.force_images" in expression, f"{image}: the manual force must publish every image"
        assert f"needs.changes.outputs.image_{image} == 'true'" in expression, (
            f"{image}: a main push must publish only what its filter matched"
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
    assert "github.ref_type == 'tag'" in str(build["if"])
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
        for clause in (
            f"needs.changes.outputs.image_{image} == 'true'",
            "github.ref_type == 'tag'",
            "inputs.force_images",
        ):
            assert clause in str(build["with"][image]) and clause in str(release["with"][image]), (
                f"{image}: images_build and images_release select images differently on {clause!r}"
            )

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


# --- deploy-release ----------------------------------------------------------


def test_the_release_dispatches_only_the_rendered_sdk_request() -> None:
    workflow = source(RELEASE)
    assert triggers(RELEASE)["workflow_dispatch"]["inputs"]["deploy_type"]["options"] == [
        "preview/tag",
        "staging",
        "prod",
    ]
    assert job(RELEASE, "request") is not None
    for clause in (
        'GITHUB_REF" != "refs/heads/main"',
        'rev-parse --verify --quiet "refs/tags/${VERSION_REF}^{commit}"',
        "version_ref must identify an existing commit tag",
        "merge-base --is-ancestor",
        '.path == ".github/workflows/ci-required.yml"',
        '.event == "push"',
        ".merge_commit_sha == $sha",
        '.base.ref == "main"',
        # infra2#571 blocker 2: staging evidence is this repo's OWN "Deploy
        # staging" run, matching infra2's verifier — never an infra2 receiver run.
        'this repo\'s own successful "Deploy staging <tag>" run URL',
    ):
        assert clause in workflow, f"the release request must still assert {clause!r}"


def test_the_health_gate_passes_the_kind_the_runtime_reports() -> None:
    """#526: the gate compared a 40-hex sha against a runtime that reports the
    release tag, so every prod release recorded FAILURE while deploying fine."""
    gate = step_text(RELEASE, "Confirm the deployed release is healthy")
    assert "version_ref" in gate, "the gate must compare the release ref the runtime reports"
    assert "source_sha" not in gate, "the gate must not pass a commit sha while the runtime reports a tag (#526)"


def test_the_release_no_longer_runs_the_surface_walk_itself() -> None:
    """#855/#860: the walk moved to its own deferred workflow so a walk flake
    (#811) costs a walk re-run, not a re-dispatch of the whole 5-6 min infra2
    deploy. deploy-release.yml's own green must no longer depend on it."""
    workflow = source(RELEASE)
    for name in (
        "Are the surface-walk credentials configured",
        "Cache the Playwright browser",
        "Walk the deployed surface",
    ):
        with pytest.raises(WorkflowContractError, match="has no step named"):
            step(RELEASE, name)
    assert "walk-release.yml" in workflow, "the release run must point at where the walk actually lives now"


def test_the_release_leaves_an_honest_but_non_blocking_walk_signal_for_prod() -> None:
    """#855/#860: `staging_run_url` used to prove BOTH "staging deployed" and
    "staging was walked", because the walk ran inside that same run. Now that
    the walk is a separate, asynchronously-triggered workflow, the prod gate's
    hard requirement (a green "Deploy staging <tag>" run) can no longer prove
    the second half — and must not be turned into a hard requirement either,
    or a direct `workflow_dispatch` to prod deadlocks exactly the way #560
    describes the moment the walk workflow has not fired yet.

    Asserted as one exact, verbatim block rather than a fragile split-and-scan
    (the reason `tests/workflow_contract.py` exists at all, #583): if a later
    edit turns this into a hard failure, the literal text below no longer
    matches and this test breaks loudly, naming exactly what moved.
    """
    dispatch = step_text(RELEASE, "Dispatch the validated request to infra2")
    assert (
        "if uv run --no-sync python tools/walk_evidence.py \\\n"
        '      --deploy-type staging --release "$version_ref" >walk_note.log 2>&1; then\n'
        "    cat walk_note.log\n"
        "  else\n"
        '    echo "::warning::promoting ${version_ref} to prod without confirmed staging '
        'walk evidence: $(tail -n1 walk_note.log)"\n'
        '    cat walk_note.log >>"$GITHUB_STEP_SUMMARY"\n'
        "  fi"
    ) in dispatch, "the staging-walk check for a prod promotion must be an ::warning::, never an ::error:: exit"


def test_the_surface_walk_examines_every_credential_it_uses() -> None:
    """#560: the member pass needs TA_MEMBER_EMAIL; a probe that checks two of
    three secrets lets the walk start and fail later, less clearly."""
    probe = step_text(WALK, "Are the surface-walk credentials configured")
    for variable in ("TA_EMAIL", "TA_PASSWORD", "TA_MEMBER_EMAIL"):
        assert f'"${{{variable}}}"' in probe, f"{variable} must be examined before the walk runs"


def test_an_unconfigured_walk_is_skipped_rather_than_exiting_zero() -> None:
    """#560: a step that exits 0 without walking reports `success`, which
    tools/walk_evidence.py reads as evidence of a walk that never ran."""
    walk = step(WALK, "Walk the deployed surface")
    assert walk["if"] == "${{ steps.walk_credentials.outputs.ready == 'true' }}", (
        "an unconfigured walk must be SKIPPED, never a step that exits 0 and reports success"
    )
    assert "exit 0" not in str(walk.get("run", ""))


def test_the_release_lane_is_not_deadlocked_by_a_missing_secret() -> None:
    """#560: failing the run on unconfigured credentials blocked every prod
    release, since prod requires a successful staging run."""
    probe = str(step(WALK, "Are the surface-walk credentials configured")["run"])
    assert "ready=false" in probe, "an unconfigured walk must be reported"
    assert "exit 1" not in probe, "and must not fail the release lane"
    assert "UNVERIFIED" in probe, "but must be unmistakable in the run summary"


def test_the_walk_only_runs_after_a_successful_deploy_or_by_hand() -> None:
    """#855/#860: a failed or cancelled deploy-release run has nothing live to
    walk. The workflow_run trigger fires on EVERY conclusion (success, failure,
    cancelled...), so the job itself must gate on success; workflow_dispatch is
    the deliberate manual exception (the #811 flake recovery)."""
    trigger = triggers(WALK)
    assert trigger["workflow_run"]["workflows"] == ["Deploy release"]
    assert trigger["workflow_run"]["types"] == ["completed"]
    dispatch_inputs = trigger["workflow_dispatch"]["inputs"]
    assert dispatch_inputs["deploy_type"]["options"] == ["staging", "prod"]
    assert "version_ref" in dispatch_inputs

    resolve = job(WALK, "resolve")
    condition = str(resolve["if"])
    assert "github.event_name == 'workflow_dispatch'" in condition
    assert "github.event.workflow_run.conclusion == 'success'" in condition

    walk_job = job(WALK, "walk")
    assert walk_job["needs"] == "resolve"
    assert "needs.resolve.outputs.applicable == 'true'" in str(walk_job["if"]), (
        "a run that could not resolve a staging/prod deploy_type+version_ref must not attempt to walk anything"
    )


def test_the_walk_checks_out_the_released_tag_not_whatever_main_has_become() -> None:
    """The tag is what deploy-release.yml actually built and deployed; main can
    advance in the minutes this workflow waits to be triggered."""
    checkout = next(
        spec for spec in job(WALK, "walk")["steps"] if str(spec.get("uses", "")).startswith("actions/checkout")
    )
    assert checkout["with"]["ref"] == "refs/tags/${{ needs.resolve.outputs.version_ref }}"


def test_the_sender_and_the_contract_agree_on_whose_staging_run_counts() -> None:
    """infra2#571 blocker 2: the sender required an infra2 receiver-run URL while
    infra2's verifier required this repo's own staging run."""
    cli = (REPO_ROOT / "tools/app_deploy_request.py").read_text(encoding="utf-8")
    assert '_STAGING_RUN_PATH_RE = re.compile(r"\\A/wangzitian0/truealpha/actions/runs/' in cli
    workflow = source(RELEASE)
    assert "https://github.com/wangzitian0/truealpha/actions/runs/" in workflow
    assert 'staging_run="$(gh api "/repos/wangzitian0/infra2/actions/runs/' not in workflow


# --- deploy-freshness --------------------------------------------------------


def test_the_scheduled_gate_does_not_use_the_dispatch_inputs_context() -> None:
    """#560: `inputs` belongs to workflow_dispatch and workflow_call; this
    workflow's PRIMARY trigger is the schedule. A guard whose own expression can
    fail on its main trigger is the failure mode it exists to prevent."""
    assert "schedule" in triggers(FRESHNESS), "the freshness gate must not be on-demand only"
    for name in ("Check ${{ matrix.environment }} freshness",):
        text = step_text(FRESHNESS, name)
        assert "github.event.inputs.max_age_days" in text
        assert "${{ inputs." not in text, "the inputs context is absent on a schedule event"


def test_the_freshness_bound_is_per_environment() -> None:
    """#819: every tag soaks staging while production is promoted only by
    `cut_release.sh --prod`, so one bound for both legs had production red every
    day for the lag the release protocol asks for. The matrix carries the bound,
    the step reads the matrix's value, and the numbers are the ones the tool and
    the release script document — three files that must not drift."""
    tool = load_tool("deploy_freshness")
    matrix = {entry["environment"]: entry for entry in job(FRESHNESS, "freshness")["strategy"]["matrix"]["include"]}
    assert matrix["staging"]["max_age_days"] == tool.STAGING_MAX_AGE_DAYS
    assert matrix["production"]["max_age_days"] == tool.PRODUCTION_MAX_AGE_DAYS
    assert matrix["production"]["max_age_days"] > matrix["staging"]["max_age_days"], (
        "production lags staging by design; a bound at or below staging's makes deliberate promotion red"
    )
    text = step_text(FRESHNESS, "Check ${{ matrix.environment }} freshness")
    assert "matrix.max_age_days" in text, "the step no longer reads the bound from the matrix"
    # The dispatch input overrides BOTH legs; a non-empty default would flatten
    # production's bound back to staging's on every manual run.
    dispatch = triggers(FRESHNESS)["workflow_dispatch"]["inputs"]["max_age_days"]
    assert not dispatch.get("default"), "a defaulted dispatch input overrides the matrix bounds on every manual run"
    script = (REPO_ROOT / "tools" / "cut_release.sh").read_text(encoding="utf-8")
    for phrase in (f"staging {tool.STAGING_MAX_AGE_DAYS} days", f"production {tool.PRODUCTION_MAX_AGE_DAYS} days"):
        assert phrase in script, f"cut_release.sh's policy header does not say {phrase!r}; the two files disagree"


def test_the_governed_pointer_bound_is_the_tools_and_runs_on_both_legs() -> None:
    """The pointer freshness step reads its bound from the matrix, the matrix carries
    the tool's number for both environments, and the step runs even after an earlier
    failure — a frozen head and a stale release are separate facts."""
    tool = load_tool("datahub_freshness")
    matrix = {entry["environment"]: entry for entry in job(FRESHNESS, "freshness")["strategy"]["matrix"]["include"]}
    for environment in ("staging", "production"):
        assert matrix[environment]["pointer_max_age_hours"] == tool.MAX_AGE_HOURS
    name = "Check ${{ matrix.environment }} governed pointer freshness"
    text = step_text(FRESHNESS, name)
    assert "matrix.pointer_max_age_hours" in text and "tools/datahub_freshness.py" in text
    step = next(s for s in job(FRESHNESS, "freshness")["steps"] if s.get("name") == name)
    assert step.get("if") == "always()"


def test_the_nightly_verdicts_are_bounded_on_both_legs_after_the_pointer() -> None:
    """#876: the Dagster nightly checks page through this step, so it must run on every leg,
    even after an earlier step failed, read the leg's own health endpoint, and resolve the
    expected checks in the checkout that holds the release tags; and the escalation that
    files the issue must still come after it."""
    steps = [s.get("name") for s in job(FRESHNESS, "freshness")["steps"]]
    name = "Check ${{ matrix.environment }} nightly check verdicts"
    pointer = "Check ${{ matrix.environment }} governed pointer freshness"
    escalate = "Escalate a scheduled failure to an issue"
    assert steps.index(pointer) < steps.index(name) < steps.index(escalate)
    text = step_text(FRESHNESS, name)
    assert "tools/nightly_verdicts.py" in text and "matrix.url" in text and "--repo ." in text
    # The grace for a newly introduced check reads deploy-release runs by their run-name word.
    assert '--deploy-type "${{ matrix.deploy_type }}"' in text, "without it a new check is red until its first tick"
    assert "--environment" in text
    step = next(s for s in job(FRESHNESS, "freshness")["steps"] if s.get("name") == name)
    assert step.get("env", {}).get("GH_TOKEN") == "${{ github.token }}", "the deploy-run lookup needs the token"
    assert step.get("if") == "always()", "a stale release must not hide a red nightly check"
    checkout = next(
        s for s in job(FRESHNESS, "freshness")["steps"] if str(s.get("uses", "")).startswith("actions/checkout")
    )
    assert checkout["with"]["fetch-depth"] == 0, "the expected checks are read at the deployed tag"


def test_the_evidence_check_passes_a_deploy_type_the_release_can_produce() -> None:
    """#560: the release run-name is built from `deploy_type` ("prod") while the
    matrix names environments for humans ("production"). Passing the latter looks
    for runs that can never exist — red forever, for an unrelated reason."""
    allowed = set(triggers(RELEASE)["workflow_dispatch"]["inputs"]["deploy_type"]["options"])
    passed = set(re.findall(r"deploy_type:\s*(\S+)", source(FRESHNESS)))
    assert passed, "the freshness matrix must carry a deploy_type per environment"
    assert passed <= allowed, (
        f"freshness passes {sorted(passed - allowed)}, which the release run-name can never "
        f"produce (it accepts {sorted(allowed)})"
    )


def test_the_release_identity_is_read_by_the_tool_not_by_shell() -> None:
    """#585: `curl | jq -r '.git_sha'` was a fourth implementation of the health
    read and the only one that validated nothing — on "unknown" it produced a
    true sentence about the wrong question."""
    text = step_text(FRESHNESS, "Check ${{ matrix.environment }} surface-walk evidence")
    assert "--url" in text, "the tool reads the identity, with the validation the shell had none of"
    assert "jq -r '.git_sha'" not in text


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


def _decides(condition: object, github: dict, *, succeeded: bool = True) -> bool:
    """Evaluate a job/step `if:` the way the runner would, for the operators these
    watchdog conditions use: `&&`, `||`, `!`, `==`, `!=`, string literals and the
    status functions. Anything else is refused rather than guessed at.

    Asserting substrings of a condition proves the text; evaluating it proves the
    decision — a PR run, a branch dispatch or a loosened override must come out
    False however the expression is spelled. A missing `if:` is `true`, which is
    what the runner does with it.
    """
    expression = str(condition).strip() if condition is not None else "true"
    if expression.startswith("${{") and expression.endswith("}}"):
        expression = expression[3:-2].strip()
    functions = set(re.findall(r"([A-Za-z_]+)\(", expression))
    assert functions <= {"success", "failure", "always", "cancelled"}, f"unsupported functions {functions}"
    assert not re.search(r"!\s*\(", expression), "negating a group is not supported by this evaluator"
    # `!x` binds tighter than `==` in GitHub's grammar and looser in Python's;
    # parenthesising the operand keeps GitHub's meaning.
    python = re.sub(r"!(?!=)\s*([A-Za-z_][\w.]*)", r"(not \1)", expression)
    python = python.replace("&&", " and ").replace("||", " or ")
    namespace = {
        "github": _Context(github),
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


def test_the_freshness_alert_opens_on_red_and_closes_on_a_scheduled_green() -> None:
    """#876 W3: #818 stayed open two days after staging was fresh again, because
    the escalation commented and never closed. Both halves go through the tool,
    under one per-environment title (#687/#688: the legs run in parallel).

    The resolve guard is the load-bearing part: a dispatch with `max_age_days`
    set is a red-proof or an experiment, and a looser override would be green
    while the scheduled bound is still red — closing a live alert. So is a
    dispatch on a branch, whose matrix may carry other bounds.
    """
    title = str(job(FRESHNESS, "freshness")["env"]["ESCALATION_TITLE"])
    assert title == "deploy-freshness is red: ${{ matrix.environment }} failed its standing checks", (
        f"the freshness title changed to {title!r}: an open issue under the old title is never closed, and a "
        f"title without the environment lets the parallel legs share one issue (#687/#688)"
    )
    escalate = step(FRESHNESS, "Escalate a scheduled failure to an issue")
    resolve = step(FRESHNESS, "Resolve the issue once the checks are green")
    _assert_calls_the_tool(escalate, "open", "freshness escalate")
    _assert_calls_the_tool(resolve, "resolve", "freshness resolve")
    assert "pull_request" not in triggers(FRESHNESS), "the workflow-level issues grant assumes no PR-triggered code"

    main = "refs/heads/main"
    closes = [
        ({"event_name": "schedule", "ref": main}, "the scheduled run"),
        (
            {"event_name": "workflow_dispatch", "ref": main, "event": {"inputs": {"max_age_days": ""}}},
            "a dispatch at the matrix bounds",
        ),
        (
            {"event_name": "workflow_dispatch", "ref": main, "event": {"inputs": {}}},
            "a dispatch with the input omitted",
        ),
    ]
    for github, what in closes:
        assert _decides(resolve["if"], github), f"{what} is green at the scheduled bounds and must close the alert"
        assert not _decides(resolve["if"], github, succeeded=False), f"a red {what} closed the alert"
        assert _decides(escalate["if"], github, succeeded=False), f"a red {what} no longer escalates"
        assert not _decides(escalate["if"], github), f"a green {what} escalates"
    never = [
        (
            {"event_name": "workflow_dispatch", "ref": main, "event": {"inputs": {"max_age_days": "30"}}},
            "a looser override",
        ),
        (
            {"event_name": "workflow_dispatch", "ref": main, "event": {"inputs": {"max_age_days": "0"}}},
            "a red-proof override",
        ),
        (
            {"event_name": "workflow_dispatch", "ref": "refs/heads/feature", "event": {"inputs": {}}},
            "a branch dispatch",
        ),
        ({"event_name": "schedule", "ref": "refs/heads/feature"}, "a run off main"),
        ({"event_name": "pull_request", "ref": "refs/pull/1/merge"}, "a pull_request run"),
        ({"event_name": "push", "ref": main}, "a push run"),
    ]
    for github, what in never:
        assert not _decides(resolve["if"], github), (
            f"{what} can close the freshness alert while the scheduled bound may still be red"
        )


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


# --- scheduler-liveness (#876 W5) ----------------------------------------------

# The estate the watchdog of watchdogs covers. Every repository here runs, or
# may run, a scheduled check; dropping one from the workflow's list makes its
# dead schedules invisible again.
ESTATE_REPOS = {
    "wangzitian0/truealpha",
    "wangzitian0/infra2",
    "wangzitian0/finance_report",
    "wangzitian0/infra2-sdk",
}
LIVENESS_CHECK = "Check every scheduled workflow in the estate is still ticking"


def _liveness_arguments() -> list[str]:
    """The tool's command line in the check step, with shell continuations joined."""
    run = str(step(LIVENESS, LIVENESS_CHECK)["run"]).replace("\\\n", " ")
    lines = [line for line in run.splitlines() if "tools/scheduler_liveness.py" in line]
    assert len(lines) == 1, f"expected one scheduler_liveness.py call, found {lines}"
    tokens = shlex.split(lines[0].split("|", 1)[0])
    return tokens[tokens.index("tools/scheduler_liveness.py") + 1 :]


def test_scheduler_liveness_covers_the_whole_estate_on_its_own_schedule() -> None:
    """#876 W5: the check runs on a schedule and by hand, never on a PR (it holds
    `issues: write` at workflow level), over all four repositories, reading the
    runs listings it needs and nothing more."""
    on = triggers(LIVENESS)
    assert set(on) == {"schedule", "workflow_dispatch"}, f"scheduler-liveness triggers on {sorted(on)}"
    workflow = yaml.safe_load(source(LIVENESS))
    assert workflow["permissions"] == {"contents": "read", "actions": "read", "issues": "write"}
    assert "permissions" not in job(LIVENESS, "liveness"), "a job-level grant would replace the pinned one"
    assert workflow["concurrency"]["cancel-in-progress"] is False, "an open and a close must not cancel each other"

    # Every positional argument is a repository, and together they are the
    # estate: nothing else narrows the run.
    positional = [argument for argument in _liveness_arguments() if not argument.startswith("-")]
    positional.remove("$BOUND_CAP_HOURS")
    assert sorted(positional) == sorted(ESTATE_REPOS), (
        f"scheduler-liveness checks {sorted(positional)}; the estate is {sorted(ESTATE_REPOS)} — a repository "
        f"missing here has its dead schedules invisible again"
    )

    tool = load_tool("scheduler_liveness")
    crons = [tool.Cron.parse(expression) for expression in tool.schedule_of(source(LIVENESS))]
    gap = tool.largest_gap(crons, datetime.now(UTC))
    assert gap == timedelta(hours=6), f"scheduler-liveness ticks every {gap}, not every 6 hours"
    assert all(0 not in cron.minutes for cron in crons), "top-of-hour schedules are the ones GitHub drops first"


def test_scheduler_liveness_passes_the_cap_through_env_and_installs_what_it_reads_with() -> None:
    """The dispatch input reaches the tool through env (never spliced into the
    shell), under the context that exists on a schedule event, with no default
    that would cap every manual run."""
    check = step(LIVENESS, LIVENESS_CHECK)
    assert check["env"]["BOUND_CAP_HOURS"] == "${{ github.event.inputs.bound_cap_hours }}"
    assert check["env"]["GH_TOKEN"] == "${{ github.token }}"
    run = str(check["run"])
    assert "${{" not in run, "an expression is interpolated into the shell; pass it through env"
    assert "set -euo pipefail" in run, "without pipefail the tee decides the step's verdict, not the tool"
    arguments = _liveness_arguments()
    assert arguments[arguments.index("--bound-cap-hours") + 1] == "$BOUND_CAP_HOURS"
    dispatch = triggers(LIVENESS)["workflow_dispatch"]["inputs"]["bound_cap_hours"]
    assert not dispatch.get("default"), "a defaulted cap would apply to every manual run, including the close"
    # The tool reads workflow files with PyYAML; the sync must install it.
    steps = job(LIVENESS, "liveness")["steps"]
    sync = [str(item.get("run", "")) for item in steps if "uv sync" in str(item.get("run", ""))]
    assert sync == ["uv sync --package truealpha-runtime --frozen"], sync
    runtime = (REPO_ROOT / "libs/runtime/pyproject.toml").read_text(encoding="utf-8")
    assert '"pyyaml>=' in runtime, "truealpha-runtime no longer declares PyYAML; the liveness sync would not install it"


def test_the_liveness_alert_opens_on_red_and_closes_only_at_the_measured_bounds() -> None:
    """The deploy-freshness lifecycle, with the cap in place of `max_age_days`: a
    capped dispatch is a drill, and must never close a real alert."""
    assert job(LIVENESS, "liveness")["env"]["ESCALATION_TITLE"] == (
        "scheduler-liveness is red: a scheduled workflow stopped ticking"
    ), "an open issue under an old title is never closed"
    escalate = step(LIVENESS, "Escalate a scheduled failure to an issue")
    resolve = step(LIVENESS, "Resolve the issue once every scheduler is ticking")
    _assert_calls_the_tool(escalate, "open", "liveness escalate")
    _assert_calls_the_tool(resolve, "resolve", "liveness resolve")

    main = "refs/heads/main"
    closes = [
        ({"event_name": "schedule", "ref": main}, "the scheduled run"),
        (
            {"event_name": "workflow_dispatch", "ref": main, "event": {"inputs": {"bound_cap_hours": ""}}},
            "a dispatch at the measured bounds",
        ),
        ({"event_name": "workflow_dispatch", "ref": main, "event": {"inputs": {}}}, "a dispatch without the input"),
    ]
    for github, what in closes:
        assert _decides(resolve["if"], github), f"{what} is green at the measured bounds and must close the alert"
        assert not _decides(resolve["if"], github, succeeded=False), f"a red {what} closed the alert"
        assert _decides(escalate["if"], github, succeeded=False), f"a red {what} no longer escalates"
        assert not _decides(escalate["if"], github), f"a green {what} escalates"
    never = [
        (
            {"event_name": "workflow_dispatch", "ref": main, "event": {"inputs": {"bound_cap_hours": "0"}}},
            "the drill",
        ),
        (
            {"event_name": "workflow_dispatch", "ref": main, "event": {"inputs": {"bound_cap_hours": "999"}}},
            "a capped dispatch",
        ),
        (
            {"event_name": "workflow_dispatch", "ref": "refs/heads/feature", "event": {"inputs": {}}},
            "a branch dispatch",
        ),
        ({"event_name": "schedule", "ref": "refs/heads/feature"}, "a run off main"),
        ({"event_name": "pull_request", "ref": "refs/pull/1/merge"}, "a pull_request run"),
        ({"event_name": "push", "ref": main}, "a push run"),
    ]
    for github, what in never:
        assert not _decides(resolve["if"], github), f"{what} can close the scheduler-liveness alert"


def test_every_schedule_in_this_repository_is_one_the_liveness_check_can_read() -> None:
    """The live run reads every repository's files; this one's are checked at
    review time too, so a cron the tool cannot read fails here before it turns
    the watchdog red on main."""
    tool = load_tool("scheduler_liveness")
    now = datetime.now(UTC)
    scheduled = {}
    for path in sorted((REPO_ROOT / ".github/workflows").glob("*.yml")):
        expressions = tool.schedule_of(path.read_text(encoding="utf-8"))
        if expressions is not None:
            scheduled[path.name] = tool.largest_gap([tool.Cron.parse(item) for item in expressions], now)
    assert {FRESHNESS, REPROOF, NIGHTLY, LIVENESS} <= set(scheduled), sorted(scheduled)


# --- issue-close-guard -------------------------------------------------------


def test_the_close_guard_can_reopen_and_sees_full_history() -> None:
    """#562: two ways this ships inert — no `issues: write`, or a shallow
    checkout that makes every commit look unreleased."""
    workflow = source(CLOSE_GUARD)
    assert "issues: write" in workflow, "the guard cannot reopen anything without it"
    assert "fetch-depth: 0" in workflow and "fetch --tags" in workflow
    assert triggers(CLOSE_GUARD)["issues"]["types"] == ["closed"]


# --- auto-release-staging: the owner's 2026-09-17 decision ("先在 staging 做吧，
# prod 回头再说", #860) is that an automatic release is staging-only, full stop.
# `tools/auto_release.py`'s own decision logic is tested in test_auto_release.py
# (#583's boundary: a tool's behaviour is tested beside the tool, a workflow's
# shape is tested here); these tests are only about what this file can never
# be made to do.


def test_auto_release_never_writes_prod_anywhere_in_the_file() -> None:
    """The direct, textual proof: no STEP this workflow runs ever types the
    substring `--prod` — scoped to parsed step content (`run`/`if`/`env`/
    `with`/`uses`), not the surrounding comments that explain why, which
    necessarily name the very flag they forbid. `cut_release.sh` itself
    additionally refuses `--auto --prod` together (test_cut_release.py) — this
    is the other half of that defense: the automated path never types it."""
    offenders = [spec_text(spec) for spec in steps(AUTO_RELEASE) if "--prod" in spec_text(spec)]
    assert not offenders, offenders


def test_auto_release_triggers_only_after_a_green_push_to_main() -> None:
    """`branches: [main]` matches the head BRANCH NAME, which a pull_request
    from a fork's own `main` also carries (main-health.yml's own comment) —
    only a push event is a main commit, and only a green one may release."""
    trigger = triggers(AUTO_RELEASE)
    assert trigger["workflow_run"]["workflows"] == ["ci-required"]
    assert trigger["workflow_run"]["types"] == ["completed"]
    assert trigger["workflow_run"]["branches"] == ["main"]
    release_job = job(AUTO_RELEASE, "release")
    condition = str(release_job["if"])
    assert "github.event.workflow_run.event == 'push'" in condition
    assert "github.event.workflow_run.conclusion == 'success'" in condition


def test_auto_release_is_debounced_for_twenty_minutes_before_it_re_reads_head() -> None:
    """Owner constraint: a burst of merges batches into one release. The wait
    itself is a plain sleep; what makes it a DEBOUNCE rather than a blind delay
    is that `tools/auto_release.py` re-reads main HEAD from the GitHub API
    afterwards and defers to a later push's own run if one landed
    (test_auto_release.py's reason-1 tests) — this test only pins the
    workflow's own half: the wait is exactly 20 minutes, and it runs before the
    checkout that will see whatever landed during it."""
    quiet = step_text(AUTO_RELEASE, "Quiet period")
    assert "sleep 1200" in quiet, "1200s = 20 min; test_auto_release.py's tick-window tests use the same constant"
    release_steps = job(AUTO_RELEASE, "release")["steps"]
    names = [s.get("name") for s in release_steps]
    checkout_index = next(
        i for i, s in enumerate(release_steps) if str(s.get("uses", "")).startswith("actions/checkout")
    )
    assert names.index("Quiet period") < checkout_index, (
        "the checkout must happen AFTER the wait, or it cannot see a later push"
    )


def test_auto_release_checks_out_a_local_main_with_full_tag_history() -> None:
    """`cut_release.sh` runs `git rev-parse main` and reads every vX.Y.Z tag —
    a detached checkout or a shallow one makes both silently wrong."""
    checkout = next(
        spec
        for spec in job(AUTO_RELEASE, "release")["steps"]
        if str(spec.get("uses", "")).startswith("actions/checkout")
    )
    assert checkout["with"]["ref"] == "main"
    assert checkout["with"]["fetch-depth"] == 0


def test_auto_release_pushes_the_tag_with_a_real_user_token_not_github_token() -> None:
    """#940: v0.0.87's tag push ran under the default GITHUB_TOKEN, and
    GitHub's recursive-workflow guard silently drops any run-triggering event
    produced by a push made with that token — the tag was pushed, claimed its
    number, and 0 workflow runs ever started for it (measured: v0.0.86, pushed
    by a human, triggered `ci-required` normally the same day). A live probe
    on a throwaway branch/tag reproduced the same split before this fix
    landed: INFRA2_PAT push -> 1 downstream run, github.token push -> 0. The
    guard does not apply to a PAT (it is tied to a real user — `gh api user`
    resolves INFRA2_PAT to the repo owner, not github-actions[bot]), so the
    checkout must carry it: everything else in this job's git operations,
    including cut_release.sh's own `git push origin $TAG`, inherits whatever
    credential checkout configures.

    This only proves the workflow's SHAPE — that the field is wired — not
    that GitHub's live recursion guard behaves as documented; that half is
    platform behaviour no unit test can exercise (the same honest limitation
    #936 already flagged for this file) and was instead verified by the probe
    above, by hand, against the real repository."""
    checkout = next(
        spec
        for spec in job(AUTO_RELEASE, "release")["steps"]
        if str(spec.get("uses", "")).startswith("actions/checkout")
    )
    assert checkout["with"].get("token") == "${{ secrets.INFRA2_PAT }}", (
        "the tag push must run under a real-user credential, not this workflow's own "
        "GITHUB_TOKEN, or its push triggers nothing downstream (#940)"
    )


def test_auto_release_fails_closed_before_checkout_when_the_push_credential_is_missing() -> None:
    """A missing or rotated INFRA2_PAT must not silently fall through to a
    bare `actions/checkout` (which would default back to github.token,
    reintroducing #940) or fail later on checkout's own opaque auth error."""
    release_steps = job(AUTO_RELEASE, "release")["steps"]
    names = [s.get("name") for s in release_steps]
    guard_index = names.index("Require a push credential GitHub's own token cannot provide")
    checkout_index = next(
        i for i, s in enumerate(release_steps) if str(s.get("uses", "")).startswith("actions/checkout")
    )
    assert guard_index < checkout_index, "the guard must run before checkout can fall back to github.token"
    guard = release_steps[guard_index]
    assert "secrets.INFRA2_PAT" in spec_text(guard)
    assert "exit 1" in spec_text(guard)


def test_auto_release_reads_the_push_credential_from_env_not_inlined_in_the_script() -> None:
    """#941 review: the guard's secret check must come through `env:`, the
    same pattern deploy-release.yml's own INFRA2_PAT check already uses — not
    spliced directly into the `run:` block, which would put the secret
    reference inside the rendered shell text `${{ }}`-expanded rather than
    read from a shell variable."""
    guard = step(AUTO_RELEASE, "Require a push credential GitHub's own token cannot provide")
    assert guard.get("env", {}).get("INFRA2_PAT") == "${{ secrets.INFRA2_PAT }}"
    assert "secrets.INFRA2_PAT" not in str(guard.get("run", "")), (
        "the secret must be read from the env var, not interpolated directly into run:"
    )
    assert "INFRA2_PAT" in str(guard.get("run", "")), "the run: block must still read the env var it was given"


def test_auto_release_configures_a_git_identity_before_tagging() -> None:
    """No other workflow in this repository creates a `git commit` or `git tag
    -a` — they all go through `gh`, which needs no local identity. This is the
    first one that does (via `cut_release.sh`'s annotated tag), and a GitHub
    Actions runner carries no default `user.name`/`user.email`: an annotated
    tag fails outright without one."""
    workflow = source(AUTO_RELEASE)
    assert "git config user.name" in workflow
    assert "git config user.email" in workflow

    steps = job(AUTO_RELEASE, "release")["steps"]
    identity_index = next(i for i, s in enumerate(steps) if "git config user.name" in str(s.get("run", "")))
    cut_index = next(i for i, s in enumerate(steps) if "cut_release.sh" in str(s.get("run", "")))
    assert identity_index < cut_index


def test_auto_release_decides_with_stdlib_only_and_installs_no_workspace() -> None:
    """`tools/auto_release.py`'s own docstring: "the job that runs this
    installs nothing but the checkout." A `uv sync` here would be pure waste —
    and if the tool ever stopped being stdlib-only, `test_ci_workflows.py`'s
    own `test_every_workflow_installs_what_the_tools_it_runs_import` would
    catch the missing install, so this is a cost check, not a safety net."""
    offenders = [spec_text(spec) for spec in steps(AUTO_RELEASE) if "uv sync" in spec_text(spec)]
    assert not offenders, offenders
    decide_text = step_text(AUTO_RELEASE, "Decide whether to release")
    assert "python3 tools/auto_release.py" in decide_text


def test_auto_release_writes_trigger_sha_only_through_env_not_into_the_script() -> None:
    """workflow_run executes this file from the default branch (main-health.yml's
    own comment on the same trigger type) with a write token; nothing from the
    triggering run is interpolated straight into a `run:` block regardless."""
    decide_step = step(AUTO_RELEASE, "Decide whether to release")
    assert decide_step["env"]["TRIGGER_SHA"] == "${{ github.event.workflow_run.head_sha }}"
    assert "github.event.workflow_run" not in str(decide_step.get("run", ""))


def test_auto_release_only_cuts_when_the_tool_said_release_and_passes_its_own_tag() -> None:
    """The gate between "decided" and "acted": a step that ran regardless of
    `steps.decide.outputs.release` would tag on every green main push, quiet
    period or not."""
    cut_step = step(AUTO_RELEASE, "Cut the release (staging only)")
    assert cut_step["if"] == "steps.decide.outputs.release == 'true'"
    text = spec_text(cut_step)
    assert "cut_release.sh" in text
    assert "--auto" in text, "marks the tag so tools/auto_release.py's own daily cap can count it (test_cut_release.py)"
    assert "TAG" in cut_step.get("env", {}) and cut_step["env"]["TAG"] == "${{ steps.decide.outputs.tag }}"


def test_auto_release_is_serialised_so_two_decisions_never_race_one_tag() -> None:
    workflow = yaml.safe_load(source(AUTO_RELEASE))
    concurrency = workflow["concurrency"]
    assert concurrency["group"] == "auto-release-staging"
    assert concurrency["cancel-in-progress"] is False


def test_auto_release_has_only_the_permissions_it_needs() -> None:
    """`contents: read` for the one contents read `github.token` still does —
    `tools/auto_release.py`'s `GET /repos/.../commits/main` (#941 review: the
    tag PUSH runs under INFRA2_PAT, not this token, so it no longer needs
    `write`). `gh run list` / `gh run view` (main's ci-required, the tag's own
    ci-required, the deploy/walk runs) are the Actions API, not `contents` —
    covered by `actions: write`, which also dispatches the staging deploy and
    surface walk. `pull-requests: read` is for the derived PR review-thread
    check. Nothing broader."""
    workflow = yaml.safe_load(source(AUTO_RELEASE))
    permissions = workflow["permissions"]
    assert permissions["contents"] == "read"
    assert permissions["actions"] == "write"
    assert permissions["pull-requests"] == "read"
    assert set(permissions) == {"contents", "actions", "pull-requests"}


# --- one migration applier (#984) --------------------------------------------

#: (workflow, job, step). Every CI job that needs a schema applies it by running
#: `db/apply_migrations.sh`, the single implementation of "apply db/migrations/*.sql in
#: glob order, then db/roles.sql, stopping on the first error". These eight steps used to
#: be three hand-written copies of that loop, and they had drifted from the one the
#: llm-service image boots with: no lock bound, no statement timeout, no
#: MIGRATIONS_DATABASE_URL. `libs/runtime/tests/test_migration_applier.py` holds the rest
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
    for path in sorted((REPO_ROOT / "libs/runtime/tests").glob("test_*.py")):
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
    for path in sorted((REPO_ROOT / "libs/runtime/tests").glob("test_*.py")):
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
        for member in ("apps/data-engine", "apps/llm-service", "libs/contracts", "libs/factors", "libs/runtime")
    } | {"truealpha_runtime", "truealpha_contracts", "data_engine", "factors"}

    def imports(script: Path) -> set[str]:
        tree = ast.parse(script.read_text(encoding="utf-8"))
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names.add(node.module.split(".")[0])
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


def test_two_different_releases_never_queue_behind_each_other() -> None:
    """`docs/release-protocol.md` tells an author the tag push is the only lock
    and that parallel releases do not deadlock. That claim rests entirely on the
    version being part of the concurrency key, which is one edit away from being
    false — and a workflow-level `group: truealpha-release` would serialise every
    release behind every other with no error anywhere to say so.

    `cancel-in-progress: false` is the other half: a second dispatch of the same
    release is usually a retry of a deploy whose outcome is unknown, and
    cancelling the first would leave nobody watching it.
    """
    workflow = yaml.safe_load(source(RELEASE))
    concurrency = workflow["concurrency"]
    assert "inputs.version_ref" in concurrency["group"], (
        f"the release concurrency key is {concurrency['group']!r} and does not include the "
        f"version, so two different releases would serialise (docs/release-protocol.md)"
    )
    assert "inputs.deploy_type" in concurrency["group"], (
        "staging and prod for one version would serialise behind each other"
    )
    assert concurrency["cancel-in-progress"] is False, "a retry must wait for the in-flight deploy, never cancel it"


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
            # A sharded lane (A4 B1) names its root on the pytest_shard.py line
            # instead of on the pytest line, whose paths come from a shell
            # array. The root counts as covered here because
            # test_pytest_shard.py separately proves the shards partition that
            # root exactly — neither check alone would be enough.
            for root in re.findall(r"pytest_shard\.py\s+(\S+)", run):
                covered.add(root)
            if run.startswith("uv run pytest "):
                # shlex, and flags filtered: a `-q` or `-k expr` token counted
                # as a path would make the equality fail on correct workflows —
                # or worse, mask a genuinely dropped package behind a flag
                # token that happens to balance the set sizes (review).
                import shlex

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


def test_the_changes_filter_reaches_every_test_that_guards_a_tool() -> None:
    """A tools-only PR used to run ZERO tests — A4 review finding (#673).

    The `changes` job's python filter did not include `tools/**`, and the
    `required` summariser treats a skipped job as success. Every deploy-gate
    tool (health_check, walk_evidence, output_invariants, issue_close_guard,
    mutation_reproof, ...) is tested from libs/runtime/tests via load_tool,
    which only ci-python executes — so the gate tooling itself could merge
    with no test running. `tools/` appeared exactly once in ci-required.yml
    before the fix: inside a comment.

    Pins three memberships (python/db/web each execute or read tools/) and
    that the python filter covers the directory of every pytest testpath, so a
    sixth package cannot land outside the filter the way tools/ did.
    """
    import tomllib

    workflow = yaml.safe_load(source(REQUIRED))
    changes_job = (workflow.get("jobs") or {})["changes"]
    # Default + assertion so a restructured changes job fails by naming the
    # missing contract, not as a bare StopIteration.
    filter_step = next(
        (step for step in changes_job["steps"] if "filters" in (step.get("with") or {})),
        None,
    )
    assert filter_step is not None, (
        "ci-required's changes job no longer carries a paths-filter step; every lane "
        "decision below reads from it (#673)"
    )
    filters = yaml.safe_load(filter_step["with"]["filters"])

    for lane in ("python", "db", "web"):
        assert "tools/**" in filters[lane], (
            f"the {lane} filter no longer includes tools/**; a tools-only PR skips that lane "
            f"and `required` reads the skip as success (#673)"
        )

    # The same class one directory over, found by #689's own check run: this
    # very file pins the shape of deploy-release, deploy-freshness,
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


def test_a_tag_run_attests_instead_of_re_running() -> None:
    """A4 D2 (#673): a tag names a SHA main already proved.

    Eleven August tags each re-ran the full suite on an identical, already-green
    SHA. On a tag the suite lanes are now replaced by one attestation job before
    the image publish (changes and the security lane still run; they cost
    seconds); every clause the deploy evidence pins (workflow path, push event,
    tag head_branch, title, success conclusion) is untouched, so this asserts
    the mechanics that make that safe:

    - the four suite lanes are EXPLICITLY off on tags — not left to whatever the
      paths filter computes for a tag push, which is undefined behaviour;
    - the attestation queries main's runs for THIS sha, green, push — drop any
      of those qualifiers and a red or foreign run attests;
    - both the image publish and the required summariser gate on it, so a tag on
      an unverified SHA publishes nothing and the run is red.
    """
    workflow = yaml.safe_load(source(REQUIRED))
    jobs = workflow["jobs"]

    for lane in ("db", "python", "runtime", "web"):
        assert "github.ref_type != 'tag'" in str(jobs[lane]["if"]), (
            f"the {lane} lane runs on tags again — the tag run is back to re-proving an already-green SHA (#673 D2)"
        )

    attest = jobs["tag_verified"]
    assert attest["if"] == "github.ref_type == 'tag'"
    # Find the query by content, not position — steps[0] would go stale on the
    # first added checkout/setup step without any behaviour change (review).
    queries = [
        str(step.get("run", ""))
        for step in attest["steps"]
        if "/actions/workflows/ci-required.yml/runs?" in str(step.get("run", ""))
    ]
    assert len(queries) == 1, (
        f"expected exactly one step querying ci-required runs in tag_verified, found {len(queries)}"
    )
    query = queries[0]
    for qualifier in ("head_sha=${{ github.sha }}", "branch=main", "event=push", "status=success"):
        assert qualifier in query, (
            f"the attestation no longer requires {qualifier!r} — without it a red, foreign or "
            f"different-SHA run can attest a tag"
        )

    assert "tag_verified" in jobs["images_release"]["needs"]
    assert "needs.tag_verified.result == 'success' || needs.tag_verified.result == 'skipped'" in str(
        jobs["images_release"]["if"]
    ), "an unattested tag must not publish images"
    assert "tag_verified" in jobs["required"]["needs"], (
        "required does not aggregate tag_verified — an unattested tag run would summarise green"
    )


def test_a_tag_re_tags_what_main_published_instead_of_rebuilding_it() -> None:
    """#860: the tag run measured 2.2 min, and every second of it re-produced images the
    SHA already had -- three cache-hit builds and three ~1 min publishes that minted a
    second digest of the same bytes and, in passing, moved main's own `sha-<short>` pointer
    onto it (v0.0.63: all three `sha-7de7813` tags equal the `v0.0.63` digests, none equal
    what main had pushed). `tag_verified` already asserts that main published this SHA, so
    the release re-tags main's digests and builds only what main did not publish (#731
    skips an image the merge left untouched). What makes that safe:

    - only a tag reuses; a main push and the manual force ARE the publisher, and the PR-time
      `images_build` (which absorbed the PR-time `images_check`, #869) never sees the input,
      so a PR still proves its image builds;
    - `plan` decides per image through tools/verified_sha_images.py, whose own tests pin
      that 404 is the one answer that means "build" and everything else is red;
    - `retag` publishes nothing but a pointer: `imagetools create` at the digest `plan`
      resolved, then an inspect that fails the job if the tag names anything else;
    - `retag` and `publish` derive their tags from one identical metadata-action spec, so a
      tag scheme change cannot leave the two paths naming different things; and the spec
      still carries `type=sha,format=short`, the spelling the tool asks the registry for.
    """
    release = job(REQUIRED, "images_release")
    assert str(release["with"]["reuse_verified_sha"]) == "${{ github.ref_type == 'tag' }}", (
        "images_release must reuse on a tag and only on a tag — main and the manual force are the publisher"
    )
    assert "reuse_verified_sha" not in (job(REQUIRED, "images_build").get("with") or {}), (
        "a PR must still prove its image BUILDS; reuse would let a broken Dockerfile merge green"
    )
    reuse = triggers(IMAGES)["workflow_call"]["inputs"]["reuse_verified_sha"]
    assert reuse["type"] == "boolean" and reuse["default"] is False and reuse["required"] is False

    select = step(IMAGES, "Select available images")
    assert "python3 tools/verified_sha_images.py" in str(select["run"]), (
        "plan no longer asks the registry what main published — every tag rebuilds again"
    )
    assert select["env"]["REUSE_VERIFIED_SHA"] == "${{ inputs.reuse_verified_sha }}"
    assert select["env"]["SHA"] == "${{ github.sha }}", "the SHA asked about must be the one the tag points at"
    assert 'if [[ "$REUSE_VERIFIED_SHA" == "true" ]]' in str(select["run"]), "the split must be gated on the input"
    assert "set -euo pipefail" in str(select["run"]), "a failed split must fail the plan, not feed jq an empty string"
    plan = job(IMAGES, "plan")
    for output in ("matrix", "has_images", "retag_matrix", "has_retag"):
        assert output in plan["outputs"], f"plan no longer exposes {output}"

    retag = job(IMAGES, "retag")
    assert retag["needs"] == "plan", "retag depends on the plan and on nothing that builds"
    assert "inputs.publish" in str(retag["if"]) and "has_retag == 'true'" in str(retag["if"]), (
        "a publish: false call (images_build on a PR) must never push a tag"
    )
    assert "fromJSON(needs.plan.outputs.retag_matrix)" in str(retag["strategy"]["matrix"])
    pointer = step(IMAGES, "Re-tag ${{ matrix.image }} at the digest main published")
    assert pointer["env"]["SOURCE"].endswith("@${{ matrix.digest }}"), (
        "the re-tag must name the digest plan resolved, not a tag that could have moved since"
    )
    run = str(pointer["run"])
    assert "docker buildx imagetools create" in run
    assert "imagetools inspect" in run and '[ "$resolved" != "$DIGEST" ]' in run and "exit 1" in run, (
        "the re-tag no longer verifies that the tag resolves to the verified digest"
    )
    assert "docker/build-push-action" not in "\n".join(spec_text(spec) for spec in retag["steps"]), (
        "retag builds — the job exists precisely so a tag never does"
    )

    def metadata(job_id: str) -> dict:
        specs = [spec for spec in job(IMAGES, job_id)["steps"] if "docker/metadata-action" in str(spec.get("uses", ""))]
        assert len(specs) == 1, f"{job_id} has {len(specs)} metadata-action steps"
        return specs[0]["with"]

    assert metadata("retag")["tags"] == metadata("publish")["tags"], (
        "retag and publish derive their tags from different specs — the two paths can name different things"
    )
    assert metadata("retag")["images"] == metadata("publish")["images"]
    assert "type=sha,format=short" in metadata("publish")["tags"], (
        "publish no longer tags sha-<short> — the reference tools/verified_sha_images.py asks the registry for"
    )
    # A build never runs for a re-tagged image: the build matrix is what the split left.
    assert str(job(IMAGES, "build")["strategy"]["matrix"]) == "${{ fromJSON(needs.plan.outputs.matrix) }}"


def test_release_script_reviews_the_pr_that_produced_the_release_sha() -> None:
    """v0.0.34's first prod dispatch: deploy-release's prod gate pins the
    reviewed PR's merge_commit_sha to the release SHA, but cut_release passed
    PR_LIST[0] — in a batch, only the last-merged PR satisfies the gate. Pin
    both halves of the fix:

    - cut_release selects the reviewed PR by merge-commit == main HEAD and
      fails closed before tagging when no named PR matches;
    - the health-confirm step's fromJson is guarded, so a failed request step
      reports its own error instead of "Error reading JToken" template noise.
    """
    script = (REPO_ROOT / "tools" / "cut_release.sh").read_text(encoding="utf-8")
    assert '[ "$MERGE_SHA" = "$LOCAL_MAIN" ] && REVIEWED_PR="$PR"' in script, (
        "cut_release no longer selects the reviewed PR by merge==HEAD"
    )
    assert '[ -n "$REVIEWED_PR" ] || fail' in script, (
        "cut_release no longer fails closed before tagging when no named PR produced main HEAD"
    )
    assert 'reviewed_change_url="https://github.com/$REPO/pull/$REVIEWED_PR"' in script, (
        "prod dispatch does not use the merge==HEAD PR as the reviewed change (the v0.0.34 PR_LIST[0] defect)"
    )

    deploy = source(RELEASE)
    guarded = "steps.request.outputs.json != '' && fromJson(steps.request.outputs.json)"
    assert guarded in deploy, (
        "EXPECTED_RELEASE fromJson is unguarded again — a failed request step will bury "
        "its error under a JToken template failure"
    )
    confirm = step(RELEASE, "Confirm the deployed release is healthy")
    assert "steps.request.outputs.json != ''" in str(confirm.get("if", "")), (
        "the health confirmation no longer skips on an empty request output — with the env "
        "guard falling back to '', health_check treats empty expected as don't-verify"
    )
    assert "refusing a vacuous health confirmation" in str(confirm.get("run", "")), (
        "the health confirmation no longer fails closed on an empty EXPECTED_RELEASE"
    )


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
    liveness job is gated on that input, or on `dagster_liveness_only`, which the nightly
    caller sets — the standing check that PR-gating does not mean "only tested by
    accident". The three heavier jobs skip on that same input so the nightly is
    liveness-only, not a timer that re-runs the whole suite. Every gate here is an input
    the caller names, never a read of the caller's event.
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

    for name in ("dagster_liveness_required", "dagster_liveness_only"):
        liveness_input = triggers(PYTHON)["workflow_call"]["inputs"][name]
        assert liveness_input["type"] == "boolean" and liveness_input["default"] is False, (
            f"{name} must default closed — an unset input on a direct call must not silently change what runs"
        )

    # The nightly is a scheduled CALLER, and ci-python.yml stays `workflow_call`-only: a
    # reusable workflow that also declares its own triggers and permissions gets NO run on a
    # PR — #878's first push never started ci-required at all, with no error anywhere.
    assert set(triggers(PYTHON)) == {"workflow_call"}, (
        f"ci-python.yml declares {sorted(triggers(PYTHON))} — a reusable workflow with its own "
        f"triggers silently gets no PR run (#878); the nightly belongs in {NIGHTLY}"
    )
    nightly = triggers(NIGHTLY)
    assert nightly.get("schedule") and nightly["schedule"][0].get("cron"), (
        f"{NIGHTLY} no longer has a schedule — #855 A4's nightly coverage for the PRs the "
        f"narrow filter does not match is gone"
    )
    assert "workflow_dispatch" in nightly, f"{NIGHTLY} must be runnable by hand for a drill"
    caller = job(NIGHTLY, "python")
    assert caller["uses"] == "./.github/workflows/ci-python.yml"
    assert caller["with"]["dagster_liveness_only"] is True, "the nightly must force the liveness job on, alone"
    assert caller["permissions"].get("actions") == "write", "setup-uv's cache saves only with actions: write (#645)"

    liveness = job(PYTHON, "dagster-code-server-liveness")
    condition = str(liveness["if"])
    assert "inputs.dagster_liveness_required" in condition, (
        f"the liveness job's if is {condition!r} — it no longer reads the caller's decision, so "
        f"it either always runs (back on the PR critical path) or never does"
    )
    assert "inputs.dagster_liveness_only" in condition, (
        f"the liveness job's if is {condition!r} — the nightly's input no longer forces it on, so "
        f"a nightly run would tick green while testing nothing"
    )
    assert "github.event_name" not in condition, (
        f"the liveness job's if is {condition!r} — inside a reusable workflow that context is the "
        f"caller's; the contract is the named inputs"
    )

    for lane in ("gates", "test-core", "test-data-engine"):
        assert str(job(PYTHON, lane)["if"]) == "${{ !inputs.dagster_liveness_only }}", (
            f"{lane} runs on the nightly too — the nightly is meant to be liveness-only, "
            f"not a timer that re-runs the whole suite"
        )


#: The three factor-expression cross-check proofs #969 migrated from pinned Qlib to the
#: Polars AST, and the sources each one proves. Kept as data so the two tests below
#: disagree loudly if one is edited without the other.
POLARS_CROSS_CHECK_TESTS = {
    "libs/factors/tests/test_gross_profit_per_employee.py",
    "libs/factors/tests/test_peg.py",
    "libs/factors/tests/test_price_to_sales.py",
}
POLARS_CROSS_CHECK_SOURCES = {
    "libs/factors/src/factors/base/gross_profit_per_employee.py",
    "libs/factors/src/factors/base/peg.py",
    "libs/factors/src/factors/base/price_to_sales.py",
    "libs/factors/src/factors/expressions/compiler.py",
    "libs/contracts/src/truealpha_contracts/ast.py",
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
    2. ci-python's test-core job actually runs `libs/factors/tests`;
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
    assert any("libs/factors/tests" in run for run in runs), (
        "ci-python's test-core job no longer runs libs/factors/tests — the proofs are covered by "
        f"the filter but nothing executes them. pytest invocations found: {runs!r}"
    )

    for relative in sorted(POLARS_CROSS_CHECK_TESTS):
        text = (REPO_ROOT / relative).read_text(encoding="utf-8")
        assert "importorskip" not in text, (
            f"{relative} gates on pytest.importorskip again — the job runs, the test reports as "
            f"passed, and the assertion never executes. That is exactly the state #969 migrated "
            f"off Qlib to escape (11 of 12 ci-python runs skipped it)."
        )
        assert "compile_expression" in text or "compile_to_polars" in text, (
            f"{relative} no longer compiles the migrated AST expression — the cross-check would "
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

    Every lockfile, not just the root one: `libs/factors/qlib-runtime/` was a
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
            f"factor-expression engine and the workspace-excluded `libs/factors/qlib-runtime` "
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
#: `skills/` holds procedures an agent executes against this repository, one of which is
#: `factor-acceptance`, the very procedure this migration changed.
QLIB_SCAN_ROOTS = (".github", "apps", "db", "libs", "skills", "tools")
#: Top-level directories deliberately NOT scanned. Every other top-level directory must
#: appear in QLIB_SCAN_ROOTS — asserted below, because the two holes this guard has
#: already had were both "a place the walk cannot reach", found by review rather than by
#: the guard. A new top-level directory now fails this test until someone classifies it.
QLIB_SCAN_EXCLUDED_ROOT_DIRS = (
    # The ADR for this migration (A5-polars-vectorbt-engine.md) and A0's amendment note
    # live here; init.md rule 25 points at them. Recording the migration is the opposite
    # of reintroducing it.
    "docs",
    # Frozen history. Accepted records pin these files' hashes, so editing the prose
    # inside one breaks the record rather than removing a dependency.
    "governance",
    # #1003 committed this so an agent host reads the vendored skills at the path it
    # looks in. Every entry under it is a symlink into `skills/`, which QLIB_SCAN_ROOTS
    # already scans, so the content IS scanned -- through its real path, once rather than
    # twice. The premise is what makes the exclusion safe, so
    # test_every_claude_entry_is_a_symlink_into_a_scanned_root asserts it: a real file
    # added here later would otherwise be silently out of scope, which is the exact shape
    # of the two holes this guard has already had.
    ".claude",
)
#: The excluded root above is only out of scope while everything in it resolves into one of
#: QLIB_SCAN_ROOTS. Named here so the assertion and the exclusion cannot drift apart.
_SYMLINK_ONLY_EXCLUDED_ROOT = ".claude"
#: The repository's own top-level FILES are scanned too, non-recursively. `rglob` from a
#: scan root cannot reach a file sitting at the repository root, and the root
#: `pyproject.toml` is exactly where a `libs/factors/qlib-runtime` workspace member would
#: come back — #969 removed one from it. Non-recursive on purpose: recursing the root
#: would pull in the excluded directories above.
QLIB_SCAN_REPOSITORY_ROOT_FILES = True
#: Records of what a contract asserted at the time, not live references. Their bytes are
#: hashed by accepted records (`governance/batches/D4-datahub-interface.v1.json` pins
#: `datahub_interface.v2.json`'s whole-file sha256) or by their own versioned successors,
#: so editing the prose inside them breaks the record rather than removing a dependency.
#: The live statement of the same red line is in docs/architecture-decisions/.
QLIB_SCAN_EXCLUDED_DIRS = (
    "libs/contracts/tests/fixtures",
    "apps/app-web/node_modules",
    "apps/app-web/.next",
)
#: Top-level documents that carry the migration's own history. They only became reachable
#: when the root files were added to the scan, and they are the same two the rest of this
#: exclusion set already covers in spirit: `init.md` rule 25 points at the ADR, and
#: `vision.md` is product scope, not a dependency surface.
QLIB_SCAN_EXCLUDED_FILES = ("init.md", "vision.md")
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
#: them by bare name made `libs/factors/build/` and its whole subtree invisible. None of
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
    "skills/factor-acceptance/SKILL.md",
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
    # than by directory, so a real reference anywhere else in libs/runtime/tests still
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
    - `init.md` rule 25 keeps a one-clause historical pointer to A5, and `vision.md` is
      product scope rather than a dependency surface (QLIB_SCAN_EXCLUDED_FILES);
    - `governance/` is frozen history and its files' hashes are pinned by accepted tests;
    - `libs/contracts/tests/fixtures/` holds versioned accepted-contract artifacts whose
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


def test_the_routing_probe_gets_a_base_not_an_endpoint() -> None:
    """A4 C1 (#673). The freshness matrix carries `url` (the health ENDPOINT,
    what walk_evidence and health_check want) and `base` (what
    surface_contract builds its own paths from). Handing the probe `url`
    requests /api/health/api/health and reports the surface down every day —
    which is what the first draft of this step did, comment and all.
    """
    freshness = yaml.safe_load(source(FRESHNESS))["jobs"]["freshness"]
    for entry in freshness["strategy"]["matrix"]["include"]:
        assert entry["url"].startswith(entry["base"]), (
            f"{entry['environment']}: url {entry['url']!r} is not under base {entry['base']!r}, so the "
            f"two describe different deployments"
        )
        assert not entry["base"].endswith("/api/health"), (
            f"{entry['environment']}: base is a health endpoint, not a base"
        )
    # The command line only: the step's own comment explains base-vs-url and
    # naming both, so scanning the whole block would match the explanation
    # rather than the invocation.
    probe = str(step(FRESHNESS, "Check ${{ matrix.environment }} routing shape")["run"])
    invocation = next(
        (line for line in probe.splitlines() if "surface_contract.py" in line and not line.strip().startswith("#")),
        None,
    )
    assert invocation is not None, (
        "the routing-shape step no longer invokes tools/surface_contract.py — the daily probe for "
        "the four-release redirect class is gone, and a bare next() would have said StopIteration"
    )
    assert "matrix.base" in invocation and "matrix.url" not in invocation, (
        f"the routing probe is invoked as {invocation.strip()!r} — on matrix.url it requests "
        f"/api/health/api/health and reports a healthy surface as down, every day"
    )


def test_main_pushes_never_collide_and_a_tag_run_can_never_be_cancelled() -> None:
    """#708 measured 2 of 14 main merges colliding and turned `cancel-in-progress`
    on for every event, so main pushes collapse instead of each burst queueing
    ~16 extra jobs in front of every open PR. Re-measured 2026-09-16 (#860): 6 of
    27 main-push runs were cancelled (22%, five times #708's rate), and one of
    them — 57d25316 — was a run `cut_release` was WAITING ON: it died mid-flight
    with "finished non-green: cancelled" and the release ceremony restarted from
    scratch instead of resuming. Collapsing collisions is not the same as
    preventing them, and #708 did not measure what happens when the collision
    kills a run something else is blocked on.

    So a non-tag push now keys its group by `github.sha`: each main commit gets
    its own group, nothing else can ever share it, and `cancel-in-progress`
    never has a second run in the group to cancel it with — main can no longer
    cancel a run something else is waiting on, by construction, the same way
    finance_report's `ci.yml` already does for its push group.

    A tag push is the one push that keeps the OLD `github.ref` key (moved from
    the test this replaces, not deleted): a tag's SHA is always one main already
    ran and published green — `cut_release` refuses to tag otherwise — so it
    never needs isolating from main, only from another run on the SAME tag ref,
    which the ref already gives it, and which the event suffix protects from a
    workflow_dispatch on that same ref: without the event in the key, a manual
    dispatch on the same tag ref shares the group and cancels the release run
    mid-publish. Two dispatches on the same ref, or two tag pushes, still
    cancel each other — that is still what you want.
    """
    concurrency = yaml.safe_load(source(REQUIRED))["concurrency"]
    group = str(concurrency["group"])
    assert concurrency["cancel-in-progress"] is True, (
        "PR runs no longer collapse to their newest push — every superseded PR run now queues "
        "behind the one that replaced it"
    )
    assert "github.event_name == 'push'" in group and "github.ref_type != 'tag'" in group, (
        f"the concurrency key is {group!r}: a non-tag push is no longer routed to its own "
        f"SHA-keyed group, so two main commits can land in the same group again and "
        f"cancel-in-progress can kill a run a release ceremony or an open PR is waiting on"
    )
    assert "github.sha" in group, (
        f"the concurrency key is {group!r}: it no longer uses github.sha, so a non-tag push "
        f"cannot get its own group and main pushes go back to colliding (measured 6/27, #860)"
    )
    assert "github.ref" in group, (
        f"the concurrency key is {group!r}: with cancel-in-progress on and no ref in the key, a "
        f"merge landing during a tag run CANCELS it — the release publishes no images and fails "
        f"after the tag push, which the protocol treats as the lock"
    )
    assert "github.event_name" in group, (
        f"the concurrency key is {group!r}: the ref alone does not isolate a tag PUSH from a "
        f"manual workflow_dispatch on the same tag, which would share the group and cancel the "
        f"release run mid-publish"
    )


def test_the_walk_warms_the_pages_it_is_about_to_open() -> None:
    """#698: the walk's first navigation raced the container swap — v0.0.34's
    prod deploy timed out on `networkidle` at /research/rankings and a rerun on
    the warm container passed with no code change (run 33356222058). The health
    gate polls llm-service; app-web is a different container.

    This asserts only the wiring — that the walk calls the warm-up first, with
    its own base URL. Whether the warm-up can fail is a property of
    `tools/warm_surface.sh`, and it is proven by RUNNING it in
    test_warm_surface.py. The first version of this test asserted that by
    slicing the YAML text, and the slice silently selected the whole script,
    including the walk invocation: PyYAML strips a block scalar's common
    indentation, so the separator (copied from how the line looks in the file)
    was never found and `split` returned its input. It passed for a reason
    unrelated to the property — the #583 shape, one function below the docstring
    that describes it.
    """
    walk = str(step(WALK, "Walk the deployed surface")["run"])
    assert "tools/warm_surface.sh" in walk, (
        "the walk no longer warms the app-web surface before opening it — the first navigation "
        "races the container swap again (#698)"
    )
    # The path must RESOLVE from the step's own working-directory, not merely
    # appear in the text. This is the defect that shipped: the step sets
    # `working-directory: apps/app-web` for the walk, so a bare
    # `tools/warm_surface.sh` resolved to apps/app-web/tools/warm_surface.sh
    # and `Deploy staging v0.0.38` died with exit 127. The guard above asserted
    # the call was present and said nothing about whether it could run —
    # another lane found it in production and spent a PR on it (#717).
    spec = step(WALK, "Walk the deployed surface")
    workdir = str(spec.get("working-directory", ""))
    for line in str(spec["run"]).splitlines():
        if "warm_surface.sh" not in line or line.strip().startswith("#"):
            continue
        invocation = line.strip().split()[0].strip('"')
        resolved = (
            REPO_ROOT / invocation.replace("$GITHUB_WORKSPACE/", "")
            if "$GITHUB_WORKSPACE" in invocation
            else REPO_ROOT / workdir / invocation
        )
        assert resolved.exists(), (
            f"the walk invokes {invocation!r} from working-directory {workdir!r}, which resolves to "
            f"{resolved.relative_to(REPO_ROOT) if REPO_ROOT in resolved.parents else resolved} — "
            f"that file does not exist, and the step dies with exit 127 on the next deploy (#717)"
        )
    assert walk.index("warm_surface.sh") < walk.index("node e2e/walk-tree.mjs"), (
        "the warm-up runs after the walk, which is no warm-up at all"
    )
    assert '"$TA_BASE_URL"' in walk.split("node e2e", 1)[0], (
        "the warm-up targets something other than the walk's own base URL — two definitions of "
        "where the deployment lives is one too many"
    )


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
        ("bun run typecheck", "the TypeScript typecheck"),
        ("tests/*.test.ts", "the DB-backed test loop (#373: 13 suites once ran nowhere)"),
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
    for name in ("build", "publish", "retag"):
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


def test_walk_evidence_can_finish_waiting_inside_the_freshness_job() -> None:
    """tools/walk_evidence.py waits (bounded) for a release caught mid-flight (#876). A wait
    longer than the freshness job's own timeout would turn "stuck release" into a cancelled
    job, which reports nothing and escalates nothing."""
    from datetime import timedelta

    walk_evidence = load_tool("walk_evidence")
    timeout = job(FRESHNESS, "freshness")["timeout-minutes"]
    # The checks before that step take well under 5 min; leave them that much.
    assert walk_evidence.IN_FLIGHT_WAIT <= timedelta(minutes=timeout - 5), (
        f"walk_evidence may wait {walk_evidence.IN_FLIGHT_WAIT} but the freshness job times out at {timeout} min"
    )


def test_every_claude_entry_is_a_symlink_into_a_scanned_root() -> None:
    """`.claude/` is excluded from the Qlib sweep on one premise: everything in it is a
    symlink into `skills/`, which is scanned. This asserts the premise.

    Without it the exclusion is a hole waiting for its first real file, which is how both of
    this guard's previous holes were shaped -- a place the walk could not reach, found by
    review rather than by the guard. Confirmed red by pointing one of the symlinks at a
    directory outside the scan roots.
    """
    listing = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z", "--", _SYMLINK_ONLY_EXCLUDED_ROOT],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    entries = [entry for entry in listing.split("\0") if entry]
    assert entries, (
        f"{_SYMLINK_ONLY_EXCLUDED_ROOT}/ is excluded from the Qlib sweep but git tracks nothing "
        f"there, so this assertion is checking nothing (GREEN-WHILE-EMPTY). Remove the exclusion "
        f"or remove this test."
    )

    scanned = {(REPO_ROOT / root).resolve() for root in QLIB_SCAN_ROOTS}
    escapes: list[str] = []
    for entry in entries:
        path = REPO_ROOT / entry
        if not path.is_symlink():
            escapes.append(f"{entry} is a real file, not a symlink")
            continue
        target = path.resolve()
        if not any(target == root or root in target.parents for root in scanned):
            escapes.append(f"{entry} -> {target} resolves outside every scanned root")
    assert not escapes, (
        f"{_SYMLINK_ONLY_EXCLUDED_ROOT}/ is excluded from the Qlib sweep only because everything "
        f"in it is scanned through its real path. These are not: {escapes}. Either move them under "
        f"a scan root or move {_SYMLINK_ONLY_EXCLUDED_ROOT} into QLIB_SCAN_ROOTS."
    )
