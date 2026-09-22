"""Unit tests for Rebalancing and VectorBT Adapter."""

import numpy as np
import pandas as pd
import polars as pl
from factors.backtest.adapter import (
    compile_factor_panel,
    compute_topk_dropout_weights,
    pivot_to_vbt_matrices,
)
from factors.expressions.dsl import Div, Ref, Sub, col


def test_compile_factor_panel_basic() -> None:
    # 2 symbols across 3 dates, unsorted input
    prices = pl.DataFrame(
        {
            "date": ["2023-01-02", "2023-01-01", "2023-01-03", "2023-01-03", "2023-01-01", "2023-01-02"],
            "symbol": ["AAPL", "AAPL", "AAPL", "MSFT", "MSFT", "MSFT"],
            "close": [110.0, 100.0, 120.0, 220.0, 200.0, 210.0],
        }
    )

    # Return factor: (close - Ref(close, 1)) / Ref(close, 1)
    ret_expr = Div(Sub("close", Ref("close", 1)), Ref("close", 1))

    panel = compile_factor_panel(ret_expr, prices)

    # Must be sorted by symbol and date
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
    assert np.isclose(aapl_vals[1], 0.10)  # (110 - 100) / 100
    assert np.isclose(aapl_vals[2], (120 - 110) / 110)


def test_compile_factor_panel_with_dynamic_universe_mask() -> None:
    prices = pl.DataFrame(
        {
            "date": ["2023-01-31", "2023-01-31", "2023-02-28", "2023-02-28"],
            "symbol": ["AAPL", "UNSEASONED", "AAPL", "UNSEASONED"],
            "close": [150.0, 50.0, 155.0, 55.0],
        }
    )

    # UNSEASONED is an unseasoned IPO or suspended stock ineligible in month 1, eligible in month 2
    mask = pl.DataFrame(
        {
            "cutoff_date": ["2023-01-31", "2023-01-31", "2023-02-28", "2023-02-28"],
            "symbol": ["AAPL", "UNSEASONED", "AAPL", "UNSEASONED"],
            "eligible": [True, False, True, True],
        }
    )

    panel = compile_factor_panel("close", prices, mask_df=mask)

    row1 = panel.filter((pl.col("symbol") == "UNSEASONED") & (pl.col("date") == "2023-01-31"))
    assert row1["factor_value"][0] is None  # Ineligible in month 1 -> factor_value is null

    row2 = panel.filter((pl.col("symbol") == "UNSEASONED") & (pl.col("date") == "2023-02-28"))
    assert row2["factor_value"][0] == 55.0  # Eligible in month 2 -> factor_value intact


def test_compile_factor_panel_missing_mask_defaults_to_eligible() -> None:
    prices = pl.DataFrame(
        {
            "date": ["2023-01-31", "2023-01-31", "2023-02-28"],
            "symbol": ["AAPL", "MSFT", "MSFT"],
            "close": [150.0, 200.0, 210.0],
        }
    )

    # mask only contains AAPL for 2023-01-31 (explicitly False). MSFT and 2023-02-28 are not present in mask.
    mask = pl.DataFrame(
        {
            "cutoff_date": ["2023-01-31"],
            "symbol": ["AAPL"],
            "eligible": [False],
        }
    )

    panel = compile_factor_panel("close", prices, mask_df=mask)

    aapl_row = panel.filter(pl.col("symbol") == "AAPL")
    assert aapl_row["factor_value"][0] is None  # explicitly False -> None

    msft_rows = panel.filter(pl.col("symbol") == "MSFT").sort("date")
    assert msft_rows["factor_value"][0] == 200.0  # missing symbol from mask -> defaults to eligible
    assert msft_rows["factor_value"][1] == 210.0  # missing date from mask -> defaults to eligible


def test_compute_topk_dropout_weights_initial_cutoff() -> None:
    # 6 symbols in cutoff 1, top_k = 3
    factor_df = pl.DataFrame(
        {
            "cutoff": ["2023-01-31"] * 6,
            "symbol": ["S1", "S2", "S3", "S4", "S5", "S6"],
            "factor_value": [10.0, 60.0, 30.0, 50.0, 20.0, 40.0],
            # Ranking descending: S2(60), S4(50), S6(40), S3(30), S5(20), S1(10)
        }
    )

    weights = compute_topk_dropout_weights(factor_df, top_k=3, dropout_k=1)
    w_map = dict(zip(weights["symbol"].to_list(), weights["target_weight"].to_list()))

    # Top 3 are S2, S4, S6 -> equal weights 1/3 each
    assert np.isclose(w_map["S2"], 1.0 / 3.0)
    assert np.isclose(w_map["S4"], 1.0 / 3.0)
    assert np.isclose(w_map["S6"], 1.0 / 3.0)

    # Ineligible or non-topk get 0.0
    assert w_map["S1"] == 0.0
    assert w_map["S3"] == 0.0
    assert w_map["S5"] == 0.0

    # Total weight normalized to 1.0
    assert np.isclose(sum(w_map.values()), 1.0)


