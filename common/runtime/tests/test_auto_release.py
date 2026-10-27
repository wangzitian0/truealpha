"""Tests for `tools/auto_release.py` — #860.

On 2026-09-17 green merges to main sat untagged for up to 16.5 min because
nobody ran the release ceremony (`tools/cut_release.sh`) by hand. This tool is
the set of reasons NOT to release even after `auto-release-staging.yml`'s
quiet period; the workflow's own shape (the 20-minute wait, and that it can
never pass `--prod`) is asserted in `test_ci_workflows.py` instead — this
file is scoped to the decision logic only (#583's boundary).

The owner's 2026-09-17 decision ("先在 staging 做吧，prod 回头再说") sets three
hard constraints. This file is the direct evidence for two of them:
  - `test_the_daily_cap_is_enforced_by_todays_auto_tags`
    and `test_a_hand_cut_tag_never_counts_against_the_automatic_cap` prove the
    "at most DEFAULT_DAILY_CAP automatic releases a day" bound.
  - `test_reason_1_a_later_push_during_the_quiet_period_defers_to_its_own_run`
    proves the debounce ITSELF: the quiet period is a plain wait in the
    workflow (asserted separately), and what makes it a debounce rather than a
    blind sleep is this re-check — only the newest push's own wait still finds
    itself at HEAD when it wakes.
The third constraint — automatic releases are staging-only, `--prod` is never
reachable from this path — has no `--prod` concept anywhere in this module to
test; it is proven in `test_cut_release.py` (the `--auto`/`--prod` refusal)
and `test_ci_workflows.py` (the workflow text never contains the string).

This module holds `TICK_WINDOW_START` against the real staging cron schedule
in `data_engine.lanes.capture`, per its own docstring's promise.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

import pytest
from truealpha_runtime.testing import load_tool

auto_release = load_tool("auto_release")
from truealpha_tools.staging_line import StageRun, StagingRelease  # noqa: E402

ReleaseTag = auto_release.ReleaseTag
Facts = auto_release.Facts
Decision = auto_release.Decision
decide = auto_release.decide
next_tag = auto_release.next_tag
in_tick_window = auto_release.in_tick_window
automatic_releases_on = auto_release.automatic_releases_on
read_tags = auto_release.read_tags
in_flight_releases = auto_release.in_flight_releases
main_head = auto_release.main_head
head_is_green = auto_release.head_is_green
gather = auto_release.gather
write_outputs = auto_release.write_outputs
main = auto_release.main
AUTO_TRAILER = auto_release.AUTO_TRAILER
DEFAULT_DAILY_CAP = auto_release.DEFAULT_DAILY_CAP
DEPLOY_LEAD = auto_release.DEPLOY_LEAD
TICK_WINDOW_START = auto_release.TICK_WINDOW_START
FIELD = auto_release._FIELD
RECORD = auto_release._RECORD

TRIGGER = "a" * 40
OTHER = "b" * 40
REPO = "wangzitian0/truealpha"
NOON = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)  # comfortably outside the tick window


def tag(name: str, commit: str = TRIGGER, tagged_at: datetime | None = NOON, auto: bool = False) -> ReleaseTag:
    message = f"{name}\n\nPRs: 1"
    if auto:
        message += f"\n\n{AUTO_TRAILER}"
    return ReleaseTag(name=name, commit=commit, tagged_at=tagged_at, message=message)


CI_STEP = "images_release / plan::Select available images"


def _red_ci_push(minute: int) -> StagingRelease:
    """One push of v0.0.6 whose own ci-required run failed in the same job."""
    when = NOON - timedelta(minutes=60 - minute)
    return StagingRelease("v0.0.6", when, ci=(StageRun("failure", when, CI_STEP, minute),))


RED_RELEASES = (_red_ci_push(0), _red_ci_push(20))


def facts(**overrides) -> Facts:
    base = dict(trigger_sha=TRIGGER, main_head=TRIGGER, head_green=True, tags=(), in_flight=(), now=NOON)
    base.update(overrides)
    return Facts(**base)


# --- decide(): one test per reason, in the order the docstring promises -----


def test_reason_1_a_later_push_during_the_quiet_period_defers_to_its_own_run() -> None:
    """This IS the debounce: every green push starts its own wait, and only the
    newest push's wait still finds itself at HEAD when it wakes. An earlier
    push whose wait elapsed while a later one landed must decline — its own
    green run already started an independent wait."""
    result = decide(facts(main_head=OTHER))
    assert result == Decision(False, result.reason)
    assert "main moved on" in result.reason
    assert OTHER[:8] in result.reason


def test_reason_2_head_not_green() -> None:
    result = decide(facts(head_green=False))
    assert result.release is False
    assert "no green ci-required" in result.reason


def test_reason_3_head_already_released_by_hand() -> None:
    result = decide(facts(tags=(tag("v0.0.5"),)))
    assert result.release is False
    assert "already released as v0.0.5" in result.reason


def test_reason_4_staging_is_red() -> None:
    """#1468 F1: two releases in a row failed at one step and no walk passed since. The reason
    names the stage and the step, and it says what lifts the stop."""
    result = decide(facts(tags=(tag("v0.0.5", commit=OTHER),), releases=RED_RELEASES))
    assert result.release is False
    assert result.tag == ""
    assert "staging is red since v0.0.6" in result.reason
    assert "ci-required / images_release / plan / Select available images" in result.reason
    assert "until a walk of any tag passes" in result.reason


def test_a_stopped_train_beats_the_tick_window_the_daily_cap_and_a_release_in_flight() -> None:
    """A red staging line is the reason an operator needs to read. The three later reasons would
    hide it. The first three reasons still win: they say that this run is moot."""
    inside = datetime(2026, 9, 22, 22, 50, tzinfo=UTC)
    today_auto_tags = tuple(tag(f"v0.0.{i}", commit=OTHER, auto=True) for i in range(DEFAULT_DAILY_CAP))
    stacked = facts(
        now=inside, tags=today_auto_tags, in_flight=("Deploy staging v0.0.9 (run 1)",), releases=RED_RELEASES
    )
    assert "staging is red" in decide(stacked).reason
    assert "main moved on" in decide(replace(stacked, main_head=OTHER)).reason
    assert "no green ci-required" in decide(replace(stacked, head_green=False)).reason
    assert "already released as" in decide(replace(stacked, tags=(tag("v0.0.5", commit=TRIGGER),))).reason


def test_a_green_walk_in_the_history_lets_the_train_release_again() -> None:
    walked = StagingRelease("v0.0.7", NOON - timedelta(minutes=5), walks=(StageRun("success", NOON, "", 9),))
    result = decide(facts(tags=(tag("v0.0.5", commit=OTHER),), releases=(*RED_RELEASES, walked)))
    assert result.release is True


def test_reason_5_inside_the_nightly_tick_window() -> None:
    inside = datetime(2026, 9, 22, 22, 50, tzinfo=UTC)
    result = decide(facts(now=inside, tags=(tag("v0.0.5", commit=OTHER),)))
    assert result.release is False
    assert "tick window" in result.reason


def test_reason_6_daily_cap_reached() -> None:
    today_auto_tags = tuple(tag(f"v0.0.{i}", commit=OTHER, auto=True) for i in range(DEFAULT_DAILY_CAP))
    result = decide(facts(tags=today_auto_tags))
    assert result.release is False
    assert f"daily cap reached: {DEFAULT_DAILY_CAP}" in result.reason


def test_reason_7_a_release_is_already_in_flight() -> None:
    result = decide(facts(in_flight=("Deploy staging v0.0.9 (run 42, in_progress)",)))
    assert result.release is False
    assert "already in flight" in result.reason
    assert "run 42" in result.reason


def test_all_clear_releases_the_next_patch() -> None:
    result = decide(facts(tags=(tag("v0.0.5", commit=OTHER),)))
    assert result.release is True
    assert result.tag == "v0.0.6"
    assert "green, quiet and untagged" in result.reason


def test_reasons_are_checked_in_order_the_first_match_wins() -> None:
    """Stacking every other reason's trigger on top of reason 1: the answer
    must still be reason 1, because a caller that fixed only the first
    returned reason must see the next one, not the same one twice."""
    result = decide(
        facts(
            main_head=OTHER,
            head_green=False,
            tags=tuple(tag(f"v0.0.{i}", commit=TRIGGER, auto=True) for i in range(DEFAULT_DAILY_CAP + 5)),
            in_flight=("Deploy staging v0.0.9 (run 1, in_progress)",),
        )
    )
    assert "main moved on" in result.reason


def test_daily_cap_must_be_non_negative() -> None:
    with pytest.raises(ValueError, match="daily cap"):
        decide(facts(), daily_cap=-1)


def test_the_daily_cap_is_enforced_by_todays_auto_tags() -> None:
    """At most `DEFAULT_DAILY_CAP` automatic staging releases a day (the rationale is at the constant).
    One fewer today still releases; the next request for today refuses: the bound is enforced, not declared."""
    below = tuple(tag(f"v0.0.{i}", commit=OTHER, auto=True) for i in range(DEFAULT_DAILY_CAP - 1))
    allowed = decide(facts(tags=below))
    assert allowed.release is True

    at_cap = tuple(tag(f"v0.0.{i}", commit=OTHER, auto=True) for i in range(DEFAULT_DAILY_CAP))
    refused = decide(facts(tags=at_cap))
    assert refused.release is False
    assert f"daily cap reached: {DEFAULT_DAILY_CAP}" in refused.reason


def test_the_default_daily_cap_is_ten() -> None:
    """The value is pinned once. A change of the number must change this line and the measurement at the constant."""
    assert DEFAULT_DAILY_CAP == 10


def test_a_hand_cut_tag_never_counts_against_the_automatic_cap() -> None:
    """A hand-cut release (no AUTO_TRAILER) must never eat into the automatic
    budget — otherwise an operator release could silently starve the
    automatic path for the rest of the day. As many hand tags as the cap would
    refuse the release if they counted."""
    hand_tags = tuple(tag(f"v0.0.{i}", commit=OTHER, auto=False) for i in range(DEFAULT_DAILY_CAP))
    result = decide(facts(tags=hand_tags))
    assert result.release is True, "hand-cut tags must not be counted by automatic_releases_on"


def test_automatic_releases_on_ignores_a_different_day_and_a_hand_tag() -> None:
    yesterday = tag("v0.0.1", tagged_at=NOON - timedelta(days=1), auto=True)
    hand_today = tag("v0.0.2", tagged_at=NOON, auto=False)
    auto_today = tag("v0.0.3", tagged_at=NOON, auto=True)
    result = automatic_releases_on(NOON, (yesterday, hand_today, auto_today))
    assert [t.name for t in result] == ["v0.0.3"]


# --- next_tag ----------------------------------------------------------------


def test_next_tag_compares_numerically_not_lexically() -> None:
    assert next_tag([tag("v0.0.9", commit=TRIGGER)]) == "v0.0.10"
    assert next_tag([tag("v0.0.9"), tag("v0.0.10"), tag("v0.1.0")]) == "v0.1.1"


def test_next_tag_refuses_with_no_prior_release() -> None:
    with pytest.raises(ValueError, match="no vX.Y.Z tag"):
        next_tag([])


# --- in_tick_window: the pure boundary, and held against the real crons ------


@pytest.mark.parametrize(
    ("when", "expected"),
    [
        (time(22, 29), False),  # 1 minute before the lead opens the window
        (time(22, 30), True),  # exactly DEPLOY_LEAD (15 min) before 22:45
        (time(22, 45), True),  # the TOPT tick itself
        (time(23, 47), True),  # the canary tick, still inside
        (time(23, 59), True),  # last minute of the UTC day
        (time(0, 0), False),  # midnight: a new day's own (later) window
        (time(12, 0), False),  # broad daylight
    ],
)
def test_in_tick_window_boundaries(when: time, expected: bool) -> None:
    now = datetime(2026, 9, 22, when.hour, when.minute, tzinfo=UTC)
    assert in_tick_window(now) is expected


def test_tick_window_holds_against_the_real_staging_cron_schedule() -> None:
    """The window this tool guards must open no later than the first staging
    tick it exists to protect, and cover the last one — read from the actual
    schedule, not a second copy of the literal times."""
    from data_engine.lanes.capture import tick_cron

    def cron_time(expr: str) -> time:
        minute, hour = expr.split()[:2]
        return time(int(hour), int(minute))

    topt_staging = cron_time(tick_cron("topt", "staging"))
    qqq = cron_time(tick_cron("qqq", "production"))
    canary = cron_time(tick_cron("canary", "staging"))

    assert topt_staging < qqq < canary, "the ticks this comment orders must still be ordered"
    assert TICK_WINDOW_START == topt_staging, "the window must open exactly at the first staging tick"
    # A release starting at the very edge of the lead must not land inside any tick.
    edge = datetime.combine(datetime(2026, 9, 22, tzinfo=UTC).date(), TICK_WINDOW_START, tzinfo=UTC) - DEPLOY_LEAD
    assert in_tick_window(edge) is True
    assert in_tick_window(edge - timedelta(minutes=1)) is False
    # The window has no early end before midnight, so the later ticks (QQQ, canary)
    # fall inside it too.
    assert in_tick_window(datetime.combine(edge.date(), qqq, tzinfo=UTC)) is True
    assert in_tick_window(datetime.combine(edge.date(), canary, tzinfo=UTC)) is True


# --- read_tags: parses git's own output format, faked rather than networked -


def _for_each_ref_blob(rows: list[tuple[str, str, str, str, str]]) -> str:
    """Build the exact bytes `git for-each-ref --format=...%1f...%1e` would
    produce for the format string `read_tags` requests, so the parser is
    exercised against its real wire shape rather than a paraphrase of it."""
    return "".join(FIELD.join(row) + RECORD for row in rows)


def test_read_tags_parses_the_for_each_ref_wire_format() -> None:
    blob = _for_each_ref_blob(
        [
            (
                "v0.0.1",
                "deadbeef" * 5,
                "",
                "1758000000",
                "v0.0.1\n\nPRs: 1",
            ),  # lightweight (no peeled sha, no date... but date given for lightweight would be blank; kept simple)
            ("v0.0.2", "cafebabe" * 5, "cafed00d" * 5, "1758100000", f"v0.0.2\n\nPRs: 2\n\n{AUTO_TRAILER}"),
            ("not-a-release-tag", "feedface" * 5, "", "", "unrelated"),
        ]
    )
    tags = read_tags(run=lambda args: blob)
    names = [t.name for t in tags]
    assert names == ["v0.0.1", "v0.0.2"], "a non-vX.Y.Z ref must be skipped"
    annotated = next(t for t in tags if t.name == "v0.0.2")
    assert annotated.commit == "cafed00d" * 5, "an annotated tag's commit is the PEELED object, not the tag object"
    assert annotated.automatic is True
    lightweight = next(t for t in tags if t.name == "v0.0.1")
    assert lightweight.commit == "deadbeef" * 5, "no peeled sha falls back to the tag's own object"
    assert lightweight.automatic is False


def test_read_tags_passes_refs_tags_to_git(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def fake_run(args):
        seen.append(list(args))
        return ""

    read_tags(run=fake_run)
    assert seen[0][:2] == ["git", "for-each-ref"]
    assert seen[0][-1] == "refs/tags"


# --- gather()/main(): the wiring, with a faked GitHub and a faked git -------


def _get_for(
    *,
    main_sha: str,
    green: bool,
    deploy_runs=(),
    walk_runs=(),
    tag_ci_runs=(),
    head_ci_runs=None,
    jobs=None,
    jobs_calls=None,
):
    jobs = jobs or {}
    jobs_calls = jobs_calls if jobs_calls is not None else []

    def get(path: str):
        if "/actions/runs/" in path and "/jobs?" in path:
            run_id = int(path.split("/actions/runs/")[1].split("/")[0])
            jobs_calls.append(run_id)
            return {"jobs": jobs.get(run_id, [])}
        if path == f"/repos/{REPO}/commits/main":
            return {"sha": main_sha}
        if "workflows/ci-required.yml/runs" in path and "status=success" in path:
            return {"total_count": 1 if green else 0}
        if "workflows/ci-required.yml/runs" in path and "head_sha=" in path:
            if head_ci_runs is not None:
                return {"workflow_runs": list(head_ci_runs)}
            return {"workflow_runs": [{"status": "in_progress"}]}
        if "workflows/deploy-release.yml/runs" in path:
            return {"workflow_runs": list(deploy_runs)}
        if "workflows/walk-release.yml/runs" in path:
            return {"workflow_runs": list(walk_runs)}
        if "workflows/ci-required.yml/runs" in path:
            return {"workflow_runs": list(tag_ci_runs)}
        raise AssertionError(f"unexpected GET {path}")

    return get


def test_gather_assembles_facts_from_the_github_api_and_local_tags() -> None:
    result = gather(REPO, TRIGGER, get=_get_for(main_sha=TRIGGER, green=True), run=lambda args: "", now=NOON)
    assert result.trigger_sha == TRIGGER
    assert result.main_head == TRIGGER
    assert result.head_green is True
    assert result.tags == ()
    assert result.in_flight == ()


def test_in_flight_releases_flags_incomplete_runs_of_any_of_the_three_kinds() -> None:
    get = _get_for(
        main_sha=TRIGGER,
        green=True,
        deploy_runs=[{"status": "in_progress", "display_title": "Deploy staging v0.0.9", "id": 1}],
        walk_runs=[{"status": "queued", "display_title": "Walk Deploy staging v0.0.9", "id": 2}],
    )
    busy = in_flight_releases(REPO, get=get)
    assert len(busy) == 2
    assert "run 1" in busy[0] and "run 2" in busy[1]


def test_in_flight_ignores_a_completed_run_and_a_tag_ci_run_for_a_non_tag_branch() -> None:
    get = _get_for(
        main_sha=TRIGGER,
        green=True,
        deploy_runs=[{"status": "completed", "display_title": "Deploy staging v0.0.8", "id": 1}],
        tag_ci_runs=[{"status": "in_progress", "head_branch": "some-feature-branch"}],
    )
    assert in_flight_releases(REPO, get=get) == ()


def test_main_writes_release_true_and_the_tag_to_github_output(tmp_path: Path) -> None:
    output = tmp_path / "gh_output"
    output.write_text("", encoding="utf-8")
    exit_code = main(
        ["--repo", REPO, "--trigger-sha", TRIGGER, "--github-output", str(output)],
        get=_get_for(main_sha=TRIGGER, green=True),
        run=lambda args: _for_each_ref_blob([("v0.0.5", OTHER, "", "1758000000", "v0.0.5\n\nPRs: 1")]),
    )
    assert exit_code == 0
    written = output.read_text(encoding="utf-8")
    assert "release=true" in written
    assert "tag=v0.0.6" in written


def test_main_writes_release_false_when_main_has_moved_on(tmp_path: Path) -> None:
    output = tmp_path / "gh_output"
    output.write_text("", encoding="utf-8")
    main(
        ["--repo", REPO, "--trigger-sha", TRIGGER, "--github-output", str(output)],
        get=_get_for(main_sha=OTHER, green=True),
        run=lambda args: "",
    )
    written = output.read_text(encoding="utf-8")
    assert "release=false" in written
    assert "main moved on" in written


def test_out_of_order_ci_completion_releases_green_head_when_trigger_is_ancestor() -> None:
    """#1284: when an older commit's CI finishes after a newer commit's CI and cancels
    the newer commit's auto-release wait, the surviving run releases main HEAD once
    HEAD's own CI has completed green and trigger_sha is an ancestor of main_head."""
    result = decide(
        facts(
            trigger_sha=TRIGGER,
            main_head=OTHER,
            head_green=True,
            head_ci_completed=True,
            trigger_is_ancestor_of_head=True,
            tags=(tag("v0.0.5", commit=TRIGGER),),
        )
    )
    assert result.release is True
    assert result.tag == "v0.0.6"
    assert OTHER[:8] in result.reason


