# ruff: noqa: I001, E401, E402, E701, E702, E731, F401, F811
# fmt: off
from datetime import UTC, datetime, timedelta; from unittest.mock import MagicMock; import dagster as dg; import pytest; from data_engine.datahub.analyst_ratings import UniverseCapture; from data_engine.datahub.fetch_key import FetchKey; from data_engine.datahub.question_coverage import GovernedHead; from data_engine.lanes import standards; from data_engine.lanes.standards import StandardBackfillConfig, run_analyst_ratings; from data_engine.sources import gateway; from data_engine.sources import moomoo as mm; from data_engine.sources import moomoo_ledger as ledger
def _kline(code: str) -> None: mm._call( object(), "request_history_kline", "test", lambda ctx: (mm.moomoo.RET_OK, []), request_uri=code, identity=FetchKey("moomoo", "request_history_kline", f"symbol:{code}", "2026-10-27"), )
def test_gate_allows_calls_under_quota(moomoo_ledger_db, monkeypatch): monkeypatch.setattr(ledger, "distinct_stock_quota", lambda: 2); _kline("US.AAPL"); assert ledger.calls_this_month() == 1; assert ledger.kline_distinct_stocks_7d() == {"moomoo://US.AAPL"}
def test_gate_blocks_once_7d_kline_stock_quota_exhausted(moomoo_ledger_db, monkeypatch):
    monkeypatch.setattr(ledger, "distinct_stock_quota", lambda: 1); _kline("US.AAPL"); ledger.gate("request_history_kline", "test", request_uri="moomoo://US.AAPL")
    with pytest.raises(ledger.BudgetExceededError): ledger.gate("request_history_kline", "test", request_uri="moomoo://US.MSFT")
def test_kline_distinct_stock_quota_staging_share_20(call_ledger, monkeypatch):
    monkeypatch.setattr(gateway, "capacity_environment", lambda: "staging"); ledger.reset_stored_history_kl_quota()
    try:
        ledger.store_history_kl_quota(100); assert ledger.distinct_stock_quota() == 20
        for i in range(20): _kline(f"US.STOCK{i}")
        assert len(ledger.kline_distinct_stocks_7d()) == 20; ledger.gate("request_history_kline", "test", request_uri="moomoo://US.STOCK0")
        with pytest.raises(ledger.BudgetExceededError) as exc_info: ledger.gate("request_history_kline", "test", request_uri="moomoo://US.STOCK21")
        assert "20/20 stocks used in the last 7 days" in str(exc_info.value)
    finally: ledger.reset_stored_history_kl_quota()
def test_two_moomoo_calls_at_share_one_capture_then_run_analyst_ratings(call_ledger, monkeypatch):
    capacity = gateway.SourceCapacity("moomoo", window_seconds=30.0, calls_per_window=1); monkeypatch.setattr(gateway, "CAPACITIES", {**gateway.CAPACITIES, "moomoo": capacity})
    with gateway.capacity_scope(): mm._call( object(), "financials_income", "capture_tick", lambda ctx: (mm.moomoo.RET_OK, []), identity=FetchKey("moomoo", "financials_income", "symbol:US.AAPL", "2026-10-27"), )
    assert len(call_ledger) == 1; sleeps: list[float] = []; real_scope = gateway.capacity_scope
    def fake_scope(gate=None): g = gate or gateway.CapacityGate( capacities={**gateway.CAPACITIES, "moomoo": capacity}, max_wait_seconds=0.0, sleep=sleeps.append, ); return real_scope(g)
    monkeypatch.setattr(gateway, "capacity_scope", fake_scope); monkeypatch.setattr(standards.psycopg, "connect", MagicMock()); monkeypatch.setattr( "data_engine.datahub.question_coverage.governed_head_of", lambda *a, **k: GovernedHead("topt", "run_1", datetime.now(UTC)), ); monkeypatch.setattr("data_engine.datahub.standards.planner.universe_issuers", lambda *a, **k: []); mock_universe = MagicMock(); mock_universe.lane_failure.return_value = None; mock_universe.issuers = [MagicMock()]; mock_universe.tickers.return_value = {"id1": "AAPL"}; mock_universe.unmapped = (); mock_universe.unmapped_by_reason.return_value = {}; mock_universe.wide_row_issuers = 1; monkeypatch.setattr(standards, "_canonical_universe_or_failure", lambda *a, **k: (mock_universe, None))
    def fake_materialize(*args, **kwargs): mm._call( object(), "get_research_analyst_consensus", "run_analyst_ratings", lambda ctx: (mm.moomoo.RET_OK, []), identity=FetchKey("moomoo", "get_research_analyst_consensus", "symbol:US.AAPL", "2026-10-27"), ); return UniverseCapture(rows=1)
    monkeypatch.setattr( "data_engine.datahub.analyst_ratings.materialize_universe_analyst_ratings", fake_materialize, ); config = StandardBackfillConfig(executed_at="2026-10-06T00:00:00+00:00", universe="topt"); context = dg.build_op_context()
    with pytest.raises(gateway.CapacityExceeded, match="rate window"): run_analyst_ratings(context, config, "{}")
    start_dt = call_ledger[0].called_at; sim_clock = {"mono": 0.0, "dt": start_dt}
    def sim_sleep(seconds: float) -> None: sleeps.append(seconds); sim_clock["mono"] += seconds; sim_clock["dt"] += timedelta(seconds=seconds)
    def fake_scope_wait(gate=None): g = gate or gateway.CapacityGate( capacities={**gateway.CAPACITIES, "moomoo": capacity}, max_wait_seconds=900.0, clock=lambda: sim_clock["mono"], now=lambda: sim_clock["dt"], sleep=sim_sleep, ); return real_scope(g)
    monkeypatch.setattr(gateway, "capacity_scope", fake_scope_wait); run_analyst_ratings(context, config, "{}"); assert len(sleeps) == 1; assert 29.0 <= sleeps[0] <= 31.0
def test_get_history_kline_refreshes_reading_and_enforces_quota(call_ledger, monkeypatch):
    ledger.reset_stored_history_kl_quota(); assert ledger.stored_history_kl_quota() is None; monkeypatch.setattr(gateway, "capacity_environment", lambda: "staging")
    class _Ctx:
        def get_history_kl_quota(self, get_detail=False): return (mm.moomoo.RET_OK, (5, 95, []))
        def request_history_kline(self, code, **kwargs): return (mm.moomoo.RET_OK, [{"close": 100.5, "time_key": "2026-07-29 00:00:00"}], None)
    ctx = _Ctx(); bars, _ = mm.get_history_kline( ctx, "US.STOCK1", start="2026-07-15", end="2026-07-29", identity=FetchKey("moomoo", "history_kline", "symbol:STOCK1", "2026-07-29"), ); assert bars is not None; assert ledger.stored_history_kl_quota() == 100; assert ledger.distinct_stock_quota() == 20
