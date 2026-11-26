"""Read-only post-deploy production surface check (stdlib only, no secrets)."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence

HttpGet = Callable[[str], tuple[int, str]]


def default_http_get(*, timeout: float = 15.0) -> HttpGet:
    """Return a stdlib GET helper that follows redirects and returns (status, body)."""

    def _get(url: str) -> tuple[int, str]:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"unsupported URL scheme: {parsed.scheme!r}")
        req = urllib.request.Request(
            url,
            method="GET",
            headers={"Accept": "application/json, text/html", "User-Agent": "truealpha-ops-audit/1.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                return int(resp.status), resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            return int(exc.code), exc.read().decode("utf-8", errors="replace")
        except urllib.error.URLError as exc:
            return 0, str(exc.reason)

    return _get


def _matches_version_ref(actual: str, expected: str) -> bool:
    if not actual or actual == "unknown":
        return False
    if expected.startswith("v"):
        return actual == expected
    return actual.startswith(expected) or expected.startswith(actual)


def verify_prod_surface(
    app_url: str,
    *,
    version_ref: str = "",
    http_get: HttpGet | None = None,
) -> list[str]:
    """Verify /api/health metadata and Next.js SSR / and /login endpoints."""
    base = app_url.strip().rstrip("/")
    if not base:
        return ["APP_URL is required"]
    getter = http_get or default_http_get()
    expected = version_ref.strip()
    problems: list[str] = []

    health_url = f"{base}/api/health"
    status, body = getter(health_url)
    if status != 200:
        problems.append(f"{health_url} returned HTTP {status}")
    else:
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            problems.append(f"{health_url} returned invalid JSON: {exc}")
            payload = None
        if payload is not None and not isinstance(payload, dict):
            problems.append(f"{health_url} did not return a JSON object")
            payload = None
        if payload is not None:
            if expected and expected != "local":
                candidates = [
                    v
                    for key in ("git_sha", "version")
                    if isinstance(v := payload.get(key), str) and v.strip() and v.strip() != "unknown"
                ]
                if not candidates or not any(_matches_version_ref(v.strip(), expected) for v in candidates):
                    problems.append(
                        f"{health_url} version/git_sha ({payload.get('version')!r}/{payload.get('git_sha')!r}) "
                        f"does not match APP_VERSION_REF={expected!r}"
                    )

            de_sha = payload.get("data_engine_git_sha")
            if not isinstance(de_sha, str) or not de_sha.strip() or de_sha.strip() == "unknown":
                problems.append(f"{health_url} missing or unknown data_engine_git_sha: {de_sha!r}")
            elif expected.startswith("v") and de_sha.strip() != expected:
                problems.append(
                    f"{health_url} data_engine_git_sha={de_sha!r} does not match APP_VERSION_REF={expected!r}"
                )

            pointers = payload.get("governed_pointers")
            if not isinstance(pointers, list) or len(pointers) == 0:
                problems.append(f"{health_url} governed_pointers must be a non-empty list, got {pointers!r}")

    for path in ("/", "/login"):
        url = f"{base}{path}"
        page_status, _ = getter(url)
        if page_status != 200:
            problems.append(f"{url} returned HTTP {page_status}")

    return problems


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-url", default=os.environ.get("APP_URL", ""))
    parser.add_argument("--version-ref", default=os.environ.get("APP_VERSION_REF", ""))
    args = parser.parse_args(list(argv) if argv is not None else None)
    problems = verify_prod_surface(args.app_url, version_ref=args.version_ref)
    if problems:
        for problem in problems:
            print(f"prod_surface check failed: {problem}", file=sys.stderr)
        return 1
    print(f"prod_surface: ok ({args.app_url.rstrip('/')})")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
