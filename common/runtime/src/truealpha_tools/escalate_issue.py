"""Open or resolve a GitHub tracking issue for scheduled watchdogs."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Callable, Sequence

LIST_LIMIT = 100
GhResult = tuple[int, str, str]
Gh = Callable[[Sequence[str]], GhResult]


class ListingFailed(RuntimeError):
    """The open issues could not be read."""


def _gh(args: Sequence[str]) -> GhResult:
    r = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    return r.returncode, r.stdout, r.stderr


def open_issues_titled(title: str, *, repo: str, gh: Gh) -> list[int]:
    code, out, err = gh(
        [
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--search",
            f'in:title "{title.replace(chr(34), "")}"',
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
    except json.JSONDecodeError as exc:
        raise ListingFailed(f"gh issue list returned something that is not JSON: {exc}") from exc
    if not isinstance(listed, list):
        raise ListingFailed(f"gh issue list returned {type(listed).__name__}, not a list")
    matches = [issue for issue in listed if isinstance(issue, dict) and issue.get("title") == title]
    try:
        return sorted(int(m["number"]) for m in matches)
    except (KeyError, TypeError, ValueError) as exc:
        raise ListingFailed(f"gh issue list returned an issue without a usable number: {exc!r}") from exc


def _write(args: Sequence[str], *, gh: Gh) -> bool:
    code, _, err = gh(args)
    if code != 0:
        print(f"::error::gh {' '.join(args[:2])} failed: {err.strip()[:300]}", file=sys.stderr)
        return False
    return True


def escalate(title: str, body_file: str, *, repo: str, gh: Gh = _gh) -> int:
    try:
        existing = open_issues_titled(title, repo=repo, gh=gh)
    except ListingFailed as exc:
        print(f"::warning::issue listing failed ({exc}); skipping escalation — the run is already red")
        return 0
    if existing:
        n = existing[0]
        ok = _write(["issue", "comment", str(n), "--repo", repo, "--body-file", body_file], gh=gh)
        print(f"commented on #{n}" if ok else f"could not comment on #{n}")
        return 0 if ok else 1
    ok = _write(["issue", "create", "--repo", repo, "--title", title, "--body-file", body_file], gh=gh)
    print(f"filed {title!r}" if ok else f"could not file {title!r}")
    return 0 if ok else 1


def resolve(title: str, body_file: str, *, repo: str, gh: Gh = _gh) -> int:
    try:
        existing = open_issues_titled(title, repo=repo, gh=gh)
    except ListingFailed as exc:
        print(f"::warning::issue listing failed ({exc}); nothing resolved — the next green run retries")
        return 0
    if not existing:
        print(f"no open issue titled {title!r}; nothing to resolve")
        return 0
    failed = False
    for n in existing:
        ok = _write(["issue", "comment", str(n), "--repo", repo, "--body-file", body_file], gh=gh) and _write(
            ["issue", "close", str(n), "--repo", repo, "--reason", "completed"], gh=gh
        )
        print(f"resolved #{n}" if ok else f"could not resolve #{n}")
        failed = failed or not ok
    return 1 if failed else 0


def main(argv: Sequence[str] | None = None, *, gh: Gh = _gh) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("open", "resolve"))
    parser.add_argument("--title", required=True)
    parser.add_argument("--body-file", required=True)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    args = parser.parse_args(argv)
    if not args.title.strip():
        print("escalate_issue: --title is empty", file=sys.stderr)
        return 1
    if not args.repo.strip():
        print("escalate_issue: no repository — pass --repo OWNER/NAME or set GITHUB_REPOSITORY", file=sys.stderr)
        return 1
    return (escalate if args.action == "open" else resolve)(args.title, args.body_file, repo=args.repo, gh=gh)


if __name__ == "__main__":
    raise SystemExit(main())
