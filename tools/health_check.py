"""Poll a deployed TrueAlpha URL until the target release is actually live.

The generic polling algorithm (HTTP-200 + status-field check, the version/
git_sha stable-mismatch budget) is infra2_sdk.deploy_health's responsibility
(#508); this wrapper only supplies TrueAlpha's own health endpoint and its
{"status": "ok"} convention -- llm-service's /health, reached through
Traefik's /api prefix route (tools/route_manifest.json).

Usage:
  python tools/health_check.py <url> [expected_version] [max_attempts]

Exit codes:
  0 - Health check passed
  1 - Health check failed (connection error, HTTP error, or version never matched)
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable, Sequence

from infra2_sdk.deploy_health import HttpGet, default_http_get, poll_until_healthy

DEFAULT_MAX_ATTEMPTS = 24
INTERVAL_SECONDS = 10.0


def check_health(
    url: str,
    *,
    expected_version: str = "",
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    http_get: HttpGet | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Poll url until healthy; print the outcome; return a shell exit code."""
    http_get = http_get or default_http_get()
    try:
        result = poll_until_healthy(
            url,
            http_get=http_get,
            expected_version=expected_version,
            require_status="ok",
            max_attempts=max_attempts,
            interval_seconds=INTERVAL_SECONDS,
            sleep=sleep,
        )
    except RuntimeError as exc:
        print(f"health check failed: {exc}", file=sys.stderr)
        return 1
    print(f"health check passed: {url} is healthy ({result.body})")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("expected_version", nargs="?", default="")
    parser.add_argument("max_attempts", nargs="?", type=int, default=DEFAULT_MAX_ATTEMPTS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return check_health(
        args.url,
        expected_version=args.expected_version,
        max_attempts=args.max_attempts,
    )


if __name__ == "__main__":
    raise SystemExit(main())