def test_non_ancestor_trigger_commit_is_refused() -> None:
    """#1284: a candidate commit that is not an ancestor of origin/main is refused."""
    result = decide(
        facts(
            trigger_sha=TRIGGER,
            main_head=OTHER,
            head_green=True,
            head_ci_completed=True,
            trigger_is_ancestor_of_head=False,
            tags=(tag("v0.0.5", commit=TRIGGER),),
        )
    )
    assert result.release is False
    assert "not an ancestor of origin/main" in result.reason


def test_non_ancestor_latest_release_tag_refuses_rollback() -> None:
    """#1284: if the highest existing release tag is not an ancestor of main HEAD, refuse to roll back."""
    result = decide(
        facts(
            trigger_sha=TRIGGER,
            main_head=TRIGGER,
            head_green=True,
            latest_tag_is_ancestor_of_head=False,
            tags=(tag("v0.0.9", commit=OTHER),),
        )
    )
    assert result.release is False
    assert "latest release v0.0.9 is not an ancestor" in result.reason


def test_is_ancestor_calls_git_merge_base_and_handles_exit_one() -> None:
    seen: list[list[str]] = []

    def fake_run(args):
        seen.append(list(args))
        if args[-2] == OTHER:
            raise subprocess.CalledProcessError(1, args)
        return ""

    assert auto_release.is_ancestor(TRIGGER, TRIGGER, run=fake_run) is True
    assert seen == [], "identical SHAs need no git subprocess"
    assert auto_release.is_ancestor(TRIGGER, OTHER, run=fake_run) is True
    assert seen[-1] == ["git", "merge-base", "--is-ancestor", TRIGGER, OTHER]
    assert auto_release.is_ancestor(OTHER, TRIGGER, run=fake_run) is False


