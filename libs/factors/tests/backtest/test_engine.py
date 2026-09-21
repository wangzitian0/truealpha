"""Unit tests for VectorBTBacktestEngine."""

import numpy as np
import pandas as pd
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
    daily_dates = pd.date_range(start_d, end_d, freq="B")

    # Interpolate daily close from monthly
    close_d = close_m.reindex(close_m.index.union(daily_dates)).interpolate(method="time").reindex(daily_dates)

    engine = VectorBTBacktestEngine()
    res = engine.run("strategy_dual", "v1.0", "universe:test", close_m, weights_m, close_daily=close_d)

    assert res.status == "succeeded"
    assert res.sharpe_daily != 0.0
    assert len(res.valuations_daily) == len(daily_dates)
    assert len(res.valuations_monthly) == len(close_m)
