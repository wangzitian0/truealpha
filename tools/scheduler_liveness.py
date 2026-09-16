"""Fail when a scheduled workflow anywhere in the estate has stopped ticking.

#876 (W5, gap 4: "watchdog of watchdogs"). Every standing check in the estate is
a GitHub `schedule` workflow, and a schedule that stops firing looks exactly like
a quiet green one: GitHub delays or drops scheduled runs under load, disables
them in a public repository after 60 days without activity, and never runs a
file that is not valid YAML. Nothing checked any of that.

For each repository named on the command line, this DISCOVERS the scheduled
workflows itself — the workflow listing, each file's content on the default
branch, its `on.schedule[*].cron` — and derives each one's bound from its own
crons: the largest gap between consecutive fire times over a 14-day window,
times two (GitHub drops single ticks), plus an hour (GitHub delays ticks under
load). No workflow name, period or bound is written down here; a constant never
stands in for a measurement (AGENTS.md).

A scheduled workflow is red when:

- its state is not `active` (`disabled_inactivity`, `disabled_manually`, ...):
  DISABLED;
- its newest scheduled run that got past startup is older than the bound: STALE.
  A `startup_failure` run is not a tick — no job ran, so neither did the check
  it hosts or that check's own escalation;
- it has never run on schedule and its file last changed longer ago than the
  bound: NEVER (a file changed more recently is simply waiting for its first
  tick);
- its file is not valid YAML, or a cron does not parse or never fires: INVALID;
- the API could not answer a question the verdict depends on: UNVERIFIABLE. An
  unreadable listing is never a silent pass.

The newest scheduled run is read from two witnesses, because the filtered
listing is not always current: on 2026-09-16 `?event=schedule` answered once
with a newest run twelve days old (and a total_count 600 short) while the
unfiltered listing showed one an hour old. Only when BOTH are older than the
bound is the workflow STALE; any run that exists proves a tick.

Cron semantics (the 5-field POSIX syntax GitHub documents, in UTC): numbers,
`*`, `*/n`, `a-b`, `a-b/n`, `a/n`, `a,b` and combinations; month and weekday
names; weekday 7 is Sunday. When BOTH day-of-month and day-of-week are
restricted a day matches if EITHER does; a field is unrestricted when it starts
with `*` (Vixie cron's rule, so `*/2` counts as unrestricted). Where cron
implementations disagree this reading yields the fewer fire times, so the larger
gap and the looser bound: a disagreement can delay a red, never raise a false
one. Nothing here decides that GitHub accepts a cron; a cron GitHub rejects
never runs, and that surfaces as NEVER or STALE.

The scheduler for THIS tool's own workflow is checked by this tool too, which
proves nothing if that scheduler is the one that died; a peer check in another
repository closes that loop (#876).

Stdlib, PyYAML and the `gh` CLI.

Usage:
  python tools/scheduler_liveness.py OWNER/REPO [OWNER/REPO ...] [--bound-cap-hours H]

`--bound-cap-hours` can only TIGHTEN a bound (min of the measured bound and the
cap); `0` forces every scheduled workflow red, which is how the alert is drilled.

Exit codes:
  0 - every scheduled workflow in every repository ticked within its bound
  1 - at least one is STALE, DISABLED, NEVER, INVALID or UNVERIFIABLE
  2 - usage error
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import re
import subprocess
import sys
import urllib.parse
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import yaml

#: The span a schedule's fire times are sampled over. Two weeks covers every
#: weekly cron twice; a sparser cron still gets its real gap, because the fire
#: before the window and the fire after it are included.
WINDOW = timedelta(days=14)
#: How far a sparse cron's neighbouring fire is searched for. Five years reaches
#: a leap day from anywhere; a cron with no fire inside it never fires.
HORIZON_DAYS = 5 * 366
#: GitHub drops single scheduled runs under load, so one missed tick is not a
#: stopped scheduler: the bound is this many gaps...
MISSED_TICKS_TOLERATED = 2
#: ...plus the delay GitHub documents for scheduled runs at busy times.
DELAY_ALLOWANCE = timedelta(hours=1)
PER_PAGE = 100
#: A runaway pagination loop is an API fault, not a large repository.
MAX_PAGES = 50
WORKFLOW_DIR = ".github/workflows/"
#: A cap only tightens, so anything past the horizon is no cap at all.
MAX_CAP_HOURS = HORIZON_DAYS * 24
ACTIVE = "active"
STARTUP_FAILURE = "startup_failure"

OK = "OK"
STALE = "STALE"
DISABLED = "DISABLED"
NEVER = "NEVER"
INVALID = "INVALID"
UNVERIFIABLE = "UNVERIFIABLE"
RED = (STALE, DISABLED, NEVER, INVALID, UNVERIFIABLE)
#: The path a verdict carries when the repository itself could not be read.
LISTING = "(workflow listing)"

#: (returncode, stdout, stderr) for `gh <arguments>`.
GhResult = tuple[int, str, str]
Gh = Callable[[Sequence[str]], GhResult]


# --- cron -----------------------------------------------------------------------


class CronError(ValueError):
    """A cron expression this checker cannot read, or one that never fires."""


_MONTHS = {name: number for number, name in enumerate(("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC").split(), 1)}
_WEEKDAYS = {name: number for number, name in enumerate(("SUN MON TUE WED THU FRI SAT").split())}
# (name, lowest, highest, names). Day of week admits 7 as a second Sunday.
_FIELDS: tuple[tuple[str, int, int, dict[str, int]], ...] = (
    ("minute", 0, 59, {}),
    ("hour", 0, 23, {}),
    ("day of month", 1, 31, {}),
    ("month", 1, 12, _MONTHS),
    ("day of week", 0, 7, _WEEKDAYS),
)
_ELEMENT = re.compile(r"^(?P<range>\*|[0-9A-Za-z]+(?:-[0-9A-Za-z]+)?)(?:/(?P<step>[0-9]+))?$")


def _value(token: str, field: str, low: int, high: int, names: dict[str, int]) -> int:
    if token.isdigit():
        number = int(token)
    elif token.upper() in names:
        number = names[token.upper()]
    else:
        raise CronError(f"{field}: {token!r} is not a number{' or a name' if names else ''}")
    if not low <= number <= high:
        raise CronError(f"{field}: {number} is outside {low}-{high}")
    return number


def _field(text: str, field: str, low: int, high: int, names: dict[str, int]) -> frozenset[int]:
    values: set[int] = set()
    for element in text.split(","):
        match = _ELEMENT.match(element)
        if not match:
            raise CronError(f"{field}: cannot read {element!r}")
        span, step_text = match.group("range"), match.group("step")
        step = int(step_text) if step_text is not None else 1
        if step < 1:
            raise CronError(f"{field}: step {step} in {element!r} must be at least 1")
        if span == "*":
            first, last = low, high
        elif "-" in span:
            start, end = span.split("-", 1)
            first, last = _value(start, field, low, high, names), _value(end, field, low, high, names)
            if first > last:
                raise CronError(f"{field}: range {element!r} runs backwards")
        else:
            first = _value(span, field, low, high, names)
            # `a/n` is `a-max/n`; a bare `a` is just `a`.
            last = high if step_text is not None else first
        values.update(range(first, last + 1, step))
    return frozenset(values)


@dataclass(frozen=True)
class Cron:
    expression: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]  # 0 = Sunday
    days_restricted: bool
    weekdays_restricted: bool

    @classmethod
    def parse(cls, expression: str) -> Cron:
        if not isinstance(expression, str):
            raise CronError(f"cron {expression!r} is not a string")
        parts = expression.split()
        if len(parts) != len(_FIELDS):
            raise CronError(f"cron {expression!r} has {len(parts)} fields, not {len(_FIELDS)}")
        minutes, hours, days, months, weekdays = (
            _field(text, *spec) for text, spec in zip(parts, _FIELDS, strict=True)
        )
        return cls(
            expression=expression,
            minutes=minutes,
            hours=hours,
            days=days,
            months=months,
            weekdays=frozenset(day % 7 for day in weekdays),
            days_restricted=not parts[2].startswith("*"),
            weekdays_restricted=not parts[4].startswith("*"),
        )

    def fires_on(self, day: date) -> bool:
        if day.month not in self.months:
            return False
        in_days = day.day in self.days
        in_weekdays = day.isoweekday() % 7 in self.weekdays
        if self.days_restricted and self.weekdays_restricted:
            return in_days or in_weekdays
        # An unrestricted field holds every value, so this is the other field alone.
        return in_days and in_weekdays

    def _times(self, day: date) -> list[datetime]:
        if not self.fires_on(day):
            return []
        return [
            datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)
            for hour in sorted(self.hours)
            for minute in sorted(self.minutes)
        ]

    def fires_between(self, start: datetime, end: datetime) -> list[datetime]:
        """Every fire time in `[start, end]`."""
        fires: list[datetime] = []
        day = start.date()
        while day <= end.date():
            fires += [moment for moment in self._times(day) if start <= moment <= end]
            day += timedelta(days=1)
        return fires

    def previous_fire(self, before: datetime) -> datetime | None:
        """The latest fire time strictly before `before`, within the horizon."""
        day = before.date()
        for _ in range(HORIZON_DAYS):
            earlier = [moment for moment in self._times(day) if moment < before]
            if earlier:
                return earlier[-1]
            day -= timedelta(days=1)
        return None

    def next_fire(self, after: datetime) -> datetime | None:
        """The earliest fire time strictly after `after`, within the horizon."""
        day = after.date()
        for _ in range(HORIZON_DAYS):
            later = [moment for moment in self._times(day) if moment > after]
            if later:
                return later[0]
            day += timedelta(days=1)
        return None


def largest_gap(crons: Sequence[Cron], now: datetime, window: timedelta = WINDOW) -> timedelta:
    """The longest wait between consecutive fire times of the UNION of `crons`
    over `[now - window, now]`, including the gaps into the fire before the
    window and the fire after `now`.

    A workflow with several crons ticks whenever any of them fires, so the union
    is what its scheduler is expected to produce.
    """
    now = now.astimezone(UTC)
    start = now - window
    fires: set[datetime] = set()
    before: list[datetime] = []
    after: list[datetime] = []
    for cron in crons:
        previous, following = cron.previous_fire(start), cron.next_fire(now)
        if previous is None and following is None:
            raise CronError(f"cron {cron.expression!r} never fires")
        fires.update(cron.fires_between(start, now))
        if previous is not None:
            before.append(previous)
        if following is not None:
            after.append(following)
    # Only the union's own neighbours: an earlier cron's older fire is not a
    # union fire time adjacent to the window, and would invent a longer gap.
    if before:
        fires.add(max(before))
    if after:
        fires.add(min(after))
    ordered = sorted(fires)
    if len(ordered) < 2:
        raise CronError(f"{[cron.expression for cron in crons]} fire fewer than twice in {HORIZON_DAYS} days")
    return max(later - earlier for earlier, later in zip(ordered, ordered[1:]))


def bound_for(gap: timedelta) -> timedelta:
    return MISSED_TICKS_TOLERATED * gap + DELAY_ALLOWANCE


# --- the GitHub API ---------------------------------------------------------------


class ApiError(RuntimeError):
    """The API did not answer, so nothing may be decided from it."""

    def __init__(self, message: str, *, not_found: bool = False) -> None:
        super().__init__(message)
        self.not_found = not_found


def _gh(arguments: Sequence[str]) -> GhResult:
    result = subprocess.run(["gh", *arguments], capture_output=True, text=True, check=False)
    return result.returncode, result.stdout, result.stderr


def _get(path: str, gh: Gh) -> object:
    code, out, err = gh(["api", path])
    if code != 0:
        message = err.strip()[:300] or f"gh api exited {code}"
        raise ApiError(f"GET {path} failed: {message}", not_found="HTTP 404" in err)
    try:
        return json.loads(out)
    except json.JSONDecodeError as error:
        raise ApiError(f"GET {path} returned something that is not JSON: {error}") from error


def _object(path: str, gh: Gh) -> dict:
    body = _get(path, gh)
    if not isinstance(body, dict):
        raise ApiError(f"GET {path} returned {type(body).__name__}, not an object")
    return body


def _page(path: str, key: str, gh: Gh, page: int = 1) -> tuple[list[dict], int]:
    separator = "&" if "?" in path else "?"
    url = f"{path}{separator}per_page={PER_PAGE}&page={page}"
    body = _object(url, gh)
    items, total = body.get(key), body.get("total_count")
    if not isinstance(items, list) or not isinstance(total, int) or not all(isinstance(item, dict) for item in items):
        raise ApiError(f"GET {url} has no usable {key!r} list and total_count")
    return items, total


def _all_pages(path: str, key: str, gh: Gh) -> list[dict]:
    collected: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        items, total = _page(path, key, gh, page)
        collected += items
        if len(collected) >= total:
            return collected
        if not items:
            # Fewer than it promised: a short read is not the whole listing,
            # and a workflow missing from it would be a silent pass.
            raise ApiError(f"{path} listed {len(collected)} of {total} {key} and then stopped")
    raise ApiError(f"{path} did not finish within {MAX_PAGES} pages")


def _timestamp(value: object, what: str) -> datetime:
    if not isinstance(value, str):
        raise ApiError(f"{what} has no timestamp ({value!r})")
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ApiError(f"{what} has an unreadable timestamp {value!r}") from error
    if moment.tzinfo is None:
        raise ApiError(f"{what} has a timestamp without a zone ({value!r})")
    return moment.astimezone(UTC)


@dataclass(frozen=True)
class Ticks:
    newest: datetime | None  # the newest scheduled run that got past startup
    startup_failures: int  # scheduled runs newer than that which never started a job


def _ticks(runs: Iterable[dict], path: str) -> Ticks:
    """The newest real tick among `runs`, taking the maximum rather than
    trusting the listing's order. `runs` may repeat a run (two witnesses)."""
    scheduled = {run.get("id", index): run for index, run in enumerate(runs) if run.get("event") == "schedule"}
    times = [
        (_timestamp(run.get("created_at"), f"a run of {path}"), run.get("conclusion")) for run in scheduled.values()
    ]
    started = [moment for moment, conclusion in times if conclusion != STARTUP_FAILURE]
    newest = max(started) if started else None
    failed = sum(
        1 for moment, conclusion in times if conclusion == STARTUP_FAILURE and (newest is None or moment > newest)
    )
    return Ticks(newest=newest, startup_failures=failed)


