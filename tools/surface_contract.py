"""Assert the deployed surface's shape against the REAL proxy — A4 C1 (#673).

One HTTP redirect cost four production releases (v0.0.26, 28, 30, 32) because
every defect in it was observable only after a deploy:

    v0.0.26  flags in the Dockerfile CMD     infra2's compose overrides CMD — never ran
    v0.0.28  FastAPI root_path               Location fixed; routing broken; endpoint 404
    v0.0.30  path-only Location, GET only    TLS and prefix right; slashless POST 405
    v0.0.32  all methods                     four properties hold at once

`apps/llm-service/tests/test_health.py` pins those properties — against a
TestClient with a simulated `X-Forwarded-Proto` header. That is the whole gap:
the failures were all in the difference between the simulated proxy and the
real one. Traefik strips `/api` before forwarding, Compose clears the image's
CMD, and neither is visible to a TestClient. So this asserts the same
properties over real HTTPS against a deployed base URL, and nothing it checks
can be satisfied by a local approximation.

Read-only and side-effect free: the POST carries no body and no session, so the
endpoint rejects it — the assertion is about WHICH rejection (405 means the
method hole is back), never about doing anything.

Measured baseline, production, 2026-09-01:

    GET  /api/mcp     307 -> https://truealpha.club/api/mcp/
    POST /api/mcp     307 -> same (a 405 here is the v0.0.30 defect)
    GET  /api/mcp/    406 (the MCP endpoint refusing a request with no Accept
                           header — a 404 here means it landed on app-web,
                           which is the v0.0.28 defect)
    GET  /api/health  200 (a precondition only — tools/health_check.py owns
                           "is the EXPECTED release live", and this must not
                           duplicate it)

Usage:
    python tools/surface_contract.py https://truealpha.club
"""

from __future__ import annotations

import argparse
import sys
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import urlparse

TIMEOUT = 25.0
# The edge rejects urllib's default `Python-urllib/3.x` with 403 — measured
# against production, where curl gets 200 for the same URL. Without this the
# daily probe would report the surface down every single day, and a check that
# cries wolf is worse than no check.
USER_AGENT = "truealpha-surface-contract (+https://github.com/wangzitian0/truealpha)"


@dataclass(frozen=True)
class Response:
    status: int
    location: str
    body: bytes


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """The redirect IS the subject — following it would discard the evidence."""

    def redirect_request(self, *arguments: object, **keywords: object) -> None:
        return None


def fetch(url: str, method: str = "GET") -> Response:
    opener = urllib.request.build_opener(_NoRedirect)
    # noqa S310: the scheme is validated in main() to be http or https before
    # anything is fetched. Both are supported on purpose — the stub-server
    # tests speak http, and a preview target may too.
    request = urllib.request.Request(  # noqa: S310
        url, method=method, headers={"User-Agent": USER_AGENT}
    )
    try:
        with opener.open(request, timeout=TIMEOUT) as answer:
            return Response(answer.status, answer.headers.get("Location", ""), answer.read())
    except urllib.error.HTTPError as error:
        # 4xx/5xx are results here, not failures: which rejection arrives is
        # exactly what distinguishes a healthy endpoint from a regressed one.
        return Response(error.code, error.headers.get("Location", ""), error.read())
    except urllib.error.URLError as error:
        raise SystemExit(f"surface_contract: {method} {url} did not answer: {error.reason}") from None


def judge_redirect(source_url: str, base_scheme: str, status: int, location: str) -> list[str]:
    """The redirect verdict, as pure logic.

    Separated from the request so the TLS-downgrade branch is reachable by a
    test: a stub server speaks http, and binding real TLS in a unit test would
    prove nothing about Traefik anyway. Judging that branch by reading the
    source for a substring — which is what the first draft did — leaves it
    passing while an `if ... : pass` silently stops detecting the founding
    defect (review).
    """
    if not 300 <= status < 400:
        return [f"GET {source_url} answered {status}, not a redirect"]
    target = urlparse(location)
    failures: list[str] = []
    if base_scheme == "https" and target.scheme == "http":
        failures.append(
            f"GET {source_url} redirects to {location} — the scheme is downgraded to "
            f"http, and a client follows it (init.md principle 21; the v0.0.26 defect)"
        )
    if not target.path.startswith("/api/"):
        failures.append(
            f"GET {source_url} redirects to {location} — the /api prefix is dropped, "
            f"so the client lands on app-web instead of the MCP endpoint (v0.0.26)"
        )
    return failures


def check(base: str) -> list[str]:
    """Every violated property, named. Empty means the surface holds.

    Deliberately NOT a version check. `tools/health_check.py` owns "is the
    expected release live" — it polls, it takes an expected version, and
    deploy-freshness already runs it per environment. This owns a different
    question that nothing else asks: does the proxy topology behave. The one
    health call below is a precondition, not a duplicate assertion: if the app
    is not serving at all, the three routing verdicts would be noise.
    """
    base = base.rstrip("/")
    scheme = urlparse(base).scheme
    failures: list[str] = []

    alive = fetch(f"{base}/api/health")
    if alive.status != 200:
        return [
            f"GET {base}/api/health answered {alive.status}, not 200 — the surface is not serving, "
            f"so its routing shape cannot be judged (is the deploy live? tools/health_check.py)"
        ]

    slashless = f"{base}/api/mcp"
    redirect = fetch(slashless)
    failures.extend(judge_redirect(slashless, scheme, redirect.status, redirect.location))

    post = fetch(slashless, method="POST")
    if post.status == 405:
        failures.append(
            f"POST {slashless} answered 405 — the redirect handler covers GET only again, which "
            f"is exactly the v0.0.30 defect (staging 405 while production stayed 200)"
        )

    endpoint = f"{base}/api/mcp/"
    served = fetch(endpoint)
    if served.status == 404:
        failures.append(
            f"GET {endpoint} answered 404 — the redirect points at nothing, which is worse than "
            f"the downgrade it replaced (the v0.0.28 root_path defect)"
        )

    return failures


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Assert a deployed surface's shape over real HTTPS.")
    parser.add_argument("base_url", help="e.g. https://truealpha.club")
    arguments = parser.parse_args(list(sys.argv[1:] if argv is None else argv))

    if urlparse(arguments.base_url).scheme not in {"http", "https"}:
        print(f"surface_contract: {arguments.base_url} is not an http(s) URL", file=sys.stderr)
        return 2

    failures = check(arguments.base_url)
    for failure in failures:
        print(f"::error::{failure}", file=sys.stderr)
    if failures:
        plural = "properties" if len(failures) > 1 else "property"
        print(f"surface_contract: {len(failures)} {plural} violated on {arguments.base_url}", file=sys.stderr)
        return 1
    print(f"surface_contract: the deployed surface holds on {arguments.base_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
