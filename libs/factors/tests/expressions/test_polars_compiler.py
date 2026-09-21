"""Unit tests for Polars Factor Expression compiler and DSL."""

import numpy as np
import polars as pl
from factors.expressions.compiler import compile_to_polars, evaluate_factor
from factors.expressions.dsl import (
    Add,
    Div,
    Feature,
    Mean,
    Mul,
    Numeric,
    Rank,
    Ref,
    Std,
    Sub,
    col,
)


def test_feature_and_numeric_compilation() -> None:
    df = pl.DataFrame(
        {
            "symbol": ["AAPL", "MSFT"],
            "date": ["2023-01-01", "2023-01-01"],
            "close": [150.0, 250.0],
        }
    )

    feat_node = Feature("close")
    num_node = Numeric(10.0)

    expr1 = compile_to_polars(feat_node)
    expr2 = compile_to_polars(num_node)

    res = df.select(feat=expr1, num=expr2)
    assert res["feat"].to_list() == [150.0, 250.0]
    assert res["num"].to_list() == [10.0, 10.0]


def test_basic_arithmetic_add_sub_mul() -> None:
    df = pl.DataFrame(
        {
            "symbol": ["AAPL", "MSFT"],
            "date": ["2023-01-01", "2023-01-01"],
            "close": [10.0, 20.0],
            "open": [8.0, 15.0],
        }
    )

    add_node = Add("close", "open")
    sub_node = Sub("close", "open")
    mul_node = Mul("close", 2.0)

    res = df.with_columns(
        add=compile_to_polars(add_node),
        sub=compile_to_polars(sub_node),
        mul=compile_to_polars(mul_node),
    )

    assert res["add"].to_list() == [18.0, 35.0]
    assert res["sub"].to_list() == [2.0, 5.0]
    assert res["mul"].to_list() == [20.0, 40.0]


def test_safe_division_by_zero() -> None:
    df = pl.DataFrame(
        {
            "symbol": ["A", "B", "C", "D"],
            "date": ["2023-01-01"] * 4,
            "numerator": [10.0, 20.0, 0.0, 30.0],
            "denominator": [2.0, 0.0, 0.0, 5.0],
        }
    )

    div_node = Div("numerator", "denominator")
    res = df.with_columns(div=compile_to_polars(div_node))

    values = res["div"].to_list()
    assert values[0] == 5.0
    # Safe division: when denominator is 0, must return None/null, NOT inf or exception
    assert values[1] is None
    assert values[2] is None
    assert values[3] == 6.0


def test_null_propagation() -> None:
    df = pl.DataFrame(
        {
            "symbol": ["A", "B", "C"],
            "date": ["2023-01-01"] * 3,
            "a": [10.0, None, 30.0],
            "b": [2.0, 5.0, None],
        }
    )

    res = df.with_columns(
        add=compile_to_polars(Add("a", "b")),
        sub=compile_to_polars(Sub("a", "b")),
        mul=compile_to_polars(Mul("a", "b")),
        div=compile_to_polars(Div("a", "b")),
    )

    assert res["add"].to_list() == [12.0, None, None]
    assert res["sub"].to_list() == [8.0, None, None]
    assert res["mul"].to_list() == [20.0, None, None]
    assert res["div"].to_list() == [5.0, None, None]


def test_ref_time_series_shift() -> None:
    # Intentionally unsorted to verify prior sorting by evaluate_factor or explicit sort
    df = pl.DataFrame(
        {
            "date": ["2023-01-03", "2023-01-01", "2023-01-02", "2023-01-02", "2023-01-01"],
            "symbol": ["AAPL", "AAPL", "AAPL", "MSFT", "MSFT"],
            "close": [103.0, 101.0, 102.0, 202.0, 201.0],
        }
    )

    # Prior sort on ['symbol', 'date']
    sorted_df = df.sort(["symbol", "date"])
    ref_node = Ref("close", n=1)
    res = sorted_df.with_columns(ref1=compile_to_polars(ref_node))

    # AAPL values: 101, 102, 103 -> shifted: null, 101, 102
    aapl_ref = res.filter(pl.col("symbol") == "AAPL")["ref1"].to_list()
    assert aapl_ref == [None, 101.0, 102.0]

    # MSFT values: 201, 202 -> shifted: null, 201
    msft_ref = res.filter(pl.col("symbol") == "MSFT")["ref1"].to_list()
    assert msft_ref == [None, 201.0]


def test_rolling_mean() -> None:
    df = pl.DataFrame(
        {
            "date": ["2023-01-01", "2023-01-02", "2023-01-03", "2023-01-04"],
            "symbol": ["AAPL"] * 4,
            "close": [10.0, 20.0, 30.0, 40.0],
        }
    ).sort(["symbol", "date"])

    mean_node = Mean("close", n=2)
    res = df.with_columns(m2=compile_to_polars(mean_node))

    # Rolling mean window=2, min_periods=2: first element is null, then (10+20)/2=15, (20+30)/2=25, etc.
    values = res["m2"].to_list()
    assert values[0] is None
    assert values[1] == 15.0
    assert values[2] == 25.0
    assert values[3] == 35.0