def test_gather_marks_head_ci_completed_on_reversed_ci_order() -> None:
    get = _get_for(
        main_sha=OTHER,
        green=True,
        head_ci_runs=[{"status": "completed", "conclusion": "success"}],
    )
    result = gather(
        REPO,
        TRIGGER,
        get=get,
        run=lambda args: (
            _for_each_ref_blob([("v0.0.5", TRIGGER, "", "1758000000", "v0.0.5")]) if "for-each-ref" in args else ""
        ),
        now=NOON,
    )
    assert result.trigger_sha == TRIGGER
    assert result.main_head == OTHER
    assert result.head_green is True
    assert result.head_ci_completed is True
    assert result.trigger_is_ancestor_of_head is True
    assert result.latest_tag_is_ancestor_of_head is True
    decision = decide(result)
    assert decision.release is True
    assert decision.tag == "v0.0.6"


def test_main_rejects_a_trigger_sha_that_is_not_a_40_hex_commit(capsys: pytest.CaptureFixture) -> None:
    exit_code = main(["--repo", REPO, "--trigger-sha", "not-a-sha"])
    assert exit_code == 2
    assert "40-hex" in capsys.readouterr().err


def test_main_fails_closed_when_a_fact_cannot_be_read(capsys: pytest.CaptureFixture) -> None:
    """A GitHub read that raises must never be treated as 'nothing to stop
    the release' — fail closed, exit non-zero, never release=true."""

    def broken_get(path: str):
        raise ValueError("simulated API outage")

    exit_code = main(["--repo", REPO, "--trigger-sha", TRIGGER], get=broken_get, run=lambda args: "")
    assert exit_code == 1
    assert "could not read its facts" in capsys.readouterr().err


