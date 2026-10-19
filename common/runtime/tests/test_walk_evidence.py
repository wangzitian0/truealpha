"""Tests for tools/walk_evidence.py — #560 (W4), moved to walk-release.yml by #855/#860.

The release run answers "is it deployed". This answers "did anyone confirm a
person can use it". They are separate because making the release run fail on a
missing walk blocked every prod release — prod requires this repo's own
successful "Deploy staging <tag>" run — including the release that would have
carried the fix.

#855/#860: the walk itself moved out of deploy-release.yml's own run into its
own deferred workflow, walk-release.yml, whose run-name echoes the upstream
title as "Walk Deploy <deploy_type> <release>". This file's fixtures build
walk-release.yml runs, not deploy-release.yml ones.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from truealpha_runtime.testing import load_tool

REPO_ROOT = Path(__file__).resolve().parents[3]
_module = load_tool("walk_evidence")
check_walk_evidence = _module.check_walk_evidence


def _api(runs: list[dict], steps: list[dict] | None = None, deploys: list[dict] | None = None):
    seen: list[str] = []

    def gh_api(path: str) -> str:
        seen.append(path)
        if "/jobs" in path:
            return json.dumps({"jobs": [{"steps": steps or []}]})
        if "workflows/deploy-release.yml/runs" in path:
            return json.dumps({"workflow_runs": deploys or []})
        assert "workflows/walk-release.yml/runs" in path, (
            "must query the walk workflow's own runs, not every run in the repository"
        )
        return json.dumps({"workflow_runs": runs})

    return gh_api


def _run(
    rid: int,
    title: str,
    created: str = "2026-08-14T09:00:00Z",
    event: str = "workflow_run",
    status: str = "completed",
    conclusion: str = "success",
    updated: str | None = None,
) -> dict:
    return {
        "id": rid,
        "display_title": title,
        "event": event,
        "created_at": created,
        "status": status,
        "conclusion": conclusion,
        "updated_at": updated or created,
    }


_WALK_OK = [{"name": "Walk the deployed surface", "conclusion": "success"}]


def test_a_walked_release_passes(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = check_walk_evidence("prod", "v0.0.20", gh_api=_api([_run(1, "Walk Deploy prod v0.0.20")], _WALK_OK))
    assert exit_code == 0
    assert "walked its surface" in capsys.readouterr().out


def test_a_manually_rerun_walk_still_counts(capsys: pytest.CaptureFixture[str]) -> None:
    """#811's flake recovery: `gh workflow run walk-release.yml -f ...` dispatches a
    manual re-run, which is real evidence exactly like the automatic one."""
    exit_code = check_walk_evidence(
        "staging",
        "v0.0.20",
        gh_api=_api([_run(2, "Walk Deploy staging v0.0.20", event="workflow_dispatch")], _WALK_OK),
    )
    assert exit_code == 0
    assert "walked its surface" in capsys.readouterr().out


def test_no_release_run_at_all_fails(capsys: pytest.CaptureFixture[str]) -> None:
    """Today's condition: environments serve a release nothing ever walked."""
    exit_code = check_walk_evidence("prod", "v0.0.19", gh_api=_api([]))
    assert exit_code == 1
    stderr = capsys.readouterr().err
    assert "no 'Walk Deploy prod v0.0.19' run in the last 100 walk-release runs" in stderr
    assert "older than the window" in stderr, "the window case must not be omitted (review)"


def test_a_release_predating_the_walk_step_fails(capsys: pytest.CaptureFixture[str]) -> None:
    """A green release run is not evidence: the step may not have existed."""
    exit_code = check_walk_evidence(
        "prod",
        "v0.0.19",
        gh_api=_api([_run(7, "Walk Deploy prod v0.0.19")], [{"name": "Confirm", "conclusion": "success"}]),
    )
    assert exit_code == 1
    assert "never verified" in capsys.readouterr().err


def test_an_unverified_walk_fails_and_says_so(capsys: pytest.CaptureFixture[str]) -> None:
    """The unconfigured-credentials path exits 0 in the walk run by design;
    this is the signal that keeps it visible."""
    exit_code = check_walk_evidence(
        "prod",
        "v0.0.20",
        gh_api=_api(
            [_run(9, "Walk Deploy prod v0.0.20")],
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
                _run(1, "Walk Deploy staging v0.0.20", "2026-08-01T00:00:00Z"),
                _run(2, "Walk Deploy staging v0.0.20", "2026-08-14T00:00:00Z"),
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
            [_run(11, "Walk Deploy staging v0.0.20")],
            [{"name": "Walk the deployed surface", "conclusion": "skipped"}],
        ),
    )
    assert exit_code == 1
    stderr = capsys.readouterr().err
    assert "'skipped'" in stderr
    assert "deployed and unverified" in stderr


# --- a release caught mid-flight (2026-09-16, #876) ---------------------------


class _Clock:
    """A clock that advances only when the tool sleeps, so the wait is exact and instant."""

    def __init__(self, start: str) -> None:
        self.now = datetime.fromisoformat(start.replace("Z", "+00:00"))
        self.slept: list[float] = []

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += timedelta(seconds=seconds)


