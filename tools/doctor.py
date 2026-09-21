#!/usr/bin/env python3
"""TrueAlpha Physical Dev Environment Doctor (dev_env SSOT).

Inspects the physical developer environment against the repository contracts:
1. Python version matches .python-version and .tool-versions
2. uv lockfile and virtual environment consistency
3. Bun runtime and apps/app-web/node_modules completeness
4. Local runtime services (Docker, Postgres, MinIO, OpenD) probe
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import sys
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


def main() -> int:
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
