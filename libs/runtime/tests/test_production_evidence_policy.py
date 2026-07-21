"""truealpha's own Production evidence contract stays true to this repo's real CI (#464).

The contract file (tools/production_evidence_policy.json) is what infra2's deploy
receiver verifies a production release's evidence runs against (infra2#576). These
tests close the drift loop IN THIS REPO: a workflow-file rename or run-name edit
that isn't reflected in the contract fails CI here, at the moment the drift is
introduced — not months later on the first production release attempt against a
stale expectation (the infra2#571 failure mode).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from infra2_sdk.deploy import (
    PRODUCTION_EVIDENCE_POLICY_PATH,
    ProductionEvidencePolicy,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
POLICY_FILE = REPO_ROOT / PRODUCTION_EVIDENCE_POLICY_PATH
# Real captured run (gh api /repos/wangzitian0/truealpha/actions/runs/29726062965,
# the v0.0.6 staging deploy) — not a hand-authored fixture, so these assertions
# compare the contract against what GitHub actually produced, not against the
# implementation's own assumptions.
STAGING_RUN_FIXTURE = Path(__file__).parent / "fixtures/staging_dispatch_run.v1.json"


@pytest.fixture(scope="module")
def policy() -> ProductionEvidencePolicy:
    return ProductionEvidencePolicy.from_dict(json.loads(POLICY_FILE.read_text(encoding="utf-8")))


def test_contract_file_lives_at_the_sdk_canonical_path(policy) -> None:
    # The path is defined ONCE, in the SDK, for both infra2's fetcher and this
    # repo — parsing via from_dict above already proved the schema validates.
    assert POLICY_FILE.is_file()
    assert policy.service == "truealpha/app"


def test_declared_workflow_files_exist(policy) -> None:
    # A workflow-file rename without a contract update fails here, same PR.
    assert (REPO_ROOT / policy.source.workflow_path).is_file()
    assert (REPO_ROOT / policy.staging.workflow_path).is_file()


def test_source_run_name_renders_the_declared_release_title(policy) -> None:
    # ci-required.yml's run-name must produce exactly the declared template on a
    # tag push. The GitHub format() expression renders "Release Images <ref_name>"
    # where ref_name is the tag — i.e. the template with {version_ref} substituted.
    workflow_text = (REPO_ROOT / policy.source.workflow_path).read_text(encoding="utf-8")
    expected_expression = "format('Release Images {0}', github.ref_name)"
    assert expected_expression in workflow_text, (
        f"{policy.source.workflow_path} run-name no longer renders the contract's "
        f"declared title template {policy.source.display_title_template!r} for tag "
        "pushes — update tools/production_evidence_policy.json in the SAME PR as "
        "the workflow change"
    )
    assert policy.source.display_title_template == "Release Images {version_ref}"
    assert policy.source.event == "push"
    assert policy.source.require_head_sha is True  # tag-push run IS the tag commit


def test_staging_run_name_renders_the_declared_deploy_title(policy) -> None:
    # deploy-release.yml: run-name "Deploy ${{ inputs.deploy_type }} ${{
    # inputs.version_ref }}" -> for deploy_type=staging exactly the declared
    # "Deploy staging {version_ref}" (lowercase, from the input value — the
    # infra2#571 blocker-4 case mismatch this contract now owns authoritatively).
    workflow_text = (REPO_ROOT / policy.staging.workflow_path).read_text(encoding="utf-8")
    assert "run-name: Deploy ${{ inputs.deploy_type }} ${{ inputs.version_ref }}" in workflow_text
    assert policy.staging.display_title_template == "Deploy staging {version_ref}"
    assert policy.staging.event == "workflow_dispatch"


def test_contract_matches_a_real_captured_staging_run(policy) -> None:
    # The real v0.0.6 staging run, captured from the GitHub API: the contract's
    # staging expectation must describe it exactly.
    run = json.loads(STAGING_RUN_FIXTURE.read_text(encoding="utf-8"))
    assert run["path"] == policy.staging.workflow_path
    assert run["event"] == policy.staging.event
    assert run["display_title"] == policy.staging.expected_display_title("v0.0.6")
    assert run["conclusion"] == "success"


def test_staging_declares_branch_dispatch_head_sha_semantics(policy) -> None:
    # The captured run proves why require_head_sha=false: dispatched on main, its
    # head_sha is main's tip at dispatch time (15c4272...), NOT the v0.0.6 tag
    # commit (0fdbfc7...) — the version linkage is the {version_ref} in the title,
    # with infra2's receiver separately pinning version_ref -> source_sha at
    # execution time. Declared, not silently skipped.
    run = json.loads(STAGING_RUN_FIXTURE.read_text(encoding="utf-8"))
    assert run["head_branch"] == "main"
    assert policy.staging.require_head_sha is False


def test_sender_staging_url_check_matches_the_contract() -> None:
    # infra2#571 blocker 2: the sender used to require an INFRA2 receiver-run URL
    # while infra2's verifier requires this repo's own staging run. Both sender
    # checks (the CLI regex and the workflow's inline bash) must point at THIS
    # repo, consistent with the contract's staging expectation.
    cli_text = (REPO_ROOT / "tools/app_deploy_request.py").read_text(encoding="utf-8")
    assert '_STAGING_RUN_PATH_RE = re.compile(r"\\A/wangzitian0/truealpha/actions/runs/' in cli_text
    workflow_text = (REPO_ROOT / ".github/workflows/deploy-release.yml").read_text(encoding="utf-8")
    assert "https://github.com/wangzitian0/truealpha/actions/runs/" in workflow_text
    assert 'staging_run="$(gh api "/repos/wangzitian0/infra2/actions/runs/' not in workflow_text