def _file_text(repo: str, path: str, branch: str, gh: Gh) -> str:
    quoted = urllib.parse.quote(path)
    body = _object(f"/repos/{repo}/contents/{quoted}?ref={urllib.parse.quote(branch, safe='')}", gh)
    if body.get("encoding") != "base64" or not isinstance(body.get("content"), str):
        raise ApiError(f"{path} came back without base64 content (encoding {body.get('encoding')!r})")
    try:
        return base64.b64decode(body["content"]).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as error:
        raise ApiError(f"{path} content does not decode: {error}") from error


def _last_changed(repo: str, path: str, branch: str, gh: Gh) -> datetime:
    quoted = urllib.parse.quote(path, safe="")
    commits = _get(f"/repos/{repo}/commits?path={quoted}&sha={urllib.parse.quote(branch, safe='')}&per_page=1", gh)
    if not isinstance(commits, list) or not commits or not isinstance(commits[0], dict):
        raise ApiError(f"no commit on {branch} touches {path}")
    committer = (commits[0].get("commit") or {}).get("committer") or {}
    return _timestamp(committer.get("date"), f"the last commit to {path}")


# --- verdicts ---------------------------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    repo: str
    path: str
    status: str
    detail: str

    @property
    def red(self) -> bool:
        return self.status in RED

    def line(self) -> str:
        return f"{self.repo} {self.path}: {self.detail}, {self.status}"


