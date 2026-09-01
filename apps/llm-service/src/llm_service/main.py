"""LLM-call orchestration only — the App reads Postgres directly, not through here
(init.md Section 1, rule 5).

Roadmap: the MCP endpoint comes first (reuses libs/factors, nearly free to wire into
Claude Desktop); the self-built /chat SSE endpoint is Tier 3 (Phase 7).
"""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from llm_service.mcp_server import mcp


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    # FastMCP's own Starlette sub-app lifespan is not invoked by FastAPI's Mount,
    # so the session manager must run from the parent app's lifespan explicitly.
    async with mcp.session_manager.run():
        yield


#: The prefix infra2's Traefik rule strips before forwarding here. Verified from
#: behaviour: a POST to /api/mcp/ reaches an app mounted at /mcp, so the prefix
#: is already gone on arrival. `tools/route_manifest.json` is the contract (#463).
#:
#: Used ONLY to rebuild outgoing URLs. It is deliberately NOT FastAPI's
#: `root_path`: that makes Starlette strip the prefix during matching, and since
#: Traefik has already stripped it the app then looks for /api/mcp in a request
#: that says /mcp and answers 404. Measured — v0.0.28 fixed the redirect and
#: killed the endpoint, staging POST /api/mcp/ went 200 -> 404 while production
#: without the change stayed 200.
ROUTED_PREFIX = "/api"

app = FastAPI(title="truealpha-llm-service", lifespan=_lifespan)

# TLS terminates at Traefik, so requests arrive over http. Without this, Starlette
# builds redirect Locations from the request scheme and `GET /api/mcp` answered
#     307 -> http://truealpha.club/mcp/
# on production: TLS dropped and the prefix dropped in one hop, landing on
# app-web's 404. init.md principle 21 requires TLS on every non-local MCP
# endpoint, and a client follows a 307.
#
# In the APP, not in the Dockerfile CMD. A first attempt added --proxy-headers
# and --root-path to that CMD, merged, deployed to staging, and changed nothing:
# infra2's compose sets an inline entrypoint ending in `exec uvicorn ... --host
# 0.0.0.0 --port 8000`, so the image's CMD has never run. Middleware ships with
# the code and does not depend on how the process is launched.
#
# Trust the proxy network, not everyone. `trusted_hosts="*"` would let any peer
# that can reach this port set the scheme and host used to build redirects, and
# "it is not reachable from outside" is an assumption about networking rather
# than something this process can check (review).
#
# Measured on the VPS: llm-service sits on dokploy-network at 10.0.1.249 and
# Traefik at 10.0.1.76 on the same network. Overridable so a preview or a
# different topology declares its own, and a wrong value fails closed — the
# headers are ignored and the redirect degrades to the pre-fix behaviour rather
# than trusting a stranger.
TRUSTED_PROXIES = os.environ.get("TRUSTED_PROXY_HOSTS", "10.0.1.0/24")
app.add_middleware(
    ProxyHeadersMiddleware,
    trusted_hosts=[host.strip() for host in TRUSTED_PROXIES.split(",") if host.strip()],
)


# Every method the MCP transport uses, not GET alone. Streamable-HTTP POSTs the
# JSON-RPC body, GETs the SSE stream and DELETEs the session, and a client that
# omits the trailing slash must reach all three.
#
# Measured on staging: with GET only, `POST /api/mcp` answered 405. The code
# this replaced answered 307 — to http, which is the defect — so restricting the
# handler to GET turned "works but downgrades" into "does not work". A narrower
# fix than the bug.
@app.api_route("/mcp", methods=["GET", "POST", "DELETE"], include_in_schema=False)
def mcp_slash(request: Request) -> RedirectResponse:
    """Send /mcp to /mcp/ ourselves, with the prefix and the scheme intact.

    Starlette's own redirect_slashes keeps neither: its Location is built from
    the request, which arrives over http with the prefix already stripped, so it
    answered http://truealpha.club/mcp/ — TLS dropped and /api dropped in one
    hop, landing on app-web's 404. init.md principle 21 requires TLS on every
    non-local MCP endpoint and a client follows a 307.

    Built by hand rather than through root_path, because root_path also changes
    ROUTING and killed the endpoint it was meant to fix.

    Path-only, so there is no host to get wrong. An absolute Location built from
    `request.url.netloc` lets the Host header — or a forwarded one — choose where
    the client is sent, and there is no allowlist here to stop it: a host-header
    injection and an open redirect (review).

    A relative Location also cannot drop TLS, since the client keeps the scheme
    it already had. That is the property this whole change exists for, obtained
    by not naming a scheme at all rather than by naming the right one.
    """
    del request  # the Location is fixed; nothing about the request may steer it
    return RedirectResponse(f"{ROUTED_PREFIX}/mcp/", status_code=307)


app.mount("/mcp", mcp.streamable_http_app())


@app.get("/health")
def health() -> dict[str, str]:
    # git_sha lets tools/health_check.py confirm the deployed release tag is
    # actually live post-deploy, not just that something answers (#508).
    #
    # data_engine_parser reports the vintage of the DATA ENGINE, which has no HTTP
    # surface of its own and therefore had no deploy-time verification at all (#712).
    # Every post-deploy check -- this endpoint, the surface walk, both canaries --
    # exercised an HTTP surface, so a promotion could leave the app that computes every
    # published number one release behind with every step green. That is exactly what
    # v0.0.37 did: web and llm took the tag, the data engine kept an older digest.
    #
    # The identity is already in the database; nothing new is written. Reported here
    # because this service already holds a mart-scoped connection, so the deploy lane can
    # read it over a surface it already calls, with no new secret and no database access
    # from the runner.
    return {
        "status": "ok",
        "git_sha": os.environ.get("GIT_COMMIT_SHA", "unknown"),
        "data_engine_parser": _data_engine_parser(),
    }


def _data_engine_parser() -> str:
    """The parser vintage behind the newest observation, or "unknown".

    Deliberately never raises and never fails the health check: this endpoint answers
    "is the service up", and turning it into a database liveness probe would make an
    unrelated outage look like a dead app. An unreadable identity reports "unknown",
    which `tools/health_check.py` treats as un-assertable rather than as a pass.
    """
    try:
        import psycopg
        from truealpha_runtime import runtime_settings

        with psycopg.connect(runtime_settings.database_url, connect_timeout=3) as connection:
            row = connection.execute(
                "select parser_version from staging.capture_normalized_observations order by recorded_at desc limit 1"
            ).fetchone()
        return str(row[0]) if row and row[0] else "unknown"
    except Exception:  # noqa: BLE001 - health must not fail on a read it only reports
        return "unknown"
