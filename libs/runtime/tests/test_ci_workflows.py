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
import importlib.util
import re
import sys
from pathlib import Path

import pytest

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
source = _contract.source
step = _contract.step
step_text = _contract.step_text
triggers = _contract.triggers

REPO_ROOT = Path(__file__).resolve().parents[3]

RELEASE = "deploy-release.yml"
FRESHNESS = "deploy-freshness.yml"
CLOSE_GUARD = "issue-close-guard.yml"
REQUIRED = "ci-required.yml"


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
    for dependency in ("security", "db", "python", "qlib", "runtime", "web"):
        assert f"needs.{dependency}.result == 'success' || needs.{dependency}.result == 'skipped'" in condition, (
            f"images_release must wait for {dependency}"
        )
    for image in ("publish", "app_web", "llm_service", "data_engine"):
        assert release["with"][image] is True

    text = source(REQUIRED)
    assert text.index("  images_release:\n") < text.index("\n  required:\n"), (
        "the required job must summarise images_release, so it comes after it"
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


def test_the_surface_walk_examines_every_credential_it_uses() -> None:
    """#560: the member pass needs TA_MEMBER_EMAIL; a probe that checks two of
    three secrets lets the walk start and fail later, less clearly."""
    probe = step_text(RELEASE, "Are the surface-walk credentials configured")
    for variable in ("TA_EMAIL", "TA_PASSWORD", "TA_MEMBER_EMAIL"):
        assert f'"${{{variable}}}"' in probe, f"{variable} must be examined before the walk runs"


def test_an_unconfigured_walk_is_skipped_rather_than_exiting_zero() -> None:
    """#560: a step that exits 0 without walking reports `success`, which
    tools/walk_evidence.py reads as evidence of a walk that never ran."""
    walk = step(RELEASE, "Walk the deployed surface")
    assert walk["if"] == "${{ steps.walk_credentials.outputs.ready == 'true' }}", (
        "an unconfigured walk must be SKIPPED, never a step that exits 0 and reports success"
    )
    assert "exit 0" not in str(walk.get("run", ""))


def test_the_release_lane_is_not_deadlocked_by_a_missing_secret() -> None:
    """#560: failing the run on unconfigured credentials blocked every prod
    release, since prod requires a successful staging run."""
    probe = str(step(RELEASE, "Are the surface-walk credentials configured")["run"])
    assert "ready=false" in probe, "an unconfigured walk must be reported"
    assert "exit 1" not in probe, "and must not fail the release lane"
    assert "UNVERIFIED" in probe, "but must be unmistakable in the run summary"


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


# --- issue-close-guard -------------------------------------------------------


def test_the_close_guard_can_reopen_and_sees_full_history() -> None:
    """#562: two ways this ships inert — no `issues: write`, or a shallow
    checkout that makes every commit look unreleased."""
    workflow = source(CLOSE_GUARD)
    assert "issues: write" in workflow, "the guard cannot reopen anything without it"
    assert "fetch-depth: 0" in workflow and "fetch --tags" in workflow
    assert triggers(CLOSE_GUARD)["issues"]["types"] == ["closed"]


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
