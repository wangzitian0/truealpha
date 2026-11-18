"""Provide LLM orchestration and MCP transport service."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.routing import APIRoute
from infra2_sdk.fastapi import init_service
from truealpha_contracts.environment_contract import load_environment_contract
from truealpha_runtime.boot import assert_environment
from truealpha_runtime.telemetry import telemetry_config_error
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from llm_service.api.v1 import admin, auth, entities, library, panel, strategy
from llm_service.config import settings
from llm_service.mcp_auth import BearerTokenGate
from llm_service.mcp_server import mcp

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


def refuse_to_boot_on_a_missing_environment() -> None:
    assert_environment(ENV_MANIFEST_PATH, require_injected=settings.is_deployed)


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    refuse_to_boot_on_a_missing_environment()
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="truealpha-llm-service", lifespan=_lifespan)
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=list(TRUSTED_PROXIES))


@app.api_route("/mcp", methods=["GET", "POST", "DELETE"], include_in_schema=False)
def mcp_slash(request: Request) -> RedirectResponse:
    del request
    return RedirectResponse(f"{ROUTED_PREFIX}/mcp/", status_code=307)


app.mount(
    "/mcp",
    BearerTokenGate(mcp.streamable_http_app(), expected_token=lambda: settings.mcp_bearer_token),
)


def _safe_query(conn: Any, query: str, transform: Any, default: Any) -> Any:
    import psycopg

    try:
        return transform(conn.execute(query))
    except psycopg.Error:
        conn.rollback()
        return default


def _first_str(cursor: Any) -> str:
    row = cursor.fetchone()
    return str(row[0]) if row and row[0] else "unknown"


def _identity(cursor: Any) -> tuple[str, str]:
    row = cursor.fetchone()
    return (str(row[0] or "unknown"), str(row[1] or "unknown")) if row else ("unknown", "unknown")


def _served_pointers(conn: Any, cursor: Any, contract: Any) -> list[dict[str, Any]] | str:
    rows = cursor.fetchall()
    if not rows and conn.execute(HEAD_ENVIRONMENTS_SQL).fetchone() is not None:
        return "unknown"
    return [
        {
            "universe_id": str(universe_id),
            "advanced_at": advanced_at.isoformat(),
            "age_hours": round(float(age_hours), 1),
            "freshness": str(freshness),
            "limit_hours": int(limit_hours),
            "staleness_reason": None if reason is None else str(reason),
            "availability": str(availability),
        }
        for universe_id, advanced_at, age_hours, freshness, limit_hours, reason, availability in rows
        if contract.is_head_scheduled(str(universe_id), settings.app_env)
    ]


def _nightly_verdicts(cursor: Any, contract: Any) -> list[dict[str, Any]]:
    return [
        {
            "check": str(check),
            "ran_at": ran_at.isoformat(),
            "ok": None if ok is None else bool(ok),
            "summary": str(summary),
        }
        for check, ran_at, ok, summary in cursor.fetchall()
        if contract.expects_verdict(str(check), settings.app_env)
    ]


def _data_engine_facts() -> tuple[str, str, str, list[dict[str, Any]] | str, list[dict[str, Any]] | str]:
    parser, git_sha, image_digest = "unknown", "unknown", "unknown"
    pointers: list[dict[str, Any]] | str = "unknown"
    verdicts: list[dict[str, Any]] | str = "unknown"
    try:
        import psycopg

        contract = load_environment_contract()
        with psycopg.connect(settings.database_url, connect_timeout=settings.database_connect_timeout_seconds) as conn:
            parser = _safe_query(
                conn,
                "select parser_version from staging.capture_normalized_observations order by recorded_at desc limit 1",
                _first_str,
                "unknown",
            )
            git_sha, image_digest = _safe_query(
                conn,
                "select git_sha, image_digest from mart.data_engine_identity order by created_at desc limit 1",
                _identity,
                ("unknown", "unknown"),
            )
            pointers = _safe_query(
                conn, GOVERNED_POINTERS_SQL, lambda cur: _served_pointers(conn, cur, contract), "unknown"
            )
            verdicts = _safe_query(conn, NIGHTLY_VERDICTS_SQL, lambda cur: _nightly_verdicts(cur, contract), "unknown")
    except Exception:  # noqa: BLE001
        pass
    return parser, git_sha, image_digest, pointers, verdicts


def _data_engine_health_extra() -> dict[str, Any]:
    parser, git_sha, image_digest, pointers, verdicts = _data_engine_facts()
    return {
        "data_engine_parser": parser,
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
try:
    init_service(
        app,
        "truealpha-llm-service",
        require_issued_identity=True,
        suppress_probe_telemetry=True,
        health_extra=_data_engine_health_extra,
    )
except ValueError as exc:
    raise telemetry_config_error(exc) from exc

for route in app.routes:
    if isinstance(route, APIRoute) and route.path in _SYSTEM_PROBES:
        route.tags = ["system"]
        route.operation_id = _SYSTEM_PROBES[route.path]
app.openapi_schema = None
