"""Compiler translating truealpha_contracts AST factor expressions into Polars expressions."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import polars as pl
from truealpha_contracts.ast import (
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
)


def compile_to_polars(
    node: Any,
    symbol_col: str = "symbol",
    date_col: str = "date",
    cutoff_col: str = "cutoff",
) -> pl.Expr:
    """Compile an AST node or raw expression into a polars.Expr.

    Supported AST node operations:
    - Feature(name) -> pl.col(name)
    - Numeric(value) -> pl.lit(value)
    - Add(a, b) -> a + b
    - Sub(a, b) -> a - b
    - Mul(a, b) -> a * b
    - Div(a, b) -> safe division: pl.when(b == 0).then(None).otherwise(a / b)
    - Ref(x, n) -> x.shift(n).over(symbol_col) (requires prior sort on [symbol, date])
    - Mean(x, n) -> x.rolling_mean(window_size=n, min_samples=n).over(symbol_col)
    - Std(x, n) -> x.rolling_std(window_size=n, min_samples=n, ddof=1).over(symbol_col)
    - Rank(x) -> cross-sectional ranking (rank - 1)/(count - 1) normalized to [0, 1] over cutoff
    """
    if isinstance(node, pl.Expr):
        return node
    if isinstance(node, str):
        return pl.col(node)
    if isinstance(node, (int, float)):
        return pl.lit(node)
    if isinstance(node, Decimal):
        return pl.lit(float(node))
    if isinstance(node, Feature):
        return pl.col(node.name)
    if isinstance(node, Numeric):
        val = float(node.value) if isinstance(node.value, Decimal) else node.value
        return pl.lit(val)

    if isinstance(node, Add):
        left = compile_to_polars(node.left, symbol_col, date_col, cutoff_col)
        right = compile_to_polars(node.right, symbol_col, date_col, cutoff_col)
        return left + right

    if isinstance(node, Sub):
        left = compile_to_polars(node.left, symbol_col, date_col, cutoff_col)
        right = compile_to_polars(node.right, symbol_col, date_col, cutoff_col)
        return left - right

    if isinstance(node, Mul):
        left = compile_to_polars(node.left, symbol_col, date_col, cutoff_col)
        right = compile_to_polars(node.right, symbol_col, date_col, cutoff_col)
        return left * right

    if isinstance(node, Div):
        left = compile_to_polars(node.left, symbol_col, date_col, cutoff_col)
        right = compile_to_polars(node.right, symbol_col, date_col, cutoff_col)
        # Safe zero-division handling
        return pl.when(right == 0).then(None).otherwise(left / right)

    if isinstance(node, Ref):
        inner = compile_to_polars(node.expr, symbol_col, date_col, cutoff_col)
        return inner.shift(node.n).over(symbol_col)

    if isinstance(node, Mean):
        inner = compile_to_polars(node.expr, symbol_col, date_col, cutoff_col)
        return inner.rolling_mean(window_size=node.n, min_samples=node.n).over(symbol_col)

    if isinstance(node, Std):
        inner = compile_to_polars(node.expr, symbol_col, date_col, cutoff_col)
        return inner.rolling_std(window_size=node.n, min_samples=node.n, ddof=1).over(symbol_col)

    if isinstance(node, Rank):
        inner = compile_to_polars(node.expr, symbol_col, date_col, cutoff_col)
        rank_expr = inner.rank(method="average").over(cutoff_col)
        count_expr = inner.count().over(cutoff_col)
        # Cross-sectional ranking normalized to [0, 1] over cutoff
        return (
            pl.when(inner.is_null())
            .then(None)
            .when(count_expr <= 1)
            .then(0.0)
            .otherwise((rank_expr - 1) / (count_expr - 1))
        )

    # Support duck-typing for Qlib nodes if present
    if hasattr(node, "kind"):
        kind = str(getattr(node, "kind"))
        if kind == "feature":
            return pl.col(str(getattr(node, "feature_binding_id", "feature")))
        if kind == "numeric":
            return pl.lit(float(getattr(node, "value", 0.0)))
        if kind == "call":
            op = str(getattr(node, "operator_id", "")).lower()
            args: list[Any] = list(getattr(node, "arguments", []))
            if len(args) >= 2:
                if "add" in op:
                    return compile_to_polars(args[0], symbol_col, date_col, cutoff_col) + compile_to_polars(
                        args[1], symbol_col, date_col, cutoff_col
                    )
                if "sub" in op:
                    return compile_to_polars(args[0], symbol_col, date_col, cutoff_col) - compile_to_polars(
                        args[1], symbol_col, date_col, cutoff_col
                    )
                if "mul" in op:
                    return compile_to_polars(args[0], symbol_col, date_col, cutoff_col) * compile_to_polars(
                        args[1], symbol_col, date_col, cutoff_col
                    )
                if "div" in op:
                    left = compile_to_polars(args[0], symbol_col, date_col, cutoff_col)
                    right = compile_to_polars(args[1], symbol_col, date_col, cutoff_col)
                    return pl.when(right == 0).then(None).otherwise(left / right)
                if "ref" in op:
                    inner = compile_to_polars(args[0], symbol_col, date_col, cutoff_col)
                    n = int(getattr(args[1], "value", 1))
                    return inner.shift(n).over(symbol_col)
                if "mean" in op:
                    inner = compile_to_polars(args[0], symbol_col, date_col, cutoff_col)
                    n = int(getattr(args[1], "value", 1))
                    return inner.rolling_mean(window_size=n, min_samples=n).over(symbol_col)

    raise TypeError(f"Unsupported AST node type for compilation: {type(node)}: {node!r}")


# Canonical alias
compile_expression = compile_to_polars


def evaluate_factor(
    df: pl.DataFrame,
    expr: Any,
    symbol_col: str = "symbol",
    date_col: str = "date",
    cutoff_col: str = "cutoff",
    target_col: str = "factor_value",
) -> pl.DataFrame:
    """Sort on [symbol, date] and evaluate the compiled factor expression."""
    sort_cols = [c for c in [symbol_col, date_col] if c in df.columns]
    sorted_df = df.sort(sort_cols) if sort_cols else df
    # Ensure cutoff column exists if cutoff_col not in columns but date_col is
    if cutoff_col not in sorted_df.columns and date_col in sorted_df.columns:
        sorted_df = sorted_df.with_columns(**{cutoff_col: pl.col(date_col)})
    compiled = compile_to_polars(expr, symbol_col=symbol_col, date_col=date_col, cutoff_col=cutoff_col)
    return sorted_df.with_columns(**{target_col: compiled})


__all__ = [
    "compile_expression",
    "compile_to_polars",
    "evaluate_factor",
]
