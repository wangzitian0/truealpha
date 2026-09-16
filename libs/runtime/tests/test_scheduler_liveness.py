"""Tests for tools/scheduler_liveness.py — #876 (W5, "watchdog of watchdogs").

A schedule that stops firing looks exactly like a quiet green one. These pin the
three things the check is made of, against a fake GitHub (no network):

- the cron reading, including the gap of every cron the estate declared when
  this was written (the live run is what discovers them; this table proves the
  arithmetic on the real shapes, not on a toy);
- the classification: OK, STALE, DISABLED, NEVER, INVALID, UNVERIFIABLE;
- that an API that does not answer is red, never a silent pass.

The workflow that runs the tool, and under which guards it opens and closes its
issue, is pinned in test_ci_workflows.py.
"""

from __future__ import annotations

import base64
import json
import urllib.parse
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta

import pytest
from truealpha_runtime.testing import load_tool

_module = load_tool("scheduler_liveness")
Cron = _module.Cron
CronError = _module.CronError
largest_gap = _module.largest_gap
bound_for = _module.bound_for

# A Wednesday, mid-morning.
NOW = datetime(2026, 9, 16, 9, 0, tzinfo=UTC)
REPO = "owner/repo"
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)


def _path(name: str) -> str:
    # Built from the tool's constant: this file may not spell out the workflow
    # directory (test_ci_workflows.py's boundary scan).
    return f"{_module.WORKFLOW_DIR}{name}"


def _gap(*expressions: str, now: datetime = NOW) -> timedelta:
    return largest_gap([Cron.parse(expression) for expression in expressions], now)


# --- cron: the estate as declared on 2026-09-16 ----------------------------------

INFRA2_OPS_CHECKS = (
    "17 2 * * *",
    "37 2 * * *",
    "37 6 * * *",
    "0 1 * * 1",
    "47 * * * *",
    "27 4 * * *",
    "17 7 * * *",
    "17 8 * * *",
    "27 8 * * *",
)
ESTATE = [
    pytest.param(("0 7 * * *",), DAY, id="truealpha-deploy-freshness"),
    pytest.param(("0 6 * * 1",), 7 * DAY, id="truealpha-mutation-reproof"),
    pytest.param(("0 5 * * *",), DAY, id="truealpha-nightly-dagster-liveness"),
    pytest.param(INFRA2_OPS_CHECKS, HOUR, id="infra2-ops-checks-union"),
    pytest.param(("17 2 * * *",), DAY, id="infra2-ops-checks-daily-alone"),
    pytest.param(("0 1 * * 1",), 7 * DAY, id="infra2-ops-checks-weekly-alone"),
    pytest.param(("47 * * * *",), HOUR, id="infra2-ops-checks-hourly-alone"),
    pytest.param(("17 3 * * *", "37 */6 * * *"), 6 * HOUR, id="finance-report-maintenance-union"),
    pytest.param(("23 4 * * *",), DAY, id="finance-report-audit-replay"),
]


@pytest.mark.parametrize(("expressions", "gap"), ESTATE)
def test_every_cron_in_the_estate_has_its_real_gap(expressions: tuple[str, ...], gap: timedelta) -> None:
    assert _gap(*expressions) == gap


@pytest.mark.parametrize(("expressions", "gap"), ESTATE)
def test_the_gap_does_not_depend_on_when_it_is_measured(expressions: tuple[str, ...], gap: timedelta) -> None:
    for offset in (timedelta(0), 5 * HOUR + timedelta(minutes=13), 3 * DAY, 11 * DAY + 17 * HOUR):
        assert _gap(*expressions, now=NOW + offset) == gap, f"measured at {NOW + offset}"


@pytest.mark.parametrize(
    ("gap", "bound"),
    [(HOUR, 3 * HOUR), (6 * HOUR, 13 * HOUR), (DAY, 49 * HOUR), (7 * DAY, 14 * DAY + HOUR)],
)
def test_the_bound_is_two_gaps_plus_an_hour(gap: timedelta, bound: timedelta) -> None:
    assert bound_for(gap) == bound


# --- cron: syntax ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expression", "gap"),
    [
        ("0,30 9-17 * * 1-5", timedelta(hours=63, minutes=30)),  # Friday 17:30 -> Monday 09:00
        ("0 0-23/8 * * *", 8 * HOUR),
        ("5/15 * * * *", timedelta(minutes=15)),
        ("*/20 * * * *", timedelta(minutes=20)),
        ("0 6 * JAN-DEC MON", 7 * DAY),
        ("0 6 * * mon", 7 * DAY),
        ("0 0 * * 7", 7 * DAY),
        ("0 0 * * 0,7", 7 * DAY),
        ("15 10 * * 1,3,5", 3 * DAY),
    ],
)
def test_lists_ranges_steps_and_names(expression: str, gap: timedelta) -> None:
    assert _gap(expression) == gap


