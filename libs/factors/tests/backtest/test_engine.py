"""Unit tests for VectorBTBacktestEngine and Anti-Puppet invariants."""

import numpy as np
import pandas as pd
import pytest
from factors.backtest.engine import (
    BacktestEngineConfig,
    VectorBTBacktestEngine,
    canonical_run_id,
)


def _generate_synthetic_data(n_months: int = 36, n_symbols: int = 5):
    dates = pd.date_range("2021-01-31", periods=n_months, freq="ME")
    symbols = [f"SYM_{i}" for i in range(n_symbols)]

    np.random.seed(42)
    base_prices = np.array([100.0, 50.0, 20.0, 10.0, 200.0])[:n_symbols]
    returns = np.random.normal(0.01, 0.05, size=(n_months, n_symbols))
    price_matrix = base_prices * np.cumprod(1.0 + returns, axis=0)
    close_df = pd.DataFrame(price_matrix, index=dates, columns=symbols)

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


def test_month_end_nav_identity():
    """Anti-Puppet: assert monthly NAV and month-end daily NAV are bit-identical (max_dev == 0)."""
    close_m, weights_m = _generate_synthetic_data(n_months=12, n_symbols=3)
    start_d = close_m.index[0]
    end_d = close_m.index[-1]
    daily_dates = pd.bdate_range(start_d, end_d).union(close_m.index).sort_values()
    close_d = close_m.reindex(close_m.index.union(daily_dates)).interpolate(method="time").reindex(daily_dates)

    engine = VectorBTBacktestEngine()
    res = engine.run("strategy_dual", "v1.0", "universe:test", close_m, weights_m, close_daily=close_d)

    assert res.status == "succeeded"
    # Find overlapping cutoff dates
    val_m = res.valuations_monthly.set_index("valuation_date")["cum_nav"]
    val_d = res.valuations_daily.set_index("valuation_date")["cum_nav"]

    common_dates = val_m.index.intersection(val_d.index)
    assert len(common_dates) == len(close_m)

    diffs = np.abs(val_m.loc[common_dates].to_numpy() - val_d.loc[common_dates].to_numpy())
    max_dev = float(np.max(diffs))

    # Strict equality: deviation must be identically 0.0
    assert max_dev == 0.0, f"Max deviation was {max_dev}, expected exactly 0.0"
    assert res.metrics_payload["tie_out_max_deviation"] == 0.0


def test_single_missing_bar_does_not_zero_position():
    """Anti-Puppet: a missing price bar (NaN) must not liquidate position at 0 or crash NAV."""
    dates = pd.date_range("2023-01-01", periods=10, freq="B")

    close_df = pd.DataFrame(
        {
            "SYM_0": [100.0] * 10,
            "SYM_1": [50.0] * 10,
        },
        index=dates,
    )

    # Cutoff rebalances on day 0
    weights_df = pd.DataFrame(
        {
            "SYM_0": [0.5],
            "SYM_1": [0.5],
        },
        index=[dates[0]],
    )

    # Inject missing quote on SYM_0 at day 5
    close_df.loc[dates[5], "SYM_0"] = np.nan

    engine = VectorBTBacktestEngine(BacktestEngineConfig(fees=0.0, slippage=0.0))
    res = engine.run("strategy_missing", "v1.0", "universe:test", close_df.iloc[[0]], weights_df, close_daily=close_df)

    navs = res.valuations_daily["cum_nav"].to_numpy()
    # At day 5, NAV must stay at ~1.0 using last known price (100.0), NOT cliff-drop to ~0.50 (-50%)
    assert np.isclose(navs[5], 1.0, atol=1e-3), f"NAV plunged to {navs[5]} due to 0-liquidation!"
    # Ensure no trades occurred on day 5
    trades_day5 = [t for t in res.trades if t["trade_date"] == str(dates[5].date())]
    assert len(trades_day5) == 0, "Fake liquidation trade executed on missing quote day!"


def test_different_fees_yield_different_run_id():
    """Anti-Puppet: canonical_run_id must hash bind fees and produce distinct IDs."""
    id1 = canonical_run_id("strat", "v1", "u1", "2023-01-01", "2023-12-31", fees=0.001)
    id2 = canonical_run_id("strat", "v1", "u1", "2023-01-01", "2023-12-31", fees=0.002)
    id3 = canonical_run_id("strat", "v1", "u1", "2023-01-01", "2023-12-31", slippage=0.001)
    id4 = canonical_run_id("strat", "v1", "u1", "2023-01-01", "2023-12-31", top_k=10)

    assert id1 != id2
    assert id1 != id3
    assert id1 != id4


def test_backtest_parameter_validation_raises():
    close_df, weights_df = _generate_synthetic_data(n_months=12, n_symbols=3)
    engine = VectorBTBacktestEngine()

    bad_weights = weights_df.rename(columns={"SYM_0": "SYM_OTHER"})
    with pytest.raises(ValueError, match="Asset mismatch"):
        engine.run("strat", "v1", "u1", close_df, bad_weights)

    unsorted_close = close_df.iloc[::-1]
    with pytest.raises(ValueError, match="Monthly index must be sorted"):
        engine.run("strat", "v1", "u1", unsorted_close, weights_df)

    unsorted_weights = weights_df.iloc[::-1]
    with pytest.raises(ValueError, match="Weights index must be sorted"):
        engine.run("strat", "v1", "u1", close_df, unsorted_weights)

    bad_daily = close_df.rename(columns={"SYM_0": "SYM_OTHER"})
    with pytest.raises(ValueError, match="Daily columns mismatch"):
        engine.run("strat", "v1", "u1", close_df, weights_df, close_daily=bad_daily)
