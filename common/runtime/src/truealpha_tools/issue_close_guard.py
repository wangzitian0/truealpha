# ruff: noqa: I001, E401, E402, E701, E702, E731, F401, F811
# fmt: off
from __future__ import annotations; import argparse; import json; import subprocess; import sys; from collections.abc import Callable, Sequence; from dataclasses import dataclass; from infra2_sdk.deploy import HttpGet, ReleaseIdentityError, default_http_get, read_deployed_release; from infra2_sdk.routing import resolve_app_hostname; REPO = "wangzitian0/truealpha"; STAGING_HEALTH = f"https://{resolve_app_hostname('truealpha', 'staging', base_domain='truealpha.club', custom_prod_domain='truealpha.club')}/api/health"; PRODUCTION_HEALTH = f"https://{resolve_app_hostname('truealpha', 'production', base_domain='truealpha.club', custom_prod_domain='truealpha.club')}/api/health"; DEFAULT_HEALTH = STAGING_HEALTH; GhApi = Callable[[str], str]; Git = Callable[[Sequence[str]], tuple[int, str]]
class GuardError(RuntimeError): """The guard could not reach a verdict."""
@dataclass(frozen=True)
class Verdict: reopen: bool; reason: str
def _gh_api(path: str) -> str:
    result = subprocess.run(["gh", "api", path], capture_output=True, text=True, check=False)
    if result.returncode != 0: raise GuardError(f"gh api {path} failed: {result.stderr.strip()}")
    return result.stdout
def _git(args: Sequence[str]) -> tuple[int, str]: result = subprocess.run(["git", *args], capture_output=True, text=True, check=False); return result.returncode, result.stdout.strip()
def closing_commit(issue: int, *, gh_api: GhApi = _gh_api) -> str | None:
    events: list[dict] = []; page = 1
    while True:
        chunk = json.loads(gh_api(f"/repos/{REPO}/issues/{issue}/timeline?per_page=100&page={page}")); events.extend(chunk)
        if len(chunk) < 100: break
        page += 1
    closes = [event for event in events if event.get("event") == "closed"]
    if not closes:
        raise GuardError(f"issue #{issue} has no close event to judge")
    last = closes[-1]
    if last.get("commit_id"): return str(last["commit_id"])
    closed_at = last.get("created_at")
    for event in events:
        if event.get("created_at") == closed_at and event.get("commit_id"): return str(event["commit_id"])
    return None
def deployed( commit: str, *, health_url: str = DEFAULT_HEALTH, http_get: HttpGet | None = None, git: Git = _git ) -> tuple[bool, str]:
    http_get = http_get or default_http_get(); serving = read_deployed_release(health_url, http_get); code, out = git(["merge-base", "--is-ancestor", commit, f"{serving}^{{commit}}"])
    if code not in (0, 1): raise GuardError(f"cannot tell whether {serving} contains {commit[:8]}; fetch tags and full history")
    return code == 0, serving
NON_PRODUCT_PREFIXES = ( "docs/", "governance/", "skills/", ".agents/", ".claude/", ".github/", ".ws-publish/", "tools/", ); NON_PRODUCT_FILES = { "AGENTS.md", "init.md", "README.md", ".gitignore", ".gitattributes", ".env.example", "LICENSE", "Makefile", }
def is_non_product_commit(commit: str, *, git: Git = _git) -> bool:
    code, out = git(["diff-tree", "-m", "--no-commit-id", "--name-only", "-r", commit])
    if code != 0: return False
    files = [f.strip() for f in out.splitlines() if f.strip()]
    if not files: return False
    for path in files:
        if path in NON_PRODUCT_FILES: continue
        if any(path.startswith(prefix) for prefix in NON_PRODUCT_PREFIXES): continue
        if path.endswith(".md") and "/" not in path: continue
        parts = path.split("/")
        if "tests" in parts or "test" in parts or "e2e" in parts: continue
        return False
    return True
def judge( issue: int, *, gh_api: GhApi = _gh_api, git: Git = _git, health_url: str = DEFAULT_HEALTH, http_get: HttpGet | None = None, ) -> Verdict:
    commit = closing_commit(issue, gh_api=gh_api)
    if commit is None:
        return Verdict( False, f'#{issue} was closed by a person, not by a merge — rule 7 asks for exactly that, and they have seen more than this guard can', )
    is_deployed, serving = deployed(commit, health_url=health_url, http_get=http_get, git=git); env_name = "staging" if "staging" in health_url else "production"
    if is_deployed:
        return Verdict(False, f"#{issue} was closed by {commit[:8]}, which {env_name} is serving ({serving})")
    if is_non_product_commit(commit, git=git):
        return Verdict( False, f'#{issue} was closed by {commit[:8]}, which only changes non-product paths (documentation, rules, skills, governance, or tooling); {env_name} serving check is not applicable under rule 6', )
    return Verdict( True, f'#{issue} was closed by merge commit {commit[:8]}, which {env_name} is NOT serving — it serves {serving}. The capability is on main and not deployed to {env_name}, which rule 6 does not call closed. Rule 7 asks for a plain `#N` reference and a hand close once the deployed evidence exists; a merge closed this regardless, which is #562. Reopened rather than left looking done.', )
def run( issue: int, *, gh_api: GhApi = _gh_api, git: Git = _git, health_url: str = DEFAULT_HEALTH, http_get: HttpGet | None = None, reopen: Callable[[int, str], None] | None = None, ) -> int:
    try: verdict = judge(issue, gh_api=gh_api, git=git, health_url=health_url, http_get=http_get)
    except (GuardError, ReleaseIdentityError) as exc: print(f"issue-close guard failed: {exc}", file=sys.stderr); return 1
    if not verdict.reopen:
        print(f"issue-close guard: leaving #{issue} closed — {verdict.reason}")
        return 0
    print(f"issue-close guard: reopening #{issue} — {verdict.reason}")
    (reopen or _reopen)(issue, verdict.reason); return 0
def _reopen(issue: int, reason: str) -> None: subprocess.run( ["gh", "issue", "reopen", str(issue), "--repo", REPO, "--comment", reason], check=True, )
def _parser() -> argparse.ArgumentParser: parser = argparse.ArgumentParser(description=""); parser.add_argument("--issue", type=int, required=True); parser.add_argument( "--health-url", default=DEFAULT_HEALTH, help="Health check URL (default: %(default)s)", ); return parser
def main(argv: Sequence[str] | None = None) -> int: args = _parser().parse_args(argv); return run(args.issue, health_url=args.health_url)
if __name__ == "__main__": raise SystemExit(main())
