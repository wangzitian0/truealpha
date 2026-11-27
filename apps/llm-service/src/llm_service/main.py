"""Provide LLM orchestration and MCP transport service."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.routing import APIRoute
from infra2_sdk.config import assert_env
from infra2_sdk.service import init_service
from starlette.types import ASGIApp, Receive, Scope, Send
from truealpha_contracts.environment_contract import load_environment_contract
from truealpha_runtime.checks import redact_postgres_exception
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from llm_service.api.v1 import admin, auth, entities, library, panel, strategy
from llm_service.config import settings
from llm_service.mcp_auth import BearerTokenGate
from llm_service.mcp_server import build_mcp_server
from llm_service.services._db import close_pools, with_ops_reader

logger = logging.getLogger(__name__)

ENV_MANIFEST_PATH = Path("apps/llm-service/required-env.generated.json")
ROUTED_PREFIX = "/api"
TRUSTED_PROXIES = ("127.0.0.1", "::1")
NIGHTLY_VERDICTS_SQL = (
    "select distinct on (check_name) check_name, ran_at, ok, summary "
    "from mart.nightly_verdicts order by check_name, ran_at desc, recorded_at desc"
)
GOVERNED_POINTERS_SQL = (
    "select universe_id, advanced_at, age_hours, freshness, limit_hours, staleness_reason, availability "
    "from mart.served_head order by universe_id"
)
HEAD_ENVIRONMENTS_SQL = "select 1 from mart.served_head_environments limit 1"

_SYSTEM_PROBES: dict[str, str] = {
    "/health": "system_health",
    "/livez": "system_livez",
    "/readyz": "system_readyz",
}


class _McpRuntime:
    """Host FastMCP Streamable HTTP inside FastAPI's ASGI lifespan without eager import side effects."""

    def __init__(self) -> None:
        self._http_app: ASGIApp | None = None

    @asynccontextmanager
    async def lifespan(self) -> AsyncIterator[None]:
        server = build_mcp_server()
        self._http_app = server.streamable_http_app()
        try:
            async with server.session_manager.run():
                yield
        finally:
            self._http_app = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self._http_app is None:
            resp = JSONResponse({"error": "mcp_unavailable"}, status_code=503)
            await resp(scope, receive, send)
            return
        await self._http_app(scope, receive, send)


_mcp_runtime = _McpRuntime()


def refuse_to_boot_on_a_missing_environment() -> None:
    assert_env(ENV_MANIFEST_PATH, require_injected=settings.is_deployed)


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    refuse_to_boot_on_a_missing_environment()
    try:
        async with _mcp_runtime.lifespan():
            yield
    finally:
        close_pools()


app = FastAPI(title="truealpha-llm-service", lifespan=_lifespan)
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=list(TRUSTED_PROXIES))


@app.api_route("/mcp", methods=["GET", "POST", "DELETE"], include_in_schema=False)
def mcp_slash(request: Request) -> RedirectResponse:
    del request
    prefix = settings.routed_prefix.rstrip("/")
    return RedirectResponse(f"{prefix}/mcp/", status_code=307)


app.mount(
    "/mcp",
    BearerTokenGate(
        _mcp_runtime,
        expected_token=lambda: settings.mcp_bearer_token,
        readonly_token=lambda: settings.mcp_readonly_token,
    ),
)


def _read_health_fact(cursor: Any, query: str, transform: Any, default: Any) -> Any:
    import psycopg

    degraded_errors = (
        psycopg.OperationalError,
        psycopg.errors.UndefinedTable,
        psycopg.errors.UndefinedColumn,
        psycopg.errors.InsufficientPrivilege,
    )
    conn = getattr(cursor, "connection", None)
    tx = conn.transaction() if conn is not None and hasattr(conn, "transaction") else nullcontext()
    try:
        with tx:
            cursor.execute(query)
            return transform(cursor)
    except degraded_errors as exc:
        logger.warning(
            "health mart fact degraded: %s",
            redact_postgres_exception(
                exc,
                database_url=settings.database_url,
                connect_timeout_seconds=settings.database_connect_timeout_seconds,
            ),
        )
        if conn is not None and not hasattr(conn, "transaction") and hasattr(conn, "rollback"):
            conn.rollback()
        return default


def _identity(cursor: Any) -> tuple[str, str]:
    row = cursor.fetchone()
    if row is None:
        return "unknown", "unknown"
    git_sha = row["git_sha"] if isinstance(row, dict) else row[0]
    image_digest = row["image_digest"] if isinstance(row, dict) else row[1]
    return str(git_sha or "unknown"), str(image_digest or "unknown")


