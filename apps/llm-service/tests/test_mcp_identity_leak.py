# ruff: noqa: I001, E401, E402, E701, E702, E731, F401, F811
# fmt: off
from __future__ import annotations; import json; import os; import re; import secrets; from collections.abc import Iterator; from typing import Any; import psycopg; import pytest; from llm_service.mcp_server import build_mcp_server; from psycopg.rows import dict_row; _BARE_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE); _IDENTITY_FIELD = re.compile(r"(^|_)ids?$"); _EXEMPT_IDENTITY_FIELDS: dict[str, str] = {}; _CUTOFF = "2026-03-31T23:59:59Z"; _TOOL_CALLS: dict[str, dict[str, Any]] = { "strategy_run": {"request": {"strategy_id": "large_model_value_v0"}}, "research_report": { "request": { "report_kind": "theme_ranking", "target_entity_ids": ["issuer:ranking-scope-unused"], "cutoff_at": _CUTOFF, } }, "research_card": { "request": { "report_kind": "theme_ranking", "target_entity_ids": ["issuer:ranking-scope-unused"], "cutoff_at": _CUTOFF, "card_kind": "ranking", } }, "topt_gppe": {}, "company_360_profile": {"request": {"issuer_id": "issuer:ranking-scope-unused"}}, "theme_purity_leaderboard": {"request": {"theme_id": "ai-infrastructure", "limit": 10}}, "etf_virtual_company_profile": {"request": {"fund_id": "etf:series:S000101292"}}, "governed_backtest": {"request": {"strategy_key": "qqq_topk_rank", "limit": 5}}, "research_report_markdown": { "request": { "report_kind": "theme_ranking", "target_entity_ids": ["issuer:ranking-scope-unused"], "cutoff_at": _CUTOFF, } }, "issuer_supply_chain_exposure": {"request": {"issuer_id": "issuer:ranking-scope-unused"}}, "issuer_analyst_ratings": {"request": {"issuer_id": "issuer:ranking-scope-unused"}}, "metric_node_values": {"request": {"node_key": "gppe_uniform_charge_v0"}}, }
def _database_url() -> str | None: return os.environ.get("DATABASE_URL")
class _BorrowedConnection:
    def __init__(self, real: psycopg.Connection) -> None: self._real = real
    def __enter__(self) -> _BorrowedConnection: return self
    def __exit__(self, *_exc: object) -> None: return None
    def cursor(self, **kwargs: Any) -> Any: return self._real.cursor(**kwargs)
    def execute(self, query: Any, params: Any = None, **kwargs: Any) -> Any: return self._real.cursor(row_factory=dict_row).execute(query, params, **kwargs)
def _walk(node: Any, path: str) -> Iterator[tuple[str, str, str]]:
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}" if path else str(key)
            if _IDENTITY_FIELD.search(str(key)):
                if isinstance(value, str): yield child, str(key), value
                elif isinstance(value, list):
                    for index, item in enumerate(value):
                        if isinstance(item, str): yield f"{child}[{index}]", str(key), item
            yield from _walk(value, child)
    elif isinstance(node, list):
        for index, item in enumerate(node): yield from _walk(item, f"{path}[{index}]")
@pytest.fixture
def anyio_backend() -> str: return "asyncio"
@pytest.fixture
def seeded_entity(monkeypatch: pytest.MonkeyPatch):
    from truealpha_runtime.testing import skip_or_fail; url = _database_url()
    if not url: skip_or_fail("DATABASE_URL must be explicitly provided for MCP identity tests (no default to port 5432)"); return
    try: connection = psycopg.connect(url, connect_timeout=3, autocommit=False)
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"): pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; ci-python runs this against a real database")
    try: alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"; lei = "".join(secrets.choice(alphabet) for _ in range(18)) + f"{secrets.randbelow(100):02d}"; legacy_id = f"issuer:lei:{lei}"; entity_uuid = str( connection.execute( "select staging.entity_mint('issuer', 'lei', %s, 'test-mcp-identity-leak')", (lei,) ).fetchone()[0] ); connection.execute( "\n            insert into staging.entity_aliases\n              (entity_id, scheme, value, valid_from, transaction_time, source, raw_ref,\n               method, confidence, mapping_version)\n            values (%s, 'lei', %s, date '2026-01-01', timestamptz '2026-01-01',\n                    'test-mcp-identity-leak', 'test-ref', 'asserted', 1.0, 'v1'),\n                   (%s, 'legacy-id', %s, date '2026-01-01', timestamptz '2026-01-01',\n                    'test-mcp-identity-leak', 'test-ref', 'asserted', 1.0, 'v1')\n            ", (entity_uuid, lei, entity_uuid, legacy_id), ); run_id = "strategy-run:" + secrets.token_hex(32); connection.execute( "\n            insert into mart.strategy_runs\n              (strategy_run_id, content_sha256, strategy_key, strategy_version,\n               definition_content_sha256, corpus_sha256, claim_ceiling, executed_at)\n            values (%s, %s, 'large_model_value_v0', 'v0', %s, %s, 'preview', timestamptz '2027-07-01')\n            ", (run_id, secrets.token_hex(32), secrets.token_hex(32), secrets.token_hex(32)), ); connection.execute( "\n            insert into mart.strategy_decisions\n              (strategy_decision_id, content_sha256, strategy_run_id, issuer_id, cutoff_at,\n               capital_adjusted_labor_efficiency, tier, current_price_to_sales,\n               target_price_to_sales, valuation_gap, eligible, outcome, rank, target_weight)\n            values (%s, %s, %s, %s, %s, '12.5', 'tech', '20', '18.75', '0.5', true, 'selected', 1, '1.0')\n            ", ( "strategy-decision:" + secrets.token_hex(32), secrets.token_hex(32), run_id, entity_uuid, _CUTOFF, ), ); monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: _BorrowedConnection(connection)); yield {"entity_uuid": entity_uuid, "legacy_id": legacy_id}
    finally: connection.rollback(); connection.close()
@pytest.mark.anyio
async def test_every_registered_tool_is_covered() -> None: server = build_mcp_server(); registered = {tool.name for tool in await server.list_tools()}; assert registered == set(_TOOL_CALLS), ( f'registered tools {sorted(registered)} do not match the covered tools {sorted(_TOOL_CALLS)}. A new MCP tool must declare how this guard calls it, or its responses go unchecked for bare-UUID identity leaks.' )
@pytest.mark.anyio
async def test_no_tool_response_carries_a_bare_uuid_identity(seeded_entity) -> None:
    server = build_mcp_server(); registered = {tool.name for tool in await server.list_tools()}; assert registered == set(_TOOL_CALLS), "tool coverage drifted; see test_every_registered_tool_is_covered"; violations: list[str] = []; resolved_sightings = 0
    for name in sorted(registered):
        _content, structured = await server.call_tool(name, _TOOL_CALLS[name])  # type: ignore[misc]
        payload = json.loads(json.dumps(structured, default=str))
        for path, field, value in _walk(payload, ""):
            if field in _EXEMPT_IDENTITY_FIELDS: continue
            if _BARE_UUID.match(value): violations.append(f"{name}: {path} = {value}")
            if value == seeded_entity["legacy_id"]: resolved_sightings += 1
    assert not violations, "MCP responses carry bare-UUID identity fields:\n  " + "\n  ".join(violations); assert resolved_sightings > 0, ( f"no response carried the seeded issuer's resolved identity {seeded_entity['legacy_id']}; the guard scanned nothing that could have leaked" )