def test_seven_is_sunday() -> None:
    assert Cron.parse("0 0 * * 7").fires_on(date(2026, 9, 20))  # a Sunday
    assert not Cron.parse("0 0 * * 7").fires_on(date(2026, 9, 19))


def test_day_of_month_or_day_of_week_when_both_are_restricted() -> None:
    """POSIX: when both day fields are restricted, a day matches if EITHER does."""
    cron = Cron.parse("0 0 1 * 1")
    assert cron.fires_on(date(2026, 9, 1)), "the 1st (a Tuesday) matches the day of month"
    assert cron.fires_on(date(2026, 9, 7)), "a Monday matches the day of week"
    assert not cron.fires_on(date(2026, 9, 8))


def test_an_unrestricted_day_field_leaves_the_other_alone() -> None:
    mondays = Cron.parse("0 0 * * 1")
    assert mondays.fires_on(date(2026, 9, 7))
    assert not mondays.fires_on(date(2026, 9, 1)), "a star day of month must not widen to every day"
    firsts = Cron.parse("0 0 1 * *")
    assert firsts.fires_on(date(2026, 9, 1))
    assert not firsts.fires_on(date(2026, 9, 7)), "a star day of week must not widen to every day"


def test_a_stepped_star_is_unrestricted_so_the_day_fields_combine_with_and() -> None:
    """Vixie cron's reading, chosen because it yields fewer fire times: a
    disagreement with GitHub can loosen a bound, never tighten one."""
    cron = Cron.parse("0 0 */2 * 1")
    assert cron.fires_on(date(2026, 9, 7)), "an odd-dated Monday"
    assert not cron.fires_on(date(2026, 9, 14)), "an even-dated Monday"
    assert not cron.fires_on(date(2026, 9, 9)), "an odd-dated Wednesday"


def test_a_sparse_cron_gets_its_real_gap_beyond_the_window() -> None:
    """The fire before the window and the fire after now are included: a monthly
    cron has no two fires inside 14 days, and its gap is still a month."""
    assert _gap("0 0 1 * *") == timedelta(days=30)  # 2026-09-01 -> 2026-10-01


def test_the_union_uses_only_its_own_neighbours() -> None:
    """The weekly cron's last fire before the window is not a union fire time
    next to the window — the daily cron fired after it. Counting it would invent
    a 36-hour gap in a schedule that never waits more than a day."""
    assert _gap("0 0 * * *", "0 12 * * 1") == DAY


@pytest.mark.parametrize(
    "expression",
    [
        "",
        "* * * *",
        "* * * * * *",
        "60 * * * *",
        "* 24 * * *",
        "* * 0 * *",
        "* * 32 * *",
        "* * * 13 *",
        "* * * * 8",
        "*/0 * * * *",
        "5-1 * * * *",
        "a * * * *",
        "* * * FOO *",
        "* * * * MON-FOO",
        "1,,2 * * * *",
        "-1 * * * *",
        "@daily",
        "? * * * *",
    ],
)
def test_an_unreadable_cron_is_refused(expression: str) -> None:
    with pytest.raises(CronError):
        Cron.parse(expression)


@pytest.mark.parametrize("expression", ["0 0 30 2 *", "0 0 31 4 *"])
def test_a_cron_that_never_fires_is_refused(expression: str) -> None:
    with pytest.raises(CronError, match="never fires"):
        _gap(expression)


def test_a_dead_cron_beside_a_live_one_is_still_refused() -> None:
    with pytest.raises(CronError, match="never fires"):
        _gap("0 7 * * *", "0 0 30 2 *")


def test_a_leap_day_cron_is_not_dead() -> None:
    assert _gap("0 0 29 2 *") >= 365 * DAY


# --- the fake GitHub ---------------------------------------------------------------


def _workflow_text(*crons: str, key: str = "on") -> str:
    lines = ["name: watchdog", f"{key}:", "  workflow_dispatch:"]
    if crons:
        lines.append("  schedule:")
        lines += [f'    - cron: "{cron}"' for cron in crons]
    lines += ["jobs:", "  check:", "    runs-on: ubuntu-latest", "    steps:", "      - run: echo ok"]
    return "\n".join(lines) + "\n"


