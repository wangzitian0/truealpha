#!/usr/bin/env python3
"""Fail fast before an agent claims repository work."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

WORKSPACE_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
WORK_ISSUE_RE = re.compile(r"^Work-Issue:[ \t]*#(?P<issue>[1-9][0-9]*)[ \t]*$", re.MULTILINE)
WORK_KEY_RE = re.compile(r"^Work-Key:[ \t]*(?P<key>[A-Za-z0-9][A-Za-z0-9._:-]*)[ \t]*$", re.MULTILINE)


class PreflightError(RuntimeError):
    pass


def run(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise PreflightError(f"{' '.join(args)} failed: {detail}")
    return result


def workspace_identity(root: Path) -> tuple[str, str]:
    name = root.name
    if WORKSPACE_RE.fullmatch(name) is None:
        raise PreflightError(f"checkout directory {name!r} cannot form a valid workspace prefix")
    return name, f"[{name}]"


def upstream_is_gone(track: str) -> bool:
    return track.strip() == "[gone]"


def issue_has_prefix(issue: dict[str, Any], prefix: str) -> bool:
    title = issue.get("title")
    return isinstance(title, str) and title.startswith(f"{prefix} ")


def expected_work_key(root: Path, issue_number: int) -> str:
    for path in sorted((root / "governance" / "batches").glob("*.json")):
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PreflightError(f"cannot read batch manifest {path.relative_to(root)}: {exc}") from exc
        if manifest.get("issue") == issue_number:
            return f"{manifest['batch_id']}:{manifest['target_rung']}"
    return f"standalone-{issue_number}"


def matching_work_claim_prs(
    pull_requests: list[dict[str, Any]],
    issue_number: int,
    work_key: str,
) -> list[dict[str, Any]]:
    matches = []
    for pull_request in pull_requests:
        body = pull_request.get("body") or ""
        declared = {int(match.group("issue")) for match in WORK_ISSUE_RE.finditer(body)}
        declared_keys = {match.group("key") for match in WORK_KEY_RE.finditer(body)}
        if declared == {issue_number} or work_key in declared_keys:
            matches.append(pull_request)
    return matches


def repository_root() -> Path:
    result = subprocess.run(
        ("git", "rev-parse", "--show-toplevel"),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise PreflightError("current directory is not a Git checkout")
    return Path(result.stdout.strip()).resolve()


def clean_gone_upstream(root: Path, branch: str, repair: bool) -> str:
    track = run(
        "git",
        "for-each-ref",
        "--format=%(upstream:track)",
        f"refs/heads/{branch}",
        cwd=root,
    ).stdout
    if not upstream_is_gone(track):
        return branch
    if not repair:
        raise PreflightError(
            f"branch {branch!r} tracks a deleted upstream; rerun with --repair-clean-gone or switch explicitly"
        )
    run("git", "switch", "main", cwd=root)
    run("git", "merge", "--ff-only", "origin/main", cwd=root)
    return "main"


def preflight(issue_number: int, repair_clean_gone: bool) -> tuple[str, str, str]:
    root = repository_root()
    workspace_name, prefix = workspace_identity(root)
    run("git", "fetch", "origin", "--prune", cwd=root)
    status = run("git", "status", "--porcelain=v1", cwd=root).stdout
    if status.strip():
        raise PreflightError("working tree is dirty; commit, stash, or isolate the changes before claiming work")
    branch = run("git", "branch", "--show-current", cwd=root).stdout.strip()
    if not branch:
        raise PreflightError("detached HEAD cannot claim agent work")
    branch = clean_gone_upstream(root, branch, repair_clean_gone)

    issue = json.loads(
        run(
            "gh",
            "issue",
            "view",
            str(issue_number),
            "--json",
            "number,title,state",
            cwd=root,
        ).stdout
    )
    if issue.get("state") != "OPEN":
        raise PreflightError(f"issue #{issue_number} is not open")
    if not issue_has_prefix(issue, prefix):
        raise PreflightError(f"issue #{issue_number} does not use workspace prefix {prefix}")
    work_key = expected_work_key(root, issue_number)

    pull_requests = json.loads(
        run(
            "gh",
            "pr",
            "list",
            "--state",
            "open",
            "--limit",
            "100",
            "--json",
            "number,body,headRefName,title",
            cwd=root,
        ).stdout
    )
    matches = matching_work_claim_prs(pull_requests, issue_number, work_key)
    if len(matches) > 1:
        numbers = ", ".join(f"#{pull_request['number']}" for pull_request in matches)
        raise PreflightError(f"Work-Issue #{issue_number} is claimed by multiple open PRs: {numbers}")
    if matches and matches[0].get("headRefName") != branch:
        raise PreflightError(
            f"Work-Issue #{issue_number} is already claimed by PR #{matches[0]['number']} "
            f"on branch {matches[0].get('headRefName')!r}"
        )

    run(sys.executable, "tools/check_delivery_governance.py", cwd=root)
    return workspace_name, branch, work_key


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-issue", type=int, required=True)
    parser.add_argument("--repair-clean-gone", action="store_true")
    args = parser.parse_args()
    try:
        workspace_name, branch, work_key = preflight(args.work_issue, args.repair_clean_gone)
    except PreflightError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(
        f"Agent preflight passed: workspace={workspace_name}, branch={branch}, "
        f"issue=#{args.work_issue}, work_key={work_key}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
