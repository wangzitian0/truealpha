"""LLM-call orchestration only — the App reads Postgres directly, not through here
(init.md Section 1, rule 5).

Roadmap: the MCP endpoint comes first (reuses libs/factors, nearly free to wire into
Claude Desktop); the self-built /chat SSE endpoint is Tier 3 (Phase 7).
"""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from llm_service.mcp_server import mcp


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    # FastMCP's own Starlette sub-app lifespan is not invoked by FastAPI's Mount,
    # so the session manager must run from the parent app's lifespan explicitly.
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="truealpha-llm-service", lifespan=_lifespan)
app.mount("/mcp", mcp.streamable_http_app())


@app.get("/health")
def health() -> dict[str, str]:
    # git_sha lets tools/health_check.py confirm the deployed release tag is
    # actually live post-deploy, not just that something answers (#508).
    return {"status": "ok", "git_sha": os.environ.get("GIT_COMMIT_SHA", "unknown")}
