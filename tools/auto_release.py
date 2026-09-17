#!/usr/bin/env python3
"""Decide whether a quiet, green main HEAD is released to staging now.

    python3 tools/auto_release.py --repo wangzitian0/truealpha --trigger-sha <40-hex> \
        [--daily-cap 4] [--github-output "$GITHUB_OUTPUT"]

`auto-release-staging.yml` runs this after its quiet period and, on `release=true`, runs
`tools/cut_release.sh <tag> --auto` — the same ceremony an operator runs, without `--prod`.
The owner approved automatic releases for STAGING ONLY on 2026-09-17 ("2 3 先在 staging
做吧，prod 回头再说"); production stays a deliberate `--prod` run (#819).

Why this exists: on 2026-09-17, green merges sat untagged for up to 16.5 min while nobody
ran the ceremony (#860 measures the rest of the merge-to-staging time). The workflow's quiet
period batches a burst of merges into one release (#855 A3); this tool is the set of reasons
not to release even after that quiet period. The first matching reason wins:

1. main moved on since the green run that started the wait. That commit's own green run
   starts its own wait, and a red one should never be released.
2. main HEAD has no green `ci-required` push run. This repeats the trigger's own condition;
   `cut_release.sh` checks it a third time before it tags.
3. main HEAD already carries a vX.Y.Z tag. Someone released it by hand while we waited.
4. Staging's nightly tick window is open, or the deploy would land inside it. The staging
   TOPT tick starts at 22:45Z, QQQ at 23:20Z and the canary at 23:47Z
   (`apps/data-engine/src/data_engine/lanes/capture.py`). A deploy restarts the data engine,
   so a release that starts shortly before 22:45 would restart it mid-tick.
   `DEPLOY_LEAD` is that margin. `test_auto_release.py` holds the window against those
   crons.
5. This UTC day already has `--daily-cap` automatic releases (default 4). They are counted
   from tags whose annotation carries `AUTO_TRAILER`, which `cut_release.sh --auto` writes.
   Hand-cut tags do not count.
6. A release is already in flight: a `deploy-release.yml` run, a staging surface walk, or a
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
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

#: The annotation line `cut_release.sh --auto` writes; the daily cap counts tags carrying it.
AUTO_TRAILER = "Release-Trigger: auto-staging"
DEFAULT_DAILY_CAP = 4
#: Staging's nightly tick window opens at the TOPT tick (22:45Z) and runs to the end of the
#: UTC day, past QQQ (23:20Z) and the canary (23:47Z).
TICK_WINDOW_START = time(22, 45)
TICK_WINDOW_LABEL = "22:45-00:00Z"
#: From starting the ceremony to the staging data engine restarting: tag CI (~1 min) plus the
#: infra2 dispatch (~6 min, docs/release-protocol.md), with margin. A release that starts
#: less than this long before the window would restart the engine inside it.
DEPLOY_LEAD = timedelta(minutes=15)
TAG_RE = re.compile(r"\Av(\d+)\.(\d+)\.(\d+)\Z")
SHA_RE = re.compile(r"\A[0-9a-f]{40}\Z")
#: Runs that are part of a release still in flight, until they complete.
DEPLOY_WORKFLOW = "deploy-release.yml"
WALK_WORKFLOW = "walk-release.yml"
CI_WORKFLOW = "ci-required.yml"
STAGING_WALK_TITLE_PREFIX = "Walk Deploy staging "
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


def decide(facts: Facts, *, daily_cap: int = DEFAULT_DAILY_CAP, lead: timedelta = DEPLOY_LEAD) -> Decision:
    if daily_cap < 0:
        raise ValueError(f"daily cap must be >= 0, got {daily_cap}")
    short = facts.trigger_sha[:8]
    if facts.main_head != facts.trigger_sha:
        return Decision(
            False,
            f"main moved on to {facts.main_head[:8]} since {short} went green; that commit's own green run decides",
        )
    if not facts.head_green:
        return Decision(False, f"main HEAD {short} has no green ci-required push run")
    released = sorted(tag.name for tag in facts.tags if tag.commit == facts.main_head)
    if released:
        return Decision(False, f"main HEAD {short} is already released as {', '.join(released)}")
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
    return Decision(
        True, f"main HEAD {short} is green, quiet and untagged; releasing {tag} to staging", tag=tag
    )


# --- gathering the facts -----------------------------------------------------------------

Runner = Callable[[Sequence[str]], str]
GitHubGet = Callable[[str], object]


def run_command(arguments: Sequence[str]) -> str:
    return subprocess.run(list(arguments), capture_output=True, text=True, check=True).stdout


def read_tags(run: Runner = run_command) -> tuple[ReleaseTag, ...]:
    """Every local vX.Y.Z tag with its commit, tagger date and annotation.

    The workflow checks out with full history and tags, so the local refs are origin's. A
    lightweight tag has no tagger date and no annotation, so it is never counted as automatic.
    """
    fmt = _FIELD.join(
        ("%(refname:strip=2)", "%(objectname)", "%(*objectname)", "%(taggerdate:unix)", "%(contents)")
    )
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


def _runs(payload: object) -> list[dict[str, object]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("workflow_runs"), list):
        raise ValueError(f"unexpected workflow runs payload: {str(payload)[:200]}")
    return [run for run in payload["workflow_runs"] if isinstance(run, dict)]


def in_flight_releases(repo: str, get: GitHubGet = gh_get) -> tuple[str, ...]:
    """Release runs that have not completed, described for the log."""
    busy: list[str] = []

    def describe(run: dict[str, object]) -> str:
        return f"{run.get('display_title') or run.get('name')} (run {run.get('id')}, {run.get('status')})"

    for run in _runs(get(f"/repos/{repo}/actions/workflows/{DEPLOY_WORKFLOW}/runs?per_page=20")):
        if run.get("status") != "completed":
            busy.append(describe(run))
    for run in _runs(get(f"/repos/{repo}/actions/workflows/{WALK_WORKFLOW}/runs?per_page=20")):
        title = str(run.get("display_title") or "")
        if run.get("status") != "completed" and title.startswith(STAGING_WALK_TITLE_PREFIX):
            busy.append(describe(run))
    for run in _runs(get(f"/repos/{repo}/actions/workflows/{CI_WORKFLOW}/runs?event=push&per_page=30")):
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


def gather(
    repo: str,
    trigger_sha: str,
    *,
    get: GitHubGet = gh_get,
    run: Runner = run_command,
    now: datetime | None = None,
) -> Facts:
    head = main_head(repo, get)
    return Facts(
        trigger_sha=trigger_sha,
        main_head=head,
        head_green=head_is_green(repo, head, get),
        tags=read_tags(run),
        in_flight=in_flight_releases(repo, get),
        now=now or datetime.now(UTC),
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
    return parser


def main(argv: Sequence[str] | None = None, *, get: GitHubGet = gh_get, run: Runner = run_command) -> int:
    args = _parser().parse_args(argv)
    if not SHA_RE.match(args.trigger_sha):
        print(f"::error::--trigger-sha must be a 40-hex commit, got {args.trigger_sha!r}", file=sys.stderr)
        return 2
    try:
        decision = decide(gather(args.repo, args.trigger_sha, get=get, run=run), daily_cap=args.daily_cap)
    except (ValueError, subprocess.CalledProcessError) as exc:
        # Fail closed: a fact that cannot be read is never a reason to release.
        print(f"::error::auto-release could not read its facts: {exc}", file=sys.stderr)
        return 1
    print(("release: " if decision.release else "skip: ") + decision.reason)
    if args.github_output is not None:
        write_outputs(args.github_output, decision)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
