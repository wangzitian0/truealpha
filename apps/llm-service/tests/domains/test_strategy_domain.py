"""Domain 2 (`strategy`) HTTP + MCP behavioral tests."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from llm_service.main import app
from llm_service.mcp_server import build_mcp_server
from llm_service.services import strategy_service

from ._helpers import auth_client, load_fixture_strategy_repo


def test_strategy_endpoints_require_authentication() -> None:
    client = TestClient(app)
    for path in (
        "/api/v1/strategy/rankings",
        "/api/v1/strategy/backtests",
        "/api/v1/strategy/backtests/backtest-run:1",
    ):
        assert client.get(path).status_code == 401


@pytest.mark.anyio
async def test_strategy_http_and_mcp_usecases(monkeypatch: pytest.MonkeyPatch) -> None:
    sample_run = strategy_service.BacktestRunDto(
        run_id="backtest-run:1",
        strategy_key="large_model_value_v0",
        strategy_version="1.0.0",
        universe_id="universe:topt-us-2026-03-31",
        start_date="2025-01-01",
        end_date="2026-03-31",
        status="succeeded",
        cagr_monthly=Decimal("0.25"),
        benchmark_cagr=Decimal("0.15"),
        executed_at="2026-03-31T00:00:00Z",
        created_at="2026-03-31T00:00:00Z",
    )
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
    monkeypatch.setattr(strategy_service, "with_mart_readonly", lambda *_a, **_k: {})
    monkeypatch.setattr(
        "llm_service.api.v1.strategy.list_backtest_runs",
        lambda **_k: strategy_service.BacktestRunsResponse(runs=[sample_run]),
    )
    monkeypatch.setattr(
        "llm_service.api.v1.strategy.get_backtest_detail",
        lambda run_id: strategy_service.BacktestDetailResponse(run=sample_run) if run_id == "backtest-run:1" else None,
    )

    client, headers = auth_client(monkeypatch)
    rank_res = client.get("/api/v1/strategy/rankings?universe=all", headers=headers)
    bt_list = client.get("/api/v1/strategy/backtests?limit=5", headers=headers)
    bt_ok = client.get("/api/v1/strategy/backtests/backtest-run:1", headers=headers)
    bt_404 = client.get("/api/v1/strategy/backtests/backtest-run:missing", headers=headers)
    assert rank_res.status_code == 200 and rank_res.json()["strategy_run_id"] == "strategy-run:1"
    assert bt_list.status_code == 200 and bt_list.json()["runs"][0]["run_id"] == "backtest-run:1"
    assert bt_ok.status_code == 200 and bt_ok.json()["run"]["status"] == "succeeded"
    assert bt_404.status_code == 404

    class _FakeBacktestReader:
        def get_backtest_report(
            self, *, run_id: str | None = None, strategy_key: str | None = None, limit: int = 10
        ) -> dict[str, Any]:
            if run_id is not None:
                return {"runs": [{"run_id": run_id}], "count": 1, "availability_status": "available"}
            return {"runs": [{"run_id": "backtest-run:1", "strategy_key": strategy_key, "limit": limit}], "count": 1}

    server = build_mcp_server(
        repository=load_fixture_strategy_repo(),
        backtest_reader=_FakeBacktestReader(),
    )
    _, r_mcp = await server.call_tool("strategy_rankings", {"request": {"strategy_id": "large_model_value_v0"}})  # type: ignore[misc]
    _, b_mcp = await server.call_tool(
        "strategy_backtests", {"request": {"strategy_key": "large_model_value_v0", "limit": 5}}
    )  # type: ignore[misc]
    _, bd_mcp = await server.call_tool("strategy_backtest_detail", {"request": {"run_id": "backtest-run:1"}})  # type: ignore[misc]
    assert r_mcp["result"]["strategy_id"] == "large_model_value_v0"
    assert b_mcp["runs"][0]["strategy_key"] == "large_model_value_v0"
    assert bd_mcp["runs"][0]["run_id"] == "backtest-run:1"


def test_strategy_removes_thread_pool_and_fails_closed_on_db_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    import psycopg

    assert not hasattr(strategy_service, "ThreadPoolExecutor")

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(strategy_service, "with_mart_readonly", _boom)
    with pytest.raises(psycopg.OperationalError, match="connection refused"):
        strategy_service._load_strategy_provenance("strategy-run:1", database_url="postgresql://unused")
    with pytest.raises(psycopg.OperationalError, match="connection refused"):
        strategy_service.list_backtest_runs(database_url="postgresql://unused")
    with pytest.raises(psycopg.OperationalError, match="connection refused"):
        strategy_service.get_backtest_detail("backtest-run:1", database_url="postgresql://unused")
    with pytest.raises(psycopg.OperationalError, match="connection refused"):
        strategy_service.PostgresBacktestReader(database_url="postgresql://unused").get_backtest_report(
            strategy_key="large_model_value_v0"
        )


def test_multi_universe_strategy_role_helpers_do_not_require_singleton() -> None:
    from truealpha_contracts.universes import universes_with_role

    strategy_keys = universes_with_role("strategy")
    assert "topt" in strategy_keys
    assert strategy_service.normalize_ranking_universe(None) in strategy_keys
    assert strategy_service.normalize_ranking_universe("qqq") == "qqq"
    assert strategy_service.normalize_ranking_universe("sp500") == "sp500"
    assert strategy_service.normalize_ranking_universe("all") == "all"


@pytest.mark.criterion("R3.C4")
@pytest.mark.anyio
async def test_r3_c4_rest_and_mcp_switch_peg_conventions_and_unblock_sp500_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R3.C4: /api/v1/strategy/rankings and MCP strategy_rankings switch between historical_cagr and consensus."""
    from datetime import UTC, datetime

    from truealpha_runtime.strategy_run import StrategyRunDecision, StrategyRunOutcome, StrategyRunReport

    recorded: list[tuple[str | None, str | None]] = []

    class _DualConventionRepo:
        def get_latest(
            self,
            *,
            strategy_id: str,
            context: Any,
            universe: str | None = None,
            convention: str | None = None,
        ) -> StrategyRunReport:
            del context
            recorded.append((universe, convention))
            is_cons = convention == "consensus"
            decisions = tuple(
                StrategyRunDecision(
                    issuer_id=f"issuer:sp{idx:03d}",
                    cutoff_at=datetime(2026, 3, 31, 23, 59, 59, tzinfo=UTC),
                    outcome=StrategyRunOutcome.RANKED_BEYOND_SELECTION_COUNT,
                    eligible=True,
                    peg=Decimal("0.42") if is_cons else Decimal("1.15"),
                    peg_rank=1 if is_cons else 2,
                    rank=idx + 1,
                )
                for idx in range(250)
            )
            return StrategyRunReport(
                strategy_id=strategy_id,  # type: ignore[arg-type]
                source="mart",
                corpus_sha256="a" * 64,
                decisions=decisions,
                strategy_run_id="strategy-run:" + "b" * 64,
            )

    repo = _DualConventionRepo()
    monkeypatch.setattr(strategy_service, "PostgresStrategyRunRepository", lambda **_k: repo)
    monkeypatch.setattr(strategy_service, "load_entity_display_items", lambda **_k: {})
    monkeypatch.setattr(strategy_service, "_load_strategy_provenance", lambda *_a, **_k: {})
    monkeypatch.setattr(strategy_service, "_load_wide_panel", lambda *_a, **_k: {})
    monkeypatch.setattr(strategy_service, "matches_ranking_universe", lambda *_a, **_k: True)

    client, headers = auth_client(monkeypatch)
    hist_res = client.get("/api/v1/strategy/rankings?universe=all&convention=historical_cagr", headers=headers)
    cons_res = client.get("/api/v1/strategy/rankings?universe=all&convention=consensus", headers=headers)
    assert hist_res.status_code == 200 and cons_res.status_code == 200
    assert hist_res.json()["convention"] == "historical_cagr"
    assert cons_res.json()["convention"] == "consensus"
    assert len(cons_res.json()["rows"]) == 250  # Default limit=1000 no longer truncates at 200!
    assert cons_res.json()["rows"][0]["peg"] == "0.42"
    assert cons_res.json()["rows"][0]["peg_rank"] == 1
    assert hist_res.json()["rows"][0]["peg"] == "1.15"
    assert hist_res.json()["rows"][0]["peg_rank"] == 2

    server = build_mcp_server(repository=repo)
    _, cons_mcp = await server.call_tool(
        "strategy_rankings",
        {"request": {"strategy_id": "large_model_value_v0", "universe": "sp500", "convention": "consensus"}},
    )  # type: ignore[misc]
    assert cons_mcp["result"]["decisions"][0]["peg"] == "0.42"
    assert cons_mcp["result"]["decisions"][0]["peg_rank"] == 1
    assert recorded[-1] == ("sp500", "consensus")


