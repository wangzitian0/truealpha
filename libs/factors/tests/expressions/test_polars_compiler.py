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


def test_safe_division_by_zero_and_nan() -> None:
    df = pl.DataFrame(
        {
            "symbol": ["A", "B", "C", "D", "E"],
            "date": ["2023-01-01"] * 5,
            "numerator": [10.0, 20.0, 0.0, 30.0, float("nan")],
            "denominator": [2.0, 0.0, 0.0, 5.0, 10.0],
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
    assert values[4] is None


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
    df = pl.DataFrame(
        {
            "date": ["2023-01-01", "2023-01-02", "2023-01-03", "2023-01-01", "2023-01-02"],
            "symbol": ["AAPL", "AAPL", "AAPL", "MSFT", "MSFT"],
            "close": [101.0, 102.0, 103.0, 201.0, 202.0],
        }
    )

    ref_node = Ref("close", n=1)
    res = df.with_columns(prev_close=compile_to_polars(ref_node))

    aapl = res.filter(pl.col("symbol") == "AAPL").sort("date")
    assert aapl["prev_close"].to_list() == [None, 101.0, 102.0]

    msft = res.filter(pl.col("symbol") == "MSFT").sort("date")
    assert msft["prev_close"].to_list() == [None, 201.0]


def test_rolling_mean() -> None:
    df = pl.DataFrame(
        {
            "date": ["2023-01-01", "2023-01-02", "2023-01-03", "2023-01-04"],
            "symbol": ["A", "A", "A", "A"],
            "close": [10.0, 20.0, 30.0, 40.0],
        }
    )

    mean_node = Mean("close", n=3)
    res = df.with_columns(m3=compile_to_polars(mean_node))

    values = res["m3"].to_list()
    assert values[0] is None
    assert values[1] is None
    assert values[2] == 20.0
    assert values[3] == 30.0


def test_rolling_std() -> None:
    df = pl.DataFrame(
        {
            "date": ["2023-01-01", "2023-01-02", "2023-01-03"],
            "symbol": ["A", "A", "A"],
            "close": [10.0, 20.0, 30.0],
        }
    )

    std_node = Std("close", n=2)
    res = df.with_columns(s2=compile_to_polars(std_node))

    values = res["s2"].to_list()
    assert values[0] is None
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

    c1 = res.filter(pl.col("cutoff") == "2023-01-31")
    c1_map = dict(zip(c1["symbol"].to_list(), c1["rank"].to_list()))
    assert c1_map["A"] == 0.0
    assert np.isclose(c1_map["C"], 1.0 / 3.0)
    assert np.isclose(c1_map["D"], 2.0 / 3.0)
    assert c1_map["B"] == 1.0

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


def test_rank_treats_nan_as_missing() -> None:
    """Anti-Puppet: assert Rank strictly converts NaN to None, not 1.0 (greatest rank)."""
    df = pl.DataFrame(
        {
            "cutoff": ["2023-01-31"] * 3,
            "symbol": ["A", "B", "C"],
            "val": [10.0, 20.0, float("nan")],
        }
    )

    res = df.with_columns(rank=compile_to_polars(Rank("val"), cutoff_col="cutoff"))
    res_map = dict(zip(res["symbol"].to_list(), res["rank"].to_list()))

    assert res_map["A"] == 0.0
    assert res_map["B"] == 1.0
    assert res_map["C"] is None
    assert res_map["C"] != 1.0


def test_dsl_operator_overloading() -> None:
    c = col("close")
    o = col("open")

    expr_add = c + o
    expr_sub = c - 5.0
    expr_mul = c * 2.0
    expr_div = c / o

    assert isinstance(expr_add, Add)
    assert isinstance(expr_sub, Sub)
    assert isinstance(expr_mul, Mul)
    assert isinstance(expr_div, Div)

    expr_ref = c.shift(2)
    expr_mean = c.mean(5)
    expr_std = c.std(5)
    expr_rank = c.rank()

    assert isinstance(expr_ref, Ref) and expr_ref.n == 2
    assert isinstance(expr_mean, Mean) and expr_mean.n == 5
    assert isinstance(expr_std, Std) and expr_std.n == 5
    assert isinstance(expr_rank, Rank)


def test_nested_momentum_expression() -> None:
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
    assert np.isclose(values[1], 0.10)
    assert np.isclose(values[2], 0.10)
