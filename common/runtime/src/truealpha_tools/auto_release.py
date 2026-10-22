#!/usr/bin/env python3
"""Decide whether a quiet, green main HEAD is released to staging now.

    python3 tools/auto_release.py --repo wangzitian0/truealpha --trigger-sha <40-hex> \
        [--daily-cap N] [--github-output "$GITHUB_OUTPUT"] [--report-staging-red]

`auto-release-staging.yml` runs this after its quiet period and, on `release=true`, runs
`tools/cut_release.sh <tag> --auto` — the same ceremony an operator runs, without `--prod`.
The owner approved automatic releases for STAGING ONLY on 2026-09-17 ("2 3 先在 staging
做吧，prod 回头再说"); production stays a deliberate `--prod` run (#819).

Why this exists: on 2026-09-17, green merges sat untagged for up to 16.5 min while nobody
ran the ceremony (#860 measures the rest of the merge-to-staging time). The workflow's quiet
period batches a burst of merges into one release (#855 A3); this tool is the set of reasons
not to release even after that quiet period. The first matching reason wins:

1. The trigger SHA is not an ancestor of `origin/main` (non-monotonic candidate), or the
   latest release tag is not an ancestor of `origin/main` (rollback protection), or `main`
   moved on since the trigger SHA went green without a completed green `ci-required` push
   run on `origin/main` HEAD. When `trigger_sha` is an ancestor of `origin/main` and
   `origin/main` HEAD has already completed its own green `ci-required` push run, the run
   releases `origin/main` HEAD instead of stranding it when out-of-order CI cancelled the
   newer workflow run (#1299).
2. main HEAD has no green `ci-required` push run. This repeats the trigger's own condition;
   `cut_release.sh` checks it a third time before it tags.
3. main HEAD already carries a vX.Y.Z tag. Someone released it by hand while we waited.
4. Staging is red (#1468 F1). The newest staging release has no passing walk. Two releases in
   a row failed at the same stage and step since the newest passing walk of any tag
   (`staging_line.py` holds the rule). The train cuts no tag until a walk passes. With
   `--report-staging-red`, this tool opens or updates one issue, `[truealpha] Staging red since
   <tag>: <step>`, and closes it when the train resumes (`staging_issue.py`). The stop is no
   failure: this tool exits 0, so a stopped train is not a red run.
5. Staging's nightly tick window is open, or the deploy would land inside it. The staging
   TOPT tick starts at 22:45Z, QQQ at 23:20Z and the canary at 23:47Z
   (`apps/data-engine/src/data_engine/lanes/capture.py`). A deploy restarts the data engine,
   so a release that starts shortly before 22:45 would restart it mid-tick.
   `DEPLOY_LEAD` is that margin. `test_auto_release.py` holds the window against those
   crons.
6. This UTC day already has `--daily-cap` automatic releases (default `DEFAULT_DAILY_CAP`). They are counted
   from tags whose annotation carries `AUTO_TRAILER`, which `cut_release.sh --auto` writes.
   Hand-cut tags do not count.
7. A release is already in flight: a `deploy-release.yml` run, a staging surface walk, or a
   tag's `ci-required` run that has not completed. Their next step belongs to someone else.
   Two auto-release runs never overlap, because the workflow's release job is serialised by
   its own concurrency group.

Otherwise the answer is `release=true` with `tag=` the next patch after the highest vX.Y.Z
tag on origin. `cut_release.sh` still re-checks that number just before its push and rolls
forward if another release claimed it first (#860).

Stdlib only: the job that runs this installs nothing but the checkout.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

from truealpha_tools import staging_issue, staging_line
from truealpha_tools.release_history import read_releases
from truealpha_tools.release_runs import (
    CI_WORKFLOW,
    DEPLOY_WORKFLOW,
    TAG_RE,
    WALK_TITLE,
    WALK_WORKFLOW,
    GitHubGet,
    workflow_runs,
)
from truealpha_tools.staging_line import StagingRelease

#: The annotation line `cut_release.sh --auto` writes; the daily cap counts tags carrying it.
AUTO_TRAILER = "Release-Trigger: auto-staging"
#: The most automatic staging releases in one UTC day. This line is the only statement of the number and
#: of its reason. Measured on 2026-10-21 (git tags, v0.2.31 to v0.2.41): 11 staging releases in 22 hours.
#: No Dagster run was interrupted by a deploy in that window (Director measurement of `dagster.runs`, #1485).
#: A change of the number needs a new measurement, not a new sentence in another file.
DEFAULT_DAILY_CAP = 10
#: Staging's nightly tick window opens at the TOPT tick (22:45Z) and runs to the end of the
#: UTC day, past QQQ (23:20Z) and the canary (23:47Z).
TICK_WINDOW_START = time(22, 45)
TICK_WINDOW_LABEL = "22:45-00:00Z"
#: From starting the ceremony to the staging data engine restarting: tag CI (~1 min) plus the
#: infra2 dispatch (~6 min, docs/release-protocol.md), with margin. A release that starts
#: less than this long before the window would restart the engine inside it.
DEPLOY_LEAD = timedelta(minutes=15)
SHA_RE = re.compile(r"\A[0-9a-f]{40}\Z")
_FIELD = "\x1f"
_RECORD = "\x1e"


@dataclass(frozen=True)
class ReleaseTag:
    name: str
    commit: str
    tagged_at: datetime | None
    message: str

    @property
    def automatic(self) -> bool:
        return AUTO_TRAILER in (line.strip() for line in self.message.splitlines())

    @property
    def version(self) -> tuple[int, int, int]:
        match = TAG_RE.match(self.name)
        if match is None:
            raise ValueError(f"{self.name!r} is not a vX.Y.Z tag")
        major, minor, patch = (int(part) for part in match.groups())
        return major, minor, patch


@dataclass(frozen=True)
class Facts:
    trigger_sha: str
    main_head: str
    head_green: bool
    tags: tuple[ReleaseTag, ...]
    in_flight: tuple[str, ...]
    now: datetime
    trigger_is_ancestor_of_head: bool = True
    head_ci_completed: bool = False
    latest_tag_is_ancestor_of_head: bool = True
    #: The recent staging releases with the outcome of each stage (`staging_line.py`).
    releases: tuple[StagingRelease, ...] = ()


@dataclass(frozen=True)
class Decision:
    release: bool
    reason: str
    tag: str = ""


def next_tag(tags: Iterable[ReleaseTag]) -> str:
    """The next patch after the highest vX.Y.Z tag, compared numerically (v0.0.9 < v0.0.10)."""
    versions = [tag.version for tag in tags]
    if not versions:
        raise ValueError("no vX.Y.Z tag exists to count from; cut the first release by hand")
    major, minor, patch = max(versions)
    return f"v{major}.{minor}.{patch + 1}"


def highest_tag(tags: Iterable[ReleaseTag]) -> ReleaseTag | None:
    """The highest vX.Y.Z tag by version tuple, or None when no release tag exists."""
    return max(tags, key=lambda tag: tag.version, default=None)


def in_tick_window(now: datetime, *, lead: timedelta = DEPLOY_LEAD) -> bool:
    """Whether a release started at `now` could restart staging inside the tick window.

    The window runs from TICK_WINDOW_START to the end of the UTC day. The lead moves its
    start earlier, never its end: a release that starts at 00:00 lands after the ticks.
    """
    now = now.astimezone(UTC)
    return now >= datetime.combine(now.date(), TICK_WINDOW_START, tzinfo=UTC) - lead


def automatic_releases_on(day: datetime, tags: Iterable[ReleaseTag]) -> list[ReleaseTag]:
    utc_day = day.astimezone(UTC).date()
    return [
        tag
        for tag in tags
        if tag.automatic and tag.tagged_at is not None and tag.tagged_at.astimezone(UTC).date() == utc_day
    ]


def early_decision(facts: Facts) -> Decision | None:
    """The first three reasons, and the refusal to roll back. They need no release history.

    A run that ends here is moot or refused before the staging line matters, so `main` reads the
    history only when this returns None.
    """
    short = facts.trigger_sha[:8]
    head_short = facts.main_head[:8]
    if not facts.trigger_is_ancestor_of_head:
        return Decision(
            False,
            f"trigger {short} is not an ancestor of origin/main {head_short}; refusing non-monotonic candidate",
        )
    if facts.main_head != facts.trigger_sha and not (facts.head_green and facts.head_ci_completed):
        return Decision(
            False,
            f"main moved on to {head_short} since {short} went green; that commit's own green run decides",
        )
    if not facts.head_green:
        return Decision(False, f"main HEAD {head_short} has no green ci-required push run")
    if not facts.latest_tag_is_ancestor_of_head:
        latest = highest_tag(facts.tags)
        latest_name = latest.name if latest is not None else "latest tag"
        return Decision(
            False,
            f"latest release {latest_name} is not an ancestor of main HEAD {head_short}; refusing to roll back",
        )
    released = sorted(tag.name for tag in facts.tags if tag.commit == facts.main_head)
    if released:
        return Decision(False, f"main HEAD {head_short} is already released as {', '.join(released)}")
    return None


def decide(facts: Facts, *, daily_cap: int = DEFAULT_DAILY_CAP, lead: timedelta = DEPLOY_LEAD) -> Decision:
    if daily_cap < 0:
        raise ValueError(f"daily cap must be >= 0, got {daily_cap}")
    early = early_decision(facts)
    if early is not None:
        return early
    head_short = facts.main_head[:8]
    red = staging_line.staging_red(facts.releases)
    if red is not None:
        return Decision(
            False,
            f"staging is red since {red.since} at {red.label}; no walk has passed since; "
            f"no new tag until a walk of any tag passes",
        )
    if in_tick_window(facts.now, lead=lead):
        return Decision(
            False,
            f"{facts.now.astimezone(UTC):%H:%M}Z is inside staging's tick window "
            f"({TICK_WINDOW_LABEL}, opened {int(lead.total_seconds() // 60)} min early for the deploy lead); "
            f"the next green main after it releases",
        )
    today = automatic_releases_on(facts.now, facts.tags)
    if len(today) >= daily_cap:
        names = ", ".join(sorted(tag.name for tag in today))
        return Decision(
            False,
            f"daily cap reached: {len(today)} automatic release(s) today (UTC) >= {daily_cap} ({names}); "
            f"cut by hand if this one cannot wait",
        )
    if facts.in_flight:
        return Decision(False, f"a release is already in flight: {'; '.join(facts.in_flight)}")
    tag = next_tag(facts.tags)
    return Decision(True, f"main HEAD {head_short} is green, quiet and untagged; releasing {tag} to staging", tag=tag)


# --- gathering the facts -----------------------------------------------------------------

Runner = Callable[[Sequence[str]], str]


def run_command(arguments: Sequence[str]) -> str:
    return subprocess.run(list(arguments), capture_output=True, text=True, check=True).stdout


def is_ancestor(ancestor: str, descendant: str, run: Runner = run_command) -> bool:
    """True when `ancestor` is a git ancestor of `descendant` in the local checkout (#1284)."""
    if ancestor == descendant:
        return True
    try:
        run(["git", "merge-base", "--is-ancestor", ancestor, descendant])
    except subprocess.CalledProcessError as exc:
        if exc.returncode == 1:
            return False
        raise
    return True


def read_tags(run: Runner = run_command) -> tuple[ReleaseTag, ...]:
    """Every local vX.Y.Z tag with its commit, tagger date and annotation.

    The workflow checks out with full history and tags, so the local refs are origin's. A
    lightweight tag has no tagger date and no annotation, so it is never counted as automatic.
    """
    fmt = _FIELD.join(("%(refname:strip=2)", "%(objectname)", "%(*objectname)", "%(taggerdate:unix)", "%(contents)"))
    fmt = fmt.replace(_FIELD, "%1f") + "%1e"
    out = run(["git", "for-each-ref", f"--format={fmt}", "refs/tags"])
    tags: list[ReleaseTag] = []
    for record in out.split(_RECORD):
        record = record.lstrip("\n")
        if not record:
            continue
        name, objectname, peeled, stamp, message = record.split(_FIELD, 4)
        if not TAG_RE.match(name):
            continue
        tagged_at = datetime.fromtimestamp(int(stamp), UTC) if stamp.strip() else None
        tags.append(ReleaseTag(name=name, commit=peeled or objectname, tagged_at=tagged_at, message=message))
    return tuple(tags)


def gh_get(path: str) -> object:
    return json.loads(run_command(["gh", "api", "-X", "GET", path]))


def in_flight_releases(repo: str, get: GitHubGet = gh_get) -> tuple[str, ...]:
    """Release runs that have not completed, described for the log."""
    busy: list[str] = []

    def describe(run: dict[str, object]) -> str:
        return f"{run.get('display_title') or run.get('name')} (run {run.get('id')}, {run.get('status')})"

    for run in workflow_runs(get(f"/repos/{repo}/actions/workflows/{DEPLOY_WORKFLOW}/runs?per_page=20")):
        if run.get("status") != "completed":
            busy.append(describe(run))
    for run in workflow_runs(get(f"/repos/{repo}/actions/workflows/{WALK_WORKFLOW}/runs?per_page=20")):
        walk = WALK_TITLE.match(str(run.get("display_title") or ""))
        if run.get("status") != "completed" and walk is not None and walk["deploy_type"] == "staging":
            busy.append(describe(run))
    for run in workflow_runs(get(f"/repos/{repo}/actions/workflows/{CI_WORKFLOW}/runs?event=push&per_page=30")):
        if run.get("status") != "completed" and TAG_RE.match(str(run.get("head_branch") or "")):
            busy.append(describe(run))
    return tuple(busy)


def main_head(repo: str, get: GitHubGet = gh_get) -> str:
    payload = get(f"/repos/{repo}/commits/main")
    sha = payload.get("sha") if isinstance(payload, dict) else None
    if not isinstance(sha, str) or not SHA_RE.match(sha):
        raise ValueError(f"could not read main HEAD for {repo}")
    return sha


def head_is_green(repo: str, sha: str, get: GitHubGet = gh_get) -> bool:
    payload = get(
        f"/repos/{repo}/actions/workflows/{CI_WORKFLOW}/runs"
        f"?head_sha={sha}&branch=main&event=push&status=success&per_page=1"
    )
    count = payload.get("total_count") if isinstance(payload, dict) else None
    return isinstance(count, int) and count >= 1


def head_ci_completed(repo: str, sha: str, get: GitHubGet = gh_get) -> bool:
    """True when every ci-required push run for `sha` on main has completed (#1299, #1284)."""
    payload = get(
        f"/repos/{repo}/actions/workflows/{CI_WORKFLOW}/runs?head_sha={sha}&branch=main&event=push&per_page=20"
    )
    if not isinstance(payload, dict):
        return False
    runs = payload.get("workflow_runs")
    if not isinstance(runs, list):
        count = payload.get("total_count")
        return isinstance(count, int) and count >= 1
    if not runs:
        return False
    return all(
        isinstance(r, dict) and r.get("status") == "completed" and r.get("conclusion", "success") == "success"
        for r in runs
    )


def read_history(repo: str, get: GitHubGet, tags: Sequence[ReleaseTag]) -> tuple[StagingRelease, ...]:
    """The staging releases since the newest passing walk (`release_history.py`)."""
    return read_releases(repo, get, next_tag=next_tag(tags) if tags else None)


def gather(
    repo: str,
    trigger_sha: str,
    *,
    get: GitHubGet = gh_get,
    run: Runner = run_command,
    now: datetime | None = None,
    history: bool = True,
) -> Facts:
    """Read the facts. With `history` false, the release history is left out (`read_history`)."""
    head = main_head(repo, get)
    tags = read_tags(run)
    latest = highest_tag(tags)
    trigger_anc = is_ancestor(trigger_sha, head, run)
    latest_anc = is_ancestor(latest.commit, head, run) if latest is not None else True
    green = head_is_green(repo, head, get)
    ci_done = head_ci_completed(repo, head, get) if (head != trigger_sha and green) else green
    return Facts(
        trigger_sha=trigger_sha,
        main_head=head,
        head_green=green,
        tags=tags,
        in_flight=in_flight_releases(repo, get),
        now=now or datetime.now(UTC),
        trigger_is_ancestor_of_head=trigger_anc,
        head_ci_completed=ci_done,
        latest_tag_is_ancestor_of_head=latest_anc,
        releases=read_history(repo, get, tags) if history else (),
    )


def write_outputs(path: Path, decision: Decision) -> None:
    reason = decision.reason.replace("\n", " ")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"release={'true' if decision.release else 'false'}\n")
        handle.write(f"tag={decision.tag}\n")
        handle.write(f"reason={reason}\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--trigger-sha", required=True, help="the main commit whose green run started the wait")
    parser.add_argument("--daily-cap", type=int, default=DEFAULT_DAILY_CAP)
    parser.add_argument("--github-output", type=Path, help="append release/tag/reason for the next step")
    parser.add_argument(
        "--report-staging-red",
        action="store_true",
        help="open or update the staging-red issue when staging is red, and close it when a walk has passed",
    )
    return parser


