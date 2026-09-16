"""Open, or resolve, the one tracking issue a scheduled watchdog owns.

#876 (W3, W12). Before this, `deploy-freshness.yml` and `mutation-reproof.yml`
each carried an inline shell copy of "comment on the open issue with this title,
or file it" (#680), and neither ever closed anything: #818 sat open two days
after staging was fresh again, so an open alert stopped meaning "broken now".
One implementation, used by every watchdog that escalates, with both halves of
the lifecycle:

- `open`: comment on the open issue whose title is EXACTLY `--title`, or file it.
- `resolve`: comment the resolution on every open issue whose title is exactly
  `--title`, then close it. No such issue is a no-op — a green run with nothing
  to resolve is the normal case.

Exact means equality on the title the listing returns. GitHub's `in:title`
search is a word match, not an equality test, so a longer title that contains
the shorter one — "main is red: ci-required failed on push (flaky)" — comes back
for the shorter query, and trusting the first hit comments on, or CLOSES, the
wrong issue.

A failed LIST never falls through to create (#680 review): a rate limit would
turn one breakage into duplicates. It is a warning and exit 0 in both modes —
the calling run's own verdict already says what happened, and the next run
retries. A failed WRITE after a successful list is exit 1: that is a token or
permission regression, and a silent one is exactly how #818 stayed open.

Stdlib and the `gh` CLI only, so a workflow can run it with `python3` and no
workspace install.

The repository comes from `--repo` or the runner's `GITHUB_REPOSITORY`, and
there is no fallback: a hand run that forgot `--repo` must not write to
whichever repository a constant names (review).

Usage:
  python3 tools/escalate_issue.py open --title T --body-file F [--repo OWNER/NAME]
  python3 tools/escalate_issue.py resolve --title T --body-file F [--repo OWNER/NAME]

Exit codes:
  0 - done, nothing to do, or skipped because the open issues could not be listed
  1 - a create, comment or close failed, or the title or repository is missing
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Callable, Sequence

# Enough that a handful of superstring matches cannot push the exact title off
# the page; gh's own default is 30.
LIST_LIMIT = 100
#: (returncode, stdout, stderr) for `gh <arguments>`.
GhResult = tuple[int, str, str]
Gh = Callable[[Sequence[str]], GhResult]


class ListingFailed(RuntimeError):
    """The open issues could not be read, so nothing may be decided from them."""


def _gh(arguments: Sequence[str]) -> GhResult:
    result = subprocess.run(["gh", *arguments], capture_output=True, text=True, check=False)
    return result.returncode, result.stdout, result.stderr


def open_issues_titled(title: str, *, repo: str, gh: Gh) -> list[int]:
    """Numbers of the open issues whose title equals `title`, oldest first."""
    # A quote inside the phrase would end it early. The equality below is what
    # decides, so dropping quotes from the QUERY only widens the candidates.
    phrase = title.replace('"', "")
    code, out, err = gh(
        [
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--search",
            f'in:title "{phrase}"',
            "--json",
            "number,title",
            "--limit",
            str(LIST_LIMIT),
        ]
    )
    if code != 0:
        raise ListingFailed(err.strip() or f"gh issue list exited {code}")
    try:
        listed = json.loads(out or "[]")
    except json.JSONDecodeError as error:
        raise ListingFailed(f"gh issue list returned something that is not JSON: {error}") from error
    if not isinstance(listed, list):
        raise ListingFailed(f"gh issue list returned {type(listed).__name__}, not a list")
    # Equality, never containment: the search already matched words, and the
    # superstring it also returns is a different breakage.
    return sorted(int(issue["number"]) for issue in listed if isinstance(issue, dict) and issue.get("title") == title)


def _write(arguments: Sequence[str], *, gh: Gh) -> bool:
    code, _, err = gh(arguments)
    if code != 0:
        print(f"::error::gh {' '.join(arguments[:2])} failed: {err.strip()[:300]}", file=sys.stderr)
        return False
    return True


def escalate(title: str, body_file: str, *, repo: str, gh: Gh = _gh) -> int:
    """Comment on the open issue with this exact title, or file it."""
    try:
        existing = open_issues_titled(title, repo=repo, gh=gh)
    except ListingFailed as error:
        print(f"::warning::issue listing failed ({error}); skipping escalation — the run is already red")
        return 0
    if existing:
        # The oldest: a pre-existing duplicate (#687/#688 came from one forced
        # failure) must not split the history further.
        number = existing[0]
        ok = _write(["issue", "comment", str(number), "--repo", repo, "--body-file", body_file], gh=gh)
        print(f"commented on #{number}" if ok else f"could not comment on #{number}")
        return 0 if ok else 1
    ok = _write(["issue", "create", "--repo", repo, "--title", title, "--body-file", body_file], gh=gh)
    print(f"filed {title!r}" if ok else f"could not file {title!r}")
    return 0 if ok else 1


def resolve(title: str, body_file: str, *, repo: str, gh: Gh = _gh) -> int:
    """Comment the resolution on, and close, every open issue with this exact title."""
    try:
        existing = open_issues_titled(title, repo=repo, gh=gh)
    except ListingFailed as error:
        print(f"::warning::issue listing failed ({error}); nothing resolved — the next green run retries")
        return 0
    if not existing:
        print(f"no open issue titled {title!r}; nothing to resolve")
        return 0
    failed = False
    # Every exact match, not the first: a duplicate left open is the stale alert
    # this mode exists to remove.
    for number in existing:
        commented = _write(["issue", "comment", str(number), "--repo", repo, "--body-file", body_file], gh=gh)
        closed = commented and _write(["issue", "close", str(number), "--repo", repo, "--reason", "completed"], gh=gh)
        print(f"resolved #{number}" if closed else f"could not resolve #{number}")
        failed = failed or not closed
    return 1 if failed else 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=("open", "resolve"))
    parser.add_argument("--title", required=True)
    parser.add_argument("--body-file", required=True)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    return parser


def main(argv: Sequence[str] | None = None, *, gh: Gh = _gh) -> int:
    args = _parser().parse_args(argv)
    if not args.title.strip():
        # An empty title would search for everything and match nothing exactly,
        # then file an untitled issue. A caller bug, named at the cause.
        print("escalate_issue: --title is empty", file=sys.stderr)
        return 1
    if not args.repo.strip():
        print("escalate_issue: no repository — pass --repo OWNER/NAME or set GITHUB_REPOSITORY", file=sys.stderr)
        return 1
    action = escalate if args.action == "open" else resolve
    return action(args.title, args.body_file, repo=args.repo, gh=gh)


if __name__ == "__main__":
    raise SystemExit(main())