def _run(
    age: timedelta,
    *,
    conclusion: str | None = "success",
    status: str | None = None,
    event: str = "schedule",
    run_id: int = 0,
) -> dict:
    created = (NOW - age).strftime("%Y-%m-%dT%H:%M:%SZ")
    # The API pairs a null conclusion with a live status, a set one with `completed`.
    status = status or ("completed" if conclusion is not None else "in_progress")
    return {
        "id": run_id or int(age.total_seconds()),
        "event": event,
        "created_at": created,
        "status": status,
        "conclusion": conclusion,
    }


class FakeGitHub:
    """The five endpoints the tool reads, answered from dicts; `fail` maps a URL
    prefix to the (exit code, stderr) `gh api` gives for it."""

    def __init__(self, repo: str = REPO, *, branch: str = "main") -> None:
        self.repos: dict[str, dict] = {repo: {"default_branch": branch}}
        self.workflows: dict[str, list[dict]] = {repo: []}
        self.files: dict[tuple[str, str], str] = {}
        self.scheduled: dict[int, list[dict]] = {}
        self.unfiltered: dict[int, list[dict]] = {}
        self.changed: dict[tuple[str, str], datetime] = {}
        self.fail: dict[str, tuple[int, str]] = {}
        self.raw: dict[str, str] = {}
        self.calls: list[str] = []

    def add(
        self,
        name: str,
        text: str | None,
        *,
        runs: Sequence[dict] = (),
        unfiltered: Sequence[dict] | None = None,
        state: str = "active",
        changed: timedelta = 30 * DAY,
        repo: str = REPO,
    ) -> int:
        workflow_id = 1000 + len(self.files) + len(self.workflows[repo])
        path = _path(name)
        self.workflows[repo].append({"id": workflow_id, "path": path, "state": state, "name": name})
        if text is not None:
            self.files[(repo, path)] = text
        self.scheduled[workflow_id] = list(runs)
        self.unfiltered[workflow_id] = list(runs) if unfiltered is None else list(unfiltered)
        self.changed[(repo, path)] = NOW - changed
        return workflow_id

    def __call__(self, arguments: Sequence[str]) -> tuple[int, str, str]:
        assert list(arguments[:1]) == ["api"] and len(arguments) == 2, arguments
        url = arguments[1]
        self.calls.append(url)
        for prefix, (code, err) in self.fail.items():
            if url.startswith(prefix):
                return code, "", err
        if url in self.raw:
            return 0, self.raw[url], ""
        split = urllib.parse.urlsplit(url)
        query = {key: values[0] for key, values in urllib.parse.parse_qs(split.query).items()}
        parts = split.path.strip("/").split("/")
        repo = "/".join(parts[1:3])
        rest = parts[3:]
        if repo not in self.repos:
            return 1, "", "gh: Not Found (HTTP 404)"
        if not rest:
            return 0, json.dumps(self.repos[repo]), ""
        if rest == ["actions", "workflows"]:
            size, page = int(query["per_page"]), int(query["page"])
            listed = self.workflows[repo]
            body = {"total_count": len(listed), "workflows": listed[(page - 1) * size : page * size]}
            return 0, json.dumps(body), ""
        if rest[:2] == ["actions", "workflows"] and rest[3:] == ["runs"]:
            workflow_id = int(rest[2])
            runs = self.scheduled[workflow_id] if query.get("event") == "schedule" else self.unfiltered[workflow_id]
            return 0, json.dumps({"total_count": len(runs), "workflow_runs": runs[: int(query["per_page"])]}), ""
        if rest[0] == "contents":
            path = urllib.parse.unquote("/".join(rest[1:]))
            assert query.get("ref") == self.repos[repo]["default_branch"], "read the file off the default branch"
            if (repo, path) not in self.files:
                return 1, "", "gh: Not Found (HTTP 404)"
            content = base64.b64encode(self.files[(repo, path)].encode()).decode()
            return 0, json.dumps({"encoding": "base64", "content": content}), ""
        if rest == ["commits"]:
            key = (repo, query["path"])
            assert query.get("sha") == self.repos[repo]["default_branch"]
            if key not in self.changed:
                return 0, "[]", ""
            stamp = self.changed[key].strftime("%Y-%m-%dT%H:%M:%SZ")
            return 0, json.dumps([{"commit": {"committer": {"date": stamp}}}]), ""
        raise AssertionError(f"unexpected API path {url}")


def _verdicts(gh: FakeGitHub, *, repo: str = REPO, cap: timedelta | None = None) -> dict[str, tuple[str, str]]:
    verdicts, _ = _module.check_repository(repo, now=NOW, gh=gh, bound_cap=cap)
    return {verdict.path.rsplit("/", 1)[-1]: (verdict.status, verdict.detail) for verdict in verdicts}


def _status(gh: FakeGitHub, name: str) -> str:
    return _verdicts(gh)[name][0]


# --- classification ------------------------------------------------------------------