def test_main_fails_closed_on_a_git_subprocess_error() -> None:
    def raising_run(args):
        raise subprocess.CalledProcessError(1, args)

    exit_code = main(
        ["--repo", REPO, "--trigger-sha", TRIGGER],
        get=_get_for(main_sha=TRIGGER, green=True),
        run=raising_run,
    )
    assert exit_code == 1


def test_write_outputs_flattens_a_multiline_reason(tmp_path: Path) -> None:
    output = tmp_path / "gh_output"
    output.write_text("existing=1\n", encoding="utf-8")
    write_outputs(output, Decision(False, "line one\nline two", tag=""))
    content = output.read_text(encoding="utf-8")
    assert "existing=1" in content, "append, never truncate — another step may already have written to it"
    assert "reason=line one line two\n" in content
    assert "release=false" in content


# --- the staging-red history: read from the run lists, rule applied, issue kept in step -----


def _run(run_id: int, title: str, conclusion: str, created: str, updated: str, branch: str = "main") -> dict:
    return {
        "id": run_id,
        "display_title": title,
        "status": "completed" if conclusion else "in_progress",
        "conclusion": conclusion or None,
        "created_at": created,
        "updated_at": updated,
        "head_branch": branch,
    }


def _job(name: str, step: str) -> dict:
    return {"name": name, "conclusion": "failure", "steps": [{"name": step, "conclusion": "failure"}]}


