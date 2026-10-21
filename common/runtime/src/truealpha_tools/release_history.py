"""Read the recent staging releases from the GitHub run lists (#1468 F1).

`staging_line.py` holds the rule and reads nothing. This module builds its input.

A release is one tag push. The tags come from three places:

* the staging `deploy-release` and `walk-release` runs. Those workflows run only for a release.
  One page of each reaches back for weeks, also while the train is stopped. The `ci-required`
  list is not a source of tags: every push to main adds a run to it.
* the failed `ci-required` runs of tag pushes. A tag whose own CI failed is deleted, and it has
  no deploy and no walk run. Without this list a hand-cut of the next number would hide it.
* the tag that the train would cut next. It may hold earlier pushes that were deleted.

The `ci-required` runs of each tag are read by branch name, which also finds a deleted tag.

A walk counts as passed only when its walk step passed. A `walk-release` run ends `success` with
the walk step skipped when the walk credentials are not set. That run walked nothing.

Stdlib only: `auto-release-staging.yml` runs the train with no `uv sync`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import replace
from datetime import datetime

from truealpha_tools.release_runs import (
    CI_WORKFLOW,
    DEPLOY_TITLE,
    DEPLOY_WORKFLOW,
    SUMMARY_JOB,
    TAG_RE,
    WALK_TITLE,
    WALK_WORKFLOW,
    parse_time,
    tag_key,
    workflow_runs,
)
from truealpha_tools.staging_line import FAILED_CONCLUSIONS, StageRun, StagingRelease
from truealpha_tools.walk_evidence import WALK_STEP_NAME

GitHubGet = Callable[[str], object]

#: How many of the newest tags the rule reads. A stop that 16 newer tags push out of this window
#: lifts without a passing walk, but a stopped train cuts no tag and a hand-cut tag is rare.
RECENT_STAGING_TAGS = 16
#: How many pushes of one tag the rule reads. One tag can fail its own CI many times.
CI_ATTEMPTS_PER_TAG = 8
RUN_PAGE_SIZE = 100

Run = dict[str, object]


def _steps(job: Run) -> list[Run]:
    steps = job.get("steps")
    return [step for step in steps if isinstance(step, dict)] if isinstance(steps, list) else []


class JobReader:
    """Read the jobs of runs, once for each run."""

    def __init__(self, repo: str, get: GitHubGet) -> None:
        self._repo = repo
        self._get = get
        self._cache: dict[int, list[Run]] = {}

    def jobs(self, run_id: int) -> list[Run]:
        if run_id not in self._cache:
            payload = self._get(f"/repos/{self._repo}/actions/runs/{run_id}/jobs?per_page=100")
            jobs = payload.get("jobs") if isinstance(payload, dict) else None
            if not isinstance(jobs, list):
                raise ValueError(f"unexpected jobs payload for run {run_id}: {str(payload)[:200]}")
            self._cache[run_id] = [job for job in jobs if isinstance(job, dict)]
        return self._cache[run_id]

    def failed_step(self, run_id: int) -> str:
        """ "<job>::<step>" of the failed step that sorts first, or "" when the API names none.

        The `ci-required` summary job is skipped: it fails whenever another job fails, so it
        names no cause. Sorting makes the label the same for the same fault on every read.
        """
        failed = sorted(
            f"{job.get('name')}::{step.get('name')}"
            for job in self.jobs(run_id)
            if job.get("conclusion") == "failure" and job.get("name") != SUMMARY_JOB
            for step in _steps(job)
            if step.get("conclusion") == "failure"
        )
        return failed[0] if failed else ""

    def walk_step_passed(self, run_id: int) -> bool:
        return any(
            step.get("name") == WALK_STEP_NAME and step.get("conclusion") == "success"
            for job in self.jobs(run_id)
            for step in _steps(job)
        )


def _run_id(run: Run) -> int:
    run_id = run.get("id")
    if not isinstance(run_id, int):
        raise ValueError(f"workflow run without an integer id: {str(run)[:200]}")
    return run_id


def _time(run: Run, key: str) -> datetime:
    stamp = parse_time(run.get(key))
    if stamp is None:
        raise ValueError(f"workflow run {run.get('id')} has no valid {key}")
    return stamp


def stage_runs(reader: JobReader, runs: Iterable[Run], *, walk: bool = False) -> tuple[StageRun, ...]:
    """The runs of one stage. Only the newest failed run gets a step name.

    `StagingRelease.failure` reads the step of the newest failed run and no other, so the jobs of
    an older failed run are not worth one API call each. With `walk`, a run that ended `success`
    without a passing walk step gets the conclusion `skipped`.
    """
    found: list[StageRun] = []
    for run in runs:
        run_id = _run_id(run)
        conclusion = str(run.get("conclusion") or "") if run.get("status") == "completed" else ""
        if walk and conclusion == "success" and not reader.walk_step_passed(run_id):
            conclusion = "skipped"
        found.append(StageRun(conclusion, _time(run, "updated_at"), "", run_id))
    failed = [run for run in found if run.conclusion in FAILED_CONCLUSIONS]
    if failed:
        newest = max(failed, key=lambda run: run.finished_at)
        found[found.index(newest)] = replace(newest, failed_step=reader.failed_step(newest.run))
    return tuple(found)


def _staging_runs(repo: str, workflow: str, title, get: GitHubGet) -> dict[str, list[Run]]:
    by_tag: dict[str, list[Run]] = {}
    for run in workflow_runs(get(f"/repos/{repo}/actions/workflows/{workflow}/runs?per_page={RUN_PAGE_SIZE}")):
        match = title.match(str(run.get("display_title") or ""))
        if match is not None and match["deploy_type"] == "staging" and TAG_RE.match(match["release"]):
            by_tag.setdefault(match["release"], []).append(run)
    return by_tag


def _failed_tag_pushes(repo: str, get: GitHubGet) -> set[str]:
    path = f"/repos/{repo}/actions/workflows/{CI_WORKFLOW}/runs?event=push&status=failure&per_page={RUN_PAGE_SIZE}"
    return {
        str(run["head_branch"])
        for run in workflow_runs(get(path))
        if TAG_RE.match(str(run.get("head_branch") or "")) and run.get("conclusion") in FAILED_CONCLUSIONS
    }


def _order(name: str) -> tuple[int, int, int]:
    return tag_key(name) or (0, 0, 0)


def tag_names(
    repo: str,
    get: GitHubGet,
    known: Iterable[str],
    *,
    next_tag: str | None,
    recent: int = RECENT_STAGING_TAGS,
) -> list[str]:
    """The tags to read, oldest first. `known` holds the tags of the staging deploy and walk runs."""
    names = sorted(set(known), key=_order)[-recent:]
    floor = _order(names[0]) if names else None
    names.extend(
        name
        for name in sorted(_failed_tag_pushes(repo, get))
        if name not in names and (floor is None or _order(name) >= floor)
    )
    if next_tag is not None and next_tag not in names:
        names.append(next_tag)
    return sorted(names, key=_order)


def _pushes(repo: str, name: str, get: GitHubGet) -> list[Run]:
    """The `ci-required` runs of the pushes of one tag, oldest first. Also finds a deleted tag."""
    path = f"/repos/{repo}/actions/workflows/{CI_WORKFLOW}/runs?branch={name}&event=push&per_page=20"
    runs = [run for run in workflow_runs(get(path)) if run.get("head_branch") == name]
    return sorted(runs, key=lambda run: str(run.get("created_at")))[-CI_ATTEMPTS_PER_TAG:]


def read_releases(
    repo: str, get: GitHubGet, *, next_tag: str | None = None, recent: int = RECENT_STAGING_TAGS
) -> tuple[StagingRelease, ...]:
    """The recent staging releases, one for each tag push, with the outcome of each stage.

    A deploy and a walk run only after a green CI run, so they belong to the newest push of the
    tag. A manual walk can start a moment before that push, so no time filter applies. A tag
    with deploy or walk runs and no CI run in the list is one release, dated by its first run.
    The tag that the train would cut next has no run yet, and it is no release.
    """
    deploys = _staging_runs(repo, DEPLOY_WORKFLOW, DEPLOY_TITLE, get)
    walks = _staging_runs(repo, WALK_WORKFLOW, WALK_TITLE, get)
    reader = JobReader(repo, get)
    releases: list[StagingRelease] = []
    for name in tag_names(repo, get, [*deploys, *walks], next_tag=next_tag, recent=recent):
        pushes = _pushes(repo, name, get)
        runs = deploys.get(name, []) + walks.get(name, [])
        if not pushes and runs:
            first = min(_time(run, "created_at") for run in runs)
            releases.append(
                StagingRelease(
                    name,
                    first,
                    deploys=stage_runs(reader, deploys.get(name, [])),
                    walks=stage_runs(reader, walks.get(name, []), walk=True),
                )
            )
        for index, push in enumerate(pushes):
            newest = index == len(pushes) - 1
            releases.append(
                StagingRelease(
                    name,
                    _time(push, "created_at"),
                    ci=stage_runs(reader, [push]),
                    deploys=stage_runs(reader, deploys.get(name, [])) if newest else (),
                    walks=stage_runs(reader, walks.get(name, []), walk=True) if newest else (),
                )
            )
    return tuple(releases)
