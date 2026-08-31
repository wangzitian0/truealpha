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
PYTHON = "ci-python.yml"


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

    - the five suite lanes are EXPLICITLY off on tags — not left to whatever the
      paths filter computes for a tag push, which is undefined behaviour;
    - the attestation queries main's runs for THIS sha, green, push — drop any
      of those qualifiers and a red or foreign run attests;
    - both the image publish and the required summariser gate on it, so a tag on
      an unverified SHA publishes nothing and the run is red.
    """
    workflow = yaml.safe_load(source(REQUIRED))
    jobs = workflow["jobs"]

    for lane in ("db", "python", "qlib", "runtime", "web"):
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
    assert lane["strategy"]["fail-fast"] is False, (
        "fail-fast is on: one red shard would cancel the other two and hide their failures"
    )