def _red_history_get(jobs_calls: list[int] | None = None):
    """v0.0.5 failed its staging deploy. Then v0.0.6 was pushed twice and failed its own CI both
    times, in the job `images_release / plan`. The `ci-required` summary job failed too."""
    summary = _job("ci-required", "Reject any failed, cancelled, or missing dependency")
    return _get_for(
        main_sha=TRIGGER,
        green=True,
        deploy_runs=[
            _run(104, "Deploy staging v0.0.5", "failure", "2026-09-22T08:00:00Z", "2026-09-22T08:05:00Z"),
            _run(105, "Deploy staging v0.0.5", "failure", "2026-09-22T08:10:00Z", "2026-09-22T08:15:00Z"),
            _run(90, "Deploy prod v0.0.5", "failure", "2026-09-22T08:20:00Z", "2026-09-22T08:25:00Z"),
        ],
        tag_ci_runs=[
            _run(150, "ci-required", "success", "2026-09-22T07:50:00Z", "2026-09-22T07:55:00Z", "v0.0.5"),
            _run(201, "ci-required", "failure", "2026-09-22T09:00:00Z", "2026-09-22T09:05:00Z", "v0.0.6"),
            _run(202, "ci-required", "failure", "2026-09-22T09:30:00Z", "2026-09-22T09:35:00Z", "v0.0.6"),
            _run(300, "ci-required", "failure", "2026-09-22T09:40:00Z", "2026-09-22T09:41:00Z", "some-feature-branch"),
        ],
        jobs={
            104: [_job("request", "Dispatch the validated request to infra2")],
            105: [_job("request", "Confirm the staging release carries its data engine")],
            201: [summary, _job("images_release / plan", "Select available images")],
            202: [summary, _job("images_release / plan", "Select available images")],
        },
        jobs_calls=jobs_calls if jobs_calls is not None else [],
    )


