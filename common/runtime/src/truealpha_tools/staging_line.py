"""Stop the staging release train while staging is red (#1468 F1).

Measured on 2026-10-19 and 2026-10-20: in 22 hours the train cut 16 automatic tags. Only one
walk passed in that time, on a hand-cut tag. Tags kept failing at steps where earlier tags had
failed. Nothing stopped the train, and nothing owned the red state.

This module holds the rule. It reads no network and no clock. `auto_release.py` gathers the
facts and calls `staging_red`.

A release is one tag push. A tag that fails its own `ci-required` run is deleted by
`cut_release.sh --auto`. The next attempt reuses the number. Each push is its own release here.

Each release passes three stages in this order:

1. `ci-required`: the CI run of the tag push.
2. `deploy-release`: the staging deploy of the tag.
3. `walk-release`: the browser walk of the deployed surface.

A release has a failure when one stage has no passing run and at least one failed run. The
failure names the stage and the failing step of its newest failed run. A stage with no run at
all gives no verdict, and the next stage decides. The `ci-required` summary job only repeats
the result of other jobs, so it never names the step. The `auto-release-staging` run of the
same release never names a step either. That run only echoes the failure of one of the three
stages. A failed run whose step the API does not name still counts as a failure. It matches
another such failure of the same stage, so that an unreadable cause never lets the train run on.

A walk that passes proves that staging works. A release with a passing walk has no failure,
even when one of its deploys failed.

The train stops when both conditions hold:

* the newest release has no passing walk, and
* two releases in a row failed at the same stage and step, and both failures are newer than
  the newest passing walk of any release.

The stop holds until a walk passes. A later release that fails at a different step does not
lift it. A hand-cut release does not lift it. Only a passing walk of any tag lifts it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from truealpha_tools.release_runs import FAILED_CONCLUSIONS

#: The number of releases in a row that fail at the same step before the train stops.
SAME_STEP_FAILURES = 2
CI_STAGE = "ci-required"
DEPLOY_STAGE = "deploy-release"
WALK_STAGE = "walk-release"
#: The label of a failure whose step the API does not name.
UNNAMED_STEP = "(no failed step named)"
ISSUE_TITLE_PREFIX = "[truealpha] Staging red since "


@dataclass(frozen=True)
class StageRun:
    """One run of a stage. An empty `conclusion` means that the run is still in flight."""

    conclusion: str
    finished_at: datetime
    #: "<job>::<step>" of the failing step. Empty when the run did not fail or the step is unknown.
    failed_step: str = ""
    run: int = 0

    @property
    def passed(self) -> bool:
        return self.conclusion == "success"

    @property
    def failed(self) -> bool:
        return self.conclusion in FAILED_CONCLUSIONS


@dataclass(frozen=True)
class Failure:
    stage: str
    step: str
    at: datetime
    run: int

    @property
    def label(self) -> str:
        return f"{self.stage} / {self.step.replace('::', ' / ') or UNNAMED_STEP}"

    def same_step(self, other: Failure) -> bool:
        """True when both failures name the same stage and the same step.

        Two failures of one stage that name no step are the same step. An unreadable cause
        is no reason to let the train run on.
        """
        return self.stage == other.stage and self.step == other.step


@dataclass(frozen=True)
class StagingRelease:
    tag: str
    cut_at: datetime
    ci: tuple[StageRun, ...] = ()
    deploys: tuple[StageRun, ...] = ()
    walks: tuple[StageRun, ...] = ()

    @property
    def walked_green(self) -> bool:
        return any(run.passed for run in self.walks)

    def failure(self) -> Failure | None:
        """The failure of this release, or None when it has none.

        The first stage with runs and without a passing run decides. A stage with no failed run
        gives no verdict. A run that is in flight does not lift an earlier failure: a re-run is
        no pass until it passes.
        """
        if self.walked_green:
            return None
        for stage, runs in ((CI_STAGE, self.ci), (DEPLOY_STAGE, self.deploys), (WALK_STAGE, self.walks)):
            if not runs or any(run.passed for run in runs):
                continue
            failed = [run for run in runs if run.failed]
            if not failed:
                return None
            newest = max(failed, key=lambda run: run.finished_at)
            return Failure(stage, newest.failed_step, newest.finished_at, newest.run)
        return None


@dataclass(frozen=True)
class StagingRed:
    #: The tag of the first release in the first pair that failed at the same step.
    since: str
    label: str
    #: The tags of every release that failed since the newest passing walk, oldest first.
    failed_tags: tuple[str, ...]
    newest_failed: str
    #: The newest failed run of the newest failed release.
    run: int
    last_green_walk: datetime | None

    @property
    def title(self) -> str:
        return f"{ISSUE_TITLE_PREFIX}{self.since}: {self.label}"

    @property
    def state_key(self) -> str:
        """The text that changes when the issue needs a new comment."""
        return f"{self.since}|{self.label}|{self.newest_failed}"


def staging_red(
    releases: Iterable[StagingRelease], *, same_step_failures: int = SAME_STEP_FAILURES
) -> StagingRed | None:
    """The red state of staging, or None when the train may cut a tag."""
    if same_step_failures < 2:
        raise ValueError(f"same_step_failures must be >= 2, got {same_step_failures}")
    ordered = sorted(releases, key=lambda release: (release.cut_at, release.tag))
    if not ordered or ordered[-1].walked_green:
        return None
    last_green = max((run.finished_at for r in ordered for run in r.walks if run.passed), default=None)
    streak: list[tuple[StagingRelease, Failure]] = []
    failed: list[tuple[StagingRelease, Failure]] = []
    tripped: tuple[StagingRelease, Failure] | None = None
    for release in ordered:
        failure = release.failure()
        if failure is None or (last_green is not None and failure.at <= last_green):
            streak = []
            continue
        failed.append((release, failure))
        if streak and streak[-1][1].same_step(failure):
            streak.append((release, failure))
        else:
            streak = [(release, failure)]
        if tripped is None and len(streak) >= same_step_failures:
            tripped = streak[0]
    if tripped is None:
        return None
    newest, newest_failure = failed[-1]
    return StagingRed(
        since=tripped[0].tag,
        label=tripped[1].label,
        failed_tags=tuple(dict.fromkeys(release.tag for release, _ in failed)),
        newest_failed=newest.tag,
        run=newest_failure.run,
        last_green_walk=last_green,
    )


def describe(releases: Iterable[StagingRelease], red: StagingRed | None) -> str:
    """One log line that tells the state of the staging line."""
    ordered = sorted(releases, key=lambda release: (release.cut_at, release.tag))
    if red is not None:
        return (
            f"red since {red.since} at {red.label}; failed since the last green walk: "
            f"{', '.join(red.failed_tags)}; a green walk of any tag resumes the train"
        )
    if not ordered:
        return "open; no staging release was found in the run history"
    newest = ordered[-1]
    failure = newest.failure()
    if newest.walked_green:
        state = "walked green"
    elif failure is not None:
        state = f"failed at {failure.label}"
    else:
        state = "not walked yet"
    return f"open; newest release {newest.tag} {state}; no two releases in a row failed at one step since the last green walk"
