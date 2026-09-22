"""Unit tests for Rebalancing and VectorBT Adapter."""

import numpy as np
import pandas as pd
import polars as pl
from factors.backtest.adapter import (
    compile_factor_panel,
    compute_topk_dropout_weights,
    pivot_to_vbt_matrices,
)
from factors.expressions.dsl import Div, Ref, Sub


def test_compile_factor_panel_basic() -> None:
    prices = pl.DataFrame(
        {
            "date": ["2023-01-02", "2023-01-01", "2023-01-03", "2023-01-03", "2023-01-01", "2023-01-02"],
            "symbol": ["AAPL", "AAPL", "AAPL", "MSFT", "MSFT", "MSFT"],
            "close": [110.0, 100.0, 120.0, 220.0, 200.0, 210.0],
        }
    )

    ret_expr = Div(Sub("close", Ref("close", 1)), Ref("close", 1))
    panel = compile_factor_panel(ret_expr, prices)

    assert panel["symbol"].to_list() == ["AAPL", "AAPL", "AAPL", "MSFT", "MSFT", "MSFT"]
    assert panel["date"].to_list() == [
        "2023-01-01",
        "2023-01-02",
        "2023-01-03",
        "2023-01-01",
        "2023-01-02",
        "2023-01-03",
    ]

    aapl_vals = panel.filter(pl.col("symbol") == "AAPL")["factor_value"].to_list()
    assert aapl_vals[0] is None
    assert np.isclose(aapl_vals[1], 0.10)
    assert np.isclose(aapl_vals[2], (120 - 110) / 110)


def test_compile_factor_panel_with_dynamic_universe_mask() -> None:
    prices = pl.DataFrame(
        {
            "date": ["2023-01-31", "2023-01-31", "2023-02-28", "2023-02-28"],
            "symbol": ["AAPL", "UNSEASONED", "AAPL", "UNSEASONED"],
            "close": [150.0, 50.0, 155.0, 55.0],
        }
    )

    mask = pl.DataFrame(
        {
            "cutoff_date": ["2023-01-31", "2023-01-31", "2023-02-28", "2023-02-28"],
            "symbol": ["AAPL", "UNSEASONED", "AAPL", "UNSEASONED"],
            "eligible": [True, False, True, True],
        }
    )

    panel = compile_factor_panel("close", prices, mask_df=mask)

    row1 = panel.filter((pl.col("symbol") == "UNSEASONED") & (pl.col("date") == "2023-01-31"))
    assert row1["factor_value"][0] is None

    row2 = panel.filter((pl.col("symbol") == "UNSEASONED") & (pl.col("date") == "2023-02-28"))
    assert row2["factor_value"][0] == 55.0


def test_compile_factor_panel_missing_mask_defaults_to_ineligible_fail_closed() -> None:
    """Fail-Closed: missing mask row must default to eligible = False."""
    prices = pl.DataFrame(
        {
            "date": ["2023-01-31", "2023-01-31", "2023-02-28"],
            "symbol": ["AAPL", "MSFT", "MSFT"],
            "close": [150.0, 200.0, 210.0],
        }
    )

    mask = pl.DataFrame(
        {
            "cutoff_date": ["2023-01-31"],
            "symbol": ["AAPL"],
            "eligible": [True],
        }
    )

    panel = compile_factor_panel("close", prices, mask_df=mask)

    aapl_row = panel.filter(pl.col("symbol") == "AAPL")
    assert aapl_row["factor_value"][0] == 150.0

    msft_rows = panel.filter(pl.col("symbol") == "MSFT").sort("date")
    assert msft_rows["factor_value"][0] is None  # absent from mask -> False -> None
    assert msft_rows["factor_value"][1] is None  # absent date from mask -> False -> None


def test_topk_absent_mask_row_is_ineligible() -> None:
    """Anti-Puppet: assert Top-K selection rejects assets absent from mask_df (Fail-Closed)."""
    factor_df = pl.DataFrame(
        {
            "cutoff": ["2023-01-31"] * 3,
            "symbol": ["A", "B", "C"],
            "factor_value": [100.0, 90.0, 80.0],
        }
    )

    # mask only contains A (True) and B (False). C is completely absent from mask_df!
    mask_df = pl.DataFrame(
        {
            "cutoff": ["2023-01-31"] * 2,
            "symbol": ["A", "B"],
            "eligible": [True, False],
        }
    )

    weights = compute_topk_dropout_weights(factor_df, top_k=2, dropout_k=1, mask_df=mask_df)
    w_map = dict(zip(weights["symbol"].to_list(), weights["target_weight"].to_list()))

    assert w_map["A"] == 1.0  # Only A is eligible
    assert w_map["B"] == 0.0  # B is explicitly ineligible
    assert w_map["C"] == 0.0  # C is absent from mask -> must be ineligible (Fail-Closed)!
    assert w_map["C"] != 0.5  # Must not be selected into Top-K!


