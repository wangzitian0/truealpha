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
RUNGS = frozenset({"E0", "E1", "E2", "E3", "E4", "E5"})


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


def batch_manifest_for_issue(root: Path, issue_number: int) -> dict[str, Any] | None:
    for path in sorted((root / "governance" / "batches").glob("*.json")):
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PreflightError(f"cannot read batch manifest {path.relative_to(root)}: {exc}") from exc
        if manifest.get("issue") == issue_number:
            return manifest
    return None


def expected_work_key(root: Path, issue_number: int) -> str:
    manifest = batch_manifest_for_issue(root, issue_number)
    if manifest is not None:
        return f"{manifest['batch_id']}:{manifest['target_rung']}"
    return f"standalone-{issue_number}"


def closed_terminal_rerun_allowed(root: Path, issue_number: int) -> bool:
    manifest = batch_manifest_for_issue(root, issue_number)
    if manifest is None or manifest.get("status") != "done":
        return False
    terminal_rung = manifest.get("terminal_rung")
    return (
        terminal_rung in RUNGS
        and manifest.get("last_accepted_rung") == terminal_rung
        and manifest.get("target_rung") == terminal_rung
    )


def validate_issue_state(
    root: Path,
    issue_number: int,
    state: str,
    allow_closed_terminal_rerun: bool,
) -> None:
    if state == "OPEN":
        return
    if not allow_closed_terminal_rerun:
        raise PreflightError(f"issue #{issue_number} is not open")
    if not closed_terminal_rerun_allowed(root, issue_number):
        raise PreflightError(
            f"issue #{issue_number} is closed and is not an accepted terminal batch eligible for a corrective rerun"
        )


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


def preflight(
    issue_number: int,
    repair_clean_gone: bool,
    allow_closed_terminal_rerun: bool = False,
) -> tuple[str, str, str]:
    root = repository_root()
    workspace_name, _ = workspace_identity(root)
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
    work_key = expected_work_key(root, issue_number)
    validate_issue_state(
        root,
        issue_number,
        str(issue.get("state", "")),
        allow_closed_terminal_rerun,
    )

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
    parser.add_argument("--allow-closed-terminal-rerun", action="store_true")
    args = parser.parse_args()
    try:
        workspace_name, branch, work_key = preflight(
            args.work_issue,
            args.repair_clean_gone,
            args.allow_closed_terminal_rerun,
        )
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
