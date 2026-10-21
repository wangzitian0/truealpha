"""Read the recent staging releases from the GitHub run lists (#1468 F1).

`staging_line.py` holds the rule and reads nothing. This module builds its input.

A release is one tag push. The rule needs only the releases that failed after the newest passing
walk, so the reader starts at that walk and reads forward. It reads these tags:

* the tag of the newest passing walk, so that the walk is in the input,
* every tag with a staging deploy or walk run that changed after that walk. Those workflows run
  only for a release. One page of each reaches back for weeks, also while the train is stopped.
  The `ci-required` list is no source of tags: every push to main adds a run to it,
* every tag whose own `ci-required` run failed after that walk. Such a tag is deleted, and it has
  no deploy and no walk run. A hand cut of the next number would hide it without this list,
* the tag that the train would cut next. It may hold earlier pushes that were deleted.

The window then takes every known tag from the lowest of these tags up. A release cut between two
failing releases breaks their run in the rule, also when it ended before the passing walk.

The `ci-required` runs of each tag are read by branch name, which also finds a deleted tag. With no
passing walk in the lists, the reader takes the newest `MAX_TAGS` tags.

A walk counts as passed only when its walk step passed. A `walk-release` run ends `success` with
the walk step skipped when the walk credentials are not set. That run walked nothing.

A re-run keeps the run id. While it is in flight, cancelled or skipped, the run list shows that
run and hides the attempt before it. The reader reads that attempt, so that a re-run does not lift
a stop and does not hide a passing walk.

Stdlib only: `auto-release-staging.yml` runs the train with no `uv sync`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, datetime

from truealpha_tools.release_runs import (
    CI_WORKFLOW,
    DEPLOY_PREFIX,
    DEPLOY_TITLE,
    DEPLOY_WORKFLOW,
    FAILED_CONCLUSIONS,
    SUMMARY_JOB,
    TAG_RE,
    WALK_PREFIX,
    WALK_TITLE,
    WALK_WORKFLOW,
    GitHubGet,
    parse_time,
    tag_key,
    workflow_runs,
)
from truealpha_tools.staging_line import StageRun, StagingRelease
from truealpha_tools.walk_evidence import WALK_STEP_NAME

#: The most tags that one read takes. A stop needs the releases since the last passing walk, and a
#: stopped train cuts no tag, so this bound is far above what a normal read finds.
MAX_TAGS = 40
#: How many pushes of one tag the rule reads. One tag can fail its own CI many times.
CI_ATTEMPTS_PER_TAG = 8
RUN_PAGE_SIZE = 100
#: The API lists a run by one conclusion at a time, so each failed conclusion has its own query.
FAILED_PUSH_STATUSES = tuple(sorted(FAILED_CONCLUSIONS))

Run = dict[str, object]


def _steps(job: Run) -> list[Run]:
    steps = job.get("steps")
    return [step for step in steps if isinstance(step, dict)] if isinstance(steps, list) else []


class JobReader:
    """Read the jobs and the attempts of runs, once for each."""

    def __init__(self, repo: str, get: GitHubGet) -> None:
        self._repo = repo
        self._get = get
        self._jobs: dict[tuple[int, int | None], list[Run]] = {}

    def jobs(self, run_id: int, attempt: int | None = None) -> list[Run]:
        key = (run_id, attempt)
        if key not in self._jobs:
            where = f"runs/{run_id}" if attempt is None else f"runs/{run_id}/attempts/{attempt}"
            payload = self._get(f"/repos/{self._repo}/actions/{where}/jobs?per_page=100")
            jobs = payload.get("jobs") if isinstance(payload, dict) else None
            if not isinstance(jobs, list):
                raise ValueError(f"unexpected jobs payload for run {run_id}: {str(payload)[:200]}")
            self._jobs[key] = [job for job in jobs if isinstance(job, dict)]
        return self._jobs[key]

    def attempt(self, run_id: int, attempt: int) -> Run:
        payload = self._get(f"/repos/{self._repo}/actions/runs/{run_id}/attempts/{attempt}")
        if not isinstance(payload, dict):
            raise ValueError(f"unexpected attempt payload for run {run_id}: {str(payload)[:200]}")
        return payload

    def failed_step(self, run_id: int, attempt: int | None = None) -> str:
        """ "<job>::<step>" of the step that failed first, or "" when the API names none.

        The first failure in time is the cause. A step that runs after a failure, such as a probe
        with `if: !cancelled()`, fails because of it. Steps that failed at the same time, and
        steps with no time, follow the order of their labels. The order of the payload is not
        stable, and the same fault must get the same label on every read. The `ci-required`
        summary job is skipped: it fails whenever another job fails, so it names no cause.
        """
        failed = [
            (
                not step.get("completed_at"),
                str(step.get("completed_at") or ""),
                f"{job.get('name')}::{step.get('name')}",
            )
            for job in self.jobs(run_id, attempt)
            for step in _steps(job)
            if job.get("conclusion") == "failure"
            and job.get("name") != SUMMARY_JOB
            and step.get("conclusion") == "failure"
        ]
        return min(failed)[2] if failed else ""

    def walk_step_passed(self, run_id: int, attempt: int | None = None) -> bool:
        return any(
            step.get("name") == WALK_STEP_NAME and step.get("conclusion") == "success"
            for job in self.jobs(run_id, attempt)
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

    `StagingRelease.failure` reads the step of the newest failed run only. The jobs of an older
    failed run are not worth one API call each. With `walk`, a run that ended `success` without a
    passing walk step gets the conclusion `skipped`. A re-run that has not passed adds the
    attempt that it hides.
    """
    found: list[tuple[StageRun, int | None]] = []
    for run in runs:
        run_id = _run_id(run)
        done = run.get("status") == "completed"
        conclusion = str(run.get("conclusion") or "") if done else ""
        if walk and conclusion == "success" and not reader.walk_step_passed(run_id):
            conclusion = "skipped"
        found.append((StageRun(conclusion, _time(run, "updated_at"), "", run_id), None))
        attempt = run.get("run_attempt")
        if (
            isinstance(attempt, int)
            and attempt > 1
            and conclusion != "success"
            and conclusion not in FAILED_CONCLUSIONS
        ):
            # A re-run keeps the run id and hides the attempt before it behind a run that is in
            # flight, cancelled or skipped. That attempt failed, or it passed, and the rule needs to know.
            earlier = reader.attempt(run_id, attempt - 1)
            before = str(earlier.get("conclusion") or "")
            if before in FAILED_CONCLUSIONS or (
                walk and before == "success" and reader.walk_step_passed(run_id, attempt - 1)
            ):
                found.append((StageRun(before, _time(earlier, "updated_at"), "", run_id), attempt - 1))
    failed = [index for index, (run, _) in enumerate(found) if run.conclusion in FAILED_CONCLUSIONS]
    result = [run for run, _ in found]
    if failed:
        index = max(failed, key=lambda i: found[i][0].finished_at)
        newest, attempt = found[index]
        result[index] = replace(newest, failed_step=reader.failed_step(newest.run, attempt))
    return tuple(result)


