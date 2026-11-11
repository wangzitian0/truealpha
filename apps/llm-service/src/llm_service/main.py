"""Provide LLM orchestration and MCP transport service."""

from collections.abc import AsyncIterator, MutableMapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from infra2_sdk.fastapi import init_service
from truealpha_contracts.environment_contract import load_environment_contract
from truealpha_runtime.boot import assert_environment
from truealpha_runtime.telemetry import init_telemetry
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from llm_service.config import settings
from llm_service.mcp_auth import BearerTokenGate
from llm_service.mcp_server import mcp

ENV_MANIFEST_PATH = Path("apps/llm-service/required-env.generated.json")
ROUTED_PREFIX = "/api"
PROBE_PATHS = frozenset({"/health", "/livez", "/readyz"})
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


def refuse_to_boot_on_a_missing_environment() -> None:
    assert_environment(ENV_MANIFEST_PATH, require_injected=settings.is_deployed)


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    refuse_to_boot_on_a_missing_environment()
    async with mcp.session_manager.run():
        yield


def _is_probe(scope: MutableMapping[str, Any]) -> bool:
    return scope.get("path") in PROBE_PATHS


app = FastAPI(title="truealpha-llm-service", lifespan=_lifespan, telemetry={"exclude": _is_probe})
_telemetry = init_telemetry()
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


def _data_engine_facts() -> tuple[str, str, str, list[dict[str, Any]] | str, list[dict[str, Any]] | str]:
    parser, git_sha, image_digest = "unknown", "unknown", "unknown"
    pointers: list[dict[str, Any]] | str = "unknown"
    verdicts: list[dict[str, Any]] | str = "unknown"
    try:
        import psycopg

        contract = load_environment_contract()
        with psycopg.connect(settings.database_url, connect_timeout=settings.database_connect_timeout_seconds) as conn:

            def _first_str(cur: Any) -> str:
                r = cur.fetchone()
                return str(r[0]) if r and r[0] else "unknown"

            def _identity(cur: Any) -> tuple[str, str]:
                r = cur.fetchone()
                return (str(r[0] or "unknown"), str(r[1] or "unknown")) if r else ("unknown", "unknown")

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

            def _pointers(cur: Any) -> list[dict[str, Any]] | str:
                rows = cur.fetchall()
                if not rows and conn.execute(HEAD_ENVIRONMENTS_SQL).fetchone() is not None:
                    return "unknown"
                return [
                    {
                        "universe_id": str(u),
                        "advanced_at": a.isoformat(),
                        "age_hours": round(float(age), 1),
                        "freshness": str(f),
                        "limit_hours": int(lim),
                        "staleness_reason": None if reason is None else str(reason),
                        "availability": str(avail),
                    }
                    for u, a, age, f, lim, reason, avail in rows
                    if contract.is_head_scheduled(str(u), settings.app_env)
                ]

            def _verdicts(cur: Any) -> list[dict[str, Any]]:
                return [
                    {
                        "check": str(c),
                        "ran_at": r.isoformat(),
                        "ok": None if ok is None else bool(ok),
                        "summary": str(s),
                    }
                    for c, r, ok, s in cur.fetchall()
                    if contract.expects_verdict(str(c), settings.app_env)
                ]

            pointers = _safe_query(conn, GOVERNED_POINTERS_SQL, _pointers, "unknown")
            verdicts = _safe_query(conn, NIGHTLY_VERDICTS_SQL, _verdicts, "unknown")
    except Exception:  # noqa: BLE001
        pass
    return parser, git_sha, image_digest, pointers, verdicts


@app.get("/health")
def health() -> dict[str, Any]:
    parser, git_sha, image_digest, pointers, verdicts = _data_engine_facts()
    return {
        "status": "ok",
        "git_sha": settings.git_commit_sha,
        "data_engine_parser": parser,
        "data_engine_git_sha": git_sha,
        "data_engine_image_digest": image_digest,
        "governed_pointers": pointers,
        "nightly_verdicts": verdicts,
    }


init_service(app, "truealpha-llm-service", enable_otel=False, suppress_probe_telemetry=True)
