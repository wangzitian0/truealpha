"""Verify /api/health readiness, app version, and data_engine_git_sha (#1647 F3)."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from infra2_sdk.deploy import default_http_get

HttpGet = Callable[[str], tuple[int, str]]
VERSION_KEYS = ("git_sha", "version")


@dataclass(frozen=True)
class HealthProbeResult:
    status_code: int
    attempts: int
    version: str
    data_engine_git_sha: str


def _matches_prefix(actual: str, expected: str) -> bool:
    return bool(actual) and (actual.startswith(expected) or expected.startswith(actual))


def _first_version(payload: dict[str, Any]) -> str:
    for key in VERSION_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _validate_payload(
    status_code: int,
    body: str,
    *,
    expected_version: str,
    expect_data_engine_version: str,
    require_status: str | None,
) -> tuple[str, str] | str:
    if status_code != 200:
        return f"HTTP {status_code}: {body[:160]}"
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        return f"non-JSON response: {body[:160]!r}"
    if not isinstance(payload, dict):
        return f"JSON response is not an object: {body[:160]!r}"
    if require_status is not None:
        actual_status = payload.get("status")
        if actual_status != require_status:
            return f"status={actual_status!r}, expected {require_status!r}"
    app_version = _first_version(payload)
    if expected_version:
        if not app_version or app_version == "unknown" or not _matches_prefix(app_version, expected_version):
            return f"version={app_version!r}, expected {expected_version!r}"
    raw_engine = payload.get("data_engine_git_sha")
    engine_version = raw_engine if isinstance(raw_engine, str) else ""
    if expect_data_engine_version:
        if (
            not engine_version
            or engine_version == "unknown"
            or not _matches_prefix(engine_version, expect_data_engine_version)
        ):
            return f"data_engine_git_sha={raw_engine!r}, expected {expect_data_engine_version!r}"
    return app_version, engine_version


def poll_health(
    url: str,
    *,
    http_get: HttpGet,
    expected_version: str = "",
    expect_data_engine_version: str = "",
    require_status: str | None = None,
    max_attempts: int = 24,
    interval_seconds: float = 10.0,
    sleep: Callable[[float], None] = time.sleep,
) -> HealthProbeResult:
    """Poll health endpoint until status, app version, and data_engine_git_sha match."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    last_error = "no attempts executed"
    for attempt in range(1, max_attempts + 1):
        status_code, body = http_get(url)
        verdict = _validate_payload(
            status_code,
            body,
            expected_version=expected_version,
            expect_data_engine_version=expect_data_engine_version,
            require_status=require_status,
        )
        if isinstance(verdict, tuple):
            return HealthProbeResult(
                status_code=status_code,
                attempts=attempt,
                version=verdict[0],
                data_engine_git_sha=verdict[1],
            )
        last_error = verdict
        if attempt < max_attempts:
            sleep(interval_seconds)
    raise RuntimeError(f"{url} failed health probe after {max_attempts} attempt(s): {last_error}")


def main(
    argv: Sequence[str] | None = None,
    *,
    http_get: HttpGet | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """CLI entrypoint compatible with infra2_sdk.deploy plus data_engine_git_sha checks."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--expected-version", default="")
    parser.add_argument("--expect-data-engine-version", default="")
    parser.add_argument("--require-status", default=None)
    parser.add_argument("--max-attempts", type=int, default=24)
    parser.add_argument("--interval-seconds", type=float, default=10.0)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--no-follow-redirects", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)
    getter = http_get or default_http_get(timeout=args.timeout, follow_redirects=not args.no_follow_redirects)
    try:
        res = poll_health(
            args.url,
            http_get=getter,
            expected_version=args.expected_version,
            expect_data_engine_version=args.expect_data_engine_version,
            require_status=args.require_status,
            max_attempts=args.max_attempts,
            interval_seconds=args.interval_seconds,
            sleep=sleep,
        )
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(
        f"healthy after {res.attempts} attempt(s): HTTP {res.status_code} "
        f"(version={res.version or '-'}, data_engine_git_sha={res.data_engine_git_sha or '-'})"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