def _staging_runs(
    repo: str, workflow: str, title: re.Pattern[str], prefix: str, get: GitHubGet
) -> dict[str, list[Run]]:
    """The staging runs of one release workflow, by tag.

    A page of runs where no title starts with `prefix` means that the run-name changed. That would
    look like an empty history, so it raises. A title of another deploy type, such as
    `Deploy preview/tag v1`, starts with the prefix and is no drift.
    """
    runs = workflow_runs(get(f"/repos/{repo}/actions/workflows/{workflow}/runs?per_page={RUN_PAGE_SIZE}"))
    by_tag: dict[str, list[Run]] = {}
    for run in runs:
        match = title.match(str(run.get("display_title") or ""))
        if match is not None and match["deploy_type"] == "staging" and TAG_RE.match(match["release"]):
            by_tag.setdefault(match["release"], []).append(run)
    if runs and not any(str(run.get("display_title") or "").startswith(prefix) for run in runs):
        raise ValueError(f"no run title of {workflow} starts with {prefix!r}; the run-name changed")
    return by_tag


def _failed_tag_pushes(repo: str, get: GitHubGet) -> dict[str, datetime]:
    """The tags whose own `ci-required` run failed, with the time of the latest failed run."""
    failed: dict[str, datetime] = {}
    for status in FAILED_PUSH_STATUSES:
        path = f"/repos/{repo}/actions/workflows/{CI_WORKFLOW}/runs?event=push&status={status}&per_page={RUN_PAGE_SIZE}"
        for run in workflow_runs(get(path)):
            branch = str(run.get("head_branch") or "")
            if TAG_RE.match(branch):
                failed[branch] = max(failed.get(branch, datetime.min.replace(tzinfo=UTC)), _time(run, "updated_at"))
    return failed