def test_a_daily_workflow_that_ran_this_morning_is_ok() -> None:
    gh = FakeGitHub()
    gh.add("daily.yml", _workflow_text("0 7 * * *"), runs=[_run(2 * HOUR)])
    status, detail = _verdicts(gh)["daily.yml"]
    assert status == "OK"
    assert detail == "last scheduled run 2h ago, bound 2d1h"


@pytest.mark.parametrize(("age", "status"), [(48 * HOUR, "OK"), (49 * HOUR + timedelta(minutes=1), "STALE")])
def test_a_daily_workflow_goes_stale_after_two_gaps_and_an_hour(age: timedelta, status: str) -> None:
    gh = FakeGitHub()
    gh.add("daily.yml", _workflow_text("0 7 * * *"), runs=[_run(age)])
    assert _status(gh, "daily.yml") == status


def test_the_bound_follows_each_workflows_own_gap() -> None:
    """A fixed bound is wrong in both directions: a weekly check three days
    after its last run is healthy, an hourly one silent for five hours is not."""
    gh = FakeGitHub()
    gh.add("weekly.yml", _workflow_text("0 6 * * 1"), runs=[_run(3 * DAY)])
    gh.add("hourly.yml", _workflow_text("47 * * * *"), runs=[_run(5 * HOUR)])
    gh.add("weekly-late.yml", _workflow_text("0 6 * * 1"), runs=[_run(15 * DAY)])
    verdicts = _verdicts(gh)
    assert verdicts["weekly.yml"][0] == "OK", verdicts["weekly.yml"]
    assert verdicts["hourly.yml"] == ("STALE", "last scheduled run 5h ago, bound 3h")
    assert verdicts["weekly-late.yml"][0] == "STALE"


@pytest.mark.parametrize("state", ["disabled_inactivity", "disabled_manually", "disabled_fork", ""])
def test_a_scheduled_workflow_that_is_not_active_is_red_even_with_a_recent_run(state: str) -> None:
    """GitHub disables schedules in a public repository after 60 days without
    activity; the last run can be recent right up to that moment."""
    gh = FakeGitHub()
    gh.add("daily.yml", _workflow_text("0 7 * * *"), runs=[_run(HOUR)], state=state)
    status, detail = _verdicts(gh)["daily.yml"]
    assert status == "DISABLED"
    assert f"state {state or 'missing'}" in detail


def test_a_workflow_that_never_ran_on_schedule_is_red_once_older_than_its_bound() -> None:
    gh = FakeGitHub()
    gh.add("old.yml", _workflow_text("0 7 * * *"), runs=[_run(HOUR, event="push")], changed=3 * DAY)
    status, detail = _verdicts(gh)["old.yml"]
    assert status == "NEVER"
    assert detail == "never ran on schedule, file last changed 3d0h ago, bound 2d1h"


def test_a_new_workflow_waiting_for_its_first_tick_is_not_red() -> None:
    gh = FakeGitHub()
    gh.add("new.yml", _workflow_text("0 7 * * *"), changed=2 * HOUR)
    status, detail = _verdicts(gh)["new.yml"]
    assert status == "OK"
    assert detail.startswith("no scheduled run yet, file last changed 2h ago")


def test_a_startup_failure_is_not_a_tick() -> None:
    """No job ran, so neither did the check the workflow hosts, nor its own escalation."""
    gh = FakeGitHub()
    gh.add(
        "broken.yml",
        _workflow_text("0 7 * * *"),
        runs=[_run(HOUR, conclusion="startup_failure"), _run(3 * DAY)],
    )
    status, detail = _verdicts(gh)["broken.yml"]
    assert status == "STALE"
    assert "(1 newer scheduled runs failed at startup or are still queued)" in detail


def test_only_startup_failures_is_stale_not_new() -> None:
    gh = FakeGitHub()
    gh.add("broken.yml", _workflow_text("0 7 * * *"), runs=[_run(HOUR, conclusion="startup_failure")], changed=HOUR)
    status, detail = _verdicts(gh)["broken.yml"]
    assert status == "STALE"
    assert detail == "1 scheduled runs listed, and none has started a job (startup failure or still queued), bound 2d1h"


@pytest.mark.parametrize("status", ["queued", "waiting", "pending", "requested", None])
def test_a_run_that_never_left_the_queue_is_not_a_tick(status: str | None) -> None:
    """Review: a scheduler that keeps creating runs no runner picks up looks
    alive by `created_at` alone, and the check it hosts never runs."""
    gh = FakeGitHub()
    stuck = _run(HOUR, conclusion=None, status="placeholder")
    stuck["status"] = status
    gh.add("stuck.yml", _workflow_text("47 * * * *"), runs=[stuck, _run(5 * HOUR)])
    status_, detail = _verdicts(gh)["stuck.yml"]
    assert status_ == "STALE", detail
    assert "still queued" in detail


