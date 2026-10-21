"""Tests for the staging-red stop of the release train — #1468 F1.

Between 2026-10-19 06:18Z and 2026-10-20 03:57Z, `auto-release-staging` cut 16 tags in 22 hours.
Only one walk passed in that time, on a hand-cut tag. Each automatic tag failed where the tag
before it had failed. This file holds the rule that stops that: `staging_line.staging_red`, and
the way `auto_release.decide` uses it.

The replay tests run the real history of 2026-10-13 to 2026-10-21 through the production path:
`FakeGitHub` answers the REST paths as GitHub did at the moment of each automatic cut,
`release_history.read_releases` reads the releases, and `auto_release.decide` decides. The fixture
`fixtures/staging_release_history_2026-10-13_to_21.json` holds that history. Its `provenance` key
lists the `gh` commands that produced it. A replay holds back each automatic cut that `decide`
refuses and keeps every other cut, hand cuts included. A held cut leaves no deploy and no walk
behind, so later decisions see only the releases that the train would have made.

Two figures in the issue text of #1468 do not match this history, and the tests state the
measured values instead:

* The 89.4 h gap (2026-10-15 09:58Z to 2026-10-19 03:24Z) holds one tag, v0.2.7, cut 12 minutes
  before the gap ended. The gap is idle time, not a stuck train.
* The failing tags were cut in the 22 h after the gap, between the green walks of v0.2.10 and
  v0.2.23. The green walk of v0.2.11 splits that stretch in two. The tests check both parts.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from truealpha_runtime.testing import load_tool
from truealpha_tools import release_history, staging_line
from truealpha_tools.staging_line import Failure, StageRun, StagingRelease, staging_red
from truealpha_tools.walk_evidence import WALK_STEP_NAME

auto_release = load_tool("auto_release")

SHA = "a" * 40


FIXTURE = Path(__file__).parent / "fixtures" / "staging_release_history_2026-10-13_to_21.json"
REPO = "wangzitian0/truealpha"


def at(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


@dataclass(frozen=True)
class Attempt:
    """One tag push of the fixture."""

    tag: str
    cut_at: datetime
    #: True when `auto-release-staging` cut the tag. A hand cut always happens in a replay.
    auto: bool
    ci: tuple[dict, ...]
    deploys: tuple[dict, ...]
    walks: tuple[dict, ...]

    def release(self) -> StagingRelease:
        """The release as the production reader reads it at the end of the history."""

        def runs(rows: tuple[dict, ...], *, walk: bool = False) -> tuple[StageRun, ...]:
            found = []
            for row in rows:
                conclusion = row["conclusion"]
                if walk and conclusion == "success" and row.get("walk_step") != "success":
                    conclusion = "skipped"
                found.append(StageRun(conclusion, at(row["finished_at"]), row.get("failed_step", ""), row["run"]))
            return tuple(found)

        return StagingRelease(self.tag, self.cut_at, runs(self.ci), runs(self.deploys), runs(self.walks, walk=True))


def load_history() -> list[Attempt]:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))["releases"]
    history = [
        Attempt(
            row["tag"], at(row["cut_at"]), row["auto"], tuple(row["ci"]), tuple(row["deploys"]), tuple(row["walks"])
        )
        for row in raw
    ]
    return sorted(history, key=lambda attempt: attempt.cut_at)


class FakeGitHub:
    """The REST API of GitHub at the moment `when`, for the pushes in `attempts`."""

    def __init__(self, attempts: list[Attempt], when: datetime, *, page: int = 100) -> None:
        self._attempts = attempts
        self._when = when
        self._page = page
        self.calls: list[str] = []

    def _run(self, row: dict, title: str, branch: str) -> dict | None:
        if at(row["started_at"]) > self._when:
            return None
        done = at(row["finished_at"]) <= self._when
        return {
            "id": row["run"],
            "display_title": title,
            "status": "completed" if done else "in_progress",
            "conclusion": row["conclusion"] if done else None,
            "created_at": row["started_at"],
            "updated_at": row["finished_at"] if done else self._when.isoformat(),
            "head_branch": branch,
            "run_attempt": 1,
        }

    def _listing(self, rows: list[dict | None]) -> dict:
        runs = [row for row in rows if row is not None]
        runs.sort(key=lambda run: run["created_at"], reverse=True)
        return {"workflow_runs": runs[: self._page]}

    def _all(self, key: str, title: str) -> list[dict | None]:
        return [
            self._run(row, f"{title} {a.tag}", "main")
            for a in self._attempts
            if a.cut_at <= self._when
            for row in getattr(a, key)
        ]

    def _ci(self, tag: str | None = None) -> list[dict | None]:
        return [
            self._run(row, "ci-required", a.tag)
            for a in self._attempts
            if a.cut_at <= self._when and tag in (None, a.tag)
            for row in a.ci
        ]

    def jobs(self, run_id: int) -> dict:
        for a in self._attempts:
            for key in ("ci", "deploys", "walks"):
                for row in getattr(a, key):
                    if row["run"] != run_id:
                        continue
                    if row["conclusion"] in ("failure", "timed_out"):
                        job, _, step = row.get("failed_step", "").partition("::")
                        failing = [
                            {"name": job, "conclusion": "failure", "steps": [{"name": step, "conclusion": "failure"}]}
                        ]
                        if not step:
                            failing = []
                        if key == "ci":
                            summary = {
                                "name": "ci-required",
                                "conclusion": "failure",
                                "steps": [{"name": "Reject any failed", "conclusion": "failure"}],
                            }
                            failing.append(summary)
                        return {"jobs": failing}
                    if key == "walks":
                        steps = [{"name": WALK_STEP_NAME, "conclusion": row.get("walk_step", "skipped")}]
                        return {"jobs": [{"name": "walk", "conclusion": "success", "steps": steps}]}
                    return {"jobs": []}
        raise AssertionError(f"unknown run {run_id}")

    def __call__(self, path: str) -> object:
        self.calls.append(path)
        run = re.search(r"/actions/runs/(\d+)/jobs\?", path)
        if run:
            return self.jobs(int(run.group(1)))
        if "workflows/deploy-release.yml/runs" in path:
            return self._listing(self._all("deploys", "Deploy staging"))
        if "workflows/walk-release.yml/runs" in path:
            return self._listing(self._all("walks", "Walk Deploy staging"))
        branch = re.search(r"workflows/ci-required.yml/runs\?branch=([^&]+)&", path)
        if branch:
            return self._listing(self._ci(branch.group(1)))
        failed = re.search(r"workflows/ci-required.yml/runs\?event=push&status=(\w+)&", path)
        if failed:
            return self._listing([r for r in self._ci() if r is not None and r["conclusion"] == failed.group(1)])
        raise AssertionError(f"unexpected GET {path}")


# --- the real history, replayed through the production reader and decide() ------------------


@dataclass(frozen=True)
class Replay:
    cut: tuple[Attempt, ...]
    held: tuple[Attempt, ...]

    def automatic_cuts(self, start: datetime, end: datetime) -> list[str]:
        return [a.tag for a in self.cut if a.auto and start < a.cut_at <= end]


def replay(history: list[Attempt]) -> Replay:
    """Run each automatic cut of the history through `read_releases` and `decide`."""
    kept: list[Attempt] = []
    held: list[Attempt] = []
    for attempt in history:
        when = attempt.cut_at
        if attempt.auto:
            tags = {"v0.1.7": auto_release.ReleaseTag("v0.1.7", "1" * 40, None, "")}
            for done in kept:
                if done.ci and done.ci[-1]["conclusion"] == "success":
                    tags[done.tag] = auto_release.ReleaseTag(done.tag, "b" * 40, None, "")
            releases = release_history.read_releases(
                REPO, FakeGitHub(kept, when), next_tag=auto_release.next_tag(tags.values())
            )
            facts = auto_release.Facts(
                trigger_sha=SHA,
                main_head=SHA,
                head_green=True,
                tags=tuple(tags.values()),
                in_flight=(),
                now=when,
                releases=releases,
            )
            decision = auto_release.decide(facts, daily_cap=10_000)
            if not decision.release:
                assert "staging is red" in decision.reason, f"{attempt.tag}: unexpected reason {decision.reason}"
                held.append(attempt)
                continue
        kept.append(attempt)
    return Replay(tuple(kept), tuple(held))


HISTORY = load_history()


def walk_end(run: int) -> datetime:
    """The finish time of one named walk run. The run must exist and must have passed."""
    found = [w for a in HISTORY for w in a.walks if w["run"] == run]
    assert len(found) == 1 and found[0]["conclusion"] == "success", f"run {run} is not a green walk of the fixture"
    return at(found[0]["finished_at"])


def first_green_walk_end(tag: str) -> datetime:
    ends = [at(w["finished_at"]) for a in HISTORY if a.tag == tag for w in a.walks if w["conclusion"] == "success"]
    assert ends, f"{tag} has no green walk in the fixture"
    return min(ends)


WALK_V0_2_10 = walk_end(39206027670)
WALK_V0_2_11 = walk_end(39225355648)
WALK_V0_2_23 = walk_end(39367650089)
END = at("2026-10-22T00:00:00Z")


def real_automatic_cuts(start: datetime, end: datetime) -> list[str]:
    return [a.tag for a in HISTORY if a.auto and start < a.cut_at <= end]


def test_the_fixture_holds_the_measured_history() -> None:
    """The replay is only evidence when the fixture holds the failing stretch. A fixture with
    the stretch cut out would pass every replay assertion without a single held tag."""
    assert len(HISTORY) == 48
    assert real_automatic_cuts(WALK_V0_2_10, WALK_V0_2_11) == ["v0.2.11"] * 6
    assert real_automatic_cuts(WALK_V0_2_11, WALK_V0_2_23) == [
        "v0.2.12", "v0.2.13", "v0.2.14", "v0.2.15", "v0.2.16", "v0.2.17", "v0.2.19", "v0.2.20", "v0.2.21", "v0.2.22",
    ]  # fmt: skip


def test_the_89_hour_gap_is_idle_time_with_one_tag_at_its_end() -> None:
    gap_start = first_green_walk_end("v0.2.6")
    gap_end = first_green_walk_end("v0.2.7")
    assert gap_end - gap_start > timedelta(hours=89)
    cut_in_gap = [a for a in HISTORY if gap_start < a.cut_at < gap_end]
    assert [a.tag for a in cut_in_gap] == ["v0.2.7"]
    assert gap_end - cut_in_gap[0].cut_at < timedelta(minutes=15)


def test_replay_cuts_at_most_three_tags_in_the_stuck_stretch() -> None:
    """Acceptance 1 of #1468 F1. The real train cut 10 automatic tags between the green walk of
    v0.2.11 and the green walk of v0.2.23. This test fails when `decide` ignores the history."""
    cut = replay(HISTORY).automatic_cuts(WALK_V0_2_11, WALK_V0_2_23)
    assert len(cut) <= 3, f"the train cut {len(cut)} tags in the stuck stretch: {cut}"
    assert cut == ["v0.2.12", "v0.2.13", "v0.2.14"]


def test_replay_stops_the_ci_red_loop_of_v0_2_11_after_two_attempts() -> None:
    """The real train pushed v0.2.11 six times. Each push failed at `ci-required` in the same job.
    The second failure stops the train, so the third to sixth push never happen."""
    assert replay(HISTORY).automatic_cuts(WALK_V0_2_10, WALK_V0_2_11) == ["v0.2.11", "v0.2.11"]


def test_replay_holds_a_tag_only_while_staging_is_red() -> None:
    """No hold before the first failing push, and no hold after the green walk of v0.2.23."""
    held = replay(HISTORY).held
    assert [a.tag for a in held] == ["v0.2.11"] * 4 + [
        "v0.2.15", "v0.2.16", "v0.2.17", "v0.2.19", "v0.2.20", "v0.2.21", "v0.2.22",
    ]  # fmt: skip
    assert all(at("2026-10-19T06:00:00Z") < a.cut_at <= WALK_V0_2_23 for a in held)


def test_replay_resumes_the_train_after_a_green_walk() -> None:
    """Acceptance 3. The hand-cut v0.2.23 passed a walk at 04:06Z. Every automatic cut after
    it goes through, including v0.2.28 and v0.2.29 that failed at the deploy dispatch, and
    v0.2.32 whose walk failed."""
    expected = ["v0.2.24", "v0.2.25", "v0.2.26", "v0.2.27", "v0.2.28", "v0.2.29", "v0.2.32"]
    assert real_automatic_cuts(WALK_V0_2_23, END) == expected
    assert replay(HISTORY).automatic_cuts(WALK_V0_2_23, END) == expected


def test_the_production_reader_reads_the_fixture_back_from_the_newest_green_walk() -> None:
    """The reader starts at the newest passing walk and reads forward. At 10:30Z on 2026-10-19 the
    newest walk is that of the hand-cut v0.2.11, so the reader returns the seven pushes of v0.2.11 and
    v0.2.12 to v0.2.17 from the full history, held pushes included, and the rule finds the stop of v0.2.13 and v0.2.14. At the
    end of the history the newest walk is that of v0.2.31, and the line is open."""
    direct = {(a.tag, a.cut_at): a.release() for a in HISTORY}
    when = at("2026-10-19T10:30:00Z")
    read = release_history.read_releases(REPO, FakeGitHub(HISTORY, when), next_tag="v0.2.18")
    assert [r.tag for r in read] == ["v0.2.11"] * 7 + ["v0.2.12", "v0.2.13", "v0.2.14", "v0.2.15", "v0.2.16", "v0.2.17"]
    red = staging_red(read)
    assert red is not None and red.since == "v0.2.13"
    end = release_history.read_releases(REPO, FakeGitHub(HISTORY, END), next_tag="v0.2.33")
    assert [r.tag for r in end] == ["v0.2.31", "v0.2.32"]
    assert staging_red(end) is None
    for got in end:
        want = direct[(got.tag, got.cut_at)]
        assert (got.walked_green, got.failure()) == (want.walked_green, want.failure())


def test_today_a_failed_walk_is_one_failure_and_the_second_one_stops_the_train() -> None:
    """On 2026-10-21 00:43Z the walk of staging v0.2.32 failed at `Walk the deployed surface`.
    That is one failure and the train keeps running. A second staging release that fails at the
    same step stops it, and the issue title names the step. The second release here is the
    real failed walk of v0.2.32, copied one hour later."""
    releases = [a.release() for a in HISTORY]
    newest = releases[-1]
    assert newest.tag == "v0.2.32"
    failure = newest.failure()
    assert failure is not None and failure.stage == "walk-release" and failure.step == "walk::Walk the deployed surface"
    assert staging_red(releases) is None
    hour = timedelta(hours=1)
    walks = tuple(replace(w, finished_at=w.finished_at + hour, run=w.run + 1) for w in newest.walks)
    second = replace(newest, tag="v0.2.33", cut_at=newest.cut_at + hour, walks=walks)
    red = staging_red([*releases, second])
    assert red is not None
    assert red.since == "v0.2.32"
    assert red.title == "[truealpha] Staging red since v0.2.32: walk-release / walk / Walk the deployed surface"


# --- the rule on small inputs -----------------------------------------------------------------

T0 = datetime(2026, 10, 19, 6, 0, tzinfo=UTC)


def at_minute(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


def run(conclusion: str, minute: int, step: str = "", number: int = 1) -> StageRun:
    return StageRun(conclusion, at_minute(minute), step, number)


DEPLOY_STEP = "request::Dispatch the validated request to infra2"
WALK_STEP = "walk::Walk the deployed surface"
CI_STEP = "images_release / plan::Select available images"


def failed_deploy(tag: str, minute: int, step: str = DEPLOY_STEP) -> StagingRelease:
    return StagingRelease(
        tag=tag,
        cut_at=at_minute(minute),
        ci=(run("success", minute + 1),),
        deploys=(run("failure", minute + 5, step, minute),),
    )


def green(tag: str, minute: int) -> StagingRelease:
    return StagingRelease(
        tag=tag,
        cut_at=at_minute(minute),
        ci=(run("success", minute + 1),),
        deploys=(run("success", minute + 5),),
        walks=(run("success", minute + 9),),
    )


def test_one_failure_does_not_stop_the_train() -> None:
    assert staging_red([green("v1", 0), failed_deploy("v2", 20)]) is None


def test_two_releases_in_a_row_that_fail_at_the_same_step_stop_the_train() -> None:
    red = staging_red([green("v1", 0), failed_deploy("v2", 20), failed_deploy("v3", 40)])
    assert red is not None
    assert red.since == "v2"
    assert red.label == "deploy-release / request / Dispatch the validated request to infra2"
    assert red.failed_tags == ("v2", "v3")
    assert red.title == f"[truealpha] Staging red since v2: {red.label}"


def test_a_threshold_of_three_needs_three_failures_in_a_row() -> None:
    two = [failed_deploy("v2", 20), failed_deploy("v3", 40)]
    assert staging_red(two, same_step_failures=3) is None
    assert staging_red([*two, failed_deploy("v4", 60)], same_step_failures=3) is not None


def test_two_failures_at_different_steps_do_not_stop_the_train() -> None:
    other = failed_deploy("v3", 40, step="request::Confirm the staging release carries its data engine")
    assert staging_red([failed_deploy("v2", 20), other]) is None


def test_the_same_step_name_in_another_stage_is_another_step() -> None:
    walk_failed = StagingRelease(
        tag="v3",
        cut_at=at_minute(40),
        ci=(run("success", 41),),
        deploys=(run("success", 45),),
        walks=(run("failure", 49, DEPLOY_STEP),),
    )
    assert staging_red([failed_deploy("v2", 20), walk_failed]) is None


def test_failures_that_are_not_next_to_each_other_do_not_stop_the_train() -> None:
    other = failed_deploy("v3", 40, step="request::Confirm the staging release carries its data engine")
    assert staging_red([failed_deploy("v2", 20), other, failed_deploy("v4", 60)]) is None


def test_two_failures_of_one_stage_with_no_named_step_are_the_same_step() -> None:
    """An unreadable cause is no reason to let the train run on."""
    red = staging_red([failed_deploy("v2", 20, step=""), failed_deploy("v3", 40, step="")])
    assert red is not None
    assert red.label == "deploy-release / (no failed step named)"


def test_a_failure_with_no_named_step_is_not_the_same_as_a_named_step() -> None:
    assert staging_red([failed_deploy("v2", 20, step=""), failed_deploy("v3", 40)]) is None


def test_a_startup_failure_is_a_failure() -> None:
    release = replace(failed_deploy("v2", 20), deploys=(run("startup_failure", 25),))
    assert release.failure() == Failure("deploy-release", "", at_minute(25), 1)


def test_a_stage_with_no_run_gives_no_verdict_and_the_next_stage_decides() -> None:
    """A release whose CI run is not in the list can still fail at its deploy."""
    no_ci = replace(failed_deploy("v2", 20), ci=())
    assert no_ci.failure() == Failure("deploy-release", DEPLOY_STEP, at_minute(25), 20)
    assert staging_red([no_ci, replace(failed_deploy("v3", 40), ci=())]) is not None


def test_a_green_walk_of_the_newest_release_lifts_the_stop() -> None:
    releases = [failed_deploy("v2", 20), failed_deploy("v3", 40)]
    assert staging_red(releases) is not None
    assert staging_red([*releases, green("v4", 60)]) is None


def test_a_green_walk_of_an_older_tag_lifts_the_stop() -> None:
    """The rule says "a green walk of any tag". A walk of v1 after the two failures proves that
    staging works again, even though v3 is still the newest release."""
    releases = [green("v1", 0), failed_deploy("v2", 20), failed_deploy("v3", 40)]
    assert staging_red(releases) is not None
    rewalk = replace(releases[0], walks=(*releases[0].walks, run("success", 70, number=7)))
    assert staging_red([rewalk, *releases[1:]]) is None


def test_a_green_walk_that_finished_before_the_failures_does_not_lift_the_stop() -> None:
    assert staging_red([green("v1", 0), failed_deploy("v2", 20), failed_deploy("v3", 40)]) is not None


def test_a_newest_release_with_a_green_walk_is_never_red() -> None:
    """Two slow releases fail at one step after the newest release passed its walk. The newest
    release has a green walk, so the line is open."""
    slow_one = replace(failed_deploy("v1", 0), deploys=(run("failure", 55, DEPLOY_STEP, 1),))
    slow_two = replace(failed_deploy("v2", 10), deploys=(run("failure", 58, DEPLOY_STEP, 2),))
    quick = green("v3", 40)
    assert staging_red([slow_one, slow_two]) is not None
    assert staging_red([slow_one, slow_two, quick]) is None


def test_a_later_failure_at_another_step_does_not_lift_the_stop() -> None:
    """The stop holds until a walk passes. A failure at a new step is no walk."""
    other = failed_deploy("v4", 60, step="request::Confirm the staging release carries its data engine")
    red = staging_red([failed_deploy("v2", 20), failed_deploy("v3", 40), other])
    assert red is not None
    assert red.since == "v2"
    assert red.newest_failed == "v4"
    assert red.failed_tags == ("v2", "v3", "v4")


def test_a_release_that_failed_a_deploy_and_then_walked_green_has_no_failure() -> None:
    walked = replace(failed_deploy("v2", 20), walks=(run("success", 40),))
    assert walked.failure() is None


def test_a_run_that_is_only_in_flight_gives_no_verdict() -> None:
    assert replace(failed_deploy("v3", 40), deploys=(run("", 45),)).failure() is None


def test_a_rerun_in_flight_does_not_lift_the_stop() -> None:
    """An operator re-runs the failed deploy of v3. The re-run is in flight, and it is no pass.
    The stop holds until a walk passes."""
    rerun = replace(failed_deploy("v3", 40), deploys=(run("failure", 45, DEPLOY_STEP, 1), run("", 50, number=2)))
    assert rerun.failure() == Failure("deploy-release", DEPLOY_STEP, at_minute(45), 1)
    assert staging_red([failed_deploy("v2", 20), rerun]) is not None


def test_a_success_run_with_a_skipped_walk_step_is_no_green_walk() -> None:
    """The reader gives such a run the conclusion `skipped`. It walked nothing."""
    unwalked = replace(failed_deploy("v3", 40), deploys=(run("success", 45),), walks=(run("skipped", 49),))
    assert not unwalked.walked_green
    assert unwalked.failure() is None


@pytest.mark.parametrize("conclusion", ["cancelled", "skipped"])
def test_a_cancelled_or_skipped_run_carries_no_verdict(conclusion: str) -> None:
    release = replace(failed_deploy("v2", 20), deploys=(run(conclusion, 25),))
    assert release.failure() is None


def test_a_release_fails_at_its_first_stage_without_a_pass() -> None:
    ci_red = StagingRelease("v2", at_minute(20), ci=(run("failure", 22, CI_STEP),))
    assert ci_red.failure() == Failure("ci-required", CI_STEP, at_minute(22), 1)
    walk_red = StagingRelease(
        "v3",
        at_minute(40),
        ci=(run("success", 41),),
        deploys=(run("success", 45),),
        walks=(run("failure", 49, WALK_STEP),),
    )
    assert walk_red.failure() == Failure("walk-release", WALK_STEP, at_minute(49), 1)


def test_a_failed_deploy_that_a_later_deploy_fixed_fails_at_the_walk() -> None:
    release = StagingRelease(
        "v2",
        at_minute(20),
        ci=(run("success", 21),),
        deploys=(run("failure", 25, DEPLOY_STEP, 1), run("success", 35, number=2)),
        walks=(run("failure", 40, WALK_STEP, 3),),
    )
    assert release.failure() == Failure("walk-release", WALK_STEP, at_minute(40), 3)


def test_the_newest_failed_run_names_the_step() -> None:
    release = replace(
        failed_deploy("v2", 20),
        deploys=(run("failure", 25, DEPLOY_STEP, 1), run("failure", 35, "request::Confirm", 2)),
    )
    assert release.failure() == Failure("deploy-release", "request::Confirm", at_minute(35), 2)


def test_the_ci_red_loop_of_one_tag_stops_after_two_pushes() -> None:
    """Each push of v2 is its own release. A tag with a red CI run is deleted and pushed again."""
    pushes = [StagingRelease("v2", at_minute(m), ci=(run("failure", m + 2, CI_STEP, m),)) for m in (0, 20, 40)]
    red = staging_red(pushes[:2])
    assert red is not None and red.since == "v2" and red.label.startswith("ci-required / ")
    assert staging_red(pushes[:1]) is None


def test_no_release_means_no_stop() -> None:
    assert staging_red([]) is None


def test_the_threshold_must_name_at_least_two_releases() -> None:
    with pytest.raises(ValueError, match="same_step_failures"):
        staging_red([], same_step_failures=1)


def test_describe_says_which_state_staging_is_in() -> None:
    red = staging_red([failed_deploy("v2", 20), failed_deploy("v3", 40)])
    assert "red since v2" in staging_line.describe([failed_deploy("v2", 20), failed_deploy("v3", 40)], red)
    assert "newest release v1 walked green" in staging_line.describe([green("v1", 0)], None)
    assert "newest release v2 failed at deploy-release" in staging_line.describe([failed_deploy("v2", 20)], None)
    assert "no staging release was found" in staging_line.describe([], None)