def test_rolling_std_ddof1() -> None:
    df = pl.DataFrame(
        {
            "date": ["2023-01-01", "2023-01-02", "2023-01-03"],
            "symbol": ["AAPL"] * 3,
            "close": [10.0, 20.0, 30.0],
        }
    ).sort(["symbol", "date"])

    std_node = Std("close", n=2)
    res = df.with_columns(s2=compile_to_polars(std_node))

    values = res["s2"].to_list()
    assert values[0] is None
    # Sample std of [10, 20] with ddof=1 is sqrt((10-15)^2 + (20-15)^2) = sqrt(50) = ~7.0710678
    assert values[1] is not None
    assert np.isclose(values[1], np.std([10.0, 20.0], ddof=1))
    assert np.isclose(values[2], np.std([20.0, 30.0], ddof=1))


def test_rank_cross_sectional_normalized_0_to_1() -> None:
    df = pl.DataFrame(
        {
            "cutoff": ["2023-01-31"] * 4 + ["2023-02-28"] * 3,
            "symbol": ["A", "B", "C", "D", "A", "B", "C"],
            "val": [10.0, 40.0, 20.0, 30.0, 100.0, 50.0, 75.0],
        }
    )

    rank_node = Rank("val")
    res = df.with_columns(rank=compile_to_polars(rank_node, cutoff_col="cutoff"))

    # Cutoff 1: A=10 (min), C=20, D=30, B=40 (max)
    # Ranks (1-indexed average): A=1, C=2, D=3, B=4
    # Normalized: (rank - 1)/(4 - 1): A=0.0, C=1/3, D=2/3, B=1.0
    c1 = res.filter(pl.col("cutoff") == "2023-01-31")
    c1_map = dict(zip(c1["symbol"].to_list(), c1["rank"].to_list()))
    assert c1_map["A"] == 0.0
    assert np.isclose(c1_map["C"], 1.0 / 3.0)
    assert np.isclose(c1_map["D"], 2.0 / 3.0)
    assert c1_map["B"] == 1.0

    # Cutoff 2: B=50 (min), C=75, A=100 (max)
    # Normalized: (rank - 1)/(3 - 1): B=0.0, C=0.5, A=1.0
    c2 = res.filter(pl.col("cutoff") == "2023-02-28")
    c2_map = dict(zip(c2["symbol"].to_list(), c2["rank"].to_list()))
    assert c2_map["B"] == 0.0
    assert c2_map["C"] == 0.5
    assert c2_map["A"] == 1.0


def test_rank_single_asset_edge_case() -> None:
    df = pl.DataFrame(
        {
            "cutoff": ["2023-01-31"],
            "symbol": ["AAPL"],
            "val": [100.0],
        }
    )

    res = df.with_columns(rank=compile_to_polars(Rank("val"), cutoff_col="cutoff"))
    # When count == 1, (count - 1) == 0, safe handling returns 0.0 without division by zero
    assert res["rank"].to_list() == [0.0]


def test_rank_with_null_values() -> None:
    df = pl.DataFrame(
        {
            "cutoff": ["2023-01-31"] * 4,
            "symbol": ["A", "B", "C", "D"],
            "val": [10.0, None, 30.0, 20.0],
        }
    )

    res = df.with_columns(rank=compile_to_polars(Rank("val"), cutoff_col="cutoff"))
    res_map = dict(zip(res["symbol"].to_list(), res["rank"].to_list()))

    assert res_map["A"] == 0.0
    assert res_map["B"] is None
    assert res_map["D"] == 0.5
    assert res_map["C"] == 1.0


def test_dsl_operator_overloading() -> None:
    c = col("close")
    o = col("open")

    # + - * / operators
    expr_add = c + o
    expr_sub = c - 5.0
    expr_mul = c * 2.0
    expr_div = c / o

    assert isinstance(expr_add, Add)
    assert isinstance(expr_sub, Sub)
    assert isinstance(expr_mul, Mul)
    assert isinstance(expr_div, Div)

    # Methods
    expr_ref = c.shift(2)
    expr_mean = c.mean(5)
    expr_std = c.std(5)
    expr_rank = c.rank()

    assert isinstance(expr_ref, Ref) and expr_ref.n == 2
    assert isinstance(expr_mean, Mean) and expr_mean.n == 5
    assert isinstance(expr_std, Std) and expr_std.n == 5
    assert isinstance(expr_rank, Rank)


def test_nested_momentum_expression() -> None:
    # Momentum: (close - Ref(close, 1)) / Ref(close, 1)
    mom_expr = Div(Sub("close", Ref("close", 1)), Ref("close", 1))

    df = pl.DataFrame(
        {
            "symbol": ["AAPL", "AAPL", "AAPL"],
            "date": ["2023-01-01", "2023-01-02", "2023-01-03"],
            "close": [100.0, 110.0, 121.0],
        }
    )

    evaluated = evaluate_factor(df, mom_expr)
    values = evaluated["factor_value"].to_list()

    assert values[0] is None
    assert np.isclose(values[1], 0.10)  # (110 - 100) / 100 = 0.10
    assert np.isclose(values[2], 0.10)  # (121 - 110) / 110 = 0.10