def _newest_green_walk(reader: JobReader, walks: dict[str, list[Run]]) -> tuple[datetime, str] | None:
    """The finish time and the tag of the newest walk run whose walk step passed."""
    passed = sorted(
        (
            (_time(run, "updated_at"), tag, run)
            for tag, runs in walks.items()
            for run in runs
            if run.get("conclusion") == "success"
        ),
        key=lambda item: item[0],
        reverse=True,
    )
    for finished, tag, run in passed:
        if reader.walk_step_passed(_run_id(run)):
            return finished, tag
    return None


def _order(name: str) -> tuple[int, int, int]:
    return tag_key(name) or (0, 0, 0)


def tag_names(
    repo: str,
    get: GitHubGet,
    deploys: dict[str, list[Run]],
    walks: dict[str, list[Run]],
    green: tuple[datetime, str] | None,
    *,
    next_tag: str | None,
    max_tags: int = MAX_TAGS,
) -> list[str]:
    """The tags to read, oldest first.

    The core is every tag with a run that changed after the newest passing walk, and the tag of
    that walk. The window then takes every known tag from the lowest core tag up. A release that
    was cut between two failing releases but ended before the walk still breaks their run, and the
    rule needs it. With no passing walk, the window takes the newest `max_tags` known tags.
    """
    failed = _failed_tag_pushes(repo, get)
    known = {*deploys, *walks, *failed}
    if green is None:
        core = known
    else:
        since = green[0]
        core = {tag for tag in known if _changed_after(tag, since, deploys, walks, failed)} | {green[1]}
    floor = min((_order(tag) for tag in core), default=None)
    window = [tag for tag in known if floor is None or _order(tag) >= floor]
    newest = sorted(window, key=_order)[-max_tags:]
    extra = {tag for tag in (green[1] if green else None, next_tag) if tag is not None}
    return sorted({*newest, *extra}, key=_order)


def _changed_after(
    tag: str,
    since: datetime,
    deploys: dict[str, list[Run]],
    walks: dict[str, list[Run]],
    failed: dict[str, datetime],
) -> bool:
    runs = [*deploys.get(tag, []), *walks.get(tag, [])]
    return any(_time(run, "updated_at") > since for run in runs) or (tag in failed and failed[tag] > since)


def _pushes(repo: str, name: str, get: GitHubGet) -> list[Run]:
    """The `ci-required` runs of the pushes of one tag, oldest first. Also finds a deleted tag."""
    path = f"/repos/{repo}/actions/workflows/{CI_WORKFLOW}/runs?branch={name}&event=push&per_page=20"
    runs = [run for run in workflow_runs(get(path)) if run.get("head_branch") == name]
    return sorted(runs, key=lambda run: str(run.get("created_at")))[-CI_ATTEMPTS_PER_TAG:]


def read_releases(
    repo: str, get: GitHubGet, *, next_tag: str | None = None, max_tags: int = MAX_TAGS
) -> tuple[StagingRelease, ...]:
    """The staging releases since the newest passing walk, one for each tag push.

    A deploy and a walk run only after a green CI run, so they belong to the newest push of the
    tag. A manual walk can start a moment before that push, so no time filter applies. A tag
    with deploy or walk runs and no CI run in the list is one release, dated by its first run.
    The tag that the train would cut next has no run yet, and it is no release.
    """
    deploys = _staging_runs(repo, DEPLOY_WORKFLOW, DEPLOY_TITLE, DEPLOY_PREFIX, get)
    walks = _staging_runs(repo, WALK_WORKFLOW, WALK_TITLE, WALK_PREFIX, get)
    reader = JobReader(repo, get)
    green = _newest_green_walk(reader, walks)
    releases: list[StagingRelease] = []
    for name in tag_names(repo, get, deploys, walks, green, next_tag=next_tag, max_tags=max_tags):
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
