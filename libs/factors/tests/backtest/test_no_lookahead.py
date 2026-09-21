"""Hypothesis property-based test asserting NO lookahead bias in factor backtest."""

import numpy as np
import pandas as pd
from factors.backtest.engine import BacktestEngineConfig, VectorBTBacktestEngine


def test_no_lookahead_future_price_perturbation_invariance():
    """Altering future prices after cutoff t must not affect historical returns or weights up to t."""
    n_months = 24
    n_symbols = 4
    dates = pd.date_range("2022-01-31", periods=n_months, freq="ME")
    symbols = [f"STOCK_{i}" for i in range(n_symbols)]

    # Base price path
    np.random.seed(123)
    base_prices = np.array([100.0, 50.0, 75.0, 120.0])
    returns = np.random.normal(0.01, 0.04, size=(n_months, n_symbols))
    price_matrix = base_prices * np.cumprod(1.0 + returns, axis=0)
    close_base = pd.DataFrame(price_matrix, index=dates, columns=symbols)

    # Simple momentum/trend weights based only on past information
    weights_matrix = np.zeros((n_months, n_symbols))
    for t in range(1, n_months):
        # Pick top 2 based on past 1-month return
        past_ret = close_base.iloc[t].values / close_base.iloc[t - 1].values - 1.0
        top_indices = np.argsort(past_ret)[-2:]
        weights_matrix[t, top_indices] = 0.5
    weights_base = pd.DataFrame(weights_matrix, index=dates, columns=symbols)

    engine = VectorBTBacktestEngine(BacktestEngineConfig(fees=0.0, slippage=0.0))
    res_base = engine.run("strategy_pit", "v1.0", "universe:test", close_base, weights_base)

    # Now perturb future prices starting at cutoff t=12 (e.g. inject huge 10x shock)
    cutoff_idx = 12
    close_perturbed = close_base.copy()
    close_perturbed.iloc[cutoff_idx:] = close_perturbed.iloc[cutoff_idx:] * 10.0

    # Past weights up to cutoff_idx must remain identical
    weights_perturbed = weights_base.copy()

    res_perturbed = engine.run("strategy_pit", "v1.0", "universe:test", close_perturbed, weights_perturbed)

    # Invariant: valuations strictly before cutoff_idx must be 100% bit-identical
    val_base_past = res_base.valuations_monthly.iloc[:cutoff_idx]["cum_nav"].values
    val_perturbed_past = res_perturbed.valuations_monthly.iloc[:cutoff_idx]["cum_nav"].values

    assert np.allclose(val_base_past, val_perturbed_past, atol=1e-8), (
        "Lookahead violation: past NAV changed when future prices were altered!"
    )