def human(span: timedelta) -> str:
    minutes = max(int(span.total_seconds() // 60), 0)
    days, minutes = divmod(minutes, 24 * 60)
    hours, minutes = divmod(minutes, 60)
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes:02d}m" if minutes else f"{hours}h"
    return f"{minutes}m"


def schedule_of(text: str) -> list[str] | None:
    """The workflow's cron strings, or None when it has no schedule.

    Raises CronError when the file cannot be a workflow GitHub would schedule.
    """
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise CronError(f"not valid YAML ({str(error).splitlines()[0]}), so GitHub will not run it") from error
    if not isinstance(document, dict):
        raise CronError("not a YAML mapping, so GitHub will not run it")
    # YAML 1.1 (PyYAML) reads a bare `on` key as the boolean True.
    triggers = document.get("on", document.get(True))
    if isinstance(triggers, str | list):
        names = [triggers] if isinstance(triggers, str) else triggers
        if "schedule" in names:
            raise CronError("`schedule` is named without any cron")
        return None
    if not isinstance(triggers, dict) or "schedule" not in triggers:
        return None
    entries = triggers["schedule"]
    if not isinstance(entries, list) or not entries:
        raise CronError("`on.schedule` is not a list of crons")
    crons = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("cron"), str):
            raise CronError(f"`on.schedule` entry {entry!r} has no cron string")
        crons.append(entry["cron"])
    return crons


