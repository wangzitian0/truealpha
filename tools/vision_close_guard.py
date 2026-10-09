"""Keep scope:vision issues open until deployed evidence exists — #1174.

Two checks, one module:

- `check-pr` blocks a pull request whose body holds a GitHub closing keyword
  that names a `scope:vision` issue. The check does not read negation. GitHub
  acts on "does not close #N" too. Authors write `Refs #N` instead.
- `reopen --issue N` reopens a `scope:vision` issue that a merge or a person
  closed with no evidence comment. Rule 6 of AGENTS.md defines the evidence:
  one comment with the headings `Deployed call site` and `Real-data output`.

Issues without `scope:vision` are never reopened by this tool.

The decision functions are pure. They take their lookups as arguments. The
`gh` calls sit in the thin CLI layer at the end of the file.

Usage:
  python3 tools/vision_close_guard.py check-pr        # reads GITHUB_EVENT_PATH
  python3 tools/vision_close_guard.py reopen --issue 1174

Exit codes:
  0 - passed, left closed, or reopened
  1 - check-pr blocked a closing keyword that names a vision issue
  2 - the guard could not reach a verdict, so it fails closed
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

REPO = "wangzitian0/truealpha"
VISION_LABEL = "scope:vision"
# Rule 6 evidence. A comment must hold both headings.
EVIDENCE_HEADINGS = ("Deployed call site", "Real-data output")
# The closing keywords in GitHub's linking grammar.
CLOSING_KEYWORDS = ("close", "closes", "closed", "fix", "fixes", "fixed", "resolve", "resolves", "resolved")
REOPEN_REASON = (
    "The vision close guard reopened this issue. It carries scope:vision, and no comment "
    "holds both headings `Deployed call site` and `Real-data output`. Rule 6 requires that "
    "evidence. Post it, then close the issue again."
)

GhApi = Callable[[str], str]
LabelsOf = Callable[[str, int], Sequence[str]]

_REFERENCE = re.compile(
    r"\b(?P<keyword>" + "|".join(CLOSING_KEYWORDS) + r")\b[ \t]*:?[ \t]*"
    r"(?:(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+))?#(?P<number>[0-9]+)\b",
    re.IGNORECASE,
)
_HEADINGS = tuple(
    re.compile(
        rf"^[ \t]{{0,3}}#{{1,6}}[ \t]+{re.escape(text)}[ \t]*:?[ \t]*\r?$",
        re.IGNORECASE | re.MULTILINE,
    )
    for text in EVIDENCE_HEADINGS
)


class GuardError(RuntimeError):
    """The guard could not reach a verdict."""


@dataclass(frozen=True)
class Reference:
    keyword: str
    repo: str
    number: int


@dataclass(frozen=True)
class PrVerdict:
    passed: bool
    reason: str


@dataclass(frozen=True)
class ReopenVerdict:
    reopen: bool
    reason: str


def closing_references(body: str, *, default_repo: str = REPO) -> list[Reference]:
    """Every closing keyword in `body` with the issue it names."""
    return [
        Reference(match["keyword"].lower(), match["repo"] or default_repo, int(match["number"]))
        for match in _REFERENCE.finditer(body)
    ]


def _target(reference: Reference, default_repo: str) -> str:
    if reference.repo.lower() == default_repo.lower():
        return f"#{reference.number}"
    return f"{reference.repo}#{reference.number}"


def check_pr_body(body: str, *, labels_of: LabelsOf, default_repo: str = REPO) -> PrVerdict:
    """Block the body when a closing keyword names a `scope:vision` issue."""
    offending = [
        reference
        for reference in closing_references(body, default_repo=default_repo)
        if VISION_LABEL in labels_of(reference.repo, reference.number)
    ]
    if not offending:
        return PrVerdict(True, "no closing keyword names a scope:vision issue")
    found = ", ".join(f"`{ref.keyword} {_target(ref, default_repo)}`" for ref in offending)
    targets = dict.fromkeys(_target(ref, default_repo) for ref in offending)
    refs = ", ".join(f"`Refs {target}`" for target in targets)
    return PrVerdict(
        False,
        f"closing keyword names a scope:vision issue: {found}. Write {refs} instead. GitHub acts on negated text too.",
    )


def pull_request_body(event: dict) -> str:
    """The pull request body from a `pull_request` event payload. Empty when null."""
    pull_request = event.get("pull_request")
    if not isinstance(pull_request, dict):
        raise GuardError("the event payload has no pull_request object")
    body = pull_request.get("body")
    if body is None:
        return ""
    if not isinstance(body, str):
        raise GuardError("the pull_request body is neither text nor null")
    return body


def has_evidence(comments: Iterable[str]) -> bool:
    """True when one comment holds every evidence heading as a Markdown heading line."""
    return any(all(pattern.search(body) for pattern in _HEADINGS) for body in comments)


def reopen_verdict(labels: Sequence[str], *, comments: Callable[[], Iterable[str]]) -> ReopenVerdict:
    """Reopen a closed `scope:vision` issue that has no evidence comment.

    `comments` is called only for a vision issue, so a non-vision close reads nothing more.
    """
    if VISION_LABEL not in labels:
        return ReopenVerdict(False, "not scope:vision; the issue-close-guard decides this close")
    if has_evidence(comments()):
        return ReopenVerdict(False, "scope:vision close carries the Rule 6 evidence comment")
    return ReopenVerdict(True, REOPEN_REASON)


def _gh_api(path: str) -> str:
    result = subprocess.run(["gh", "api", path], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise GuardError(f"gh api {path} failed: {result.stderr.strip()}")
    return result.stdout


def _reopen(repo: str, issue: int, reason: str) -> None:
    result = subprocess.run(
        ["gh", "issue", "reopen", str(issue), "--repo", repo, "--comment", reason],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise GuardError(f"gh issue reopen #{issue} failed: {result.stderr.strip()}")


def _json_object(text: str, what: str) -> dict:
    value = json.loads(text)
    if not isinstance(value, dict):
        raise GuardError(f"{what} is not an object")
    return value


def _json_list(text: str, what: str) -> list:
    value = json.loads(text)
    if not isinstance(value, list):
        raise GuardError(f"{what} is not a list")
    return value


def _label_names(repo: str, number: int, gh_api: GhApi) -> list[str]:
    issue = _json_object(gh_api(f"/repos/{repo}/issues/{number}"), f"issue {repo}#{number}")
    labels = issue.get("labels")
    if not isinstance(labels, list):
        raise GuardError(f"issue {repo}#{number} has no labels list")
    names = []
    for label in labels:
        if not isinstance(label, dict) or not isinstance(label.get("name"), str):
            raise GuardError(f"issue {repo}#{number} has a label without a name")
        names.append(label["name"])
    return names


def _comment_bodies(repo: str, number: int, gh_api: GhApi) -> list[str]:
    """Every comment body. Paged, because the evidence may sit on a later page."""
    bodies: list[str] = []
    page = 1
    while True:
        chunk = _json_list(
            gh_api(f"/repos/{repo}/issues/{number}/comments?per_page=100&page={page}"),
            f"comments of {repo}#{number}",
        )
        for comment in chunk:
            if not isinstance(comment, dict):
                raise GuardError(f"a comment of {repo}#{number} is not an object")
            body = comment.get("body")
            bodies.append(body if isinstance(body, str) else "")
        if len(chunk) < 100:
            return bodies
        page += 1


def run_check_pr(body: str, *, gh_api: GhApi = _gh_api, default_repo: str = REPO) -> int:
    try:
        verdict = check_pr_body(
            body,
            labels_of=lambda repo, number: _label_names(repo, number, gh_api),
            default_repo=default_repo,
        )
    except GuardError as exc:
        print(f"vision close guard could not reach a verdict: {exc}", file=sys.stderr)
        return 2
    if verdict.passed:
        print(f"vision close guard: passed — {verdict.reason}")
        return 0
    print(f"vision close guard: blocked — {verdict.reason}", file=sys.stderr)
    return 1


def run_reopen(
    issue: int,
    *,
    gh_api: GhApi = _gh_api,
    reopen: Callable[[int, str], None] | None = None,
    repo: str = REPO,
) -> int:
    try:
        labels = _label_names(repo, issue, gh_api)
        verdict = reopen_verdict(labels, comments=lambda: _comment_bodies(repo, issue, gh_api))
        if not verdict.reopen:
            print(f"vision close guard: leaving #{issue} closed — {verdict.reason}")
            return 0
        print(f"vision close guard: reopening #{issue} — {verdict.reason}")
        (reopen or functools.partial(_reopen, repo))(issue, verdict.reason)
    except GuardError as exc:
        print(f"vision close guard could not reach a verdict for #{issue}: {exc}", file=sys.stderr)
        return 2
    return 0


def _event_body() -> str:
    path = os.environ.get("GITHUB_EVENT_PATH")
    if not path:
        raise GuardError("GITHUB_EVENT_PATH is not set; run this inside GitHub Actions")
    try:
        event = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GuardError(f"cannot read the event payload: {exc}") from exc
    if not isinstance(event, dict):
        raise GuardError("the event payload is not an object")
    return pull_request_body(event)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check-pr", help="block closing keywords that name a scope:vision issue")
    reopen = commands.add_parser("reopen", help="reopen a scope:vision issue closed without evidence")
    reopen.add_argument("--issue", type=int, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo = os.environ.get("GITHUB_REPOSITORY") or REPO
    if args.command == "check-pr":
        try:
            body = _event_body()
        except GuardError as exc:
            print(f"vision close guard could not reach a verdict: {exc}", file=sys.stderr)
            return 2
        return run_check_pr(body, default_repo=repo)
    return run_reopen(args.issue, repo=repo)


if __name__ == "__main__":
    raise SystemExit(main())