def test_a_cancelled_or_failed_run_still_ran_a_job() -> None:
    gh = FakeGitHub()
    gh.add("failed.yml", _workflow_text("0 7 * * *"), runs=[_run(HOUR, conclusion="failure")])
    gh.add("cancelled.yml", _workflow_text("0 7 * * *"), runs=[_run(HOUR, conclusion="cancelled")])
    verdicts = _verdicts(gh)
    assert verdicts["failed.yml"][0] == "OK"
    assert verdicts["cancelled.yml"][0] == "OK"


def test_a_startup_failure_verdict_does_not_depend_on_the_file_history() -> None:
    """Review: the verdict is already decided by the runs, so a failing history
    read must neither be made nor turn it UNVERIFIABLE."""
    gh = FakeGitHub()
    gh.add("broken.yml", _workflow_text("0 7 * * *"), runs=[_run(HOUR, conclusion="startup_failure")])
    gh.fail[f"/repos/{REPO}/commits"] = (1, "gh: Server Error (HTTP 502)")
    assert _status(gh, "broken.yml") == "STALE"
    assert not any(url.startswith(f"/repos/{REPO}/commits") for url in gh.calls)


def test_a_ticking_workflow_never_reads_the_file_history() -> None:
    gh = FakeGitHub()
    gh.add("daily.yml", _workflow_text("0 7 * * *"), runs=[_run(HOUR)])
    gh.add("late.yml", _workflow_text("0 7 * * *"), runs=[_run(9 * DAY)])
    _verdicts(gh)
    assert not any(url.startswith(f"/repos/{REPO}/commits") for url in gh.calls)


def test_a_run_in_progress_is_a_tick() -> None:
    gh = FakeGitHub()
    gh.add("daily.yml", _workflow_text("0 7 * * *"), runs=[_run(timedelta(minutes=3), conclusion=None)])
    assert _status(gh, "daily.yml") == "OK"


def test_the_newest_run_is_the_maximum_not_the_first_listed() -> None:
    gh = FakeGitHub()
    gh.add("daily.yml", _workflow_text("0 7 * * *"), runs=[_run(4 * DAY), _run(HOUR), _run(3 * DAY)])
    assert _status(gh, "daily.yml") == "OK"


def test_a_stale_filtered_listing_is_overruled_by_the_unfiltered_one() -> None:
    """2026-09-16: `?event=schedule` answered once with a newest run twelve days
    old while the unfiltered listing showed one an hour old. Any run that exists
    proves a tick."""
    gh = FakeGitHub()
    gh.add(
        "hourly.yml",
        _workflow_text("47 * * * *"),
        runs=[_run(12 * DAY)],
        unfiltered=[_run(10 * timedelta(minutes=1), event="pull_request"), _run(HOUR)],
    )
    status, detail = _verdicts(gh)["hourly.yml"]
    assert status == "OK", detail
    assert detail.startswith("last scheduled run 1h ago")


def test_the_unfiltered_witness_counts_only_scheduled_runs() -> None:
    gh = FakeGitHub()
    gh.add(
        "hourly.yml",
        _workflow_text("47 * * * *"),
        runs=[_run(12 * DAY)],
        unfiltered=[_run(10 * timedelta(minutes=1), event="push"), _run(HOUR, event="workflow_dispatch")],
    )
    assert _status(gh, "hourly.yml") == "STALE"


def test_a_healthy_workflow_costs_no_second_witness() -> None:
    gh = FakeGitHub()
    workflow_id = gh.add("daily.yml", _workflow_text("0 7 * * *"), runs=[_run(HOUR)])
    _verdicts(gh)
    unfiltered = [url for url in gh.calls if url.startswith(f"/repos/{REPO}/actions/workflows/{workflow_id}/runs?per")]
    assert unfiltered == []


@pytest.mark.parametrize(
    ("cap", "status"), [(timedelta(0), "STALE"), (30 * DAY, "OK"), (HOUR + timedelta(minutes=1), "OK")]
)
def test_the_cap_only_tightens(cap: timedelta, status: str) -> None:
    """The drill passes 0 and must see red; a cap above the measured bound must
    not loosen it — the hourly workflow stays red at a 30-day cap."""
    gh = FakeGitHub()
    gh.add("daily.yml", _workflow_text("0 7 * * *"), runs=[_run(HOUR)])
    gh.add("hourly.yml", _workflow_text("47 * * * *"), runs=[_run(5 * HOUR)])
    verdicts = _verdicts(gh, cap=cap)
    assert verdicts["daily.yml"][0] == status
    assert verdicts["hourly.yml"][0] == "STALE"