def test_compute_topk_dropout_weights_retention_and_dropout_limit() -> None:
    # 2 cutoffs with 8 symbols
    # Cutoff 1: S1..S8 scores: S1=80, S2=70, S3=60, S4=50, S5=40, S6=30, S7=20, S8=10
    # Top 5 held at t=0: {S1, S2, S3, S4, S5}
    #
    # Cutoff 2: scores change so that:
    # S6=95 (new rank 1), S7=90 (new rank 2), S8=85 (new rank 3)
    # S1=80 (rank 4), S2=70 (rank 5)
    # S3=30 (rank 6), S4=20 (rank 7), S5=10 (rank 8)
    #
    # Held {S1, S2, S3, S4, S5}:
    # In top-5: S1 (rank 4), S2 (rank 5) -> retained!
    # Out of top-5: S3 (rank 6), S4 (rank 7), S5 (rank 8) -> 3 out-of-top-k held assets
    # With dropout_k=2: drop at most 2 worst (S5 rank 8, S4 rank 7).
    # S3 (rank 6) is RETAINED!
    # Available slots = 5 - 3 = 2 slots -> filled by top candidates S6, S7!
    # Resulting holdings: {S1, S2, S3, S6, S7}, each 0.20 weight!
    c1_symbols = [f"S{i}" for i in range(1, 9)]
    c1_scores = [80.0, 70.0, 60.0, 50.0, 40.0, 30.0, 20.0, 10.0]

    c2_symbols = [f"S{i}" for i in range(1, 9)]
    c2_scores = [80.0, 70.0, 30.0, 20.0, 10.0, 95.0, 90.0, 85.0]

    df = pl.DataFrame(
        {
            "cutoff": ["2023-01-31"] * 8 + ["2023-02-28"] * 8,
            "symbol": c1_symbols + c2_symbols,
            "factor_value": c1_scores + c2_scores,
        }
    )

    weights = compute_topk_dropout_weights(df, top_k=5, dropout_k=2)

    c2_w = weights.filter(pl.col("cutoff") == "2023-02-28")
    w_map = dict(zip(c2_w["symbol"].to_list(), c2_w["target_weight"].to_list()))

    # S1, S2 in top 5 retained
    assert np.isclose(w_map["S1"], 0.20)
    assert np.isclose(w_map["S2"], 0.20)

    # S3 was rank 6 (out of top 5) but retained due to dropout_k=2 limit
    assert np.isclose(w_map["S3"], 0.20)

    # Top candidates S6, S7 added
    assert np.isclose(w_map["S6"], 0.20)
    assert np.isclose(w_map["S7"], 0.20)

    # S4, S5 dropped (0.0) and S8 not added (0.0)
    assert w_map["S4"] == 0.0
    assert w_map["S5"] == 0.0
    assert w_map["S8"] == 0.0

    assert np.isclose(sum(w_map.values()), 1.0)


def test_compute_topk_dropout_weights_ineligible_dropped_immediately() -> None:
    # If a held asset becomes ineligible, it must be dropped immediately regardless of dropout_k
    c1_symbols = ["A", "B", "C"]
    c2_symbols = ["A", "B", "C"]

    df = pl.DataFrame(
        {
            "cutoff": ["2023-01-31"] * 3 + ["2023-02-28"] * 3,
            "symbol": c1_symbols + c2_symbols,
            "factor_value": [30.0, 20.0, 10.0, 30.0, 20.0, 10.0],
            # B becomes ineligible at cutoff 2
            "eligible": [True, True, True, True, False, True],
        }
    )

    # top_k=2, dropout_k=0 (meaning zero dropout allowed normally)
    weights = compute_topk_dropout_weights(df, top_k=2, dropout_k=0)

    # In cutoff 1: A and B held
    c1_w = weights.filter(pl.col("cutoff") == "2023-01-31")
    assert dict(zip(c1_w["symbol"].to_list(), c1_w["target_weight"].to_list())) == {
        "A": 0.5,
        "B": 0.5,
        "C": 0.0,
    }

    # In cutoff 2: B is ineligible, so B must be dropped even with dropout_k=0, and C takes its place!
    c2_w = weights.filter(pl.col("cutoff") == "2023-02-28")
    assert dict(zip(c2_w["symbol"].to_list(), c2_w["target_weight"].to_list())) == {
        "A": 0.5,
        "B": 0.0,
        "C": 0.5,
    }


