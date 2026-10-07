#!/usr/bin/env python3
"""TrueAlpha Physical Dev Environment Doctor (dev_env SSOT).

Inspects the physical developer environment against the repository contracts:
1. Python version matches .python-version and .tool-versions
2. uv lockfile and virtual environment consistency
3. Bun runtime and apps/app-web/node_modules completeness
4. Local runtime services (Docker, Postgres, MinIO, OpenD) probe
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

ROOT = Path(__file__).resolve().parent.parent


def check_python_version() -> bool:
    pv_file = ROOT / ".python-version"
    tv_file = ROOT / ".tool-versions"

    expected_pv = pv_file.read_text(encoding="utf-8").strip() if pv_file.exists() else None
    actual = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"

    print(f"[*] Python runtime: {actual}")
    if expected_pv and actual != expected_pv:
        print(f"    [FAIL] Python mismatch: expected {expected_pv} (from .python-version), got {actual}")
        return False

    if tv_file.exists():
        tv_content = tv_file.read_text(encoding="utf-8")
        for line in tv_content.splitlines():
            line = line.strip()
            if line.startswith("python "):
                expected_tv = line.split("python ", 1)[1].strip()
                if expected_tv != expected_pv:
                    print(
                        f"    [FAIL] Version file conflict: .tool-versions ({expected_tv}) != .python-version ({expected_pv})"
                    )
                    return False

    print("    [OK] Python version aligned across .python-version and .tool-versions")
    return True


def check_uv() -> bool:
    uv_bin = shutil.which("uv")
    if not uv_bin:
        print("    [FAIL] 'uv' binary not found in PATH")
        return False

    lock = ROOT / "uv.lock"
    if not lock.exists():
        print("    [FAIL] uv.lock does not exist")
        return False

    venv_py = ROOT / ".venv" / "bin" / "python"
    if not venv_py.exists():
        print("    [WARN] .venv does not exist. Run 'make install' or 'uv sync'")
        return False

    print("    [OK] uv installed and virtual environment exists")
    return True


def check_bun_and_web() -> bool:
    bun_bin = shutil.which("bun")
    if not bun_bin:
        print("    [FAIL] 'bun' binary not found in PATH")
        return False

    web_dir = ROOT / "apps" / "app-web"
    node_modules = web_dir / "node_modules"
    if not node_modules.exists():
        print("    [FAIL] apps/app-web/node_modules missing. Run 'make install' or 'cd apps/app-web && bun install'")
        return False

    # Check critical dependencies
    critical_deps = ["pg", "bcryptjs", "@aws-sdk/client-s3", "next", "react"]
    for dep in critical_deps:
        dep_path = node_modules / dep
        if not dep_path.exists():
            print(f"    [FAIL] Missing required dependency in node_modules: {dep}")
            return False

    print("    [OK] Bun runtime and apps/app-web/node_modules complete")
    return True


def probe_port(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, TimeoutError):
        return False


def check_local_services() -> None:
    print("[*] Checking local service availability (informational):")
    # Docker
    docker_bin = shutil.which("docker")
    if docker_bin:
        res = subprocess.run([docker_bin, "info"], capture_output=True)
        if res.returncode == 0:
            print("    [INFO] Docker daemon is RUNNING")
        else:
            print("    [INFO] Docker daemon is NOT responding")
    else:
        print("    [INFO] Docker binary not found")

    # Local Postgres (5432), Staging loopback (15432), Prod loopback (15433)
    if probe_port("127.0.0.1", 5432):
        print("    [INFO] Local Postgres (5432): OPEN")
    else:
        print("    [INFO] Local Postgres (5432): closed (start with 'make runtime-up')")

    if probe_port("127.0.0.1", 15432):
        print("    [INFO] Staging Postgres loopback (15432): REACHABLE")

    # OpenD (11111)
    if probe_port("127.0.0.1", 11111):
        print("    [INFO] OpenD loopback (11111): OPEN")
    else:
        print("    [INFO] OpenD loopback (11111): closed (not running or host outside VPS)")


def check_remote(env: str) -> bool:
    urls = {
        "production": "https://truealpha.club/api/health",
        "staging": "https://truealpha-staging.truealpha.club/api/health",
    }
    url = urls.get(env)
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
        u_id = p.get("universe_id")
        age = p.get("age_hours", 0)
        adv = p.get("advanced_at")
        if age > 36:
            print(f"    [STALE] {u_id:30} | age {age:5.1f}h (advanced at {adv})")
            pointers_ok = False
        else:
            print(f"    [OK]    {u_id:30} | age {age:5.1f}h (advanced at {adv})")

    verdicts = data.get("nightly_verdicts", [])
    print(f"\n[*] Nightly Verdicts ({len(verdicts)}):")
    verdicts_ok = True
    for v in verdicts:
        chk = v.get("check")
        ok = v.get("ok")
        summary = v.get("summary")
        if ok is True:
            status_str = "[PASS]"
        elif ok is False:
            status_str = "[FAIL]"
            verdicts_ok = False
        else:
            status_str = "[SKIP]"
        print(f"    {status_str:6} {chk:35} | {summary}")

    overall = pointers_ok and verdicts_ok
    if overall:
        print(f"\n✅ Remote {env} environment is healthy.")
    else:
        print(f"\n⚠️ Remote {env} environment has alerts or stale pointers.")
    return overall


def check_vps(name_filter: str = "truealpha") -> bool:
    host = os.environ.get("VPS_HOST", "")
    if not host:
        print("[-] VPS_HOST environment variable is not set; skipping VPS container inspection.")
        return False
    print(f"=== TrueAlpha VPS Container Truth ({host}) ===")
    try:
        try:
            import runtime_truth
        except ImportError:
            from tools import runtime_truth  # type: ignore[no-redef]

        containers = runtime_truth.fetch_inspect(host, name_filter)
        print(runtime_truth.render(containers))
        return True
    except Exception as exc:
        print(f"❌ Failed to inspect VPS containers: {exc}")
        return False


def check_deploy_provenance(target_ref: str) -> bool:
    """Assert that the target release ref contains current HEAD commits.

    Prevents deploying an outdated release or deploying from an unmerged branch.
    """
    print(f"=== TrueAlpha Deploy Provenance Guard ({target_ref}) ===")
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        target = subprocess.run(
            ["git", "rev-parse", target_ref], capture_output=True, text=True, check=True
        ).stdout.strip()
    except subprocess.CalledProcessError as exc:
        print(f"❌ Failed to resolve git references: {exc}")
        return False

    is_ancestor = (
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", head, target],
            check=False,
        ).returncode
        == 0
    )

    if is_ancestor:
        print(f"✅ Target {target_ref} ({target[:8]}) contains current HEAD ({head[:8]}). Deploy authorized.")
        return True
    else:
        print(f"❌ REFUSAL: Target {target_ref} ({target[:8]}) does NOT contain current HEAD ({head[:8]}).")
        print("   Current changes are unmerged or not included in the target release ref.")
        return False


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TrueAlpha Physical & Remote Doctor")
    parser.add_argument(
        "--remote",
        choices=["production", "staging"],
        help="Inspect deployed remote health, pointers, and nightly verdicts",
    )
    parser.add_argument(
        "--vps",
        action="store_true",
        help="Inspect container reality on VPS via SSH",
    )
    parser.add_argument(
        "--verify-deploy-ref",
        metavar="REF",
        help="Verify that target release ref/tag contains current HEAD before deploy",
    )
    args = parser.parse_args(argv)

    if args.verify_deploy_ref:
        return 0 if check_deploy_provenance(args.verify_deploy_ref) else 1
    if args.remote:
        return 0 if check_remote(args.remote) else 1
    if args.vps:
        return 0 if check_vps() else 1

    print("=== TrueAlpha Dev Environment Doctor ===")
    ok = True
    ok = check_python_version() and ok
    ok = check_uv() and ok
    ok = check_bun_and_web() and ok
    check_local_services()

    if ok:
        print("\n✅ dev_env is healthy and all required dependencies are installed.")
        return 0
    else:
        print("\n❌ dev_env has errors. Run 'make bootstrap' or fix the issues above.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
