"""Reopen a product issue auto-closed by a merge commit that staging does not yet serve (#562)."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from infra2_sdk.deploy import HttpGet, ReleaseIdentityError, default_http_get, read_deployed_release
from infra2_sdk.routing import resolve_app_hostname

REPO = "wangzitian0/truealpha"
STAGING_HEALTH = f"https://{resolve_app_hostname('truealpha', 'staging', base_domain='truealpha.club', custom_prod_domain='truealpha.club')}/api/health"
PRODUCTION_HEALTH = f"https://{resolve_app_hostname('truealpha', 'production', base_domain='truealpha.club', custom_prod_domain='truealpha.club')}/api/health"
DEFAULT_HEALTH = STAGING_HEALTH
GhApi = Callable[[str], str]
Git = Callable[[Sequence[str]], tuple[int, str]]

NON_PRODUCT_PREFIXES = (
    "docs/",
    "governance/",
    "skills/",
    ".agents/",
    ".claude/",
    ".github/",
    ".ws-publish/",
    "tools/",
)
NON_PRODUCT_FILES = {
    "AGENTS.md",
    "init.md",
    "README.md",
    ".gitignore",
    ".gitattributes",
    ".env.example",
    "LICENSE",
    "Makefile",
}


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
    return next((str(e["commit_id"]) for e in events if e.get("created_at") == closed_at and e.get("commit_id")), None)


def deployed(
    commit: str, *, health_url: str = DEFAULT_HEALTH, http_get: HttpGet | None = None, git: Git = _git
) -> tuple[bool, str]:
    serving = read_deployed_release(health_url, http_get or default_http_get())
    code, _ = git(["merge-base", "--is-ancestor", commit, f"{serving}^{{commit}}"])
    if code not in (0, 1):
        raise GuardError(f"cannot tell whether {serving} contains {commit[:8]}; fetch tags and full history")
    return code == 0, serving


def is_non_product_commit(commit: str, *, git: Git = _git) -> bool:
    code, out = git(["diff-tree", "-m", "--no-commit-id", "--name-only", "-r", commit])
    if code != 0:
        return False
    files = [f.strip() for f in out.splitlines() if f.strip()]
    if not files:
        return False
    for path in files:
        parts = path.split("/")
        if (
            path in NON_PRODUCT_FILES
            or any(path.startswith(prefix) for prefix in NON_PRODUCT_PREFIXES)
            or (path.endswith(".md") and "/" not in path)
            or bool({"tests", "test", "e2e"} & set(parts))
        ):
            continue
        return False
    return True


def judge(
    issue: int,
    *,
    gh_api: GhApi = _gh_api,
    git: Git = _git,
    health_url: str = DEFAULT_HEALTH,
    http_get: HttpGet | None = None,
) -> Verdict:
    commit = closing_commit(issue, gh_api=gh_api)
    if commit is None:
        return Verdict(False, f"#{issue} was closed by a person, not by a merge")
    is_deployed, serving = deployed(commit, health_url=health_url, http_get=http_get, git=git)
    env_name = "staging" if "staging" in health_url else "production"
    if is_deployed:
        return Verdict(False, f"#{issue} was closed by {commit[:8]}, which {env_name} is serving ({serving})")
    if is_non_product_commit(commit, git=git):
        return Verdict(
            False,
            f"#{issue} was closed by {commit[:8]}, which only changes non-product paths; "
            f"{env_name} serving check is not applicable under rule 6",
        )
    return Verdict(
        True,
        f"#{issue} was closed by merge commit {commit[:8]}, which {env_name} is NOT serving — it "
        f"serves {serving}. Reopened under rule 6 (#562).",
    )


def run(
    issue: int,
    *,
    gh_api: GhApi = _gh_api,
    git: Git = _git,
    health_url: str = DEFAULT_HEALTH,
    http_get: HttpGet | None = None,
    reopen: Callable[[int, str], None] | None = None,
) -> int:
    try:
        verdict = judge(issue, gh_api=gh_api, git=git, health_url=health_url, http_get=http_get)
    except (GuardError, ReleaseIdentityError) as exc:
        print(f"issue-close guard failed: {exc}", file=sys.stderr)
        return 1
    if not verdict.reopen:
        print(f"issue-close guard: leaving #{issue} closed — {verdict.reason}")
        return 0
    print(f"issue-close guard: reopening #{issue} — {verdict.reason}")
    (reopen or _reopen)(issue, verdict.reason)
    return 0


def _reopen(issue: int, reason: str) -> None:
    subprocess.run(["gh", "issue", "reopen", str(issue), "--repo", REPO, "--comment", reason], check=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issue", type=int, required=True)
    parser.add_argument("--health-url", default=DEFAULT_HEALTH)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return run(args.issue, health_url=args.health_url)


if __name__ == "__main__":
    raise SystemExit(main())
