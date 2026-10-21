"""Keep one GitHub issue in step with the staging line (#1468 F1).

While staging is red, exactly one open issue names the failing step. The title is
`[truealpha] Staging red since <tag>: <stage / job / step>`. When the state changes, the tool adds
a comment to that issue. It never opens a second issue. When the line is open again, the tool
closes the issue.

A hidden marker in the issue body and in each comment records the state. A run adds a comment only
when no text of the issue holds the marker of the current state.

Stdlib only: `auto-release-staging.yml` runs the train with no `uv sync`.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence

from truealpha_tools.staging_line import ISSUE_TITLE_PREFIX, StagingRed

Runner = Callable[[Sequence[str]], str]
#: How many open issues one run reads.
ISSUE_LIST_LIMIT = 300


def open_red_issues(repo: str, gh: Runner) -> list[dict[str, object]]:
    """The open staging-red issues, the oldest first.

    This reads the list of open issues and filters by title. The search index lags behind a new
    issue, and a lagging answer would open a second issue.
    """
    out = gh(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--limit",
            str(ISSUE_LIST_LIMIT),
            "--json",
            "number,title",
        ]
    )
    issues = json.loads(out or "[]")
    if not isinstance(issues, list):
        raise ValueError(f"unexpected issue list payload: {out[:200]}")
    if len(issues) >= ISSUE_LIST_LIMIT:
        # An older staging-red issue could be past the limit, and the run would open a second one.
        raise ValueError(f"{len(issues)} open issues reach the list limit of {ISSUE_LIST_LIMIT}; raise the limit")
    red = [
        issue
        for issue in issues
        if isinstance(issue, dict) and str(issue.get("title", "")).startswith(ISSUE_TITLE_PREFIX)
    ]
    return sorted(red, key=lambda issue: int(issue["number"]))  # type: ignore[call-overload]


def state_marker(red: StagingRed) -> str:
    return f"<!-- staging-red-state: {hashlib.sha1(red.state_key.encode()).hexdigest()[:12]} -->"


def _facts(repo: str, red: StagingRed) -> list[str]:
    return [
        f"- The failing step is {red.label}.",
        f"- Releases that failed since the last green walk: {', '.join(red.failed_tags)}.",
        f"- Newest failed run: https://github.com/{repo}/actions/runs/{red.run}",
    ]


def issue_body(repo: str, red: StagingRed) -> str:
    green = f"{red.last_green_walk:%Y-%m-%d %H:%M}Z" if red.last_green_walk else "not in the history that was read"
    return "\n".join(
        [
            "The staging release train is stopped (#1468 F1).",
            "",
            f"- Staging is red since {red.since}.",
            *_facts(repo, red),
            f"- The newest green walk finished at: {green}.",
            "",
            "`auto-release-staging` cuts no new staging tag now. It exits green and prints this reason.",
            "A green walk of any tag resumes the train. The next run then closes this issue.",
            "",
            "To resume:",
            "",
            "1. Fix the cause that the failing step names.",
            "2. Cut a release by hand with `tools/cut_release.sh <next tag>`.",
            "   The stop does not hold a hand-cut release.",
            "   A walk of a deployed tag also lifts the stop, but it does not fix the cause:",
            "   `gh workflow run walk-release.yml -f deploy_type=staging -f version_ref=<tag>`.",
            "",
            "To replay the decision by hand (read-only without `--report-staging-red`):",
            "",
            f"`python3 tools/auto_release.py --repo {repo} --trigger-sha $(git rev-parse origin/main)`",
            "",
            state_marker(red),
        ]
    )


def reconcile(repo: str, red: StagingRed | None, line: str, head: str, gh: Runner) -> str:
    """Make the open issues match the staging line. Return one log line.

    Red: open one issue, or comment on the oldest open one when no text of it holds the current
    state. Not red: close each open staging-red issue.
    """
    open_issues = open_red_issues(repo, gh)
    if red is None:
        for issue in open_issues:
            gh(
                [
                    "gh", "issue", "close", str(issue["number"]), "--repo", repo,
                    "--comment", f"The staging line is open again: {line}.",
                ]
            )  # fmt: skip
        return "closed " + ", ".join(f"#{i['number']}" for i in open_issues) if open_issues else "none open"
    if not open_issues:
        url = gh(["gh", "issue", "create", "--repo", repo, "--title", red.title, "--body", issue_body(repo, red)])
        return f"opened {url.strip()}"
    number = str(open_issues[0]["number"])
    seen = json.loads(gh(["gh", "issue", "view", number, "--repo", repo, "--json", "body,comments"]))
    texts = [str(seen.get("body"))] + [str(comment.get("body")) for comment in seen.get("comments") or []]
    if any(state_marker(red) in text for text in texts):
        return f"#{number} already holds this state"
    body = "\n".join(
        [
            f"The train held the release for main {head[:8]}. Staging is still red since {red.since}.",
            *_facts(repo, red),
            "",
            state_marker(red),
        ]
    )
    gh(["gh", "issue", "comment", number, "--repo", repo, "--body", body])
    return f"commented on #{number}"