def test_strategy_page_passes_universe_and_drops_float_casts(monkeypatch: pytest.MonkeyPatch) -> None:
    import inspect

    from llm_service.services.auth_service import PrincipalRecord, access_context_for
    from truealpha_runtime.strategy_run import StrategyRunUnavailable

    assert "::float" not in inspect.getsource(strategy_service)
    assert "::float" not in strategy_service._BACKTEST_RUN_SELECT

    recorded: list[tuple[str, str | None]] = []

    class _FakeRepo:
        def __init__(self, *, database_url: str) -> None:
            del database_url

        def get_latest(self, *, strategy_id: str, context: Any, universe: str | None = None) -> StrategyRunUnavailable:
            del context
            recorded.append((strategy_id, universe))
            return StrategyRunUnavailable(strategy_id=strategy_id, reason="no_runs_recorded")

    monkeypatch.setattr(strategy_service, "PostgresStrategyRunRepository", _FakeRepo)
    ctx = access_context_for(PrincipalRecord(principal_id="p:1", tenant_id="t:1", principal_kind="member"))
    page = strategy_service.get_strategy_run_page(ctx, universe="qqq", database_url="postgresql://unused")
    assert not page.available
    assert page.universe == "qqq"
    assert recorded == [(strategy_service.DASHBOARD_STRATEGY_ID, "qqq")]
