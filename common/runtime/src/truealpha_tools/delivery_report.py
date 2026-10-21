"""The delivery report: one daily dashboard of the delivery path (#1412 R2 and R3).

ADR A8 ("strong dashboards, weak gates"): this report is a dashboard, not a gate. It opens
no issue and blocks no merge. The workflow run is red only when the report cannot run.

The report replaces `deploy-freshness.yml`. The data-plane checks of that workflow run
inside each environment: `mart.served_head` ages each governed head at read time, and the
nightly `report_surface_proof` fails on a stale scheduled head and on zero scheduled heads.
The release age, the walk evidence, the nightly verdict watch and the routing probe have no
in-environment equivalent, so this report shows them.

Sections:

1. Deploy streak (R2). Every attempt of every `deploy-release.yml` run, newest first.
   Staging and production count together. `gh run list` shows only the last attempt of a
   run, so the report reads each earlier attempt, and a failed earlier attempt counts.
2. Release age per environment: the served release against the newest `vX.Y.Z` tag. The
   bounds (staging 3 days, production 14 days, #819) are shown, not enforced. Main commits
   that no release tag holds, with the age of the oldest (#560).
3. Governed heads per environment, as `/api/health` reports them. An empty list or
   "unknown" is not proven (#28 V3).
4. Surface walk evidence of the served release (`walk_evidence.in_flight` and `find_walk`).
5. Nightly check verdicts per environment (`nightly_verdicts`): red, stale or missing.
6. Routing shape of the real proxy (`surface_contract.check`, A4 C1).
7. Tier rates over the last ISO weeks (R3): first-run pass rate of `ci-required` on PR
   heads and on main, flake rate, deploy pass rates, each with 3-sigma p-chart limits (S5),
   flake rate per job, lead times, and PRs per production tag.

Inputs: the GitHub REST API of this repository, the public `/api/health` of each
environment, the `/api/mcp` routing probe, and a checkout with tags (the nightly
expectations at the served release). An input that cannot be read prints as "unknown".
The report never prints a number that it did not measure.

Usage:
  python tools/delivery_report.py [--repo PATH] [--weeks N]

GH_TOKEN (or GITHUB_TOKEN) authenticates the API reads. The report goes to stdout. When
GITHUB_STEP_SUMMARY names a file, the report is also appended to that file.
"""

from __future__ import annotations

import argparse
import http.client
import json
import math
import os
import re
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial

from truealpha_tools import nightly_verdicts, surface_contract, walk_evidence
from truealpha_tools.release_runs import DEPLOY_TITLE, FAILED_CONCLUSIONS, SUMMARY_JOB, parse_time, tag_key

GhApi = Callable[[str], str]
HttpGet = Callable[[str], tuple[int, str]]
Routing = Callable[[str], list[str]]

REPOSITORY = "wangzitian0/truealpha"
API_ROOT = "https://api.github.com"
USER_AGENT = "truealpha-delivery-report (+https://github.com/wangzitian0/truealpha)"
HTTP_TIMEOUT_SECONDS = 30
#: A bound on GitHub API reads per report. The workflow token allows 1,000 reads per hour.
CALL_BUDGET = 600
PAGE_SIZE = 100

DEPLOY_RUNS = f"/repos/{REPOSITORY}/actions/workflows/deploy-release.yml/runs"
CI_RUNS = f"/repos/{REPOSITORY}/actions/workflows/ci-required.yml/runs"
TAGS = f"/repos/{REPOSITORY}/tags"
PULLS = f"/repos/{REPOSITORY}/pulls?state=closed&base=main&sort=updated&direction=desc"
MAIN_COMMITS = f"/repos/{REPOSITORY}/commits?sha=main"
DEPLOY_PAGES = 4
CI_PAGES_PER_WEEK = 10
PULL_PAGES = 10
COMMIT_PAGES = 15
TAG_PAGES = 5
#: Each flaky SHA costs one job listing per failed attempt. Past this bound the job is unknown.
JOB_READS = 80

#: R2 acceptance (#1412): zero failures in 30 consecutive deploys, staging and production
#: together, and at most 1 failure in production's last 10 deploys (S4: below 10% at 95%).
STREAK_TARGET = 30
PRODUCTION_WINDOW = 10
PRODUCTION_MAX_FAILURES = 1
WEEKS = 8
#: PRs merged this long before the first week can reach production inside the window.
LEAD_MARGIN = timedelta(weeks=3)
Z95 = 1.959963984540054

#: deploy-release.yml sets cancel-in-progress to false, so no newer run cancels a deploy.
#: A cancelled deploy did not deliver its release, so it counts as a failure.
DEPLOY_FAILED = frozenset({"failure", "cancelled", "timed_out", "startup_failure"})
#: The conclusion of an earlier attempt that the API did not return.
UNREADABLE = "unknown"
DEPLOY_TYPES = ("staging", "prod")

_SAFE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


class InputUnavailable(RuntimeError):
    """An input of the report could not be read. The report prints it as unknown."""


@dataclass(frozen=True)
class Environment:
    name: str
    #: The word of the deploy-release.yml run-name ("Deploy prod v0.2.3").
    deploy_type: str
    base: str
    #: The release-age bound in days (#819). The report shows it and does not enforce it.
    max_age_days: int

    @property
    def health(self) -> str:
        return f"{self.base}/api/health"


ENVIRONMENTS = (
    Environment("staging", "staging", "https://truealpha-staging.truealpha.club", 3),
    Environment("production", "prod", "https://truealpha.club", 14),
)


# --- inputs ---------------------------------------------------------------------------