V0_0_5 = tag("v0.0.5", commit=OTHER)


def _git_with_v0_0_5(args):
    return _for_each_ref_blob([("v0.0.5", OTHER, "", "1758000000", "v0.0.5\n\nPRs: 1")])


def test_gather_carries_the_releases_into_the_facts() -> None:
    result = gather(REPO, TRIGGER, get=_red_history_get(), run=_git_with_v0_0_5, now=NOON)
    assert [r.tag for r in result.releases] == ["v0.0.5", "v0.0.6", "v0.0.6"]


class IssueDesk:
    """A fake `gh issue` CLI. It records each call and answers the three reads."""

    def __init__(self, *, open_issues: list[dict] | None = None, last_text: str = "") -> None:
        self.open_issues = open_issues or []
        self.last_text = last_text
        self.calls: list[list[str]] = []

    def __call__(self, args):
        self.calls.append(list(args))
        verb = args[2]
        if verb == "list":
            return json.dumps(self.open_issues)
        if verb == "view":
            return json.dumps({"body": self.last_text, "comments": []})
        if verb == "create":
            return "https://github.com/wangzitian0/truealpha/issues/9999\n"
        return ""

    def verbs(self) -> list[str]:
        return [call[2] for call in self.calls]


RED_TITLE = "[truealpha] Staging red since v0.0.6: ci-required / images_release / plan / Select available images"