def check_workflow(
    repo: str,
    branch: str,
    workflow: dict,
    *,
    now: datetime,
    gh: Gh,
    bound_cap: timedelta | None = None,
) -> Verdict | None:
    """The verdict on one listed workflow, or None when it has no schedule on
    the default branch (so no scheduler is expected to fire it)."""
    path = str(workflow.get("path", ""))
    if not path.startswith(WORKFLOW_DIR):
        # GitHub-managed (`dynamic/...`: Copilot, Pages): no file, no schedule of ours.
        return None
    try:
        text = _file_text(repo, path, branch, gh)
    except ApiError as error:
        if error.not_found:
            # Not on the default branch, and only the default branch's schedules run.
            return None
        return Verdict(repo, path, UNVERIFIABLE, f"cannot verify: {error}")

    try:
        expressions = schedule_of(text)
        if expressions is None:
            return None
        gap = largest_gap([Cron.parse(expression) for expression in expressions], now)
    except CronError as error:
        return Verdict(repo, path, INVALID, str(error))
    bound = bound_for(gap)
    if bound_cap is not None:
        bound = min(bound, bound_cap)
    budget = f"bound {human(bound)}"

    workflow_state = str(workflow.get("state", ""))
    if workflow_state != ACTIVE:
        return Verdict(
            repo, path, DISABLED, f"state {workflow_state or 'missing'}, GitHub will not schedule it, {budget}"
        )

    workflow_id = workflow.get("id")
    try:
        runs, _ = _page(f"/repos/{repo}/actions/workflows/{workflow_id}/runs?event=schedule", "workflow_runs", gh)
        ticks = _ticks(runs, path)
        if ticks.newest is None or now - ticks.newest > bound:
            # The second witness: the unfiltered listing (see the module docstring).
            everything, _ = _page(f"/repos/{repo}/actions/workflows/{workflow_id}/runs", "workflow_runs", gh)
            ticks = _ticks([*runs, *everything], path)
        changed = _last_changed(repo, path, branch, gh) if ticks.newest is None else None
    except ApiError as error:
        return Verdict(repo, path, UNVERIFIABLE, f"cannot verify: {error}, {budget}")

    failures = f" ({ticks.startup_failures} newer scheduled runs failed at startup)" if ticks.startup_failures else ""
    if ticks.newest is not None:
        age = now - ticks.newest
        status = STALE if age > bound else OK
        return Verdict(repo, path, status, f"last scheduled run {human(age)} ago{failures}, {budget}")
    if ticks.startup_failures:
        return Verdict(
            repo,
            path,
            STALE,
            f"every scheduled run read ({ticks.startup_failures}) failed at startup, none ran a job, {budget}",
        )
    assert changed is not None
    since = now - changed
    if since > bound:
        return Verdict(repo, path, NEVER, f"never ran on schedule, file last changed {human(since)} ago, {budget}")
    return Verdict(repo, path, OK, f"no scheduled run yet, file last changed {human(since)} ago, {budget}")


