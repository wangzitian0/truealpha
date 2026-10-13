"""Test-time reproducibility oracle: the Polars AST compiler and its DSL (init.md rule 25).

Tests compile a factor's versioned `truealpha_contracts.ast` definition through this package and
check that it reproduces the native Decimal result. No deployed path imports it.
"""

from .compiler import compile_expression, compile_to_polars, evaluate_factor
from .dsl import (
    Add,
    Div,
    FactorASTNode,
    Feature,
    Mean,
    Mul,
    Numeric,
    Rank,
    Ref,
    Std,
    Sub,
    col,
    lit,
)

__all__ = [
    "Add",
    "Div",
    "FactorASTNode",
    "Feature",
    "Mean",
    "Mul",
    "Numeric",
    "Rank",
    "Ref",
    "Std",
    "Sub",
    "col",
    "compile_expression",
    "compile_to_polars",
    "evaluate_factor",
    "lit",
]
