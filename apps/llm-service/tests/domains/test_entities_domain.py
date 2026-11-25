"""Domain 3 (`entities`) HTTP + MCP behavioral tests."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from llm_service.main import app
from llm_service.mcp_server import build_mcp_server
from llm_service.services import entities_service, strategy_service

from ._helpers import auth_client


def test_entities_endpoints_require_authentication() -> None:
    client = TestClient(app)
    for path in (
        "/api/v1/entities/holdings",
        "/api/v1/entities/themes",
        "/api/v1/entities/trace?issuer_id=issuer:aapl",
        "/api/v1/entities/issuer:aapl/supply-chain",
        "/api/v1/entities/issuer:aapl/analyst-ratings",
        "/api/v1/entities/issuer:aapl",
    ):
        assert client.get(path).status_code == 401


@pytest.mark.anyio
async def test_entities_http_and_mcp_usecases(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "llm_service.api.v1.entities.get_fund_valuations",
        lambda: entities_service.FundValuationsResponse(
            funds=[
                entities_service.FundValuationDto(
                    fund_id="etf:series:S000101292",
                    fund_name="QQQ",
                    report_period="2026-06-30",
                    weighted_gap="0.12",
                )
            ]
        ),
    )
    monkeypatch.setattr(
        "llm_service.api.v1.entities.get_themes_overview",
        lambda: entities_service.ThemesResponse(
            groups=[
                entities_service.ThemePurityGroupDto(
                    theme_id="ai-infrastructure",
                    theme="AI Infrastructure",
                    definition_version="v0",
                    definition_sha256="a" * 64,
                    cutoff="2026-06-30T00:00:00Z",
                    run_id="run:1",
                    rows=[],
                )
            ]
        ),
    )
    monkeypatch.setattr(
        "llm_service.api.v1.entities.get_trace",
        lambda *_a, issuer_id, **_k: entities_service.TraceResponse(
            available=True, issuer_id=issuer_id, trace_id="trace:1"
        ),
    )
    monkeypatch.setattr(
        "llm_service.api.v1.entities.load_supply_chain_exposure",
        lambda *, issuer_id: entities_service.IssuerModuleUnavailable(
            issuer_id=issuer_id, module="supply_chain_exposure"
        ),
    )
    monkeypatch.setattr(
        "llm_service.api.v1.entities.load_analyst_ratings",
        lambda *, issuer_id: entities_service.IssuerModuleUnavailable(issuer_id=issuer_id, module="analyst_ratings"),
    )
    monkeypatch.setattr(
        "llm_service.api.v1.entities.get_entity_detail",
        lambda *_a, issuer_id, **_k: entities_service.EntityDetailResponse(
            issuer_id=issuer_id, label="AAPL · APPLE INC"
        ),
    )

    client, headers = auth_client(monkeypatch)
    h_res = client.get("/api/v1/entities/holdings", headers=headers)
    t_res = client.get("/api/v1/entities/themes", headers=headers)
    tr_res = client.get("/api/v1/entities/trace?issuer_id=issuer:aapl", headers=headers)
    sc_res = client.get("/api/v1/entities/issuer:aapl/supply-chain", headers=headers)
    ar_res = client.get("/api/v1/entities/issuer:aapl/analyst-ratings", headers=headers)
    dt_res = client.get("/api/v1/entities/issuer:aapl", headers=headers)

    assert h_res.status_code == 200 and h_res.json()["funds"][0]["fund_name"] == "QQQ"
    assert t_res.status_code == 200 and t_res.json()["groups"][0]["theme_id"] == "ai-infrastructure"
    assert tr_res.status_code == 200 and tr_res.json()["trace_id"] == "trace:1"
    assert sc_res.status_code == 200 and sc_res.json()["module"] == "supply_chain_exposure"
    assert ar_res.status_code == 200 and ar_res.json()["module"] == "analyst_ratings"
    assert dt_res.status_code == 200 and dt_res.json()["label"] == "AAPL · APPLE INC"
    assert client.get("/api/v1/entities/trace", headers=headers).status_code == 422

    monkeypatch.setattr(
        entities_service,
        "get_strategy_run_page",
        lambda *_a, **_k: strategy_service.StrategyRunPageResponse(
            available=False, reason="empty", strategy_id="large_model_value_v0"
        ),
    )

    class _FakeEtf:
        def get_etf_profile(self, *, fund_id: str) -> dict[str, Any]:
            return {"fund_id": fund_id, "fund_name": "QQQ"}

    class _FakeTheme:
        def get_leaderboard(self, *, theme_id: str, limit: int = 10) -> dict[str, Any]:
            return {"theme_id": theme_id, "limit": limit, "rows": []}

    class _FakeCompany:
        def get_company_profile(self, *, issuer_id: str) -> dict[str, Any]:
            return {"issuer_id": issuer_id, "tier": "tier_1"}

    class _FakeModules:
        def supply_chain_exposure(self, _issuer_id: str) -> None:
            return None

        def analyst_ratings(self, _issuer_id: str) -> None:
            return None

    server = build_mcp_server(
        company_profile_reader=_FakeCompany(),
        theme_purity_reader=_FakeTheme(),
        etf_profile_reader=_FakeEtf(),
        issuer_module_reader=_FakeModules(),  # type: ignore[arg-type]
    )
    _, eh = await server.call_tool("entities_holdings", {"request": {"fund_id": "etf:series:S000101292"}})  # type: ignore[misc]
    _, et = await server.call_tool("entities_themes", {"request": {"theme_id": "ai-infrastructure", "limit": 5}})  # type: ignore[misc]
    _, etr = await server.call_tool("entities_trace", {"request": {"issuer_id": "issuer:aapl"}})  # type: ignore[misc]
    _, esc = await server.call_tool("entities_supply_chain", {"request": {"issuer_id": "issuer:aapl"}})  # type: ignore[misc]
    _, ear = await server.call_tool("entities_analyst_ratings", {"request": {"issuer_id": "issuer:aapl"}})  # type: ignore[misc]
    _, ed = await server.call_tool("entities_detail", {"request": {"issuer_id": "issuer:aapl"}})  # type: ignore[misc]
    assert eh["fund_name"] == "QQQ"
    assert et["theme_id"] == "ai-infrastructure"
    assert etr["issuer_id"] == "issuer:aapl"
    assert esc["result"]["module"] == "supply_chain_exposure"
    assert ear["result"]["module"] == "analyst_ratings"
    assert ed["tier"] == "tier_1"


def test_entities_removes_thread_pools_and_fails_closed_on_db_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    import psycopg
    from llm_service.services.auth_service import PrincipalRecord, access_context_for

    assert not hasattr(entities_service, "ThreadPoolExecutor")

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(entities_service, "with_mart_readonly", _boom)
    monkeypatch.setattr(
        entities_service,
        "get_strategy_run_page",
        lambda *_a, **_k: strategy_service.StrategyRunPageResponse(
            available=False, reason="empty", strategy_id="large_model_value_v0"
        ),
    )

    ctx = access_context_for(PrincipalRecord(principal_id="p:1", tenant_id="t:1", principal_kind="member"))
    with pytest.raises(psycopg.OperationalError, match="connection refused"):
        entities_service.get_entity_detail(ctx, issuer_id="issuer:nvda", database_url="postgresql://unused")
    with pytest.raises(psycopg.OperationalError, match="connection refused"):
        entities_service.get_themes_overview(database_url="postgresql://unused")
    with pytest.raises(psycopg.OperationalError, match="connection refused"):
        entities_service.get_fund_valuations(database_url="postgresql://unused")
    with pytest.raises(psycopg.OperationalError, match="connection refused"):
        entities_service.get_company_profile_payload(issuer_id="issuer:nvda", database_url="postgresql://unused")
    with pytest.raises(psycopg.OperationalError, match="connection refused"):
        entities_service.get_theme_purity_leaderboard(theme_id="ai-infrastructure", database_url="postgresql://unused")
    with pytest.raises(psycopg.OperationalError, match="connection refused"):
        entities_service.get_etf_profile_payload(fund_id="QQQ", database_url="postgresql://unused")
    with pytest.raises(psycopg.OperationalError, match="connection refused"):
        entities_service.get_trace(
            ctx,
            issuer_id="issuer:nvda",
            page=strategy_service.StrategyRunPageResponse(
                available=True, strategy_id="large_model_value_v0", strategy_run_id="r:1", decisions=[]
            ),
            database_url="postgresql://unused",
        )


def test_get_trace_falls_back_to_company_decision_for_non_topt_issuer(monkeypatch: pytest.MonkeyPatch) -> None:
    """QQQ/SP500 issuers outside TOPT's page still resolve their trace decision and lineage via mart."""
    from llm_service.services.auth_service import PrincipalRecord, access_context_for

    seen_calls: list[tuple[str, str, str]] = []
    decision_row = {
        "issuer_id": "issuer:cik:0000882095",
        "strategy_run_id": "strategy-run:qqq-1",
        "corpus_sha256": "b" * 64,
        "cutoff_at": "2026-11-20T01:05:00Z",
        "eligible": True,
        "tier": "tier_1",
        "capital_adjusted_labor_efficiency": "125000",
        "labor_efficiency_metric": "gppe_uniform_charge_v0",
        "valuation_gap": "0.18",
        "current_price_to_sales": "4.2",
        "target_price_to_sales": "5.0",
        "current_price_to_gross_profit": "8.1",
        "target_price_to_gross_profit": "9.6",
        "outcome": "ranked",
        "exclusion_reason": None,
        "rank": 4,
        "target_weight": "0.05",
        "peg": "1.1",
        "peg_rank": 3,
        "peg_reason_codes": [],
        "availability_status": "available",
        "confidence": "0.88",
        "reason_codes": [],
    }

    monkeypatch.setattr(
        entities_service,
        "with_mart_readonly",
        lambda fn, database_url=None: fn(object()),
    )
    monkeypatch.setattr(
        entities_service,
        "resolve_issuer_identity_from_cursor",
        lambda _cur, iid: ([iid], {}),
    )
    monkeypatch.setattr(
        entities_service,
        "_fetch_company_decision_from_cursor",
        lambda _cur, _cand, _leg: decision_row,
    )
    monkeypatch.setattr(
        entities_service,
        "_fetch_issuer_display_map_from_cursor",
        lambda _cur, iid, _cand, _leg: {
            iid: entities_service.EntityDisplayItem(
                issuer_id=iid, ticker="GILD", display_name="GILEAD SCIENCES", label="GILD · GILEAD SCIENCES"
            )
        },
    )
    monkeypatch.setattr(entities_service, "_fetch_trace_nodes_from_cursor", lambda _cur, _cand: [])

    def _fake_lineage(_cur: Any, strategy_run_id: str, issuer_id: str, cutoff_at: str) -> tuple[str, str, list[Any]]:
        seen_calls.append((strategy_run_id, issuer_id, cutoff_at))
        return "result:qqq-1", "listing:xnas:gild", []

    monkeypatch.setattr(entities_service, "read_trace_lineage", _fake_lineage)

    ctx = access_context_for(PrincipalRecord(principal_id="p:1", tenant_id="t:1", principal_kind="member"))
    topt_page = strategy_service.StrategyRunPageResponse(
        available=True,
        strategy_id="large_model_value_v0",
        strategy_run_id="strategy-run:topt-1",
        corpus_sha256="a" * 64,
        decisions=[],
    )
    resp = entities_service.get_trace(ctx, issuer_id="issuer:cik:0000882095", page=topt_page)
    assert resp.available is True
    assert resp.issuer_id == "issuer:cik:0000882095"
    assert resp.label == "GILD · GILEAD SCIENCES"
    assert resp.corpus_sha256 == "b" * 64
    assert resp.lineage_result_id == "result:qqq-1"
    assert resp.decision is not None and resp.decision.tier == "tier_1" and resp.decision.peg == "1.1"
    assert seen_calls == [("strategy-run:qqq-1", "issuer:cik:0000882095", "2026-11-20T01:05:00Z")]


