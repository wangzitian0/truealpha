"""Fail when a deployed environment is serving work that finished days ago.

#560. Every web property this repository guards is guaranteed on ``main`` and
guaranteed nowhere a person is. On 2026-08-14 both environments served
``v0.0.19`` (2026-07-30) while ``main`` was 18 commits ahead, including the
factor corrections, the datahub fixes and the entire app-web remediation. That
lasted 15 days and **nothing was red about it**, because no acceptance criterion
in this repository was ever about how old what a user sees is.

The bound is age, not commit count, and that is a deliberate choice: a count
measures how busy the repository has been, while age measures how long finished
work has been invisible — which is the thing that actually hurts. Ten commits
merged this morning are not a problem; one commit merged last week is.

Usage:
  python tools/deploy_freshness.py <url> [--max-age-days N] [--repo PATH]

Exit codes:
  0 - the deployed release is within the bound (or main has nothing newer)
  1 - stale, unreachable, or the runtime does not report a release identity
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from infra2_sdk.deploy_health import HttpGet, default_http_get

# Daily releases were the norm before the lane stopped (v0.0.16 through v0.0.19
# in three days). Three days is therefore comfortably above normal cadence and
# far below the 15-day gap that went unnoticed: it cannot fire on a weekend of
# ordinary work, and it cannot stay quiet through the failure it exists for.
DEFAULT_MAX_AGE_DAYS = 3
_VERSION_KEYS = ("git_sha", "version")

# The reported release arrives over HTTP and is then handed to git. A value
# beginning with "-" would be read as an option rather than a revision, and
# anything with whitespace or shell-significant characters makes the failure
# non-deterministic. Accept only what a release identifier can actually look
# like — a tag or a sha — and reject the rest with a message that names the
# value (review).
_SAFE_REF = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._/-]{0,199}$")


class FreshnessFailure(RuntimeError):
    """The environment could not be confirmed fresh."""


@dataclass(frozen=True)
class Staleness:
    environment: str
    deployed_ref: str
    unreleased_commits: int
    oldest_unreleased_age: timedelta | None
    oldest_unreleased_subject: str


def reported_release(url: str, http_get: HttpGet) -> str:
    """The release identifier the environment reports, or raise."""
    status_code, body = http_get(url)
    if status_code != 200:
        raise FreshnessFailure(f"{url} answered HTTP {status_code}, so its release is unknown")
    try:
        payload = json.loads(body)
    except (TypeError, ValueError) as exc:
        raise FreshnessFailure(f"{url} did not answer JSON: {body[:120]!r}") from exc
    for key in _VERSION_KEYS:
        value = payload.get(key) if isinstance(payload, dict) else None
        if isinstance(value, str) and value and value != "unknown":
            return value
    raise FreshnessFailure(
        f"{url} does not report a release identity (body {body[:120]!r}); freshness cannot be "
        f"judged until the deployer threads GIT_COMMIT_SHA through"
    )


def _git(args: Sequence[str], repo: str, run: Callable[..., subprocess.CompletedProcess[str]]) -> str:
    result = run(["git", "-C", repo, *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise FreshnessFailure(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def measure(
    environment: str,
    deployed_ref: str,
    *,
    repo: str = ".",
    now: datetime | None = None,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> Staleness:
    """How far behind ``origin/main`` the deployed release is."""
    if not _SAFE_REF.match(deployed_ref):
        raise FreshnessFailure(
            f"{environment} reports {deployed_ref!r}, which is not a usable release identifier — "
            f"a leading '-' would be read by git as an option, and whitespace makes the failure "
            f"non-deterministic. This value never reaches git"
        )
    # Not via `_git`: a failing rev-parse must produce the message an operator
    # can act on ("that ref is not in this checkout" — a shallow clone, or tags
    # not fetched), not git's own stderr.
    resolved = run(
        ["git", "-C", repo, "rev-parse", "--verify", f"{deployed_ref}^{{commit}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if resolved.returncode != 0 or not resolved.stdout.strip():
        raise FreshnessFailure(
            f"{environment} reports {deployed_ref!r}, which is not a commit here — fetch tags, "
            f"or check that the environment reports a ref this repository knows"
        )
    log = _git(["log", "--reverse", "--format=%cI%x1f%s", f"{deployed_ref}..origin/main"], repo, run)
    lines = [line for line in log.splitlines() if line]
    if not lines:
        return Staleness(environment, deployed_ref, 0, None, "")
    oldest_iso, _, subject = lines[0].partition("\x1f")
    oldest = datetime.fromisoformat(oldest_iso)
    reference = now or datetime.now(UTC)
    return Staleness(environment, deployed_ref, len(lines), reference - oldest, subject)


def format_failure(staleness: Staleness, max_age: timedelta) -> str:
    """The message an operator must be able to act on without opening a shell."""
    age = staleness.oldest_unreleased_age
    assert age is not None
    return (
        f"{staleness.environment} is stale: it serves {staleness.deployed_ref} while "
        f"{staleness.unreleased_commits} commit(s) sit unreleased on main, the oldest merged "
        f"{age.days}d{age.seconds // 3600}h ago (limit {max_age.days}d) — "
        f"{staleness.oldest_unreleased_subject[:80]!r}. Finished work is invisible to every user "
        f"of this environment until a release carries it (#560)."
    )


def check_freshness(
    url: str,
    *,
    environment: str = "",
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    repo: str = ".",
    http_get: HttpGet | None = None,
    now: datetime | None = None,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> int:
    http_get = http_get or default_http_get()
    max_age = timedelta(days=max_age_days)
    try:
        deployed_ref = reported_release(url, http_get)
        staleness = measure(environment or url, deployed_ref, repo=repo, now=now, run=run)
    except FreshnessFailure as exc:
        print(f"freshness check failed: {exc}", file=sys.stderr)
        return 1

    if staleness.unreleased_commits == 0:
        print(f"{staleness.environment} is current: serving {deployed_ref}, nothing newer on main")
        return 0
    age = staleness.oldest_unreleased_age
    assert age is not None
    if age > max_age:
        print(f"freshness check failed: {format_failure(staleness, max_age)}", file=sys.stderr)
        return 1
    print(
        f"{staleness.environment} is fresh enough: serving {deployed_ref}, "
        f"{staleness.unreleased_commits} commit(s) unreleased, oldest {age.days}d "
        f"(limit {max_age.days}d)"
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--environment", default="")
    parser.add_argument("--max-age-days", type=int, default=DEFAULT_MAX_AGE_DAYS)
    parser.add_argument("--repo", default=".")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return check_freshness(
        args.url,
        environment=args.environment,
        max_age_days=args.max_age_days,
        repo=args.repo,
    )


if __name__ == "__main__":
    raise SystemExit(main())
