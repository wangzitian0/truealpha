"""Domain 1 (`panel`) HTTP + MCP behavioral tests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from llm_service.main import app
from llm_service.mcp_server import build_mcp_server
from llm_service.services import panel_service, strategy_service

from ._helpers import auth_client


def test_panel_endpoints_require_authentication() -> None:
    client = TestClient(app)
    for path in ("/api/v1/panel/universes", "/api/v1/panel/coverage", "/api/v1/panel/macro"):
        assert client.get(path).status_code == 401


@pytest.mark.anyio
async def test_panel_http_and_mcp_usecases(monkeypatch: pytest.MonkeyPatch) -> None:
    sample_universes = panel_service.UniverseOverviewResponse(
        heads=[{"kind": "topt", "sequence": 1, "universe_id": "universe:topt-us-2026-03-31"}],
        history=[{"kind": "topt", "sequence": 1}],
        members={"topt": [{"ticker": "NVDA"}]},
    )
    sample_macro = panel_service.MacroPanelResponse(
        available=True,
        series=[
            panel_service.MacroSeriesDto(
                series_id="us_treasury_note_10_year",
                label="10Y Treasury",
                unit="pct",
                source_id="fred",
                max_age_days=7,
                is_fresh=True,
                latest={"observation_date": "2026-11-16", "value": "4.25", "age_days": 1},
            )
        ],
        months=[{"series_id": "us_treasury_note_10_year", "observation_date": "2026-11-16", "value": "4.25"}],
    )

    class _FakeOpsCur:
        def __init__(self) -> None:
            self._sql = ""

        def execute(self, sql: str, _params: Any = None) -> None:
            self._sql = sql

        def fetchall(self) -> list[dict[str, Any]]:
            if "staging.accepted_rulesets" in self._sql:
                return [
                    {
                        "kind": "topt",
                        "sequence": 1,
                        "note": "ok",
                        "advanced_at": "2026-03-31T00:00:00Z",
                        "universe_id": "universe:topt-us-2026-03-31",
                        "instrument_count": 1,
                        "mapping_sha256": "a" * 64,
                    }
                ]
            return [
                {
                    "ticker": "NVDA",
                    "company_name": "NVIDIA Corp",
                    "cik": 1045810,
                    "figi": "BBG000BBJQV0",
                    "market_cap": "1000",
                    "as_of": "2026-03-31",
                    "source": "sec",
                    "fetch_sha256": "b" * 64,
                    "object_uri": "s3://raw/1",
                }
            ]

    class _FakeMartCur:
        def __init__(self) -> None:
            self._sql = ""

        def execute(self, sql: str, _params: Any = None) -> None:
            self._sql = sql

        def fetchall(self) -> list[dict[str, Any]]:
            if "mart.macro_series_status" in self._sql:
                return [
                    {
                        "series_id": "us_treasury_note_10_year",
                        "label": "10Y Treasury",
                        "unit": "pct",
                        "source_id": "fred",
                        "max_age_days": 7,
                        "is_fresh": True,
                        "observation_day": "2026-11-16",
                        "value": "4.25",
                        "source": "fred",
                        "fetched_at": datetime(2026, 11, 17, tzinfo=UTC),
                        "age_days": 1,
                    }
                ]
            if "mart.macro_series_current" in self._sql:
                return [{"series_id": "us_treasury_note_10_year", "observation_day": "2026-11-16", "value": "4.25"}]
            return []

    monkeypatch.setattr(panel_service, "with_ops_reader", lambda fn, **_k: fn(_FakeOpsCur()))
    monkeypatch.setattr(panel_service, "with_mart_readonly", lambda fn, **_k: fn(_FakeMartCur()))
    monkeypatch.setattr(
        strategy_service,
        "get_strategy_run_page",
        lambda *_a, **_k: strategy_service.StrategyRunPageResponse(
            available=True,
            strategy_id="large_model_value_v0",
            strategy_run_id="strategy-run:1",
            cutoff_at="2026-06-30T00:00:00Z",
            universe="all",
            total_decisions=0,
            decisions=[],
        ),
    )

    client, headers = auth_client(monkeypatch)
    uni_res = client.get("/api/v1/panel/universes", headers=headers)
    cov_res = client.get("/api/v1/panel/coverage?limit=10", headers=headers)
    mac_res = client.get("/api/v1/panel/macro", headers=headers)
    assert uni_res.status_code == 200 and uni_res.json()["members"]["topt"][0]["ticker"] == "NVDA"
    assert cov_res.status_code == 200 and cov_res.json()["strategy_run_id"] == "strategy-run:1"
    assert mac_res.status_code == 200 and mac_res.json()["series"][0]["latest"]["value"] == "4.25"
    assert client.get("/api/v1/panel/coverage?limit=9999", headers=headers).status_code == 422

    class _FakeMetricNodeRepo:
        def latest(
            self, *, node_key: str, issuer_id: str | None = None, limit: int = 100
        ) -> panel_service.MetricNodeValueUnavailable:
            del issuer_id, limit
            return panel_service.MetricNodeValueUnavailable(node_key=node_key, reason="no_served_head")

    monkeypatch.setattr(panel_service, "load_universes_panel", lambda **_k: sample_universes)
    monkeypatch.setattr(panel_service, "load_macro_curve", lambda **_k: sample_macro)
    import llm_service.mcp_server as mcp_mod

    monkeypatch.setattr(mcp_mod, "load_universes_panel", lambda **_k: sample_universes)
    monkeypatch.setattr(mcp_mod, "load_macro_curve", lambda **_k: sample_macro)
    server = build_mcp_server(metric_node_repository=_FakeMetricNodeRepo())  # type: ignore[arg-type]

    _, uni_mcp = await server.call_tool("panel_universes", {})  # type: ignore[misc]
    _, cov_mcp = await server.call_tool("panel_coverage", {"request": {"node_key": "gppe_uniform_charge_v0"}})  # type: ignore[misc]
    _, mac_mcp = await server.call_tool("panel_macro", {})  # type: ignore[misc]
    assert uni_mcp["heads"][0]["kind"] == "topt"
    assert cov_mcp["result"]["node_key"] == "gppe_uniform_charge_v0"
    assert mac_mcp["available"] is True
