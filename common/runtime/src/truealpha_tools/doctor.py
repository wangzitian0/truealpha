#!/usr/bin/env python3
"""TrueAlpha Physical Dev Environment Doctor (dev_env SSOT).

Inspects the physical developer environment against the repository contracts:
1. Python version matches .python-version and .tool-versions
2. uv lockfile and virtual environment consistency
3. Bun runtime and apps/app-web/node_modules completeness
4. The per-commit hook (T0, #1412): core.hooksPath names the tracked hook directory
5. Local runtime services (Docker, Postgres, MinIO, OpenD) probe
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Sequence
from pathlib import Path

from infra2_sdk.routing import resolve_app_hostname

ROOT = Path(__file__).resolve().parents[4]
#: The tracked hook directory. core.hooksPath is relative, so each worktree of a clone
#: runs its own checked-out hook (#1412 T0).
HOOKS_DIR = "tools/git-hooks"
HOOK_REMEDIATION = "Run: git config core.hooksPath tools (or ln -sf ../../tools/prepush.sh .git/hooks/pre-push)"


def check_python_version() -> bool:
    pv_file, tv_file = ROOT / ".python-version", ROOT / ".tool-versions"
    expected_pv = pv_file.read_text(encoding="utf-8").strip() if pv_file.exists() else None
    actual = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    print(f"[*] Python runtime: {actual}")
    if expected_pv and actual != expected_pv:
        print(f"    [FAIL] Python mismatch: expected {expected_pv} (from .python-version), got {actual}")
        return False
    if tv_file.exists():
        for line in tv_file.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("python "):
                expected_tv = line.strip().split("python ", 1)[1].strip()
                if expected_tv != expected_pv:
                    print(
                        f"    [FAIL] Version file conflict: .tool-versions ({expected_tv}) != .python-version ({expected_pv})"
                    )
                    return False
    print("    [OK] Python version aligned across .python-version and .tool-versions")
    return True


def check_uv() -> bool:
    if not shutil.which("uv"):
        print("    [FAIL] 'uv' binary not found in PATH")
        return False
    if not (ROOT / "uv.lock").exists():
        print("    [FAIL] uv.lock does not exist")
        return False
    if not (ROOT / ".venv" / "bin" / "python").exists():
        print("    [WARN] .venv does not exist. Run 'make install' or 'uv sync'")
        return False
    print("    [OK] uv installed and virtual environment exists")
    return True


def check_bun_and_web() -> bool:
    if not shutil.which("bun"):
        print("    [FAIL] 'bun' binary not found in PATH")
        return False
    node_modules = ROOT / "apps" / "app-web" / "node_modules"
    if not node_modules.exists():
        print("    [FAIL] apps/app-web/node_modules missing. Run 'make install' or 'cd apps/app-web && bun install'")
        return False
    for dep in ("pg", "bcryptjs", "@aws-sdk/client-s3", "next", "react"):
        if not (node_modules / dep).exists():
            print(f"    [FAIL] Missing required dependency in node_modules: {dep}")
            return False
    print("    [OK] Bun runtime and apps/app-web/node_modules complete")
    return True


def _hooks_path(root: Path) -> str:
    result = subprocess.run(
        ["git", "config", "--get", "core.hooksPath"], cwd=root, capture_output=True, text=True, check=False
    )
    return result.stdout.strip()


def check_git_hook(root: Path | None = None) -> bool:
    """T0 (#1412): a clone without the hook runs no per-commit check at all."""
    root = root or ROOT
    hook = root / HOOKS_DIR / "pre-commit"
    configured = _hooks_path(root)
    print(f"[*] Per-commit hook: core.hooksPath = {configured or '<unset>'}")
    if configured != HOOKS_DIR:
        print(f"    [FAIL] the pre-commit hook is not installed (core.hooksPath must be {HOOKS_DIR}): run make install")
        print(f"    {HOOK_REMEDIATION}")
        return False
    if not (hook.is_file() and os.access(hook, os.X_OK)):
        print(f"    [FAIL] {HOOKS_DIR}/pre-commit is missing or not executable")
        return False
    print("    [OK] staged files are checked on every commit, in every worktree of this clone")
    return True


def install_git_hook(root: Path | None = None) -> bool:
    """Point core.hooksPath at the tracked hook directory. Idempotent; the setting is per clone."""
    root = root or ROOT
    subprocess.run(["git", "config", "core.hooksPath", HOOKS_DIR], cwd=root, check=True)
    return check_git_hook(root)


def probe_port(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, TimeoutError):
        return False


def check_local_services() -> None:
    print("[*] Checking local service availability (informational):")
    docker_bin = shutil.which("docker")
    if docker_bin:
        running = subprocess.run([docker_bin, "info"], capture_output=True, check=False).returncode == 0
        print(f"    [INFO] Docker daemon is {'RUNNING' if running else 'NOT responding'}")
    else:
        print("    [INFO] Docker binary not found")
    print(
        "    [INFO] Local Postgres (5432): OPEN"
        if probe_port("127.0.0.1", 5432)
        else "    [INFO] Local Postgres (5432): closed (start with 'make runtime-up')"
    )
    if probe_port("127.0.0.1", 15432):
        print("    [INFO] Staging Postgres loopback (15432): REACHABLE")
    print(
        "    [INFO] OpenD loopback (11111): OPEN"
        if probe_port("127.0.0.1", 11111)
        else "    [INFO] OpenD loopback (11111): closed (not running or host outside VPS)"
    )


def _remote_health_url(env: str) -> str | None:
    if env not in ("production", "staging"):
        return None
    host = resolve_app_hostname("truealpha", env, base_domain="truealpha.club", custom_prod_domain="truealpha.club")
    return f"https://{host}/api/health"


def check_remote(env: str) -> bool:
    url = _remote_health_url(env)
    if not url:
        print(f"[FAIL] Unknown remote environment: {env}")
        return False

    print(f"=== TrueAlpha Remote Health: {env.upper()} ({url}) ===")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "TrueAlpha-Doctor/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        print(f"❌ Failed to reach {url}: {exc}")
        return False

    print(f"[*] Status: {data.get('status')}")
    print(f"[*] Git SHA: {data.get('git_sha')} | Data Engine SHA: {data.get('data_engine_git_sha')}")
    print(f"[*] Digest: {data.get('data_engine_image_digest')}")

    pointers = data.get("governed_pointers", [])
    print(f"\n[*] Governed Pointers ({len(pointers)}):")
    pointers_ok = True
    for p in pointers:
        age = p.get("age_hours", 0)
        tag = "[STALE]" if age > 36 else "[OK]   "
        pointers_ok = pointers_ok and age <= 36
        print(f"    {tag} {p.get('universe_id'):30} | age {age:5.1f}h (advanced at {p.get('advanced_at')})")

    verdicts = data.get("nightly_verdicts", [])
    print(f"\n[*] Nightly Verdicts ({len(verdicts)}):")
    verdicts_ok = True
    for v in verdicts:
        ok = v.get("ok")
        status_str = "[PASS]" if ok is True else ("[FAIL]" if ok is False else "[SKIP]")
        verdicts_ok = verdicts_ok and ok is not False
        print(f"    {status_str:6} {v.get('check'):35} | {v.get('summary')}")

    overall = pointers_ok and verdicts_ok
    print(
        f"\n✅ Remote {env} environment is healthy."
        if overall
        else f"\n⚠️ Remote {env} environment has alerts or stale pointers."
    )
    return overall


def check_vps(name_filter: str = "truealpha") -> bool:
    host = os.environ.get("VPS_HOST", "")
    if not host:
        print("[-] VPS_HOST environment variable is not set; skipping VPS container inspection.")
        return False
    print(f"=== TrueAlpha VPS Container Truth ({host}) ===")
    try:
        from truealpha_tools import runtime_truth

        print(runtime_truth.render(runtime_truth.fetch_inspect(host, name_filter)))
        return True
    except Exception as exc:
        print(f"❌ Failed to inspect VPS containers: {exc}")
        return False


def check_deploy_provenance(target_ref: str, cwd: str | Path | None = None) -> bool:
    """Assert that the target release ref contains current HEAD commits."""
    print(f"=== TrueAlpha Deploy Provenance Guard ({target_ref}) ===")
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True, check=True
        ).stdout.strip()
        target = subprocess.run(
            ["git", "rev-parse", target_ref], cwd=cwd, capture_output=True, text=True, check=True
        ).stdout.strip()
    except subprocess.CalledProcessError as exc:
        print(f"❌ Failed to resolve git references: {exc}")
        return False
    if subprocess.run(["git", "merge-base", "--is-ancestor", head, target], cwd=cwd, check=False).returncode == 0:
        print(f"✅ Target {target_ref} ({target[:8]}) contains current HEAD ({head[:8]}). Deploy authorized.")
        return True
    print(f"❌ REFUSAL: Target {target_ref} ({target[:8]}) does NOT contain current HEAD ({head[:8]}).")
    print("   Current changes are unmerged or not included in the target release ref.")
    return False


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TrueAlpha Physical & Remote Doctor")
    parser.add_argument("--remote", choices=["production", "staging"])
    parser.add_argument("--vps", action="store_true")
    parser.add_argument("--install-hooks", action="store_true")
    parser.add_argument("--verify-deploy-ref", metavar="REF")
    args = parser.parse_args(argv)

    if args.install_hooks:
        return 0 if install_git_hook() else 1
    if args.verify_deploy_ref:
        return 0 if check_deploy_provenance(args.verify_deploy_ref) else 1
    if args.remote:
        return 0 if check_remote(args.remote) else 1
    if args.vps:
        return 0 if check_vps() else 1

    print("=== TrueAlpha Dev Environment Doctor ===")
    checks = [check_python_version(), check_uv(), check_bun_and_web(), check_git_hook()]
    check_local_services()
    if all(checks):
        print("\n✅ dev_env is healthy and all required dependencies are installed.")
        return 0
    print("\n❌ dev_env has errors. Run 'make bootstrap' or fix the issues above.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
