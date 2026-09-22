"""Tests for `tools/auto_release.py` — #860.

On 2026-09-17 green merges to main sat untagged for up to 16.5 min because
nobody ran the release ceremony (`tools/cut_release.sh`) by hand. This tool is
the set of reasons NOT to release even after `auto-release-staging.yml`'s
quiet period; the workflow's own shape (the 20-minute wait, and that it can
never pass `--prod`) is asserted in `test_ci_workflows.py` instead — this
file is scoped to the decision logic only (#583's boundary).

The owner's 2026-09-17 decision ("先在 staging 做吧，prod 回头再说") sets three
hard constraints. This file is the direct evidence for two of them:
  - `test_the_daily_cap_defaults_to_four_and_is_enforced_by_todays_auto_tags`
    and `test_a_hand_cut_tag_never_counts_against_the_automatic_cap` prove the
    "at most 4 automatic releases a day" bound.
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

import subprocess
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

import pytest
from truealpha_runtime.testing import load_tool

auto_release = load_tool("auto_release")
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


def test_reason_4_inside_the_nightly_tick_window() -> None:
    inside = datetime(2026, 9, 22, 22, 50, tzinfo=UTC)
    result = decide(facts(now=inside, tags=(tag("v0.0.5", commit=OTHER),)))
    assert result.release is False
    assert "tick window" in result.reason


def test_reason_5_daily_cap_reached() -> None:
    today_auto_tags = tuple(tag(f"v0.0.{i}", commit=OTHER, auto=True) for i in range(4))
    result = decide(facts(tags=today_auto_tags))
    assert result.release is False
    assert "daily cap reached: 4" in result.reason


def test_reason_6_a_release_is_already_in_flight() -> None:
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
            tags=tuple(tag(f"v0.0.{i}", commit=TRIGGER, auto=True) for i in range(9)),
            in_flight=("Deploy staging v0.0.9 (run 1, in_progress)",),
        )
    )
    assert "main moved on" in result.reason


def test_daily_cap_must_be_non_negative() -> None:
    with pytest.raises(ValueError, match="daily cap"):
        decide(facts(), daily_cap=-1)


def test_the_daily_cap_defaults_to_four_and_is_enforced_by_todays_auto_tags() -> None:
    """Owner constraint: at most 4 automatic releases a day. 3 today still
    releases; the 4th request for today refuses — proving the bound is
    enforced, not merely declared."""
    assert DEFAULT_DAILY_CAP == 4
    three_today = tuple(tag(f"v0.0.{i}", commit=OTHER, auto=True) for i in range(3))
    allowed = decide(facts(tags=three_today))
    assert allowed.release is True

    four_today = tuple(tag(f"v0.0.{i}", commit=OTHER, auto=True) for i in range(4))
    refused = decide(facts(tags=four_today))
    assert refused.release is False
    assert "daily cap reached: 4" in refused.reason


def test_a_hand_cut_tag_never_counts_against_the_automatic_cap() -> None:
    """A hand-cut release (no AUTO_TRAILER) must never eat into the automatic
    budget — otherwise an operator release could silently starve the
    automatic path for the rest of the day."""
    four_hand_tags = tuple(tag(f"v0.0.{i}", commit=OTHER, auto=False) for i in range(4))
    result = decide(facts(tags=four_hand_tags))
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
    from data_engine.lanes.capture import CANARY_DAILY_CRON, QQQ_LIVE_CRON, live_topt_cron

    def cron_time(expr: str) -> time:
        minute, hour = expr.split()[:2]
        return time(int(hour), int(minute))

    topt_staging = cron_time(live_topt_cron("staging"))
    qqq = cron_time(QQQ_LIVE_CRON)
    canary = cron_time(CANARY_DAILY_CRON)

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


def _get_for(*, main_sha: str, green: bool, deploy_runs=(), walk_runs=(), tag_ci_runs=()):
    def get(path: str):
        if path == f"/repos/{REPO}/commits/main":
            return {"sha": main_sha}
        if "workflows/ci-required.yml/runs" in path and "status=success" in path:
            return {"total_count": 1 if green else 0}
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
