"""Polars Factor Expression DSL constructors and AST helpers."""

from __future__ import annotations

from decimal import Decimal

from truealpha_contracts.ast import (
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
)


def col(name: str) -> Feature:
    """Create a feature (column) node."""
    return Feature(name=name)


def lit(value: float | int | Decimal) -> Numeric:
    """Create a numeric literal node."""
    return Numeric(value=value)


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
    "lit",
]