class GitHub:
    """GET-only reads of the GitHub REST API, with a cache and a call budget."""

    def __init__(self, token: str = "", *, budget: int = CALL_BUDGET) -> None:
        self.token = token
        self.budget = budget
        self.calls = 0
        self._cache: dict[str, str] = {}

    def __call__(self, path: str) -> str:
        if path in self._cache:
            return self._cache[path]
        if self.calls >= self.budget:
            raise InputUnavailable(f"the budget of {self.budget} GitHub API reads is spent")
        self.calls += 1
        headers = {"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(API_ROOT + path, headers=headers)  # noqa: S310 - a fixed https root
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:  # noqa: S310
                text = response.read().decode("utf-8")
        except (urllib.error.URLError, http.client.HTTPException, OSError) as error:
            raise InputUnavailable(f"GET {path} failed: {error}") from None
        self._cache[path] = text
        return text


def http_get(url: str) -> tuple[int, str]:
    """(status, body) of a GET. A connection failure is status 0 with the reason as the body."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:  # noqa: S310
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")
    except (urllib.error.URLError, http.client.HTTPException, OSError) as error:
        return 0, str(getattr(error, "reason", error))


def _memoized(get: HttpGet) -> HttpGet:
    answers: dict[str, tuple[int, str]] = {}

    def read(url: str) -> tuple[int, str]:
        if url not in answers:
            answers[url] = get(url)
        return answers[url]

    return read


def _json(gh: GhApi, path: str) -> object:
    try:
        return json.loads(gh(path))
    except json.JSONDecodeError as error:
        raise InputUnavailable(f"GET {path} did not answer JSON") from error


def _page_path(path: str, page: int) -> str:
    return f"{path}{'&' if '?' in path else '?'}per_page={PAGE_SIZE}&page={page}"


def _pages(
    gh: GhApi,
    path: str,
    key: str | None,
    *,
    max_pages: int,
    done: Callable[[list[dict]], bool] | None = None,
) -> tuple[list[dict], bool]:
    """The items of a paged list, and whether the read reached its end (or `done` said so)."""
    items: list[dict] = []
    for page in range(1, max_pages + 1):
        payload = _json(gh, _page_path(path, page))
        batch = payload.get(key) if key and isinstance(payload, dict) else payload
        if not isinstance(batch, list):
            raise InputUnavailable(f"GET {path} answered no list")
        items.extend(batch)
        if len(batch) < PAGE_SIZE or (done is not None and done(batch)):
            return items, True
    return items, False


@dataclass(frozen=True)
class Unknown:
    reason: str

    def __str__(self) -> str:
        return f"unknown ({self.reason})"


READ_ERRORS: tuple[type[BaseException], ...] = (
    InputUnavailable,
    nightly_verdicts.VerdictCheckFailure,
    OSError,
    ValueError,
    KeyError,
    TypeError,
)


def _read[T](reader: Callable[[], T]) -> T | Unknown:
    try:
        return reader()
    except READ_ERRORS as error:
        return Unknown(str(error) or type(error).__name__)


# --- R2: deploy attempts --------------------------------------------------------------


@dataclass(frozen=True)
class Attempt:
    run_id: int
    number: int
    deploy_type: str
    release: str
    started_at: datetime
    completed_at: datetime | None
    conclusion: str


@dataclass(frozen=True)
class DeployHistory:
    #: Every counted attempt, newest first.
    attempts: list[Attempt]
    #: True when the read reached the oldest run of the workflow.
    exhaustive: bool
    unreadable: list[str]
    in_flight: int
    runs_read: int


def deploy_outcome(conclusion: str) -> str | None:
    """ "success", "failure", "unknown", or None for an attempt that ran nothing."""
    if conclusion == "success":
        return "success"
    if conclusion in DEPLOY_FAILED:
        return "failure"
    if conclusion == UNREADABLE:
        return UNREADABLE
    return None


def _deploy_attempt(record: dict, run_id: int, number: int, title: re.Match[str]) -> Attempt | None:
    started = parse_time(record.get("run_started_at")) or parse_time(record.get("created_at"))
    if started is None:
        return None
    return Attempt(
        run_id,
        number,
        title["deploy_type"],
        title["release"],
        started,
        parse_time(record.get("updated_at")),
        str(record.get("conclusion") or ""),
    )


def read_deploy_history(gh: GhApi, *, max_pages: int = DEPLOY_PAGES) -> DeployHistory:
    runs, exhaustive = _pages(gh, DEPLOY_RUNS, "workflow_runs", max_pages=max_pages)
    attempts: list[Attempt | None] = []
    unreadable: list[str] = []
    in_flight = 0
    for run in runs:
        title = DEPLOY_TITLE.match(str(run.get("display_title", "")))
        if title is None:
            continue
        run_id, last = int(run["id"]), int(run.get("run_attempt") or 1)
        if run.get("status") == "completed":
            attempts.append(_deploy_attempt(run, run_id, last, title))
        else:
            in_flight += 1
        # The run list shows the last attempt only. Each earlier attempt was a real deploy try.
        for number in range(1, last):
            try:
                earlier = _json(gh, f"/repos/{REPOSITORY}/actions/runs/{run_id}/attempts/{number}")
            except InputUnavailable as error:
                unreadable.append(f"run {run_id} attempt {number}: {error}")
                earlier = {"created_at": run.get("created_at"), "conclusion": UNREADABLE}
            attempts.append(_deploy_attempt(earlier if isinstance(earlier, dict) else {}, run_id, number, title))
    counted = [a for a in attempts if a is not None and deploy_outcome(a.conclusion) is not None]
    counted.sort(key=lambda attempt: attempt.started_at, reverse=True)
    return DeployHistory(counted, exhaustive, unreadable, in_flight, len(runs))


@dataclass(frozen=True)
class Streak:
    count: int
    #: False when the true streak is at least `count`: the read ran out, or an attempt was unreadable.
    exact: bool

    def text(self) -> str:
        return str(self.count) if self.exact else f"at least {self.count}"


def streak(attempts: Iterable[Attempt], *, exhaustive: bool) -> Streak:
    """Consecutive successes from the newest attempt back to the first failure."""
    count = 0
    for attempt in attempts:
        outcome = deploy_outcome(attempt.conclusion)
        if outcome is None:
            continue
        if outcome == "success":
            count += 1
            continue
        return Streak(count, outcome == "failure")
    return Streak(count, exhaustive)


@dataclass(frozen=True)
class Window:
    wanted: int
    size: int
    failures: int
    unknown: int

    @property
    def rate(self) -> float | None:
        return self.failures / self.size if self.size and not self.unknown else None


def window(attempts: Sequence[Attempt], wanted: int) -> Window:
    outcomes = [deploy_outcome(attempt.conclusion) for attempt in attempts]
    chosen = [outcome for outcome in outcomes if outcome is not None][:wanted]
    return Window(wanted, len(chosen), chosen.count("failure"), chosen.count(UNREADABLE))


@dataclass(frozen=True)
class R2:
    pooled: Streak
    staging: Streak
    production: Streak
    pooled_window: Window
    staging_window: Window
    production_window: Window
    #: True met, False not met, None when the inputs cannot decide.
    met: bool | None


def r2_verdict(attempts: Sequence[Attempt], *, exhaustive: bool) -> R2:
    staging = [a for a in attempts if a.deploy_type == "staging"]
    production = [a for a in attempts if a.deploy_type == "prod"]
    pooled = streak(attempts, exhaustive=exhaustive)
    last_ten = window(production, PRODUCTION_WINDOW)
    met: bool | None
    if last_ten.failures > PRODUCTION_MAX_FAILURES or (pooled.exact and pooled.count < STREAK_TARGET):
        met = False
    elif pooled.count >= STREAK_TARGET and last_ten.size == PRODUCTION_WINDOW and not last_ten.unknown:
        met = True
    else:
        met = None
    return R2(
        pooled,
        streak(staging, exhaustive=exhaustive),
        streak(production, exhaustive=exhaustive),
        window(attempts, STREAK_TARGET),
        window(staging, STREAK_TARGET),
        last_ten,
        met,
    )


# --- statistics (S4, S5) --------------------------------------------------------------


def wilson(failures: int, n: int, z: float = Z95) -> tuple[float, float] | None:
    """The Wilson score interval of the proportion failures/n (S4)."""
    if n <= 0:
        return None
    p = failures / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return max(0.0, centre - margin), min(1.0, centre + margin)


@dataclass(frozen=True)
class PChart:
    center: float | None
    #: Per point: (lower, upper) 3-sigma limits, or None for a point without data.
    limits: list[tuple[float, float] | None]


def p_chart(points: Sequence[tuple[int, int] | None]) -> PChart:
    """3-sigma p-chart limits (S5). The center line is the pooled proportion of all points."""
    known = [point for point in points if point is not None and point[1] > 0]
    total = sum(n for _, n in known)
    if not total:
        return PChart(None, [None] * len(points))
    center = sum(x for x, _ in known) / total
    limits: list[tuple[float, float] | None] = []
    for point in points:
        if point is None or point[1] <= 0:
            limits.append(None)
            continue
        sigma = math.sqrt(center * (1 - center) / point[1])
        limits.append((max(0.0, center - 3 * sigma), min(1.0, center + 3 * sigma)))
    return PChart(center, limits)


def percentile(values: Sequence[timedelta], q: float) -> timedelta | None:
    """The nearest-rank percentile."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


# --- release age ----------------------------------------------------------------------


def newest_tag(tags: Iterable[str]) -> str | None:
    versions = [(key, name) for name in tags if (key := tag_key(name)) is not None]
    return max(versions)[1] if versions else None


def read_tags(gh: GhApi) -> dict[str, str]:
    """Tag name -> commit SHA, for every tag the API lists."""
    entries, complete = _pages(gh, TAGS, None, max_pages=TAG_PAGES)
    if not complete:
        raise InputUnavailable(f"more than {TAG_PAGES * PAGE_SIZE} tags; the newest tag is not provable")
    return {str(entry["name"]): str(entry["commit"]["sha"]) for entry in entries}


def read_served(environment: Environment, get: HttpGet) -> str:
    """The release identity that the environment's /api/health reports."""
    status, body = get(environment.health)
    if status != 200:
        raise InputUnavailable(f"{environment.health} answered HTTP {status}")
    payload = json.loads(body)
    value = payload.get("git_sha") if isinstance(payload, dict) else None
    if not isinstance(value, str) or value == "unknown" or not _SAFE_REF.match(value):
        raise InputUnavailable(f"{environment.health} reports no release identity ({value!r})")
    return value


@dataclass(frozen=True)
class ReleaseAge:
    environment: Environment
    served: str
    newest: str
    tags_behind: int | None
    commits_behind: int
    #: None when the served release holds every commit of the newest tag.
    age: timedelta | None

    @property
    def within_bound(self) -> bool:
        return self.age is None or self.age <= timedelta(days=self.environment.max_age_days)


def release_age(environment: Environment, served: str, tags: Iterable[str], gh: GhApi, now: datetime) -> ReleaseAge:
    """The age of the oldest commit that the newest tag holds and the served release lacks."""
    names = list(tags)
    newest = newest_tag(names)
    if newest is None:
        raise InputUnavailable("no vX.Y.Z tag is listed")
    served_key = tag_key(served)
    behind = None if served_key is None else sum(1 for name in names if (key := tag_key(name)) and key > served_key)
    if served == newest:
        return ReleaseAge(environment, served, newest, 0, 0, None)
    comparison = _json(gh, f"/repos/{REPOSITORY}/compare/{served}...{newest}?per_page=1")
    if not isinstance(comparison, dict):
        raise InputUnavailable(f"the comparison {served}...{newest} answered no object")
    ahead = int(comparison["ahead_by"])
    if ahead == 0:
        return ReleaseAge(environment, served, newest, behind, 0, None)
    oldest = parse_time(comparison["commits"][0]["commit"]["committer"]["date"])
    if oldest is None:
        raise InputUnavailable(f"the comparison {served}...{newest} names no commit time")
    return ReleaseAge(environment, served, newest, behind, ahead, now - oldest)


@dataclass(frozen=True)
class UntaggedMain:
    newest: str
    commits: int
    #: The age of the oldest main commit that no release tag holds; None when there is none.
    age: timedelta | None


def untagged_main(tags: Iterable[str], gh: GhApi, now: datetime) -> UntaggedMain:
    """Main commits that no release tag holds (#560): merged work that no environment can serve.

    On a linear main every older tag is an ancestor of the newest tag, so the comparison of the
    newest tag with main counts them. When main does not hold the newest tag, the count is unknown.
    """
    newest = newest_tag(tags)
    if newest is None:
        raise InputUnavailable("no vX.Y.Z tag is listed")
    comparison = _json(gh, f"/repos/{REPOSITORY}/compare/{newest}...main?per_page=1")
    if not isinstance(comparison, dict):
        raise InputUnavailable(f"the comparison {newest}...main answered no object")
    status = comparison.get("status")
    if status not in ("ahead", "identical"):
        raise InputUnavailable(f"main does not hold {newest} (compare status {status!r}); the count is not provable")
    ahead = int(comparison["ahead_by"])
    if ahead == 0:
        return UntaggedMain(newest, 0, None)
    oldest = parse_time(comparison["commits"][0]["commit"]["committer"]["date"])
    if oldest is None:
        raise InputUnavailable(f"the comparison {newest}...main names no commit time")
    return UntaggedMain(newest, ahead, now - oldest)


# --- R3: CI tier rates ----------------------------------------------------------------


@dataclass(frozen=True)
class Week:
    start: datetime

    @property
    def end(self) -> datetime:
        return self.start + timedelta(weeks=1)

    @property
    def label(self) -> str:
        year, number, _ = self.start.isocalendar()
        return f"{year}-W{number:02d}"


def iso_weeks(now: datetime, count: int) -> list[Week]:
    """The last `count` ISO weeks, oldest first. The last week is the current, partial one."""
    monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return [Week(monday - timedelta(weeks=back)) for back in reversed(range(count))]


def week_of(stamp: datetime | None, weeks: Sequence[Week]) -> Week | None:
    if stamp is None:
        return None
    return next((week for week in weeks if week.start <= stamp < week.end), None)


@dataclass(frozen=True)
class CiAttempt:
    #: "pr" for a pull_request run, "main" for a push to main.
    tier: str
    sha: str
    run_id: int
    number: int
    started_at: datetime
    conclusion: str


@dataclass(frozen=True)
class ShaRecord:
    tier: str
    sha: str
    #: Attempts with a verdict (success or failure), oldest first.
    attempts: list[CiAttempt]

    @property
    def first_passed(self) -> bool:
        return self.attempts[0].conclusion == "success"

    @property
    def flaky(self) -> bool:
        """A failed attempt, then a passed attempt, on the same SHA."""
        return bool(self.failed_before_pass())

    def failed_before_pass(self) -> list[CiAttempt]:
        passes = [index for index, attempt in enumerate(self.attempts) if attempt.conclusion == "success"]
        if not passes:
            return []
        return [attempt for attempt in self.attempts[: passes[-1]] if attempt.conclusion in FAILED_CONCLUSIONS]


def sha_records(attempts: Iterable[CiAttempt]) -> list[ShaRecord]:
    grouped: dict[tuple[str, str], list[CiAttempt]] = defaultdict(list)
    for attempt in attempts:
        if attempt.conclusion == "success" or attempt.conclusion in FAILED_CONCLUSIONS:
            grouped[(attempt.tier, attempt.sha)].append(attempt)
    return [
        ShaRecord(tier, sha, sorted(group, key=lambda attempt: attempt.started_at))
        for (tier, sha), group in sorted(grouped.items())
    ]


def _ci_tier(run: dict) -> str | None:
    if run.get("event") == "pull_request":
        return "pr"
    if run.get("event") == "push" and run.get("head_branch") == "main":
        return "main"
    return None


@dataclass(frozen=True)
class CiHistory:
    attempts: list[CiAttempt]
    #: Weeks whose run listing hit the API's result bound; their rates are not shown.
    truncated: list[Week]
    unreadable: list[str]


def _ci_attempts_of(gh: GhApi, run: dict, tier: str, unreadable: list[str]) -> list[CiAttempt]:
    run_id, last, sha = int(run["id"]), int(run.get("run_attempt") or 1), str(run["head_sha"])
    found: list[CiAttempt] = []
    started = parse_time(run.get("run_started_at")) or parse_time(run.get("created_at"))
    if run.get("status") == "completed" and started is not None:
        found.append(CiAttempt(tier, sha, run_id, last, started, str(run.get("conclusion") or "")))
    for number in range(1, last):
        try:
            earlier = _json(gh, f"/repos/{REPOSITORY}/actions/runs/{run_id}/attempts/{number}")
        except InputUnavailable as error:
            unreadable.append(f"{sha[:8]}: run {run_id} attempt {number}: {error}")
            return []
        stamp = parse_time(earlier.get("run_started_at")) if isinstance(earlier, dict) else None
        if stamp is None or not isinstance(earlier, dict):
            unreadable.append(f"{sha[:8]}: run {run_id} attempt {number} names no start time")
            return []
        found.append(CiAttempt(tier, sha, run_id, number, stamp, str(earlier.get("conclusion") or "")))
    return found


def read_ci_history(gh: GhApi, weeks: Sequence[Week]) -> CiHistory:
    attempts: list[CiAttempt] = []
    truncated: list[Week] = []
    unreadable: list[str] = []
    for week in weeks:
        last_day = week.end - timedelta(days=1)
        path = f"{CI_RUNS}?created={week.start:%Y-%m-%d}..{last_day:%Y-%m-%d}"
        runs, complete = _pages(gh, path, "workflow_runs", max_pages=CI_PAGES_PER_WEEK)
        if not complete:
            truncated.append(week)
            continue
        for run in runs:
            tier = _ci_tier(run)
            if tier is not None:
                attempts.extend(_ci_attempts_of(gh, run, tier, unreadable))
    return CiHistory(attempts, truncated, unreadable)


def failed_jobs(gh: GhApi, attempt: CiAttempt) -> set[str]:
    path = f"/repos/{REPOSITORY}/actions/runs/{attempt.run_id}/attempts/{attempt.number}/jobs?per_page=100"
    payload = _json(gh, path)
    jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
    return {
        str(job.get("name"))
        for job in jobs
        if job.get("conclusion") in FAILED_CONCLUSIONS and str(job.get("name")) != SUMMARY_JOB
    }


# --- R3: lead time --------------------------------------------------------------------


@dataclass(frozen=True)
class MergedPr:
    number: int
    merge_sha: str
    merged_at: datetime


@dataclass(frozen=True)
class LeadTime:
    pr: MergedPr
    staging_at: datetime | None
    production_at: datetime | None
    production_release: str | None

    @property
    def to_staging(self) -> timedelta | None:
        return self.staging_at - self.pr.merged_at if self.staging_at else None

    @property
    def to_production(self) -> timedelta | None:
        return self.production_at - self.staging_at if self.production_at and self.staging_at else None


def _first_successes(deploys: Iterable[Attempt]) -> dict[tuple[str, str], datetime]:
    first: dict[tuple[str, str], datetime] = {}
    for attempt in deploys:
        if attempt.conclusion != "success" or attempt.completed_at is None:
            continue
        key = (attempt.deploy_type, attempt.release)
        if key not in first or attempt.completed_at < first[key]:
            first[key] = attempt.completed_at
    return first


def lead_times(
    prs: Iterable[MergedPr], main: Sequence[str], tags: dict[str, str], deploys: Iterable[Attempt]
) -> list[LeadTime]:
    """Per PR: the first successful staging deploy and production deploy of a tag that holds it.

    `main` is the main branch history, newest first. A tag holds a PR when the tag's commit is
    the PR's merge commit or newer on main. This reading needs a linear main (squash merges).
    """
    position = {sha: index for index, sha in enumerate(main)}
    tag_position = {name: position[sha] for name, sha in tags.items() if sha in position}
    first = _first_successes(deploys)
    leads: list[LeadTime] = []
    for pr in prs:
        if pr.merge_sha not in position:
            continue
        holding = [name for name, index in tag_position.items() if index <= position[pr.merge_sha]]
        staged = [first[("staging", name)] for name in holding if ("staging", name) in first]
        produced = [(first[("prod", name)], name) for name in holding if ("prod", name) in first]
        production = min(produced) if produced else None
        leads.append(
            LeadTime(
                pr,
                min(staged) if staged else None,
                production[0] if production else None,
                production[1] if production else None,
            )
        )
    return leads


def read_merged_prs(gh: GhApi, since: datetime) -> list[MergedPr]:
    def older(batch: list[dict]) -> bool:
        stamps = [parse_time(item.get("updated_at")) for item in batch]
        return all(stamp is not None and stamp < since for stamp in stamps)

    pulls, complete = _pages(gh, PULLS, None, max_pages=PULL_PAGES, done=older)
    if not complete:
        raise InputUnavailable(f"more than {PULL_PAGES * PAGE_SIZE} PRs changed since {since:%Y-%m-%d}")
    merged = []
    for pull in pulls:
        stamp = parse_time(pull.get("merged_at"))
        if stamp is not None and stamp >= since and pull.get("merge_commit_sha"):
            merged.append(MergedPr(int(pull["number"]), str(pull["merge_commit_sha"]), stamp))
    return merged


def read_main(gh: GhApi, since: datetime) -> tuple[list[str], bool]:
    """Main commit SHAs since `since`, newest first, and whether main is linear in that span."""
    path = f"{MAIN_COMMITS}&since={since:%Y-%m-%dT%H:%M:%SZ}"
    commits, complete = _pages(gh, path, None, max_pages=COMMIT_PAGES)
    if not complete:
        raise InputUnavailable(f"more than {COMMIT_PAGES * PAGE_SIZE} main commits since {since:%Y-%m-%d}")
    linear = all(len(commit.get("parents") or []) <= 1 for commit in commits)
    return [str(commit["sha"]) for commit in commits], linear


# --- rendering --------------------------------------------------------------------------


def _percent(value: float) -> str:
    return f"{100 * value:.1f}%"


def _interval(bounds: tuple[float, float] | None) -> str:
    return f"{_percent(bounds[0])} to {_percent(bounds[1])}" if bounds else "none (no attempt)"


def _duration(value: timedelta | None) -> str:
    if value is None:
        return "none"
    hours = value.total_seconds() / 3600
    return f"{hours:.1f} h" if hours < 48 else f"{hours / 24:.1f} d"


def _cell(text: object) -> str:
    return str(text).replace("|", "/").replace("\n", " ")


def _span(part: Window) -> str:
    if part.size == part.wanted:
        return f"last {part.size} attempts"
    return f"all {part.size} attempts read (fewer than {part.wanted})"


def _window_row(scope: str, current: Streak, target: str, part: Window) -> str:
    if part.unknown:
        rate, bounds = f"unknown ({part.unknown} unreadable)", "unknown"
    elif part.size == 0:
        rate, bounds = "none (no attempt)", "none"
    else:
        rate, bounds = _percent(part.failures / part.size), _interval(wilson(part.failures, part.size))
    return f"| {scope} | {current.text()} | {target} | {_span(part)} | {part.failures} | {rate} | {bounds} |"


def deploy_section(history: DeployHistory | Unknown) -> list[str]:
    lines = ["## 1. Deploy streak (R2)", ""]
    if isinstance(history, Unknown):
        return [*lines, f"Deploy history: {history}.", ""]
    r2 = r2_verdict(history.attempts, exhaustive=history.exhaustive)
    status = {True: "met", False: "not met", None: "unknown (the inputs cannot decide)"}[r2.met]
    lines += [
        f"Source: `deploy-release.yml`, {history.runs_read} runs and {len(history.attempts)} counted attempts, "
        f"newest first. A failed earlier attempt of a rerun counts. A cancelled attempt counts as a failure. "
        f"Runs in flight now: {history.in_flight}.",
        "",
        "| Scope | Consecutive successes now | Target | Window | Failures | Failure rate | Wilson interval (z 1.96) |",
        "|---|---|---|---|---|---|---|",
        _window_row("staging + production", r2.pooled, str(STREAK_TARGET), r2.pooled_window),
        _window_row("staging", r2.staging, "shown", r2.staging_window),
        _window_row(
            "production",
            r2.production,
            f"at most {PRODUCTION_MAX_FAILURES} failure in the window",
            r2.production_window,
        ),
        "",
        f"R2 acceptance (pooled streak of {STREAK_TARGET}, and at most {PRODUCTION_MAX_FAILURES} failure in "
        f"production's last {PRODUCTION_WINDOW}): **{status}**.",
    ]
    lines += [f"- unreadable: {_cell(item)}" for item in history.unreadable]
    return [*lines, ""]


def release_section(
    served: dict[str, str | Unknown], tags: dict[str, str] | Unknown, gh: GhApi, now: datetime
) -> list[str]:
    lines = [
        "## 2. Release age",
        "",
        "The age of the oldest commit that the newest tag holds and the served release lacks. "
        "The bound is shown, not enforced (ADR A8).",
        "",
        "| Environment | Serves | Newest tag | Tags behind | Commits behind | Age | Bound | Within bound |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for environment in ENVIRONMENTS:
        release = served[environment.name]
        bound = f"{environment.max_age_days} d"
        if isinstance(release, Unknown):
            lines.append(
                f"| {environment.name} | unknown | unknown | unknown | unknown | {_cell(release)} | {bound} | unknown |"
            )
            continue
        if isinstance(tags, Unknown):
            lines.append(
                f"| {environment.name} | {release} | unknown | unknown | unknown | {_cell(tags)} | {bound} | unknown |"
            )
            continue
        age = _read(partial(release_age, environment, release, tags, gh, now))
        if isinstance(age, Unknown):
            lines.append(
                f"| {environment.name} | {release} | unknown | unknown | unknown | {_cell(age)} | {bound} | unknown |"
            )
            continue
        behind = "unknown (not a tag)" if age.tags_behind is None else str(age.tags_behind)
        shown = "none (serves every commit of the newest tag)" if age.age is None else _duration(age.age)
        lines.append(
            f"| {environment.name} | {age.served} | {age.newest} | {behind} | {age.commits_behind} | {shown} | "
            f"{bound} | {'yes' if age.within_bound else 'no'} |"
        )
    return [*lines, "", _untagged_line(tags, gh, now), ""]


def _untagged_line(tags: dict[str, str] | Unknown, gh: GhApi, now: datetime) -> str:
    if isinstance(tags, Unknown):
        return f"Main commits in no release tag: {tags}."
    found = _read(partial(untagged_main, tags, gh, now))
    if isinstance(found, Unknown):
        return f"Main commits in no release tag: {found}."
    if found.age is None:
        return f"Main: every commit is in a release tag ({found.newest})."
    return (
        f"Main: {found.commits} commits in no release tag (newer than {found.newest}); "
        f"the oldest is {_duration(found.age)} old."
    )


def _heads_lines(environment: Environment, get: HttpGet) -> list[str]:
    status, body = get(environment.health)
    if status != 200:
        raise InputUnavailable(f"{environment.health} answered HTTP {status}")
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise InputUnavailable(f"{environment.health} answered no JSON object")
    if "governed_pointers" not in payload:
        return [f"- {environment.name}: not proven: the served release predates governed_pointers"]
    heads = payload["governed_pointers"]
    if heads == "unknown":
        return [f'- {environment.name}: not proven: /api/health reports "unknown"']
    if not isinstance(heads, list):
        raise InputUnavailable(f"{environment.health} reports governed_pointers of the wrong shape")
    if not heads:
        return [f"- {environment.name}: not proven: no governed head (an empty list)"]
    lines = [f"- {environment.name}: {len(heads)} governed heads (labels from mart.served_head)"]
    for head in heads:
        reason = f" ({head['staleness_reason']})" if head.get("staleness_reason") else ""
        lines.append(
            f"  - {head['universe_id']}: {float(head['age_hours']):.1f} h, {head['freshness']}{reason}, "
            f"{head['availability']}"
        )
    return lines


def heads_section(get: HttpGet) -> list[str]:
    lines = [
        "## 3. Governed heads",
        "",
        "Each head the environment serves, as `/api/health` reports it. The age and the label come from "
        '`mart.served_head`; this report computes no age. An empty list or "unknown" proves nothing '
        "(#28 V3). In each environment the nightly `report_surface_proof` fails on a stale scheduled head "
        "and on zero scheduled heads.",
        "",
    ]
    for environment in ENVIRONMENTS:
        found = _read(partial(_heads_lines, environment, get))
        lines += [f"- {environment.name}: {found}"] if isinstance(found, Unknown) else found
    return [*lines, ""]


def _walk_text(environment: Environment, release: str, gh: GhApi, now: datetime) -> str:
    pending = walk_evidence.in_flight(environment.deploy_type, release, gh_api=gh, now=now)
    if pending is not None:
        return f"in flight: {pending}"
    try:
        found = walk_evidence.find_walk(environment.deploy_type, release, environment=environment.name, gh_api=gh)
    except walk_evidence.MissingWalkEvidence as missing:
        return f"missing: {missing}"
    return f"run {found['run_id']}: walk step {found['conclusion']}"


def walk_section(served: dict[str, str | Unknown], gh: GhApi, now: datetime) -> list[str]:
    lines = [
        "## 4. Surface walk evidence",
        "",
        "| Environment | Serves | Walk of the served release |",
        "|---|---|---|",
    ]
    for environment in ENVIRONMENTS:
        release = served[environment.name]
        if isinstance(release, Unknown):
            lines.append(f"| {environment.name} | unknown | {_cell(release)} |")
            continue
        text = _read(partial(_walk_text, environment, release, gh, now))
        lines.append(f"| {environment.name} | {release} | {_cell(text)} |")
    return [*lines, ""]


def _as_verdict_api(gh: GhApi) -> GhApi:
    def read(path: str) -> str:
        try:
            return gh(path)
        except InputUnavailable as error:
            raise nightly_verdicts.VerdictCheckFailure(str(error)) from None

    return read


def _verdict_lines(environment: Environment, get: HttpGet, gh: GhApi, repo: str, now: datetime) -> list[str]:
    report = nightly_verdicts.read_report(environment.health, get)
    if report.verdicts is None:
        return [f"- {environment.name}: the served release predates the nightly verdict report."]
    expectations, source = nightly_verdicts.expectations_for(
        report.release,
        nightly_verdicts.git_release_reader(repo),
        environment=environment.name,
        contract_reader=nightly_verdicts.git_release_reader(repo, path=nightly_verdicts.RELEASE_CONTRACT_PATH),
    )
    if expectations is None:
        return [f"- {environment.name}: the data engine release {source}; nothing to bound."]
    release = report.release

    def first_declared(check: str) -> datetime | None:
        if release is None:
            return None
        return nightly_verdicts.first_declared_at(
            check, release=release, deploy_type=environment.deploy_type, repo=repo, gh_api=_as_verdict_api(gh)
        )

    notes: list[str] = []
    failures = nightly_verdicts.judge(report.verdicts, expectations, now, first_declared=first_declared, notes=notes)
    head = (
        f"- {environment.name}: {len(expectations) - len(failures)} of {len(expectations)} expected checks are "
        f"green and on time (expectations {source})."
    )
    return [
        head,
        *(f"  - red, stale or missing: {_cell(item)}" for item in failures),
        *(f"  - {_cell(n)}" for n in notes),
    ]


def verdict_section(get: HttpGet, gh: GhApi, repo: str, now: datetime) -> list[str]:
    lines = [
        "## 5. Nightly check verdicts",
        "",
        "The in-environment checks record a verdict per run (`mart.nightly_verdicts`). A verdict older than "
        "twice its cadence is stale. An environment cannot report its own stopped daemon, so this watch "
        "runs outside it.",
        "",
    ]
    for environment in ENVIRONMENTS:
        found = _read(partial(_verdict_lines, environment, get, gh, repo, now))
        lines += [f"- {environment.name}: {found}"] if isinstance(found, Unknown) else found
    return [*lines, ""]


def routing_section(
    routing: Routing = surface_contract.check, environments: Sequence[Environment] = ENVIRONMENTS
) -> list[str]:
    """`surface_contract.check` per environment: the routing shape and the MCP credential probe
    (#42). A violated property is a row, not a page (ADR A8)."""
    lines = ["## 6. Routing shape and MCP credential (A4 C1, #42)", ""]
    for environment in environments:
        failures = _read(partial(routing, environment.base))
        if isinstance(failures, Unknown):
            lines.append(f"- {environment.name}: {failures}")
        elif failures:
            lines.append(f"- {environment.name}: {len(failures)} violated:")
            lines += [f"  - {_cell(failure)}" for failure in failures]
        else:
            lines.append(f"- {environment.name}: the deployed surface holds")
    return [*lines, ""]


def _chart_rows(name: str, weeks: Sequence[Week], points: Sequence[tuple[int, int] | None]) -> list[str]:
    if all(point is None for point in points):
        return [f"**{name}**: unknown.", ""]
    chart = p_chart(points)
    center = "none" if chart.center is None else _percent(chart.center)
    lines = [
        f"**{name}** (center line {center})",
        "",
        "| Week | Count | Rate | LCL | UCL | Signal |",
        "|---|---|---|---|---|---|",
    ]
    for week, point, limits in zip(weeks, points, chart.limits, strict=True):
        if point is None:
            lines.append(f"| {week.label} | unknown | unknown | | | |")
            continue
        if limits is None:
            lines.append(f"| {week.label} | 0 | none | | | no data |")
            continue
        rate = point[0] / point[1]
        signal = "above UCL" if rate > limits[1] else "below LCL" if rate < limits[0] else ""
        lines.append(
            f"| {week.label} | {point[0]}/{point[1]} | {_percent(rate)} | {_percent(limits[0])} | "
            f"{_percent(limits[1])} | {signal} |"
        )
    return [*lines, ""]


def _ci_points(
    records: Sequence[ShaRecord], weeks: Sequence[Week], truncated: Sequence[Week]
) -> dict[str, list[tuple[int, int] | None]]:
    passed: dict[tuple[str, Week], int] = defaultdict(int)
    total: dict[tuple[str, Week], int] = defaultdict(int)
    for record in records:
        week = week_of(record.attempts[0].started_at, weeks)
        if week is None:
            continue
        for key, hit in ((record.tier, record.first_passed), ("flake", record.flaky)):
            passed[(key, week)] += int(hit)
            total[(key, week)] += 1
    return {
        key: [None if week in truncated else (passed[(key, week)], total[(key, week)]) for week in weeks]
        for key in ("pr", "main", "flake")
    }


def _deploy_points(
    history: DeployHistory | Unknown, weeks: Sequence[Week], deploy_type: str
) -> list[tuple[int, int] | None]:
    if isinstance(history, Unknown):
        return [None] * len(weeks)
    oldest = min((attempt.started_at for attempt in history.attempts), default=None)
    points: list[tuple[int, int] | None] = []
    for week in weeks:
        if not history.exhaustive and (oldest is None or oldest > week.start):
            points.append(None)
            continue
        outcomes = [
            deploy_outcome(a.conclusion)
            for a in history.attempts
            if a.deploy_type == deploy_type and week.start <= a.started_at < week.end
        ]
        points.append(None if UNREADABLE in outcomes else (outcomes.count("success"), len(outcomes)))
    return points


def _flake_jobs(gh: GhApi, records: Sequence[ShaRecord]) -> tuple[dict[str, int], int]:
    """Failed job name -> flaky SHAs, and the flaky SHAs whose failed jobs are unknown."""
    per_job: dict[str, int] = defaultdict(int)
    unknown = 0
    reads = 0
    for record in records:
        if not record.flaky:
            continue
        names: set[str] = set()
        for attempt in record.failed_before_pass():
            if reads >= JOB_READS:
                names = set()
                break
            reads += 1
            found = _read(partial(failed_jobs, gh, attempt))
            if isinstance(found, Unknown):
                names = set()
                break
            names |= found
        if not names:
            unknown += 1
        for name in names:
            per_job[name] += 1
    return dict(per_job), unknown


def ci_section(gh: GhApi, history: DeployHistory | Unknown, weeks: Sequence[Week]) -> list[str]:
    lines = [f"## 7. Tier rates, last {len(weeks)} ISO weeks (R3)", ""]
    ci = _read(lambda: read_ci_history(gh, weeks))
    if isinstance(ci, Unknown):
        lines.append(f"ci-required history: {ci}.")
        records: list[ShaRecord] = []
        points: dict[str, list[tuple[int, int] | None]] = {key: [None] * len(weeks) for key in ("pr", "main", "flake")}
    else:
        records = sha_records(ci.attempts)
        points = _ci_points(records, weeks, ci.truncated)
        lines.append(
            "A SHA counts once per tier, in the week of its first attempt with a verdict. A cancelled CI run "
            "carries no verdict. The last week is partial."
        )
        lines += [f"- unreadable: {_cell(item)}" for item in ci.unreadable]
        lines += [f"- {week.label}: the run listing hit the API bound; its rates are unknown" for week in ci.truncated]
    lines.append("")
    lines += _chart_rows("T3 PR heads: first-run pass rate of ci-required", weeks, points["pr"])
    lines += _chart_rows("T4 main: first-run pass rate of ci-required", weeks, points["main"])
    lines += _chart_rows("Flake rate: SHAs that failed and then passed", weeks, points["flake"])
    lines += _chart_rows("T5 staging: deploy pass rate per attempt", weeks, _deploy_points(history, weeks, "staging"))
    lines += _chart_rows("T6 production: deploy pass rate per attempt", weeks, _deploy_points(history, weeks, "prod"))
    if records:
        lines += _flake_job_rows(gh, records)
    return lines


def _flake_job_rows(gh: GhApi, records: Sequence[ShaRecord]) -> list[str]:
    per_job, unknown = _flake_jobs(gh, records)
    total = len(records)
    lines = [
        f"**Flake rate per job** (failed, then passed on the same SHA; denominator: {total} SHAs with a verdict)",
        "",
        "| Job | Flaky SHAs | Rate | Wilson interval (z 1.96) |",
        "|---|---|---|---|",
    ]
    for name, count in sorted(per_job.items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"| {_cell(name)} | {count} | {_percent(count / total)} | {_interval(wilson(count, total))} |")
    if not per_job:
        lines.append("| none | 0 | | |")
    if unknown:
        lines.append(f"\n{unknown} flaky SHAs have failed jobs that were not read (the API failed or the read bound).")
    return [*lines, ""]


def _lead_rows(leads: Sequence[LeadTime], weeks: Sequence[Week]) -> list[str]:
    lines = [
        "**Lead time by merge week** (merge to the first staging deploy that holds the PR, then to the first "
        "production deploy)",
        "",
        "| Week | PRs merged | In staging | Merge to staging p50 | p90 | In production | Staging to production p50 | p90 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for week in weeks:
        merged = [lead for lead in leads if week.start <= lead.pr.merged_at < week.end]
        staged = [lead.to_staging for lead in merged if lead.to_staging is not None]
        produced = [lead.to_production for lead in merged if lead.to_production is not None]
        lines.append(
            f"| {week.label} | {len(merged)} | {len(staged)} | {_duration(percentile(staged, 0.5))} | "
            f"{_duration(percentile(staged, 0.9))} | {len(produced)} | {_duration(percentile(produced, 0.5))} | "
            f"{_duration(percentile(produced, 0.9))} |"
        )
    return [*lines, ""]


def _per_tag_rows(leads: Sequence[LeadTime], weeks: Sequence[Week]) -> list[str]:
    lines = [
        "**PRs per production tag by deploy week** (each PR counts once, at its first production deploy)",
        "",
        "| Week | Production tags | PRs they carried | PRs per tag |",
        "|---|---|---|---|",
    ]
    for week in weeks:
        reached = [lead for lead in leads if lead.production_at and week.start <= lead.production_at < week.end]
        tags = {lead.production_release for lead in reached}
        ratio = f"{len(reached) / len(tags):.1f}" if tags else "none"
        lines.append(f"| {week.label} | {len(tags)} | {len(reached)} | {ratio} |")
    return [*lines, ""]


def lead_section(
    gh: GhApi, history: DeployHistory | Unknown, tags: dict[str, str] | Unknown, weeks: Sequence[Week]
) -> list[str]:
    since = weeks[0].start - LEAD_MARGIN
    if isinstance(history, Unknown) or isinstance(tags, Unknown):
        reason = history if isinstance(history, Unknown) else tags
        return [f"Lead times and PRs per production tag: {reason}.", ""]
    found = _read(lambda: (read_merged_prs(gh, since), read_main(gh, since)))
    if isinstance(found, Unknown):
        return [f"Lead times and PRs per production tag: {found}.", ""]
    prs, (main, linear) = found
    leads = lead_times(prs, main, tags, history.attempts)
    lines = [
        f"PRs merged since {since:%Y-%m-%d}: {len(prs)}; {len(leads)} have a merge commit on the main listing. "
        "A PR that has not reached an environment has no lead time there.",
    ]
    if not linear:
        lines.append("Main is not linear in this span: a tag can hold a PR that this reading misses.")
    if not history.exhaustive:
        lines.append("The deploy listing is partial: an early deploy can be missing.")
    return [*lines, "", *_lead_rows(leads, weeks), *_per_tag_rows(leads, weeks)]


def build_report(
    gh: GhApi,
    get: HttpGet,
    *,
    now: datetime,
    repo: str = ".",
    weeks: int = WEEKS,
    routing: Routing = surface_contract.check,
) -> str:
    get = _memoized(get)
    history = _read(lambda: read_deploy_history(gh))
    tags = _read(lambda: read_tags(gh))
    served = {env.name: _read(partial(read_served, env, get)) for env in ENVIRONMENTS}
    span = iso_weeks(now, weeks)
    lines = [
        "# Delivery report",
        "",
        f"Generated {now:%Y-%m-%d %H:%M} UTC. A dashboard, not a gate (ADR A8). An input that cannot be read "
        "prints as unknown.",
        "",
        *deploy_section(history),
        *release_section(served, tags, gh, now),
        *heads_section(get),
        *walk_section(served, gh, now),
        *verdict_section(get, gh, repo, now),
        *routing_section(routing),
        *ci_section(gh, history, span),
        *lead_section(gh, history, tags, span),
    ]
    calls = getattr(gh, "calls", None)
    if calls is not None:
        lines.append(f"GitHub API reads: {calls} (budget {getattr(gh, 'budget', 'none')}).")
    return "\n".join(lines) + "\n"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default=".", help="checkout that holds the release tags")
    parser.add_argument("--weeks", type=int, default=WEEKS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    report = build_report(GitHub(token), http_get, now=datetime.now(UTC), repo=arguments.repo, weeks=arguments.weeks)
    print(report)
    summary = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