def _settling_api(states: list[tuple[list[dict], list[dict]]], steps: list[dict]):
    """Each poll of the deploy runs advances to the next (deploys, walks) state; the last one sticks."""
    polls = {"n": -1}

    def gh_api(path: str) -> str:
        if "/jobs" in path:
            return json.dumps({"jobs": [{"steps": steps}]})
        if "workflows/deploy-release.yml/runs" in path:
            polls["n"] = min(polls["n"] + 1, len(states) - 1)
            return json.dumps({"workflow_runs": states[polls["n"]][0]})
        return json.dumps({"workflow_runs": states[polls["n"]][1]})

    return gh_api


def test_a_release_whose_deploy_is_still_running_is_waited_for_not_failed(capsys: pytest.CaptureFixture[str]) -> None:
    """The live false alarm: staging served v0.0.72 at 09:29 while `Deploy staging v0.0.72`
    was still running, so the dispatched freshness run filed "no walk" for a release that was
    walked minutes later. The check now waits for the outcome and judges that."""
    clock = _Clock("2026-09-16T09:29:00Z")
    deploy = "Deploy staging v0.0.72"
    states = [
        ([_run(1, deploy, "2026-09-16T09:25:00Z", status="in_progress", conclusion="")], []),
        (
            [_run(1, deploy, "2026-09-16T09:25:00Z", updated="2026-09-16T09:34:00Z")],
            [_run(2, f"Walk {deploy}", "2026-09-16T09:34:02Z", status="in_progress", conclusion="")],
        ),
        (
            [_run(1, deploy, "2026-09-16T09:25:00Z", updated="2026-09-16T09:34:00Z")],
            [_run(2, f"Walk {deploy}", "2026-09-16T09:34:02Z", updated="2026-09-16T09:36:00Z")],
        ),
    ]
    exit_code = check_walk_evidence(
        "staging", "v0.0.72", gh_api=_settling_api(states, _WALK_OK), sleep=clock.sleep, clock=clock
    )
    assert exit_code == 0, capsys.readouterr().err
    out = capsys.readouterr().out
    assert "is in_progress" in out and "walked its surface" in out
    assert len(clock.slept) == 2


def test_a_green_deploy_whose_walk_has_not_been_created_yet_is_waited_for() -> None:
    """The workflow_run hand-off: seconds between the deploy completing and the walk existing."""
    clock = _Clock("2026-09-16T09:34:10Z")
    deploy = "Deploy staging v0.0.72"
    done = _run(1, deploy, "2026-09-16T09:25:00Z", updated="2026-09-16T09:34:00Z")
    states = [
        ([done], []),
        ([done], [_run(2, f"Walk {deploy}", "2026-09-16T09:34:20Z", updated="2026-09-16T09:36:00Z")]),
    ]
    assert (
        check_walk_evidence(
            "staging", "v0.0.72", gh_api=_settling_api(states, _WALK_OK), sleep=clock.sleep, clock=clock
        )
        == 0
    )
    assert len(clock.slept) == 1


def test_a_green_deploy_that_never_got_a_walk_is_red_once_the_grace_is_over(capsys: pytest.CaptureFixture[str]) -> None:
    """The grace is for the hand-off, not a place to hide an unwalked release."""
    clock = _Clock("2026-09-16T10:00:00Z")
    done = _run(1, "Deploy staging v0.0.72", "2026-09-16T09:25:00Z", updated="2026-09-16T09:34:00Z")
    exit_code = check_walk_evidence(
        "staging", "v0.0.72", gh_api=_settling_api([([done], [])], _WALK_OK), sleep=clock.sleep, clock=clock
    )
    assert exit_code == 1
    assert "no 'Walk Deploy staging v0.0.72' run" in capsys.readouterr().err
    assert clock.slept == []


def test_a_failed_deploy_is_an_outcome_not_in_flight(capsys: pytest.CaptureFixture[str]) -> None:
    """walk-release never runs after a red deploy, so there is nothing to wait for."""
    clock = _Clock("2026-09-16T09:34:10Z")
    failed = _run(
        1, "Deploy staging v0.0.72", "2026-09-16T09:25:00Z", conclusion="failure", updated="2026-09-16T09:34:00Z"
    )
    exit_code = check_walk_evidence(
        "staging", "v0.0.72", gh_api=_settling_api([([failed], [])], _WALK_OK), sleep=clock.sleep, clock=clock
    )
    assert exit_code == 1
    assert clock.slept == []
    assert "walk evidence missing" in capsys.readouterr().err


def test_a_release_still_in_flight_after_the_bound_is_red(capsys: pytest.CaptureFixture[str]) -> None:
    clock = _Clock("2026-09-16T09:29:00Z")
    stuck = _run(1, "Deploy staging v0.0.72", "2026-09-16T09:25:00Z", status="queued", conclusion="")
    exit_code = check_walk_evidence(
        "staging",
        "v0.0.72",
        gh_api=_settling_api([([stuck], [])], _WALK_OK),
        sleep=clock.sleep,
        clock=clock,
        wait=timedelta(minutes=2),
    )
    assert exit_code == 1
    err = capsys.readouterr().err
    assert "still in flight" in err and "is queued" in err
    assert sum(clock.slept) >= 120
