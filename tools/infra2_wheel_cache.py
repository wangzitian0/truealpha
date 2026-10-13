#!/usr/bin/env python3
"""Cache and verify the infra2-sdk wheel to eliminate CI 504 flakes (Issue #1248).

GitHub Releases intermittently return HTTP 504 Gateway Timeout when downloading
release assets (e.g. infra2-sdk wheels). This tool ensures that:
1. When the wheel exists in the local cache, its sha256 is verified against uv.lock.
   If valid, no network call is made and uv uses the cached wheel via UV_FIND_LINKS.
2. A mismatched or corrupted wheel in cache fails the job immediately.
3. If not cached, the wheel is fetched with exponential backoff retries and verified
   against the sha256 pinned in uv.lock before being written to the cache.

Acceptance criteria:
- A CI job that cannot reach the release URL still installs the wheel from the cache
  when the sha256 matches.
- A wrong sha256 still fails the job.

Stdlib-only: runs under any system python3 without dependencies.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOCK_FILE = REPO_ROOT / "uv.lock"
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "infra2-sdk"


def extract_infra2_wheel_spec(lock_path: Path) -> tuple[str, str, str]:
    """Parse uv.lock to find infra2-sdk wheel filename, url, and expected sha256.

    Returns:
        (filename, url, expected_sha256)
    """
    if not lock_path.exists():
        raise FileNotFoundError(f"uv.lock not found at {lock_path}")

    text = lock_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    in_package = False
    in_infra2 = False
    wheel_url = ""
    wheel_hash = ""

    for line in lines:
        stripped = line.strip()
        if stripped == "[[package]]":
            in_package = True
            in_infra2 = False
            continue
        if in_package and stripped == 'name = "infra2-sdk"':
            in_infra2 = True
            continue
        if in_infra2:
            if stripped.startswith("[[package]]"):
                break
            if 'url = "https://' in stripped and "infra2_sdk" in stripped:
                start = stripped.find('url = "') + len('url = "')
                end = stripped.find('"', start)
                wheel_url = stripped[start:end]
            if 'hash = "sha256:' in stripped:
                start = stripped.find('hash = "sha256:') + len('hash = "sha256:')
                end = stripped.find('"', start)
                wheel_hash = stripped[start:end]

    if not wheel_url or not wheel_hash:
        raise ValueError(f"Could not locate infra2-sdk wheel url and sha256 in {lock_path}")

    filename = wheel_url.split("/")[-1]
    return filename, wheel_url, wheel_hash


def compute_sha256(file_path: Path) -> str:
    """Compute sha256 hex digest of a file."""
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


def ensure_wheel(
    *,
    lock_path: Path = DEFAULT_LOCK_FILE,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    verify_only: bool = False,
    offline: bool = False,
    max_retries: int = 5,
    initial_backoff: float = 1.0,
) -> int:
    """Ensure the infra2-sdk wheel exists in cache_dir with verified sha256."""
    filename, url, expected_hash = extract_infra2_wheel_spec(lock_path)
    cache_dir.mkdir(parents=True, exist_ok=True)
    wheel_path = cache_dir / filename

    if wheel_path.exists():
        actual_hash = compute_sha256(wheel_path)
        if actual_hash == expected_hash:
            print(f"infra2-sdk wheel cache hit (sha256 verified: {actual_hash[:16]}...)")
            return 0
        else:
            print(
                f"::error::infra2-sdk cached wheel sha256 mismatch! Expected {expected_hash}, got {actual_hash}",
                file=sys.stderr,
            )
            return 1

    if verify_only:
        print(f"::error::infra2-sdk wheel {filename} not present in cache {cache_dir}", file=sys.stderr)
        return 1

    if offline:
        print(
            f"::error::Network access disabled and infra2-sdk wheel {filename} not in cache {cache_dir}",
            file=sys.stderr,
        )
        return 1

    # Fetch with exponential backoff
    backoff = initial_backoff
    last_error: Exception | None = None

    for attempt in range(1, max_retries + 1):
        print(f"Downloading infra2-sdk wheel from {url} (attempt {attempt}/{max_retries})...")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "TrueAlpha-CI/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()

            actual_hash = hashlib.sha256(data).hexdigest()
            if actual_hash != expected_hash:
                print(
                    f"::error::Downloaded wheel sha256 mismatch! Expected {expected_hash}, got {actual_hash}",
                    file=sys.stderr,
                )
                return 1

            # Write atomically to cache
            tmp_path = cache_dir / f"{filename}.tmp.{os.getpid()}"
            tmp_path.write_bytes(data)
            tmp_path.rename(wheel_path)
            print(f"Successfully downloaded and cached {filename} (sha256: {actual_hash[:16]}...)")
            return 0
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            last_error = exc
            print(f"Download attempt {attempt} failed: {exc}", file=sys.stderr)
            if attempt < max_retries:
                time.sleep(backoff)
                backoff *= 2.0

    print(f"::error::Failed to download infra2-sdk wheel after {max_retries} attempts: {last_error}", file=sys.stderr)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Cache and verify infra2-sdk wheel against uv.lock sha256")
    parser.add_argument("--lock-file", type=Path, default=DEFAULT_LOCK_FILE, help="Path to uv.lock")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR, help="Directory for wheel cache")
    parser.add_argument("--verify-only", action="store_true", help="Fail if wheel is not already cached and verified")
    parser.add_argument("--offline", action="store_true", help="Disable network downloads")
    parser.add_argument("--max-retries", type=int, default=5, help="Maximum download retry attempts")

    args = parser.parse_args()
    return ensure_wheel(
        lock_path=args.lock_file,
        cache_dir=args.cache_dir,
        verify_only=args.verify_only,
        offline=args.offline,
        max_retries=args.max_retries,
    )


if __name__ == "__main__":
    sys.exit(main())