def _why(exc: Exception) -> str:
    """The error text, with the standard error of a failed command."""
    stderr = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) and exc.stderr else ""
    return f"{exc}: {stderr[-300:]}" if stderr else str(exc)


def main(
    argv: Sequence[str] | None = None,
    *,
    get: GitHubGet = gh_get,
    run: Runner = run_command,
    gh: Runner = run_command,
) -> int:
    args = _parser().parse_args(argv)
    if not SHA_RE.match(args.trigger_sha):
        print(f"::error::--trigger-sha must be a 40-hex commit, got {args.trigger_sha!r}", file=sys.stderr)
        return 2
    try:
        facts = gather(args.repo, args.trigger_sha, get=get, run=run, history=False)
        early = early_decision(facts)
        if early is None:
            # The release history costs about a dozen API calls. A moot run does not need it.
            facts = replace(facts, releases=read_history(args.repo, get, facts.tags))
        decision = early or decide(facts, daily_cap=args.daily_cap)
    except (ValueError, subprocess.CalledProcessError) as exc:
        # Fail closed: a fact that cannot be read is never a reason to release.
        print(f"::error::auto-release could not read its facts: {_why(exc)}", file=sys.stderr)
        return 1
    if early is not None:
        print(f"skip: {early.reason}")
        if args.github_output is not None:
            write_outputs(args.github_output, early)
        return 0
    red = staging_line.staging_red(facts.releases)
    line = staging_line.describe(facts.releases, red)
    print(("release: " if decision.release else "skip: ") + decision.reason)
    print("staging line: " + line)
    if args.github_output is not None:
        write_outputs(args.github_output, decision)
    if args.report_staging_red:
        try:
            print("staging issue: " + staging_issue.reconcile(args.repo, red, line, facts.main_head, gh))
        except (ValueError, subprocess.CalledProcessError) as exc:
            if red is None:
                # Closing an issue is housekeeping. It never blocks a release that is decided.
                print(f"::warning::could not close the staging-red issue: {_why(exc)}", file=sys.stderr)
                return 0
            # The train is held either way. A red state without an owner is the defect, so say it loudly.
            print(f"::error::could not update the staging-red issue: {_why(exc)}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
