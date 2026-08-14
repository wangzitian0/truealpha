"""Tests for tools/walk_evidence.py — #560 (W4).

The release run answers "is it deployed". This answers "did anyone confirm a
person can use it". They are separate because making the release run fail on a
missing walk blocked every prod release — prod requires this repo's own
successful "Deploy staging <tag>" run — including the release that would have
carried the fix.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = REPO_ROOT / "tools/walk_evidence.py"
SPEC = importlib.util.spec_from_file_location("truealpha_walk_evidence", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = _module
SPEC.loader.exec_module(_module)
check_walk_evidence = _module.check_walk_evidence


def _api(runs: list[dict], steps: list[dict] | None = None):
    seen: list[str] = []

    def gh_api(path: str) -> str:
        seen.append(path)
        if "/jobs" in path:
            return json.dumps({"jobs": [{"steps": steps or []}]})
        assert "workflows/deploy-release.yml/runs" in path, (
            "must query the release workflow's own runs, not every run in the repository"
        )
        return json.dumps({"workflow_runs": runs})

    return gh_api


def _run(rid: int, title: str, created: str = "2026-08-14T09:00:00Z") -> dict:
    return {"id": rid, "display_title": title, "event": "workflow_dispatch", "created_at": created}


_WALK_OK = [{"name": "Walk the deployed surface", "conclusion": "success"}]


def test_a_walked_release_passes(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = check_walk_evidence("prod", "v0.0.20", gh_api=_api([_run(1, "Deploy prod v0.0.20")], _WALK_OK))
    assert exit_code == 0
    assert "walked its surface" in capsys.readouterr().out


def test_no_release_run_at_all_fails(capsys: pytest.CaptureFixture[str]) -> None:
    """Today's condition: environments serve a release nothing ever walked."""
    exit_code = check_walk_evidence("prod", "v0.0.19", gh_api=_api([]))
    assert exit_code == 1
    stderr = capsys.readouterr().err
    assert "no 'Deploy prod v0.0.19' run in the last 100 deploy-release runs" in stderr
    assert "older than the window" in stderr, "the window case must not be omitted (review)"


def test_a_release_predating_the_walk_step_fails(capsys: pytest.CaptureFixture[str]) -> None:
    """A green release run is not evidence: the step may not have existed."""
    exit_code = check_walk_evidence(
        "prod",
        "v0.0.19",
        gh_api=_api([_run(7, "Deploy prod v0.0.19")], [{"name": "Confirm", "conclusion": "success"}]),
    )
    assert exit_code == 1
    assert "never verified" in capsys.readouterr().err


def test_an_unverified_walk_fails_and_says_so(capsys: pytest.CaptureFixture[str]) -> None:
    """The unconfigured-credentials path exits 0 in the release run by design;
    this is the signal that keeps it visible."""
    exit_code = check_walk_evidence(
        "prod",
        "v0.0.20",
        gh_api=_api(
            [_run(9, "Deploy prod v0.0.20")],
            [{"name": "Walk the deployed surface", "conclusion": "failure"}],
        ),
    )
    assert exit_code == 1
    stderr = capsys.readouterr().err
    assert "deployed and unverified" in stderr
    assert "9" in stderr, "the operator must be told which run to open"


def test_the_newest_matching_run_is_the_one_that_counts() -> None:
    """A redeploy of the same tag supersedes an older attempt."""
    exit_code = check_walk_evidence(
        "staging",
        "v0.0.20",
        gh_api=_api(
            [
                _run(1, "Deploy staging v0.0.20", "2026-08-01T00:00:00Z"),
                _run(2, "Deploy staging v0.0.20", "2026-08-14T00:00:00Z"),
            ],
            _WALK_OK,
        ),
    )
    assert exit_code == 0


def test_a_skipped_walk_is_not_evidence(capsys: pytest.CaptureFixture[str]) -> None:
    """The hole this file was written to close, and then briefly had.

    An unconfigured walk that exits 0 gives the step a `success` conclusion, so
    this check reported "walked its surface" about a walk that never ran —
    satisfied by the exact case it exists to catch. The workflow now SKIPS the
    step instead, and anything other than success is missing evidence.
    """
    exit_code = check_walk_evidence(
        "staging",
        "v0.0.20",
        environment="staging",
        gh_api=_api(
            [_run(11, "Deploy staging v0.0.20")],
            [{"name": "Walk the deployed surface", "conclusion": "skipped"}],
        ),
    )
    assert exit_code == 1
    stderr = capsys.readouterr().err
    assert "'skipped'" in stderr
    assert "deployed and unverified" in stderr


def test_an_unconfigured_walk_is_skipped_rather_than_exiting_zero() -> None:
    """A step that exits 0 without walking reports `success`, which this
    check reads as evidence. The workflow must gate the step with an `if`."""
    workflow = (REPO_ROOT / ".github/workflows/deploy-release.yml").read_text()
    walk = workflow.split("- name: Walk the deployed surface", 1)[1].split("run: |", 1)[0]
    assert "if: ${{ steps.walk_credentials.outputs.ready == 'true' }}" in walk, (
        "an unconfigured walk must be SKIPPED, never a step that exits 0 and reports success"
    )
    body = workflow.split("- name: Walk the deployed surface", 1)[1]
    assert "exit 0" not in body.split("node e2e/walk-tree.mjs", 1)[0]


def test_the_release_lane_is_still_not_deadlocked() -> None:
    """Failing the run on unconfigured credentials blocked every prod release,
    since prod requires a successful staging run. The credential probe must
    report, not fail."""
    workflow = (REPO_ROOT / ".github/workflows/deploy-release.yml").read_text()
    probe = workflow.split("id: walk_credentials", 1)[1].split("- name: Walk the deployed", 1)[0]
    assert "ready=false" in probe, "an unconfigured walk must be reported"
    assert "exit 1" not in probe, "and must not fail the release lane"
    assert "UNVERIFIED" in probe, "but must be unmistakable in the run summary"