def check_repository(
    repo: str, *, now: datetime, gh: Gh = _gh, bound_cap: timedelta | None = None
) -> tuple[list[Verdict], int]:
    """The verdicts for one repository and how many workflows were listed."""
    try:
        branch = _object(f"/repos/{repo}", gh).get("default_branch")
        if not isinstance(branch, str) or not branch:
            raise ApiError(f"/repos/{repo} names no default branch")
        workflows = _all_pages(f"/repos/{repo}/actions/workflows", "workflows", gh)
    except ApiError as error:
        return [Verdict(repo, LISTING, UNVERIFIABLE, f"cannot verify: {error}")], 0
    verdicts = []
    for workflow in workflows:
        verdict = check_workflow(repo, branch, workflow, now=now, gh=gh, bound_cap=bound_cap)
        if verdict is not None:
            verdicts.append(verdict)
    return verdicts, len(workflows)


def run(repos: Sequence[str], *, now: datetime, gh: Gh = _gh, bound_cap: timedelta | None = None) -> int:
    red: list[Verdict] = []
    scheduled = 0
    for repo in repos:
        verdicts, listed = check_repository(repo, now=now, gh=gh, bound_cap=bound_cap)
        if not verdicts:
            print(f"{repo}: {listed} workflows listed, none scheduled")
        for verdict in verdicts:
            print(verdict.line())
            scheduled += verdict.path != LISTING
        red += [verdict for verdict in verdicts if verdict.red]
    cap = f" (bounds capped at {human(bound_cap)})" if bound_cap is not None else ""
    print(f"\n{scheduled} scheduled workflows in {len(repos)} repositories{cap}: {len(red)} red")
    for verdict in red:
        print(f"::error::{verdict.line()}", file=sys.stderr)
    return 1 if red else 0