def _served_pointers(cursor: Any, contract: Any) -> list[dict[str, Any]] | str:
    rows = cursor.fetchall()
    if not rows:
        cursor.execute(HEAD_ENVIRONMENTS_SQL)
        if cursor.fetchone() is not None:
            return "unknown"
    out: list[dict[str, Any]] = []
    for row in rows:
        universe_id, advanced_at, age_hours, freshness, limit_hours, reason, availability = (
            (
                row["universe_id"],
                row["advanced_at"],
                row["age_hours"],
                row["freshness"],
                row["limit_hours"],
                row["staleness_reason"],
                row["availability"],
            )
            if isinstance(row, dict)
            else row
        )
        if contract.is_head_scheduled(str(universe_id), settings.environment):
            out.append(
                {
                    "universe_id": str(universe_id),
                    "advanced_at": advanced_at.isoformat(),
                    "age_hours": round(float(age_hours), 1),
                    "freshness": str(freshness),
                    "limit_hours": int(limit_hours),
                    "staleness_reason": None if reason is None else str(reason),
                    "availability": str(availability),
                }
            )
    return out


def _nightly_verdicts(cursor: Any, contract: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in cursor.fetchall():
        check, ran_at, ok, summary = (
            (row["check_name"], row["ran_at"], row["ok"], row["summary"]) if isinstance(row, dict) else row
        )
        if contract.expects_verdict(str(check), settings.environment):
            out.append(
                {
                    "check": str(check),
                    "ran_at": ran_at.isoformat(),
                    "ok": None if ok is None else bool(ok),
                    "summary": str(summary),
                }
            )
    return out


def _data_engine_facts() -> tuple[str, str, list[dict[str, Any]] | str, list[dict[str, Any]] | str]:
    import psycopg

    git_sha, image_digest = "unknown", "unknown"
    pointers: list[dict[str, Any]] | str = "unknown"
    verdicts: list[dict[str, Any]] | str = "unknown"
    contract = load_environment_contract()
    degraded_errors = (
        psycopg.OperationalError,
        psycopg.errors.UndefinedTable,
        psycopg.errors.InsufficientPrivilege,
    )
    try:

        def _collect(cur: Any) -> tuple[str, str, list[dict[str, Any]] | str, list[dict[str, Any]] | str]:
            sha, digest = _read_health_fact(
                cur,
                "select git_sha, image_digest from mart.data_engine_identity order by created_at desc limit 1",
                _identity,
                ("unknown", "unknown"),
            )
            ptr = _read_health_fact(cur, GOVERNED_POINTERS_SQL, lambda c: _served_pointers(c, contract), "unknown")
            vrd = _read_health_fact(cur, NIGHTLY_VERDICTS_SQL, lambda c: _nightly_verdicts(c, contract), "unknown")
            return sha, digest, ptr, vrd

        git_sha, image_digest, pointers, verdicts = with_ops_reader(_collect)
    except degraded_errors as exc:
        logger.warning(
            "health database unavailable: %s",
            redact_postgres_exception(
                exc,
                database_url=settings.database_url,
                connect_timeout_seconds=settings.database_connect_timeout_seconds,
            ),
        )
    return git_sha, image_digest, pointers, verdicts


def _data_engine_health_extra() -> dict[str, Any]:
    git_sha, image_digest, pointers, verdicts = _data_engine_facts()
    return {
        "data_engine_git_sha": git_sha,
        "data_engine_image_digest": image_digest,
        "governed_pointers": pointers,
        "nightly_verdicts": verdicts,
    }


app.include_router(panel.router)
app.include_router(strategy.router)
app.include_router(entities.router)
app.include_router(library.router)
app.include_router(auth.router)
app.include_router(admin.router)

# Replace FastMCP's default Rich stderr handler so init_service installs SDK stdout JSON logging.
logging.getLogger().handlers.clear()
init_service(
    app,
    "truealpha-llm-service",
    require_issued_identity=True,
    suppress_probe_telemetry=True,
    health_extra=_data_engine_health_extra,
)

for route in app.routes:
    if isinstance(route, APIRoute) and route.path in _SYSTEM_PROBES:
        route.tags = ["system"]
        route.operation_id = _SYSTEM_PROBES[route.path]
app.openapi_schema = None