def test_compute_topk_dropout_weights_initial_cutoff() -> None:
    factor_df = pl.DataFrame(
        {
            "cutoff": ["2023-01-31"] * 6,
            "symbol": ["S1", "S2", "S3", "S4", "S5", "S6"],
            "factor_value": [10.0, 60.0, 30.0, 50.0, 20.0, 40.0],
        }
    )

    weights = compute_topk_dropout_weights(factor_df, top_k=3, dropout_k=1)
    w_map = dict(zip(weights["symbol"].to_list(), weights["target_weight"].to_list()))

    assert np.isclose(w_map["S2"], 1.0 / 3.0)
    assert np.isclose(w_map["S4"], 1.0 / 3.0)
    assert np.isclose(w_map["S6"], 1.0 / 3.0)
    assert w_map["S1"] == 0.0
    assert w_map["S3"] == 0.0
    assert w_map["S5"] == 0.0


def test_compute_topk_dropout_weights_rebalance_with_dropout() -> None:
    # Month 1: S1=100, S2=90, S3=80, S4=70, S5=60 -> Top 3 held: S1, S2, S3
    m1_df = pl.DataFrame(
        {
            "cutoff": ["2023-01-31"] * 5,
            "symbol": ["S1", "S2", "S3", "S4", "S5"],
            "factor_value": [100.0, 90.0, 80.0, 70.0, 60.0],
        }
    )
    # Month 2: New candidates emerge: S4=120, S5=110, S1=100, S2=50, S3=40
    # Candidate order in Month 2: S4, S5, S1, S2, S3
    # S1 is in top 3 (rank 2). S2 (rank 3) and S3 (rank 4) are out of top 3.
    # With dropout_k=1: drop only 1 worst out-of-top-k (S3 dropped, S2 kept).
    # Remaining slot filled by best new addition (S4).
    # Expected held: S1, S2, S4.
    m2_df = pl.DataFrame(
        {
            "cutoff": ["2023-02-28"] * 5,
            "symbol": ["S1", "S2", "S3", "S4", "S5"],
            "factor_value": [100.0, 50.0, 40.0, 120.0, 110.0],
        }
    )
    combined = pl.concat([m1_df, m2_df])
    weights = compute_topk_dropout_weights(combined, top_k=3, dropout_k=1)

    m2_weights = weights.filter(pl.col("cutoff") == "2023-02-28")
    w_map = dict(zip(m2_weights["symbol"].to_list(), m2_weights["target_weight"].to_list()))

    assert np.isclose(w_map["S1"], 1.0 / 3.0)
    assert np.isclose(w_map["S4"], 1.0 / 3.0)
    assert np.isclose(w_map["S2"], 1.0 / 3.0)
    assert w_map["S3"] == 0.0
    assert w_map["S5"] == 0.0


def test_pivot_to_vbt_matrices_strict_alignment() -> None:
    weights_df = pl.DataFrame(
        {
            "date": ["2023-01-31", "2023-01-31", "2023-02-28", "2023-02-28"],
            "symbol": ["AAPL", "MSFT", "AAPL", "MSFT"],
            "target_weight": [0.5, 0.5, 0.6, 0.4],
        }
    )
    prices_df = pl.DataFrame(
        {
            "date": ["2023-01-31", "2023-01-31", "2023-02-28", "2023-02-28"],
            "symbol": ["MSFT", "AAPL", "MSFT", "AAPL"],
            "close": [240.0, 140.0, 250.0, 150.0],
        }
    )

    close_m, weights_m = pivot_to_vbt_matrices(weights_df, prices_df)

    assert (close_m.columns == weights_m.columns).all()
    assert list(close_m.columns) == ["AAPL", "MSFT"]
    assert len(close_m) == 2
    assert close_m.loc[pd.Timestamp("2023-01-31"), "AAPL"] == 140.0
    assert weights_m.loc[pd.Timestamp("2023-01-31"), "AAPL"] == 0.5