def _cap(text: str) -> timedelta | None:
    if not text.strip():
        return None
    try:
        hours = float(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number of hours") from error
    # `not >=` also refuses NaN; a finite ceiling refuses `inf`, which no
    # timedelta can hold.
    if not 0 <= hours <= MAX_CAP_HOURS:
        raise argparse.ArgumentTypeError(f"{text!r} must be between 0 and {MAX_CAP_HOURS} hours")
    return timedelta(hours=hours)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("repos", nargs="+", metavar="OWNER/REPO")
    # Empty means "no cap", so a workflow can pass a dispatch input through
    # unconditionally.
    parser.add_argument("--bound-cap-hours", type=_cap, default=None)
    return parser


def main(argv: Sequence[str] | None = None, *, gh: Gh = _gh, now: datetime | None = None) -> int:
    args = _parser().parse_args(argv)
    for repo in args.repos:
        # Spliced into API paths, so nothing that could walk out of /repos/.
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or {".", ".."} & set(repo.split("/")):
            print(f"scheduler_liveness: {repo!r} is not OWNER/REPO", file=sys.stderr)
            return 2
    return run(args.repos, now=now or datetime.now(UTC), gh=gh, bound_cap=args.bound_cap_hours)


if __name__ == "__main__":
    raise SystemExit(main())
