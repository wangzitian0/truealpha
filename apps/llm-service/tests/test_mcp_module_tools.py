# ruff: noqa: I001, E401, E402, E701, E702, E731, F401, F811
# fmt: off
from __future__ import annotations; import importlib.util; import json; import os; from datetime import UTC, datetime, timedelta; from pathlib import Path; from typing import Any; import psycopg; import pytest; from llm_service.mcp_server import ResearchReportMarkdown, build_mcp_server; from mcp.shared.memory import create_connected_server_and_client_session; from truealpha_contracts.access import AccessContext, AuthenticationMethod, PrincipalKind; from truealpha_contracts.research_report import ( ResearchReportKind, ResearchReportRequest, build_research_report, render_report_markdown, ); from truealpha_contracts.research_report_mart import MartResearchReadRepository; from truealpha_contracts.strategy_run_fixture import FixtureStrategyRunRepository; _SEED_PATH = Path(__file__).resolve().parents[3] / "common" / "contracts" / "conformance" / "issuer_modules_parity.py"; _SPEC = importlib.util.spec_from_file_location("issuer_modules_parity_seed", _SEED_PATH); assert _SPEC is not None and _SPEC.loader is not None; _SEED = importlib.util.module_from_spec(_SPEC); _SPEC.loader.exec_module(_SEED); CANON = _SEED.CANON
@pytest.fixture
def anyio_backend() -> str: return "asyncio"
class _BorrowedConnection:
    def __init__(self, real: psycopg.Connection) -> None: self._real = real
    def __enter__(self) -> _BorrowedConnection: return self
    def __exit__(self, *_exc: object) -> None: return None
    def cursor(self, **kwargs: Any) -> Any: return self._real.cursor(**kwargs)
@pytest.fixture
def database(monkeypatch: pytest.MonkeyPatch):
    from truealpha_runtime.testing import skip_or_fail; url = os.environ.get("DATABASE_URL")
    if not url: skip_or_fail("DATABASE_URL must be explicitly provided for the MCP module tool tests"); return
    try: connection = psycopg.connect(url, connect_timeout=3, autocommit=False)
    except psycopg.OperationalError as error: pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
    try: connection.execute("set local session_replication_role = replica"); monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: _BorrowedConnection(connection)); yield connection
    finally: connection.rollback(); connection.close()
async def _call(name: str, issuer_id: str) -> dict[str, Any]:
    _content, structured = await build_mcp_server().call_tool(  # type: ignore[misc]
        name, {"request": {"issuer_id": issuer_id}}
    )
    return json.loads(json.dumps(structured))["result"]
@pytest.mark.anyio
async def test_the_module_tools_answer_the_served_row_the_app_shows(database: psycopg.Connection) -> None: _SEED.seed_module_rows(database); _SEED.seed_served_head(database); assert await _call("issuer_supply_chain_exposure", CANON["issuer_id"]) == CANON["supply_chain_exposure"]["expected"]; assert await _call("issuer_analyst_ratings", CANON["issuer_id"]) == CANON["analyst_ratings"]["expected"]
@pytest.mark.anyio
async def test_no_served_run_is_a_typed_unavailable_answer(database: psycopg.Connection) -> None:
    _SEED.seed_module_rows(database)
    for name, module in ( ("issuer_supply_chain_exposure", "supply_chain_exposure"), ("issuer_analyst_ratings", "analyst_ratings"), ): assert await _call(name, CANON["issuer_id"]) == { "issuer_id": CANON["issuer_id"], "module": module, "reason": "no_row_in_served_head", }
_DATABASE_DETAIL = 'connection to server at "10.0.0.5", port 15432 failed: FATAL: role "mart_readonly"'
class _FailingModules:
    def supply_chain_exposure(self, issuer_id: str) -> None: raise psycopg.OperationalError(_DATABASE_DETAIL)
    def analyst_ratings(self, issuer_id: str) -> None: raise psycopg.OperationalError(_DATABASE_DETAIL)
@pytest.mark.anyio
async def test_a_failed_read_is_a_tool_error_not_an_empty_answer() -> None:
    server = build_mcp_server(issuer_module_reader=_FailingModules())
    async with create_connected_server_and_client_session(server._mcp_server) as client:
        await client.initialize()
        for name in ("issuer_supply_chain_exposure", "issuer_analyst_ratings"):
            result = await client.call_tool(name, {"request": {"issuer_id": "issuer:any"}}); assert result.isError is True, f"{name} hid a failed read as an answer"; text = " ".join(getattr(block, "text", "") for block in result.content); assert "the mart read failed" in text, text
            for detail in ("10.0.0.5", "15432", "mart_readonly"): assert detail not in text, f"{name} shows the database detail {detail!r} to the caller"
class _NoServedModuleRows:
    def supply_chain_exposure(self, issuer_id: str) -> None: return None
    def analyst_ratings(self, issuer_id: str) -> None: return None
def _context() -> AccessContext: now = datetime.now(UTC); return AccessContext( context_id="ctx:test", principal_id="principal:test", tenant_id="tenant:test", session_id="session:test", authentication_method=AuthenticationMethod.SERVICE_IDENTITY, principal_kind=PrincipalKind.SERVICE, issued_at=now, expires_at=now + timedelta(hours=1), )
@pytest.mark.anyio
async def test_the_markdown_tool_renders_the_report_of_the_same_request() -> None:
    repository = MartResearchReadRepository( strategy_repository=FixtureStrategyRunRepository(), issuer_modules=_NoServedModuleRows() ); server = build_mcp_server(repository=FixtureStrategyRunRepository(), research_repository=repository); request = {"report_kind": "company", "target_entity_ids": ["issuer:adm"], "cutoff_at": "2026-06-30T23:59:59Z"}
    _content, rendered = await server.call_tool("research_report_markdown", {"request": request})  # type: ignore[misc]
    _content, report = await server.call_tool("research_report", {"request": request})  # type: ignore[misc]
    markdown = ResearchReportMarkdown.model_validate(rendered); expected = build_research_report( ResearchReportRequest( report_kind=ResearchReportKind.COMPANY, target_entity_ids=("issuer:adm",), cutoff_at=datetime(2026, 6, 30, 23, 59, 59, tzinfo=UTC), ), repository, context=_context(), ); assert markdown.report_id == report["report_id"] == expected.report_id; assert markdown.markdown == render_report_markdown(expected); assert f"- Report ID: `{expected.report_id}`" in markdown.markdown
    assert "### Supply-chain exposure" in markdown.markdown
