"""Stdlib-only health endpoint poller for truealpha runtime hooks.

Sets an explicit ``User-Agent`` header so Cloudflare WAF does not reject
``Python-urllib`` requests with HTTP 403 (fixed upstream in ``infra2-sdk``
post-v4.11.2), and avoids cold ``uv sync`` overhead inside the 120s
``staging.operation`` smoke budget.
"""

from __future__ import annotations

import argparse
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

_USER_AGENT = "infra2-sdk/4.11.2 (truealpha-health-poll)"
_HttpGet = Callable[[str, float], tuple[int, bytes]]


@dataclass(frozen=True)
class HealthPollResult:
    ok: bool
    attempts: int
    status_code: int | None = None
    observed_version: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, hdrs: Any, newurl: str) -> None:
        return None


def _validate_http_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"poll url must be a valid http(s) URL, got {url!r}")
    return url.strip()


def _default_http_get(url: str, timeout_seconds: float, *, follow_redirects: bool = False) -> tuple[int, bytes]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "application/json",
        },
        method="GET",
    )
    opener = urllib.request.build_opener() if follow_redirects else urllib.request.build_opener(_NoRedirectHandler())
    try:
        with opener.open(req, timeout=timeout_seconds) as resp:
            return (int(resp.status), resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read() if hasattr(exc, "read") else b""
        return (int(exc.code), body)


def _version_matches(expected: str, candidates: Sequence[str]) -> bool:
    exp = expected.strip()
    if not exp:
        return True
    for raw in candidates:
        cand = raw.strip()
        if not cand:
            continue
        if cand == exp:
            return True
        if len(exp) >= 7 and len(cand) >= 7 and (cand.startswith(exp) or exp.startswith(cand)):
            return True
    return False


def poll_until_healthy(
    url: str,
    *,
    expected_version: str | None = None,
    timeout_seconds: float = 60.0,
    request_timeout_seconds: float = 5.0,
    interval_seconds: float = 2.0,
    follow_redirects: bool = False,
    http_get: _HttpGet | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> HealthPollResult:
    target_url = _validate_http_url(url)
    for label, val in (
        ("timeout_seconds", timeout_seconds),
        ("request_timeout_seconds", request_timeout_seconds),
        ("interval_seconds", interval_seconds),
    ):
        if not math.isfinite(val) or val <= 0:
            raise ValueError(f"{label} must be positive")

    getter: _HttpGet = (
        (lambda u, t: _default_http_get(u, t, follow_redirects=follow_redirects)) if http_get is None else http_get
    )
    deadline = monotonic() + timeout_seconds
    attempts = 0
    last_status: int | None = None
    last_version = ""
    last_detail = ""

    while True:
        attempts += 1
        try:
            status_code, body = getter(target_url, request_timeout_seconds)
            last_status = status_code
            payload: Mapping[str, Any] = {}
            if body:
                try:
                    parsed = json.loads(body.decode("utf-8", errors="replace"))
                    if isinstance(parsed, Mapping):
                        payload = parsed
                except json.JSONDecodeError:
                    payload = {}
            version_candidates = [
                str(payload.get(k) or "").strip()
                for k in ("service_version", "version", "git_sha")
                if str(payload.get(k) or "").strip()
            ]
            last_version = version_candidates[0] if version_candidates else ""
            if status_code == 200:
                if not expected_version or _version_matches(expected_version, version_candidates):
                    return HealthPollResult(
                        ok=True,
                        attempts=attempts,
                        status_code=status_code,
                        observed_version=last_version,
                        detail="ok",
                    )
                last_detail = f"version mismatch: expected {expected_version!r}, got {version_candidates!r}"
            else:
                last_detail = f"HTTP {status_code}"
        except Exception as exc:  # noqa: BLE001
            last_status = None
            last_detail = f"{type(exc).__name__}: {exc}"

        remaining = deadline - monotonic()
        if remaining <= 0:
            return HealthPollResult(
                ok=False,
                attempts=attempts,
                status_code=last_status,
                observed_version=last_version,
                detail=last_detail or "health poll timed out",
            )
        sleep(min(interval_seconds, remaining))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 tools/health_poll.py")
    parser.add_argument("--url", required=True)
    parser.add_argument("--expected-version")
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    parser.add_argument("--request-timeout-seconds", type=float, default=5.0)
    parser.add_argument("--interval-seconds", type=float, default=5.0)
    parser.add_argument("--follow-redirects", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)

    res = poll_until_healthy(
        args.url,
        expected_version=args.expected_version,
        timeout_seconds=args.timeout_seconds,
        request_timeout_seconds=args.request_timeout_seconds,
        interval_seconds=args.interval_seconds,
        follow_redirects=args.follow_redirects,
    )
    print(json.dumps(res.to_dict(), sort_keys=True))
    return 0 if res.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
