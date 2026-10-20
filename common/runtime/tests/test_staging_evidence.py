"""Tests for tools/staging_evidence.py (#1412 R1).

`test_cut_release.py` runs the evidence rules end to end through `cut_release.sh --prod`. This
file covers the cases that the end-to-end table does not: a red tag CI run, a staging deploy of
another tag that is still in progress, a deploy of another tag that ended before the evidence
deploy, and the import of the module on a bare `python3`.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from truealpha_runtime.testing import load_tool

REPO_ROOT = Path(__file__).resolve().parents[3]
_module = load_tool("staging_evidence")
SHA = "0123456789abcdef0123456789abcdef01234567"
TAG = "v0.2.23"


def _run(run_id: int, title: str, started: str, ended: str, *, status: str = "completed", **extra: object) -> dict:
    return {
        "id": run_id,
        "display_title": title,
        "event": "workflow_dispatch",
        "status": status,
        "conclusion": "success" if status == "completed" else None,
        "created_at": started,
        "run_started_at": started,
        "updated_at": ended,
        **extra,
    }


def _ci(conclusion: str = "success") -> dict:
    return {
        "id": 2020,
        "head_branch": TAG,
        "head_sha": SHA,
        "event": "push",
        "status": "completed",
        "conclusion": conclusion,
        "created_at": "2026-10-20T04:05:21Z",
    }


DEPLOY = _run(3030, f"Deploy staging {TAG}", "2026-10-20T04:09:11Z", "2026-10-20T04:16:30Z")
WALK = _run(4040, f"Walk Deploy staging {TAG}", "2026-10-20T04:16:33Z", "2026-10-20T04:18:32Z")


def _sources(*, ci: list[dict] | None = None, deploys: list[dict] | None = None):
    def runs(path: str) -> list[dict]:
        if "ci-required.yml" in path:
            return ci if ci is not None else [_ci()]
        if "deploy-release.yml" in path:
            return deploys if deploys is not None else [DEPLOY]
        assert "walk-release.yml" in path, path
        return [WALK]

    def api(path: str) -> str:
        assert path.endswith("/runs/4040/jobs"), path
        return json.dumps({"jobs": [{"steps": [{"name": "Walk the deployed surface", "conclusion": "success"}]}]})

    return {"runs": runs, "api": api}


def test_the_evidence_of_a_verified_tag_names_its_three_runs() -> None:
    evidence = _module.find_staging_evidence(TAG, SHA, **_sources())

    assert evidence == _module.StagingEvidence(tag_run_id=2020, deploy_run_id=3030, walk_run_id=4040)


def test_a_tag_ci_run_at_another_commit_does_not_anchor_this_commit() -> None:
    """The number was used before at another commit, and the push of the new tag started no
    run (#940: a push with a workflow's own token starts nothing). The old run proves only
    when the tag named the OLD commit, so it cannot bind any deploy to this one."""
    old = {**_ci(), "head_sha": "f" * 40}

    with pytest.raises(_module.NotPromotable, match=f"no ci-required push run of {TAG} at {SHA[:8]}"):
        _module.find_staging_evidence(TAG, SHA, **_sources(ci=[old]))


def test_a_red_tag_ci_run_is_not_a_release() -> None:
    with pytest.raises(_module.NotPromotable, match="concluded 'failure'"):
        _module.find_staging_evidence(TAG, SHA, **_sources(ci=[_ci("failure")]))


def test_a_staging_deploy_of_another_tag_still_in_progress_during_the_walk_is_a_refusal() -> None:
    running = _run(3031, "Deploy staging v0.2.24", "2026-10-20T04:17:00Z", "2026-10-20T04:17:30Z", status="in_progress")

    with pytest.raises(_module.NotPromotable, match="Deploy staging v0.2.24"):
        _module.find_staging_evidence(TAG, SHA, **_sources(deploys=[DEPLOY, running]))


def test_a_deploy_of_another_tag_that_ended_before_the_evidence_deploy_is_no_overlap() -> None:
    """The real timeline of 2026-10-20: v0.2.22 deployed 03:58-04:00, before v0.2.23."""
    earlier = _run(3029, "Deploy staging v0.2.22", "2026-10-20T03:58:37Z", "2026-10-20T04:00:58Z")
    later = _run(3032, "Deploy staging v0.2.24", "2026-10-20T04:22:37Z", "2026-10-20T04:31:23Z")

    evidence = _module.find_staging_evidence(TAG, SHA, **_sources(deploys=[earlier, DEPLOY, later]))

    assert evidence.deploy_run_id == 3030


@pytest.mark.parametrize(("release", "sha"), [("0.2.23", SHA), (TAG, SHA[:8]), (TAG, SHA.upper())])
def test_a_malformed_release_or_sha_is_a_refusal(release: str, sha: str) -> None:
    with pytest.raises(_module.NotPromotable, match="40-character SHA"):
        _module.find_staging_evidence(release, sha, **_sources())


def test_the_evidence_check_runs_on_a_bare_python_without_site_packages() -> None:
    """`cut_release.sh --prod` runs on the operator's `python3`, which has no infra2-sdk. The
    walk rule it reuses (`walk_evidence.find_walk`) must import without the SDK."""
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(REPO_ROOT / "tools" / "staging_evidence.py"), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