def test_compute_topk_dropout_weights_universe_smaller_than_topk() -> None:
    df = pl.DataFrame(
        {
            "cutoff": ["2023-01-31"] * 2,
            "symbol": ["A", "B"],
            "factor_value": [10.0, 20.0],
        }
    )

    # top_k=5, but only 2 assets available
    weights = compute_topk_dropout_weights(df, top_k=5, dropout_k=2)
    w_map = dict(zip(weights["symbol"].to_list(), weights["target_weight"].to_list()))

    assert np.isclose(w_map["A"], 0.5)
    assert np.isclose(w_map["B"], 0.5)
    assert np.isclose(sum(w_map.values()), 1.0)


def test_pivot_to_vbt_matrices_strict_column_alignment() -> None:
    prices = pl.DataFrame(
        {
            "date": ["2023-01-31", "2023-01-31", "2023-01-31", "2023-02-28", "2023-02-28", "2023-02-28"],
            "symbol": ["MSFT", "AAPL", "GOOG", "MSFT", "AAPL", "GOOG"],
            "close": [240.0, 150.0, 95.0, 250.0, 155.0, 100.0],
        }
    )

    # Only AAPL and MSFT have non-zero target weights; GOOG is omitted from weights
    weights = pl.DataFrame(
        {
            "date": ["2023-01-31", "2023-01-31", "2023-02-28"],
            "symbol": ["AAPL", "MSFT", "AAPL"],
            "target_weight": [0.5, 0.5, 1.0],
        }
    )

    close_m, weights_m = pivot_to_vbt_matrices(weights, prices)

    # 1. Strictly asserts column order equality: assert (close_matrix.columns == weights_matrix.columns).all()
    assert (close_m.columns == weights_m.columns).all()
    assert list(close_m.columns) == ["AAPL", "GOOG", "MSFT"]

    # 2. Missing weights filled with 0.0
    assert weights_m.loc["2023-01-31", "GOOG"] == 0.0
    assert weights_m.loc["2023-02-28", "GOOG"] == 0.0
    assert weights_m.loc["2023-02-28", "MSFT"] == 0.0
    assert weights_m.loc["2023-01-31", "AAPL"] == 0.5
    assert weights_m.loc["2023-02-28", "AAPL"] == 1.0

    # 3. Index must be DatetimeIndex and match
    assert isinstance(close_m.index, pd.DatetimeIndex)
    assert isinstance(weights_m.index, pd.DatetimeIndex)
    assert (close_m.index == weights_m.index).all()


def test_end_to_end_panel_to_vbt_flow() -> None:
    # Full end-to-end integration:
    # 1. price panel -> 2. compile factor expression -> 3. top-k dropout rebalance -> 4. pivot to vbt matrices
    symbols = ["AAPL", "MSFT", "GOOG", "AMZN"]
    dates = ["2023-01-31", "2023-02-28", "2023-03-31"]

    rows = []
    np.random.seed(42)
    for d in dates:
        for s in symbols:
            rows.append(
                {
                    "date": d,
                    "symbol": s,
                    "close": float(np.random.uniform(100.0, 300.0)),
                }
            )
    prices_df = pl.DataFrame(rows)

    # Use cross-sectional rank of close
    factor_expr = col("close").rank()
    factor_panel = compile_factor_panel(factor_expr, prices_df)

    assert "factor_value" in factor_panel.columns

    # Generate Top-2 weights
    weights_df = compute_topk_dropout_weights(factor_panel, top_k=2, dropout_k=1)
    assert "target_weight" in weights_df.columns

    # Pivot to VectorBT matrices
    close_mat, weights_mat = pivot_to_vbt_matrices(weights_df, prices_df)

    assert (close_mat.columns == weights_mat.columns).all()
    assert len(close_mat) == 3
    assert len(weights_mat) == 3

    # Each date weights sum to 1.0
    for d in weights_mat.index:
        assert np.isclose(weights_mat.loc[d].sum(), 1.0)