def _main_red(tmp_path: Path, *args: str, desk: IssueDesk, capsys: pytest.CaptureFixture) -> tuple[int, str, str]:
    output = tmp_path / "gh_output"
    output.write_text("", encoding="utf-8")
    code = main(
        ["--repo", REPO, "--trigger-sha", TRIGGER, "--github-output", str(output), *args],
        get=_red_history_get(),
        run=_git_with_v0_0_5,
        gh=desk,
    )
    out = capsys.readouterr()
    return code, out.out, output.read_text(encoding="utf-8")


def test_main_exits_green_and_says_why_when_the_train_is_stopped(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """A stopped train is a decision and no failure: exit 0, `release=false`, and a reason line."""
    desk = IssueDesk()
    code, out, written = _main_red(tmp_path, desk=desk, capsys=capsys)
    assert code == 0
    assert "release=false" in written and "tag=\n" in written
    assert "skip: staging is red since v0.0.6" in out
    assert "staging line: red since v0.0.6" in out
    assert desk.calls == [], "without --report-staging-red the run is a read-only replay"


def test_main_opens_one_issue_with_the_title_of_the_rule(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    desk = IssueDesk()
    code, out, _ = _main_red(tmp_path, "--report-staging-red", desk=desk, capsys=capsys)
    assert code == 0
    assert desk.verbs() == ["list", "create"]
    create = desk.calls[1]
    assert create[create.index("--title") + 1] == RED_TITLE
    body = create[create.index("--body") + 1]
    assert "tools/cut_release.sh" in body and "walk-release.yml" in body
    assert "issues/9999" in out


def test_main_comments_on_the_open_issue_and_never_opens_a_second_one(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    desk = IssueDesk(open_issues=[{"number": 41, "title": RED_TITLE}], last_text="older text")
    code, _, _ = _main_red(tmp_path, "--report-staging-red", desk=desk, capsys=capsys)
    assert code == 0
    assert desk.verbs() == ["list", "view", "comment"]
    assert "create" not in desk.verbs()
    assert desk.calls[2][3] == "41"


def test_main_does_not_repeat_a_comment_that_holds_the_same_state(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    first = IssueDesk()
    _main_red(tmp_path, "--report-staging-red", desk=first, capsys=capsys)
    marker = first.calls[1][first.calls[1].index("--body") + 1].splitlines()[-1]
    assert marker.startswith("<!-- staging-red-state: ")
    second = IssueDesk(open_issues=[{"number": 41, "title": RED_TITLE}], last_text=f"text\n{marker}")
    code, out, _ = _main_red(tmp_path, "--report-staging-red", desk=second, capsys=capsys)
    assert code == 0
    assert second.verbs() == ["list", "view"]
    assert "#41 already holds this state" in out


def test_main_ignores_an_open_issue_whose_title_is_not_a_staging_red_title(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    desk = IssueDesk(open_issues=[{"number": 5, "title": "Staging red since last week: who knows"}])
    _main_red(tmp_path, "--report-staging-red", desk=desk, capsys=capsys)
    assert desk.verbs() == ["list", "create"]


def test_main_closes_the_issue_when_the_train_runs_again(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    desk = IssueDesk(open_issues=[{"number": 41, "title": RED_TITLE}])
    output = tmp_path / "gh_output"
    output.write_text("", encoding="utf-8")
    code = main(
        ["--repo", REPO, "--trigger-sha", TRIGGER, "--github-output", str(output), "--report-staging-red"],
        get=_get_for(main_sha=TRIGGER, green=True),
        run=_git_with_v0_0_5,
        gh=desk,
    )
    out = capsys.readouterr().out
    assert code == 0
    assert desk.verbs() == ["list", "close"]
    assert desk.calls[1][3] == "41"
    assert "staging line: open" in out
    assert "release=true" in output.read_text(encoding="utf-8")


def test_main_prints_the_staging_line_on_a_normal_run(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    code = main(
        ["--repo", REPO, "--trigger-sha", TRIGGER],
        get=_get_for(main_sha=TRIGGER, green=True),
        run=_git_with_v0_0_5,
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "release: main HEAD" in out
    assert "staging line: open; no staging release was found" in out


def test_main_fails_loud_when_the_red_state_cannot_get_an_owner(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """The hold itself is safe. A red state that no issue names is the defect of #1468, so a
    failing `gh` turns the run red instead of passing in silence."""

    def broken(args):
        raise subprocess.CalledProcessError(1, args)

    output = tmp_path / "gh_output"
    output.write_text("", encoding="utf-8")
    code = main(
        ["--repo", REPO, "--trigger-sha", TRIGGER, "--github-output", str(output), "--report-staging-red"],
        get=_red_history_get(),
        run=_git_with_v0_0_5,
        gh=broken,
    )
    assert code == 1
    assert "could not update the staging-red issue" in capsys.readouterr().err
    assert "release=false" in output.read_text(encoding="utf-8"), "the hold is written before the report"


def test_a_failing_issue_search_on_an_open_line_never_blocks_a_release(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """Closing an issue is housekeeping. `release=true` is decided, and the cut step must run."""

    def broken(args):
        raise subprocess.CalledProcessError(1, args, stderr="HTTP 502")

    output = tmp_path / "gh_output"
    output.write_text("", encoding="utf-8")
    code = main(
        ["--repo", REPO, "--trigger-sha", TRIGGER, "--github-output", str(output), "--report-staging-red"],
        get=_get_for(main_sha=TRIGGER, green=True),
        run=_git_with_v0_0_5,
        gh=broken,
    )
    assert code == 0
    assert "::warning::could not close the staging-red issue" in capsys.readouterr().err
    assert "release=true" in output.read_text(encoding="utf-8")


def test_an_error_carries_the_standard_error_of_the_failed_command(capsys: pytest.CaptureFixture) -> None:
    def broken(args):
        raise subprocess.CalledProcessError(1, args, stderr="Resource not accessible by integration")

    code = main(["--repo", REPO, "--trigger-sha", TRIGGER], get=_get_for(main_sha=TRIGGER, green=True), run=broken)
    assert code == 1
    assert "Resource not accessible by integration" in capsys.readouterr().err


def test_a_moot_run_reads_no_release_history_and_touches_no_issue(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """Main moved on since this run's push, so a later run decides. The release history costs
    about a dozen API calls, and this run needs none of them."""
    calls: list[str] = []
    base = _get_for(main_sha=OTHER, green=True)

    def get(path: str):
        calls.append(path)
        return base(path)

    def gh(args):
        raise AssertionError(f"a moot run must not call gh: {args}")

    output = tmp_path / "gh_output"
    output.write_text("", encoding="utf-8")
    code = main(
        ["--repo", REPO, "--trigger-sha", TRIGGER, "--github-output", str(output), "--report-staging-red"],
        get=get,
        run=_git_with_v0_0_5,
        gh=gh,
    )
    assert code == 0
    assert "skip: main moved on" in capsys.readouterr().out
    assert "release=false" in output.read_text(encoding="utf-8")
    history_calls = [c for c in calls if "status=failure" in c or "status=timed_out" in c or "branch=v" in c]
    assert history_calls == []
