"""Unit tests for VectorBTBacktestEngine."""

import numpy as np
import pandas as pd
import pytest
from factors.backtest.engine import (
    BacktestEngineConfig,
    VectorBTBacktestEngine,
)


def _generate_synthetic_data(n_months: int = 36, n_symbols: int = 5):
    dates = pd.date_range("2021-01-31", periods=n_months, freq="ME")
    symbols = [f"SYM_{i}" for i in range(n_symbols)]

    # Deterministic price series
    np.random.seed(42)
    base_prices = np.array([100.0, 50.0, 20.0, 10.0, 200.0])[:n_symbols]
    returns = np.random.normal(0.01, 0.05, size=(n_months, n_symbols))
    price_matrix = base_prices * np.cumprod(1.0 + returns, axis=0)
    close_df = pd.DataFrame(price_matrix, index=dates, columns=symbols)

    # Equal weights on top 2 assets each month
    weights_matrix = np.zeros((n_months, n_symbols))
    for t in range(n_months):
        weights_matrix[t, 0] = 0.5
        weights_matrix[t, 1] = 0.5
    weights_df = pd.DataFrame(weights_matrix, index=dates, columns=symbols)

    return close_df, weights_df


def test_backtest_engine_determinism():
    close_df, weights_df = _generate_synthetic_data(n_months=24, n_symbols=5)
    engine = VectorBTBacktestEngine(BacktestEngineConfig(fees=0.001, slippage=0.0005))

    res1 = engine.run("strategy_smoke", "v1.0", "universe:test", close_df, weights_df)
    res2 = engine.run("strategy_smoke", "v1.0", "universe:test", close_df, weights_df)

    assert res1.run_id == res2.run_id
    assert res1.cagr_monthly == res2.cagr_monthly
    assert res1.max_dd_daily == res2.max_dd_daily
    assert res1.status == "succeeded"
    assert len(res1.trades) == len(res2.trades)
    assert np.allclose(res1.valuations_monthly["cum_nav"], res2.valuations_monthly["cum_nav"])


def test_backtest_engine_handles_unlisted_and_ipo_gracefully():
    close_df, weights_df = _generate_synthetic_data(n_months=24, n_symbols=5)
    # Simulate SYM_4 as an unlisted asset during the first 12 months
    close_df.iloc[:12, 4] = np.nan
    # Weight on SYM_4 must be 0 when unlisted
    weights_df.iloc[:12, 4] = 0.0
    weights_df.iloc[12:, 4] = 0.5
    weights_df.iloc[12:, 0] = 0.0  # replace SYM_0 with SYM_4

    engine = VectorBTBacktestEngine()
    res = engine.run("strategy_ipo", "v1.0", "universe:test", close_df, weights_df)

    assert res.status == "succeeded"
    # Verify no NaN in valuations
    assert not res.valuations_monthly["cum_nav"].isna().any()
    assert not res.valuations_monthly["drawdown"].isna().any()


def test_backtest_dual_resolution_tie_out():
    close_m, weights_m = _generate_synthetic_data(n_months=12, n_symbols=3)
    # Generate daily data for the last 6 months
    start_d = close_m.index[6]
    end_d = close_m.index[-1]
    # Ensure daily index includes the monthly dates so reconciliation logic is exercised
    daily_dates = pd.bdate_range(start_d, end_d).union(close_m.index[6:]).sort_values()

    # Interpolate daily close from monthly
    close_d = close_m.reindex(close_m.index.union(daily_dates)).interpolate(method="time").reindex(daily_dates)

    engine = VectorBTBacktestEngine()
    res = engine.run("strategy_dual", "v1.0", "universe:test", close_m, weights_m, close_daily=close_d)

    assert res.status == "succeeded"
    assert res.sharpe_daily != 0.0
    assert len(res.valuations_daily) == len(daily_dates)
    assert len(res.valuations_monthly) == len(close_m)
    # Verify tie-out reconciliation was exercised and recorded in metrics_payload
    assert "tie_out_max_deviation" in res.metrics_payload
    assert res.metrics_payload["tie_out_max_deviation"] <= engine.config.nav_tie_out_tolerance


def test_backtest_dual_resolution_tie_out_exceeds_tolerance_raises():
    close_m, weights_m = _generate_synthetic_data(n_months=12, n_symbols=3)
    start_d = close_m.index[6]
    end_d = close_m.index[-1]
    daily_dates = pd.bdate_range(start_d, end_d).union(close_m.index[6:]).sort_values()
    close_d = close_m.reindex(close_m.index.union(daily_dates)).interpolate(method="time").reindex(daily_dates)

    # With a strict tolerance like 0.0001 (1 bp), the ~1% daily rebalance deviation must raise ValueError
    strict_engine = VectorBTBacktestEngine(BacktestEngineConfig(nav_tie_out_tolerance=0.0001))
    with pytest.raises(ValueError, match="Monthly-daily NAV tie-out deviation"):
        strict_engine.run("strategy_dual", "v1.0", "universe:test", close_m, weights_m, close_daily=close_d)


def test_backtest_parameter_validation_raises():
    close_df, weights_df = _generate_synthetic_data(n_months=12, n_symbols=3)
    engine = VectorBTBacktestEngine()

    # Column mismatch
    bad_weights = weights_df.rename(columns={"SYM_0": "SYM_OTHER"})
    with pytest.raises(ValueError, match="Asset mismatch"):
        engine.run("strat", "v1", "u1", close_df, bad_weights)

    # Unsorted monthly close
    unsorted_close = close_df.iloc[::-1]
    with pytest.raises(ValueError, match="Monthly index must be sorted"):
        engine.run("strat", "v1", "u1", unsorted_close, weights_df)

    # Unsorted monthly weights
    unsorted_weights = weights_df.iloc[::-1]
    with pytest.raises(ValueError, match="Weights index must be sorted"):
        engine.run("strat", "v1", "u1", close_df, unsorted_weights)

    # Daily column mismatch
    bad_daily = close_df.rename(columns={"SYM_0": "SYM_OTHER"})
    with pytest.raises(ValueError, match="Daily columns mismatch"):
        engine.run("strat", "v1", "u1", close_df, weights_df, close_daily=bad_daily)
