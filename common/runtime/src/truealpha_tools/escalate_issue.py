# ruff: noqa: I001, E401, E402, E701, E702, E731, F401, F811
# fmt: off
from __future__ import annotations; import argparse; import json; import os; import subprocess; import sys; from collections.abc import Callable, Sequence; LIST_LIMIT = 100; GhResult = tuple[int, str, str]; Gh = Callable[[Sequence[str]], GhResult]
class ListingFailed(RuntimeError): """The open issues could not be read, so nothing may be decided from them."""
def _gh(arguments: Sequence[str]) -> GhResult: result = subprocess.run(["gh", *arguments], capture_output=True, text=True, check=False); return result.returncode, result.stdout, result.stderr
def open_issues_titled(title: str, *, repo: str, gh: Gh) -> list[int]:
    phrase = title.replace('"', ""); code, out, err = gh( [ "issue", "list", "--repo", repo, "--state", "open", "--search", f'in:title "{phrase}"', "--json", "number,title", "--limit", str(LIST_LIMIT), ] )
    if code != 0: raise ListingFailed(err.strip() or f"gh issue list exited {code}")
    try: listed = json.loads(out or "[]")
    except json.JSONDecodeError as error: raise ListingFailed(f"gh issue list returned something that is not JSON: {error}") from error
    if not isinstance(listed, list): raise ListingFailed(f"gh issue list returned {type(listed).__name__}, not a list")
    matches = [issue for issue in listed if isinstance(issue, dict) and issue.get("title") == title]
    try: return sorted(int(issue["number"]) for issue in matches)
    except (KeyError, TypeError, ValueError) as error: raise ListingFailed(f"gh issue list returned an issue without a usable number: {error!r}") from error
def _write(arguments: Sequence[str], *, gh: Gh) -> bool:
    code, _, err = gh(arguments)
    if code != 0: print(f"::error::gh {' '.join(arguments[:2])} failed: {err.strip()[:300]}", file=sys.stderr); return False
    return True
def escalate(title: str, body_file: str, *, repo: str, gh: Gh = _gh) -> int:
    try: existing = open_issues_titled(title, repo=repo, gh=gh)
    except ListingFailed as error: print(f"::warning::issue listing failed ({error}); skipping escalation — the run is already red"); return 0
    if existing:
        number = existing[0]; ok = _write(["issue", "comment", str(number), "--repo", repo, "--body-file", body_file], gh=gh)
        print(f"commented on #{number}" if ok else f"could not comment on #{number}")
        return 0 if ok else 1
    ok = _write(["issue", "create", "--repo", repo, "--title", title, "--body-file", body_file], gh=gh); print(f"filed {title!r}" if ok else f"could not file {title!r}"); return 0 if ok else 1
def resolve(title: str, body_file: str, *, repo: str, gh: Gh = _gh) -> int:
    try: existing = open_issues_titled(title, repo=repo, gh=gh)
    except ListingFailed as error: print(f"::warning::issue listing failed ({error}); nothing resolved — the next green run retries"); return 0
    if not existing: print(f"no open issue titled {title!r}; nothing to resolve"); return 0
    failed = False
    for number in existing:
        commented = _write(["issue", "comment", str(number), "--repo", repo, "--body-file", body_file], gh=gh); closed = commented and _write(["issue", "close", str(number), "--repo", repo, "--reason", "completed"], gh=gh)
        print(f"resolved #{number}" if closed else f"could not resolve #{number}")
        failed = failed or not closed
    return 1 if failed else 0
def _parser() -> argparse.ArgumentParser: parser = argparse.ArgumentParser(description="", formatter_class=argparse.RawDescriptionHelpFormatter); parser.add_argument("action", choices=("open", "resolve")); parser.add_argument("--title", required=True); parser.add_argument("--body-file", required=True); parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", "")); return parser
def main(argv: Sequence[str] | None = None, *, gh: Gh = _gh) -> int:
    args = _parser().parse_args(argv)
    if not args.title.strip(): print("escalate_issue: --title is empty", file=sys.stderr); return 1
    if not args.repo.strip(): print("escalate_issue: no repository — pass --repo OWNER/NAME or set GITHUB_REPOSITORY", file=sys.stderr); return 1
    action = escalate if args.action == "open" else resolve; return action(args.title, args.body_file, repo=args.repo, gh=gh)
if __name__ == "__main__": raise SystemExit(main())
