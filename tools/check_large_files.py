"""Audit tracked repository files against size and line-count invariants.

Rules:
- A tracked file must not exceed 1.0 MB without an entry in tools/large_file_allowlist.json.
- A tracked source file (*.py, *.ts, *.tsx) must not exceed 2,000 lines without approval.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

MAX_FILE_BYTES = 1024 * 1024  # 1 MB
MAX_SOURCE_LINES = 2000
ALLOWLIST_PATH = Path(__file__).parent / "large_file_allowlist.json"
REPO_ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    if not ALLOWLIST_PATH.is_file():
        print(f"ERROR: allowlist not found at {ALLOWLIST_PATH}", file=sys.stderr)
        return 2

    with open(ALLOWLIST_PATH, encoding="utf-8") as f:
        allowlist = set(json.load(f).get("exempt_paths", []))

    try:
        tracked_files = subprocess.check_output(["git", "ls-files"], cwd=REPO_ROOT, text=True).splitlines()
    except subprocess.CalledProcessError as exc:
        print(f"ERROR: failed to list git files: {exc}", file=sys.stderr)
        return 2

    violations: list[str] = []
    for rel_path in tracked_files:
        if rel_path in allowlist:
            continue
        full_path = REPO_ROOT / rel_path
        if not full_path.is_file():
            continue

        size = full_path.stat().st_size
        if size > MAX_FILE_BYTES:
            violations.append(f"[SIZE] {rel_path}: {size / 1024 / 1024:.2f} MB exceeds 1.0 MB limit")

        if rel_path.endswith((".py", ".ts", ".tsx")):
            try:
                with open(full_path, "rb") as fp:
                    lines = sum(1 for _ in fp)
                if lines > MAX_SOURCE_LINES:
                    violations.append(f"[LINES] {rel_path}: {lines} lines exceeds {MAX_SOURCE_LINES} line limit")
            except OSError as err:
                violations.append(f"[READ_ERROR] {rel_path}: {err}")

    if violations:
        print("ERROR: Unapproved large files detected in git tracking:", file=sys.stderr)
        for violation in violations:
            print(f"  - {violation}", file=sys.stderr)
        return 1

    print(f"OK: all tracked files comply with size invariants ({len(allowlist)} exemptions active).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