# --- what is and is not a scheduled workflow ------------------------------------------


def test_a_workflow_without_a_schedule_gets_no_verdict() -> None:
    gh = FakeGitHub()
    gh.add("push.yml", _workflow_text())
    gh.add("string.yml", "on: push\njobs: {}\n")
    gh.add("list.yml", "on: [push, pull_request]\njobs: {}\n")
    gh.add("empty-on.yml", "name: x\non:\njobs: {}\n")
    assert _verdicts(gh) == {}


def test_a_quoted_on_key_is_read_too() -> None:
    """infra2 writes `"on":`; PyYAML reads a bare `on` as True. Both must be found."""
    gh = FakeGitHub()
    gh.add("quoted.yml", _workflow_text("0 7 * * *", key='"on"'), runs=[_run(HOUR)])
    gh.add("bare.yml", _workflow_text("0 7 * * *", key="on"), runs=[_run(HOUR)])
    assert _verdicts(gh) == {
        "quoted.yml": ("OK", "last scheduled run 1h ago, bound 2d1h"),
        "bare.yml": ("OK", "last scheduled run 1h ago, bound 2d1h"),
    }


def test_github_managed_workflows_are_skipped_without_a_read() -> None:
    gh = FakeGitHub()
    gh.workflows[REPO].append({"id": 7, "path": "dynamic/agents/copilot-pull-request-reviewer", "state": "active"})
    assert _verdicts(gh) == {}
    assert not any("/contents/" in url for url in gh.calls)


def test_a_listed_workflow_absent_from_the_default_branch_is_skipped() -> None:
    """Only the default branch's schedules run; a 404 there means no scheduler expects it."""
    gh = FakeGitHub()
    gh.add("branch-only.yml", None)
    assert _verdicts(gh) == {}


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("on:\n  schedule:\n    - cron: '0 7 * * *'\n  push: [unclosed\n", "not valid YAML"),
        ("- just\n- a list\n", "not a YAML mapping"),
        ("on: [push, schedule]\n", "without any cron"),
        ("on:\n  schedule: '0 7 * * *'\n", "not a list of crons"),
        ("on:\n  schedule: []\n", "not a list of crons"),
        ("on:\n  schedule:\n    - interval: 5m\n", "has no cron string"),
        ("on:\n  schedule:\n    - cron: 7\n", "has no cron string"),
        (_workflow_text("0 7 * *"), "has 4 fields"),
        (_workflow_text("0 0 30 2 *"), "never fires"),
    ],
    ids=[
        "broken-yaml",
        "not-a-mapping",
        "bare-schedule",
        "schedule-string",
        "schedule-empty",
        "no-cron",
        "cron-int",
        "short-cron",
        "dead-cron",
    ],
)
def test_a_file_github_cannot_schedule_is_invalid(text: str, reason: str) -> None:
    gh = FakeGitHub()
    gh.add("bad.yml", text, runs=[_run(HOUR)])
    status, detail = _verdicts(gh)["bad.yml"]
    assert status == "INVALID"
    assert reason in detail


# --- an API that does not answer is red ------------------------------------------------


def test_an_unreadable_workflow_listing_is_red_not_empty(capsys: pytest.CaptureFixture[str]) -> None:
    gh = FakeGitHub()
    gh.add("daily.yml", _workflow_text("0 7 * * *"), runs=[_run(HOUR)])
    gh.fail[f"/repos/{REPO}/actions/workflows?"] = (1, "gh: API rate limit exceeded (HTTP 403)")
    assert _module.run([REPO], now=NOW, gh=gh) == 1
    out = capsys.readouterr().out
    assert f"{REPO} (workflow listing): cannot verify:" in out
    assert out.rstrip().endswith("0 scheduled workflows in 1 repositories: 1 red")


