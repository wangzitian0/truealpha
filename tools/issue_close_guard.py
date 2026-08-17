"""Reopen an issue a merge closed while its capability is not yet released.

#562. `AGENTS.md` rule 7 tells an author to reference an issue with a plain
`#N` and close it by hand when a criterion needs a user journey, a
role-dependent view, or a deployed-environment state. Authors did that. GitHub
closed the issues anyway — #371 on 2026-07-18 and again on 2026-08-14, #494 on
2026-08-14, each within seconds of a merge whose body said "Not `Closes`".

Rule 7's own second half applies to itself: a convention is a run that happened,
not a check that runs again.

What this deliberately does NOT do: touch an issue a human closed. A hand
close is exactly what rule 7 asks for, and the person doing it has seen more
than this script can. It fires only on the auto-close case — an issue closed by
a commit that PRODUCTION IS NOT SERVING — which is the defect and nothing else.

"Serving", not "a release tag contains": v0.0.20 contains the commits that
closed #371, #494 and #495 while production served v0.0.19, so a tag-existence
test called all three released when nothing a user touches had them. Rule 6 is
explicit that a tag is not the bar.

Usage:
  python tools/issue_close_guard.py --issue 494

Exit codes:
  0 - nothing to do, or the issue was reopened successfully
  1 - the guard could not reach a verdict
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from infra2_sdk.deploy_health import HttpGet, default_http_get

REPO = "wangzitian0/truealpha"
# Rule 6 asks whether a user has it, so the question is what production SERVES,
# not what a tag contains.
PRODUCTION_HEALTH = "https://truealpha.club/api/health"
# Same lesson as tools/deploy_freshness.py, which had to learn it first: the
# release identifier arrives over HTTP and is then handed to git, where a
# leading "-" is read as an option. Duplicated rather than imported because
# these are standalone scripts, not a package.
_SAFE_REF = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._/-]{0,199}$")
GhApi = Callable[[str], str]
Git = Callable[[Sequence[str]], tuple[int, str]]


class GuardError(RuntimeError):
    """The guard could not reach a verdict."""


@dataclass(frozen=True)
class Verdict:
    reopen: bool
    reason: str


def _gh_api(path: str) -> str:
    result = subprocess.run(["gh", "api", path], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise GuardError(f"gh api {path} failed: {result.stderr.strip()}")
    return result.stdout


def _git(args: Sequence[str]) -> tuple[int, str]:
    result = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    return result.returncode, result.stdout.strip()


def closing_commit(issue: int, *, gh_api: GhApi = _gh_api) -> str | None:
    """The commit that closed the issue, or None when a human closed it.

    The `closed` event does NOT reliably carry `commit_id`. Verified against the
    real #494 close, which is the event this guard exists for:

        2026-08-14T10:45:28Z closed      commit=null
        2026-08-14T10:45:28Z referenced  commit=3057e970…   <- PR #565's merge

    A first version keyed on `commit_id` alone and would therefore have judged
    every auto-close a hand close and shipped inert against its own defect. The
    merge is identified by the commit-bearing event sharing the close's
    timestamp.
    """
    # Paginated: a long-running issue accumulates hundreds of timeline events
    # (#494 has been closed, reopened and closed again across three weeks), and
    # reading only the first page would judge a stale close (review).
    events: list[dict] = []
    page = 1
    while True:
        chunk = json.loads(gh_api(f"/repos/{REPO}/issues/{issue}/timeline?per_page=100&page={page}"))
        events.extend(chunk)
        if len(chunk) < 100:
            break
        page += 1
    closes = [event for event in events if event.get("event") == "closed"]
    if not closes:
        raise GuardError(f"issue #{issue} has no close event to judge")
    last = closes[-1]
    if last.get("commit_id"):
        return str(last["commit_id"])
    closed_at = last.get("created_at")
    for event in events:
        if event.get("created_at") == closed_at and event.get("commit_id"):
            return str(event["commit_id"])
    return None


def deployed(
    commit: str, *, health_url: str = PRODUCTION_HEALTH, http_get: HttpGet | None = None, git: Git = _git
) -> tuple[bool, str]:
    """Is the commit in what PRODUCTION is actually serving?

    Not "does a release tag contain it". Verified against reality while writing
    this: v0.0.20 contains the commits that closed #371, #494 and #495, and
    production serves v0.0.19 — so a tag-existence test would have called all
    three released while nothing a user touches had them. Tag existence is the
    thing rule 6 explicitly is not satisfied by.
    """
    http_get = http_get or default_http_get()
    status_code, body = http_get(health_url)
    if status_code != 200:
        raise GuardError(f"{health_url} answered HTTP {status_code}; what is deployed is unknown")
    try:
        payload = json.loads(body)
    except (TypeError, ValueError) as exc:
        raise GuardError(f"{health_url} did not answer JSON: {body[:120]!r}") from exc
    # A bare list or string is valid JSON; `.get` on it raises AttributeError
    # and the guard would fail as a crash rather than as a verdict (review).
    serving = payload.get("git_sha") if isinstance(payload, dict) else None
    if not isinstance(serving, str) or not serving or serving == "unknown":
        raise GuardError(f"{health_url} does not report a release identity")
    if not _SAFE_REF.match(serving):
        raise GuardError(
            f"{health_url} reports {serving!r}, which is not a usable release identifier — this value never reaches git"
        )
    code, out = git(["merge-base", "--is-ancestor", commit, f"{serving}^{{commit}}"])
    if code not in (0, 1):
        raise GuardError(f"cannot tell whether {serving} contains {commit[:8]}; fetch tags and full history")
    return code == 0, serving


def judge(
    issue: int,
    *,
    gh_api: GhApi = _gh_api,
    git: Git = _git,
    health_url: str = PRODUCTION_HEALTH,
    http_get: HttpGet | None = None,
) -> Verdict:
    commit = closing_commit(issue, gh_api=gh_api)
    if commit is None:
        return Verdict(
            False,
            f"#{issue} was closed by a person, not by a merge — rule 7 asks for exactly that, "
            f"and they have seen more than this guard can",
        )
    is_deployed, serving = deployed(commit, health_url=health_url, http_get=http_get, git=git)
    if is_deployed:
        return Verdict(False, f"#{issue} was closed by {commit[:8]}, which production is serving ({serving})")
    return Verdict(
        True,
        f"#{issue} was closed by merge commit {commit[:8]}, which production is NOT serving — it "
        f"serves {serving}. The capability is on main and nowhere a user is, which rule 6 does not "
        f"call closed. Rule 7 asks for a plain `#N` reference and a hand close once the deployed "
        f"evidence exists; a merge closed this regardless, which is #562. Reopened rather than "
        f"left looking done.",
    )


def run(
    issue: int,
    *,
    gh_api: GhApi = _gh_api,
    git: Git = _git,
    health_url: str = PRODUCTION_HEALTH,
    http_get: HttpGet | None = None,
    reopen: Callable[[int, str], None] | None = None,
) -> int:
    try:
        verdict = judge(issue, gh_api=gh_api, git=git, health_url=health_url, http_get=http_get)
    except GuardError as exc:
        print(f"issue-close guard failed: {exc}", file=sys.stderr)
        return 1
    if not verdict.reopen:
        print(f"issue-close guard: leaving #{issue} closed — {verdict.reason}")
        return 0
    print(f"issue-close guard: reopening #{issue} — {verdict.reason}")
    (reopen or _reopen)(issue, verdict.reason)
    return 0


def _reopen(issue: int, reason: str) -> None:
    subprocess.run(
        ["gh", "issue", "reopen", str(issue), "--repo", REPO, "--comment", reason],
        check=True,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issue", type=int, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    return run(_parser().parse_args(argv).issue)


if __name__ == "__main__":
    raise SystemExit(main())