@pytest.mark.parametrize("cutoff", ["bad", "2026-13-45", "yesterday", "'; drop table x--"])
def test_get_trace_refuses_a_cutoff_that_is_not_a_timestamp_before_any_database_read(
    monkeypatch: pytest.MonkeyPatch, cutoff: str
) -> None:
    """A bad `cutoff` once reached `%s::timestamptz` and ended in HTTP 500 (found 2026-11-21 on both environments)."""
    from llm_service.services.auth_service import PrincipalRecord, access_context_for

    def _no_database(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("an invalid cutoff must not reach the database")

    monkeypatch.setattr(entities_service, "get_strategy_run_page", _no_database)
    monkeypatch.setattr(entities_service, "with_mart_readonly", _no_database)
    ctx = access_context_for(PrincipalRecord(principal_id="p:1", tenant_id="t:1", principal_kind="member"))

    resp = entities_service.get_trace(ctx, issuer_id="issuer:cik:1", cutoff=cutoff)

    assert resp.available is False
    assert resp.reason == "invalid_cutoff"
    assert resp.issuer_id == "issuer:cik:1"


@pytest.mark.parametrize(
    "cutoff", ["2026-11-20", "2026-11-20T01:05:00Z", "2026-11-20T01:05:00+00:00", "2026-11-20T09:05:00+08:00"]
)
def test_a_timestamp_cutoff_is_accepted(cutoff: str) -> None:
    assert entities_service._parses_as_instant(cutoff) is True


@pytest.mark.parametrize("status_val", ["low_confidence", "stale", "error"])
def test_entity_decision_row_preserves_status_and_reason_codes(status_val: str) -> None:
    dto = entities_service._decision_row_to_dto(
        {
            "issuer_id": "issuer:aapl",
            "cutoff_at": "2026-11-20T00:00:00Z",
            "corpus_sha256": "c" * 64,
            "tier": "tier_1",
            "current_price_to_sales": "5.0",
            "target_price_to_sales": "6.0",
            "eligible": True,
            "outcome": "selected",
            "availability_status": status_val,
            "source_evidence_status": "degraded",
            "factor_validation_status": "accepted",
            "peg_reason_codes": ["non_positive_growth"],
        },
        display_map={},
    )
    assert dto.availability == status_val
    assert dto.source_evidence_status == "degraded"
    assert dto.factor_validation_status == "accepted"
    assert dto.peg_reason_codes == ["non_positive_growth"]
