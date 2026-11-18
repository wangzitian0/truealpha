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