@pytest.mark.parametrize(
    "setup",
    [
        lambda gh: gh.fail.__setitem__(f"/repos/{REPO}", (1, "gh: Bad credentials (HTTP 401)")),
        lambda gh: gh.raw.__setitem__(f"/repos/{REPO}", "<html>502</html>"),
        lambda gh: gh.raw.__setitem__(f"/repos/{REPO}", "[]"),
        lambda gh: gh.raw.__setitem__(f"/repos/{REPO}", json.dumps({"default_branch": ""})),
        lambda gh: gh.raw.__setitem__(
            f"/repos/{REPO}/actions/workflows?per_page=100&page=1", json.dumps({"workflows": []})
        ),
        lambda gh: gh.raw.__setitem__(
            f"/repos/{REPO}/actions/workflows?per_page=100&page=1", json.dumps({"total_count": 3, "workflows": []})
        ),
        lambda gh: gh.raw.__setitem__(
            f"/repos/{REPO}/actions/workflows?per_page=100&page=1", json.dumps({"total_count": 1, "workflows": "x"})
        ),
        lambda gh: gh.fail.__setitem__(f"/repos/{REPO}", (1, "gh: Not Found (HTTP 404)")),
    ],
    ids=[
        "repo-401",
        "repo-not-json",
        "repo-not-object",
        "no-default-branch",
        "no-total-count",
        "listing-stops-short",
        "listing-not-a-list",
        "repo-404",
    ],
)
def test_every_listing_failure_is_unverifiable(setup, capsys: pytest.CaptureFixture[str]) -> None:  # noqa: ANN001
    gh = FakeGitHub()
    gh.add("daily.yml", _workflow_text("0 7 * * *"), runs=[_run(HOUR)])
    setup(gh)
    verdicts, _ = _module.check_repository(REPO, now=NOW, gh=gh)
    assert [(verdict.path, verdict.status) for verdict in verdicts] == [("(workflow listing)", "UNVERIFIABLE")]
    assert _module.run([REPO], now=NOW, gh=gh) == 1


def test_the_listing_is_read_across_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_module, "PER_PAGE", 2)
    gh = FakeGitHub()
    for index in range(5):
        gh.add(f"w{index}.yml", _workflow_text("0 7 * * *"), runs=[_run(HOUR)])
    gh.add("w-stale.yml", _workflow_text("0 7 * * *"), runs=[_run(9 * DAY)])
    verdicts = _verdicts(gh)
    assert len(verdicts) == 6, sorted(verdicts)
    assert verdicts["w-stale.yml"][0] == "STALE", "the workflow on the last page was never judged"


def test_a_listing_that_never_ends_is_unverifiable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_module, "MAX_PAGES", 3)
    monkeypatch.setattr(_module, "PER_PAGE", 1)
    gh = FakeGitHub()
    for page in range(1, 4):
        gh.raw[f"/repos/{REPO}/actions/workflows?per_page=1&page={page}"] = json.dumps(
            {"total_count": 99, "workflows": [{"id": page, "path": "dynamic/x", "state": "active"}]}
        )
    verdicts, _ = _module.check_repository(REPO, now=NOW, gh=gh)
    assert [verdict.status for verdict in verdicts] == ["UNVERIFIABLE"]


@pytest.mark.parametrize("where", ["file", "scheduled runs", "unfiltered runs", "file history"])
def test_a_failed_read_for_one_workflow_is_red_and_the_others_are_still_judged(where: str) -> None:
    gh = FakeGitHub()
    # Never run and old, so every read below is reached.
    target = gh.add("target.yml", _workflow_text("0 7 * * *"), runs=[], changed=5 * DAY)
    gh.add("other.yml", _workflow_text("0 7 * * *"), runs=[_run(HOUR)])
    prefix = {
        "file": f"/repos/{REPO}/contents/{urllib.parse.quote(_path('target.yml'))}",
        "scheduled runs": f"/repos/{REPO}/actions/workflows/{target}/runs?event=schedule",
        "unfiltered runs": f"/repos/{REPO}/actions/workflows/{target}/runs?per_page",
        "file history": f"/repos/{REPO}/commits",
    }[where]
    gh.fail[prefix] = (1, "gh: Server Error (HTTP 502)")
    verdicts = _verdicts(gh)
    assert verdicts["target.yml"][0] == "UNVERIFIABLE", verdicts["target.yml"]
    assert "cannot verify" in verdicts["target.yml"][1]
    assert verdicts["other.yml"][0] == "OK"


@pytest.mark.parametrize(
    "body",
    [
        {"encoding": "none", "content": ""},
        {"encoding": "base64"},
        {"encoding": "base64", "content": "!!!not base64"},
    ],
    ids=["not-base64", "no-content", "undecodable"],
)
def test_an_unusable_file_body_is_unverifiable(body: dict) -> None:
    gh = FakeGitHub()
    gh.add("target.yml", _workflow_text("0 7 * * *"), runs=[_run(HOUR)])
    gh.raw[f"/repos/{REPO}/contents/{urllib.parse.quote(_path('target.yml'))}?ref=main"] = json.dumps(body)
    assert _status(gh, "target.yml") == "UNVERIFIABLE"


