# ruff: noqa: I001, E401, E402, E701, E702, E731, F401, F811
# fmt: off
from __future__ import annotations; import json; import os; import secrets; from typing import Any; import psycopg; import pytest; from llm_service.mcp_server import build_mcp_server; from truealpha_contracts.strategy import ( StrategyDefinitionIdentity, registered_strategy_definition, strategy_definition_registry, ); from truealpha_contracts.strategy_run import StrategyRunReport; from truealpha_runtime.testing import synthetic_provisional_strategy_definition; _REGISTRY = strategy_definition_registry(); _REVIEWED = next(entry for entry in _REGISTRY if not entry.provisional_semantics); _DELETED_V0 = ("0.1.0", "e5c56da455a2845d99e67265d96b30e9b479384ef11a8a4d242494b1816fe79d"); _EXECUTED_AT = "2028-01-01T00:00:00Z"
class _BorrowedConnection:
    def __init__(self, real: psycopg.Connection[Any]) -> None: self._real = real
    def __enter__(self) -> _BorrowedConnection: return self
    def __exit__(self, *_exc: object) -> None: return None
    def cursor(self, **kwargs: Any) -> Any: return self._real.cursor(**kwargs)
@pytest.fixture
def anyio_backend() -> str: return "asyncio"
@pytest.fixture
def database(monkeypatch: pytest.MonkeyPatch):
    from truealpha_runtime.testing import skip_or_fail; url = os.environ.get("DATABASE_URL")
    if not url: skip_or_fail("DATABASE_URL must be explicitly provided for MCP tests (no default to port 5432)"); return
    try: connection = psycopg.connect(url, connect_timeout=3, autocommit=False)
    except psycopg.OperationalError as error: pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
    monkeypatch.setattr(psycopg, "connect", lambda *_args, **_kwargs: _BorrowedConnection(connection))
    try: yield connection
    finally: connection.rollback(); connection.close()
async def _served_result(connection: psycopg.Connection[Any], *, version: str, content_sha256: str) -> dict[str, Any]:
    run_id = "strategy-run:" + secrets.token_hex(32); connection.execute( "\n        insert into mart.strategy_runs\n          (strategy_run_id, content_sha256, strategy_key, strategy_version,\n           definition_content_sha256, corpus_sha256, claim_ceiling, executed_at)\n        values (%s, %s, 'large_model_value_v0', %s, %s, %s, 'preview', %s)\n        ", (run_id, secrets.token_hex(32), version, content_sha256, secrets.token_hex(32), _EXECUTED_AT), ); connection.execute( "\n        insert into mart.strategy_decisions\n          (strategy_decision_id, content_sha256, strategy_run_id, issuer_id, cutoff_at, eligible, outcome)\n        values (%s, %s, %s, 'issuer:mcp-1235', timestamptz '2026-06-30T23:59:59Z', true, 'selected')\n        ", ("strategy-decision:" + secrets.token_hex(32), secrets.token_hex(32), run_id), )
    _content, structured = await build_mcp_server().call_tool(  # type: ignore[misc]
        "strategy_run", {"request": {"strategy_id": "large_model_value_v0"}}
    )
    result: dict[str, Any] = structured["result"]  # type: ignore[index]
    assert result.get("strategy_run_id") == run_id, f"the tool served {result!r}, not the seeded run"; return result
@pytest.mark.anyio
@pytest.mark.parametrize("registered", _REGISTRY, ids=lambda entry: f"definition-{entry.version}")
async def test_the_strategy_run_tool_names_the_definition_and_its_review_state( database: psycopg.Connection[Any], registered: StrategyDefinitionIdentity ) -> None: result = await _served_result(database, version=registered.version, content_sha256=registered.content_sha256); expected_state = "provisional" if registered.provisional_semantics else "reviewed"; assert result.get("definition") == { "version": registered.version, "content_sha256": registered.content_sha256, "review_state": expected_state, }; report = StrategyRunReport.model_validate_json(json.dumps(result)); assert report.definition is not None; assert report.definition.review_state == expected_state
@pytest.mark.anyio
async def test_the_strategy_run_tool_calls_a_provisional_definition_provisional( database: psycopg.Connection[Any], ) -> None:
    synthetic = synthetic_provisional_strategy_definition()
    with registered_strategy_definition(synthetic): result = await _served_result( database, version=synthetic.definition_version, content_sha256=synthetic.content_sha256 ); report = StrategyRunReport.model_validate_json(json.dumps(result))
    assert result.get("definition") == { "version": synthetic.definition_version, "content_sha256": synthetic.content_sha256, "review_state": "provisional", }; assert report.definition is not None; assert report.definition.review_state == "provisional"
@pytest.mark.anyio
@pytest.mark.parametrize( ("version", "content_sha256"), [ pytest.param(*_DELETED_V0, id="deleted-0.1.0"), pytest.param(_REVIEWED.version, "f" * 64, id="hash-outside-registry"), ], )
async def test_the_strategy_run_tool_calls_a_definition_outside_the_registry_unknown( database: psycopg.Connection[Any], version: str, content_sha256: str ) -> None: result = await _served_result(database, version=version, content_sha256=content_sha256); assert result.get("definition") == { "version": version, "content_sha256": content_sha256, "review_state": "unknown", }
