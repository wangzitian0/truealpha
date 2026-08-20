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
#: behaviour, not assumed: a POST to /api/mcp/ reaches an app mounted at /mcp, so
#: the prefix is already gone on arrival. Declared so every URL this app
#: generates carries it back. `tools/route_manifest.json` is the contract (#463).
ROOT_PATH = "/api"

app = FastAPI(title="truealpha-llm-service", lifespan=_lifespan, root_path=ROOT_PATH)

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


@app.get("/mcp", include_in_schema=False)
def mcp_slash(request: Request) -> RedirectResponse:
    """Send /mcp to /mcp/ ourselves, with the prefix and scheme intact.

    Starlette's own redirect_slashes handles this, and its Location keeps
    neither: `root_path` is not applied to a Mount's redirect, so even with
    ProxyHeadersMiddleware restoring the scheme the answer was
    https://truealpha.club/mcp/ — TLS fixed, /api still gone, still app-web's
    404. Measured both ways rather than assumed.

    Building it from `request.url_for` keeps the root_path this app declares, so
    the Location is whatever the deployment actually routes.
    """
    return RedirectResponse(f"{request.url_for('mcp_slash')}/", status_code=307)


app.mount("/mcp", mcp.streamable_http_app())


@app.get("/health")
def health() -> dict[str, str]:
    # git_sha lets tools/health_check.py confirm the deployed release tag is
    # actually live post-deploy, not just that something answers (#508).
    return {"status": "ok", "git_sha": os.environ.get("GIT_COMMIT_SHA", "unknown")}