@pytest.mark.parametrize(
    "runs",
    [
        {"total_count": 1, "workflow_runs": [{"id": 1, "event": "schedule", "created_at": None}]},
        {"total_count": 1, "workflow_runs": [{"id": 1, "event": "schedule", "created_at": "yesterday"}]},
        {"total_count": 1, "workflow_runs": [{"id": 1, "event": "schedule", "created_at": "2026-09-16T08:00:00"}]},
        {"total_count": 1, "workflow_runs": None},
        {"message": "Not Found"},
    ],
    ids=["null-time", "unreadable-time", "zoneless-time", "null-runs", "error-object"],
)
def test_an_unusable_run_listing_is_unverifiable(runs: dict) -> None:
    gh = FakeGitHub()
    target = gh.add("target.yml", _workflow_text("0 7 * * *"), runs=[_run(HOUR)])
    gh.raw[f"/repos/{REPO}/actions/workflows/{target}/runs?event=schedule&per_page=100&page=1"] = json.dumps(runs)
    assert _status(gh, "target.yml") == "UNVERIFIABLE"


def test_a_never_run_workflow_with_no_history_is_unverifiable() -> None:
    gh = FakeGitHub()
    gh.add("target.yml", _workflow_text("0 7 * * *"))
    gh.changed.clear()
    assert _status(gh, "target.yml") == "UNVERIFIABLE"


# --- the command line -------------------------------------------------------------------


def test_the_cli_reports_one_line_per_scheduled_workflow_and_exits_by_verdict(
    capsys: pytest.CaptureFixture[str],
) -> None:
    gh = FakeGitHub()
    gh.add("daily.yml", _workflow_text("0 7 * * *"), runs=[_run(HOUR)])
    gh.add("push.yml", _workflow_text())
    assert _module.main([REPO], gh=gh, now=NOW) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == f"{REPO} {_path('daily.yml')}: last scheduled run 1h ago, bound 2d1h, OK"
    assert out[-1] == "1 scheduled workflows in 1 repositories: 0 red"

    gh.add("hourly.yml", _workflow_text("47 * * * *"), runs=[_run(5 * HOUR)])
    assert _module.main([REPO], gh=gh, now=NOW) == 1
    captured = capsys.readouterr()
    assert f"{REPO} {_path('hourly.yml')}: last scheduled run 5h ago, bound 3h, STALE" in captured.out
    assert f"::error::{REPO} {_path('hourly.yml')}" in captured.err


def test_one_unreadable_repository_does_not_hide_the_next(capsys: pytest.CaptureFixture[str]) -> None:
    gh = FakeGitHub()
    gh.add("daily.yml", _workflow_text("0 7 * * *"), runs=[_run(HOUR)])
    assert _module.main(["owner/missing", REPO], gh=gh, now=NOW) == 1
    out = capsys.readouterr().out
    assert "owner/missing (workflow listing): cannot verify" in out
    assert f"{REPO} {_path('daily.yml')}: last scheduled run 1h ago" in out


def test_a_repository_with_nothing_scheduled_says_so(capsys: pytest.CaptureFixture[str]) -> None:
    gh = FakeGitHub()
    gh.add("push.yml", _workflow_text())
    assert _module.main([REPO], gh=gh, now=NOW) == 0
    assert f"{REPO}: 1 workflows listed, none scheduled" in capsys.readouterr().out


@pytest.mark.parametrize(("value", "status", "code"), [("", "OK", 0), ("0", "STALE", 1), ("720", "OK", 0)])
def test_the_cap_option_accepts_an_empty_value(value: str, status: str, code: int, capsys) -> None:  # noqa: ANN001
    """The workflow passes its dispatch input through unconditionally; empty is no cap."""
    gh = FakeGitHub()
    gh.add("daily.yml", _workflow_text("0 7 * * *"), runs=[_run(HOUR)])
    assert _module.main([REPO, "--bound-cap-hours", value], gh=gh, now=NOW) == code
    assert f", {status}\n" in capsys.readouterr().out


@pytest.mark.parametrize(
    "argv",
    [
        ["--bound-cap-hours", "-1", REPO],
        ["--bound-cap-hours", "soon", REPO],
        ["--bound-cap-hours", "nan", REPO],
        ["--bound-cap-hours", "inf", REPO],
        ["--bound-cap-hours", "1e12", REPO],
        [],
    ],
)
def test_the_cli_refuses_a_bad_cap_or_no_repository(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exited:
        _module.main(argv, gh=FakeGitHub(), now=NOW)
    assert exited.value.code == 2


@pytest.mark.parametrize("repo", ["repo", "owner/repo/extra", "owner/re po", "../x", "owner/..", "owner/"])
def test_the_cli_refuses_a_malformed_repository(repo: str) -> None:
    gh = FakeGitHub()
    assert _module.main([repo], gh=gh, now=NOW) == 2
    assert gh.calls == []
