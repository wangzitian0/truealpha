# ruff: noqa: I001, E401, E402, E701, E702, E731, F401, F811
# fmt: off
from collections.abc import AsyncIterator; from contextlib import asynccontextmanager; from pathlib import Path; from typing import Any; from fastapi import FastAPI, Request; from fastapi.responses import RedirectResponse; from infra2_sdk.fastapi import init_service; from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor; from truealpha_contracts.environment_contract import load_environment_contract; from truealpha_runtime.boot import assert_environment; from truealpha_runtime.telemetry import init_telemetry; from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware; from llm_service.config import settings; from llm_service.mcp_auth import BearerTokenGate; from llm_service.mcp_server import mcp; ENV_MANIFEST_PATH = Path("apps/llm-service/required-env.generated.json")
def refuse_to_boot_on_a_missing_environment() -> None: assert_environment(ENV_MANIFEST_PATH, require_injected=settings.is_deployed)
@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    refuse_to_boot_on_a_missing_environment()
    async with mcp.session_manager.run(): yield
ROUTED_PREFIX = "/api"; app = FastAPI(title="truealpha-llm-service", lifespan=_lifespan); _telemetry = init_telemetry()
if _telemetry is not None: FastAPIInstrumentor.instrument_app( app, tracer_provider=_telemetry.tracer_provider, meter_provider=_telemetry.meter_provider )
TRUSTED_PROXIES = ("127.0.0.1", "::1"); app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=list(TRUSTED_PROXIES))
@app.api_route("/mcp", methods=["GET", "POST", "DELETE"], include_in_schema=False)
def mcp_slash(request: Request) -> RedirectResponse: del request; return RedirectResponse(f"{ROUTED_PREFIX}/mcp/", status_code=307)
app.mount("/mcp", BearerTokenGate(mcp.streamable_http_app(), expected_token=lambda: settings.mcp_bearer_token))
@app.get("/health")
def health() -> dict[str, Any]:
    parser, git_sha, image_digest, pointers, verdicts = _data_engine_facts(); return {
        "status": "ok",
        "git_sha": settings.git_commit_sha,
        "data_engine_parser": parser,
        "data_engine_git_sha": git_sha,
        "data_engine_image_digest": image_digest,
        "governed_pointers": pointers,
        "nightly_verdicts": verdicts,
    }
init_service(app, "truealpha-llm-service", enable_otel=False); NIGHTLY_VERDICTS_SQL = "select distinct on (check_name) check_name, ran_at, ok, summary from mart.nightly_verdicts order by check_name, ran_at desc, recorded_at desc"; GOVERNED_POINTERS_SQL = "select universe_id, advanced_at, age_hours, freshness, limit_hours, staleness_reason, availability from mart.served_head order by universe_id"; HEAD_ENVIRONMENTS_SQL = "select 1 from mart.served_head_environments limit 1"
def _data_engine_facts() -> tuple[str, str, str, list[dict[str, Any]] | str, list[dict[str, Any]] | str]:
    parser, git_sha, image_digest = "unknown", "unknown", "unknown"; pointers: list[dict[str, Any]] | str = "unknown"; verdicts: list[dict[str, Any]] | str = "unknown"
    try:
        import psycopg
        with psycopg.connect( settings.database_url, connect_timeout=settings.database_connect_timeout_seconds ) as connection:
            try: row = connection.execute( "select parser_version from staging.capture_normalized_observations order by recorded_at desc limit 1" ).fetchone(); parser = str(row[0]) if row and row[0] else "unknown"
            except psycopg.Error: connection.rollback()
            try:
                row = connection.execute( "select git_sha, image_digest from mart.data_engine_identity order by created_at desc limit 1" ).fetchone()
                if row is not None: git_sha, image_digest = str(row[0] or "unknown"), str(row[1] or "unknown")
            except psycopg.Error: connection.rollback()
            try:
                rows = connection.execute(GOVERNED_POINTERS_SQL).fetchall()
                if rows or connection.execute(HEAD_ENVIRONMENTS_SQL).fetchone() is None: pointers = [ { "universe_id": str(u), "advanced_at": adv.isoformat(), "age_hours": round(float(age), 1), "freshness": str(f), "limit_hours": int(lim), "staleness_reason": None if sr is None else str(sr), "availability": str(avail), } for u, adv, age, f, lim, sr, avail in rows if load_environment_contract().is_head_scheduled(str(u), settings.app_env) ]
            except psycopg.Error: connection.rollback()
            try: verdicts = [ { "check": str(c), "ran_at": r.isoformat(), "ok": None if ok is None else bool(ok), "summary": str(s), } for c, r, ok, s in connection.execute(NIGHTLY_VERDICTS_SQL).fetchall() if load_environment_contract().expects_verdict(str(c), settings.app_env) ]
            except psycopg.Error: connection.rollback()
    except Exception: pass
    return parser, git_sha, image_digest, pointers, verdicts
