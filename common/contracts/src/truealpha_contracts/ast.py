"""Factor Expression Abstract Syntax Tree (AST) contracts."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class FactorASTNode(BaseModel):
    """Base class for all factor expression AST nodes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    def __add__(self, other: Any) -> Add:
        return Add(left=self, right=_coerce_node(other))

    def __radd__(self, other: Any) -> Add:
        return Add(left=_coerce_node(other), right=self)

    def __sub__(self, other: Any) -> Sub:
        return Sub(left=self, right=_coerce_node(other))

    def __rsub__(self, other: Any) -> Sub:
        return Sub(left=_coerce_node(other), right=self)

    def __mul__(self, other: Any) -> Mul:
        return Mul(left=self, right=_coerce_node(other))

    def __rmul__(self, other: Any) -> Mul:
        return Mul(left=_coerce_node(other), right=self)

    def __truediv__(self, other: Any) -> Div:
        return Div(left=self, right=_coerce_node(other))

    def __rtruediv__(self, other: Any) -> Div:
        return Div(left=_coerce_node(other), right=self)

    def shift(self, n: int) -> Ref:
        return Ref(expr=self, n=n)

    def rolling_mean(self, n: int) -> Mean:
        return Mean(expr=self, n=n)

    def mean(self, n: int) -> Mean:
        return Mean(expr=self, n=n)

    def rolling_std(self, n: int) -> Std:
        return Std(expr=self, n=n)

    def std(self, n: int) -> Std:
        return Std(expr=self, n=n)

    def rank(self) -> Rank:
        return Rank(expr=self)


def _coerce_node(val: Any) -> FactorASTNode:
    if isinstance(val, FactorASTNode):
        return val
    if isinstance(val, str):
        return Feature(name=val)
    if isinstance(val, (int, float, Decimal)):
        return Numeric(value=float(val) if isinstance(val, Decimal) else val)
    raise TypeError(f"Cannot coerce {type(val)} to FactorASTNode: {val!r}")


class Feature(FactorASTNode):
    name: str

    def __init__(self, name: str | None = None, **data: Any) -> None:
        if name is not None and "name" not in data:
            data["name"] = name
        super().__init__(**data)


class Numeric(FactorASTNode):
    value: float | int

    def __init__(self, value: float | int | Decimal | None = None, **data: Any) -> None:
        if value is not None and "value" not in data:
            data["value"] = float(value) if isinstance(value, Decimal) else value
        super().__init__(**data)


class Add(FactorASTNode):
    left: FactorASTNode
    right: FactorASTNode

    def __init__(self, left: Any = None, right: Any = None, **data: Any) -> None:
        if left is not None and "left" not in data:
            data["left"] = _coerce_node(left)
        elif "left" in data:
            data["left"] = _coerce_node(data["left"])
        if right is not None and "right" not in data:
            data["right"] = _coerce_node(right)
        elif "right" in data:
            data["right"] = _coerce_node(data["right"])
        super().__init__(**data)


class Sub(FactorASTNode):
    left: FactorASTNode
    right: FactorASTNode

    def __init__(self, left: Any = None, right: Any = None, **data: Any) -> None:
        if left is not None and "left" not in data:
            data["left"] = _coerce_node(left)
        elif "left" in data:
            data["left"] = _coerce_node(data["left"])
        if right is not None and "right" not in data:
            data["right"] = _coerce_node(right)
        elif "right" in data:
            data["right"] = _coerce_node(data["right"])
        super().__init__(**data)


class Mul(FactorASTNode):
    left: FactorASTNode
    right: FactorASTNode

    def __init__(self, left: Any = None, right: Any = None, **data: Any) -> None:
        if left is not None and "left" not in data:
            data["left"] = _coerce_node(left)
        elif "left" in data:
            data["left"] = _coerce_node(data["left"])
        if right is not None and "right" not in data:
            data["right"] = _coerce_node(right)
        elif "right" in data:
            data["right"] = _coerce_node(data["right"])
        super().__init__(**data)


class Div(FactorASTNode):
    left: FactorASTNode
    right: FactorASTNode

    def __init__(self, left: Any = None, right: Any = None, **data: Any) -> None:
        if left is not None and "left" not in data:
            data["left"] = _coerce_node(left)
        elif "left" in data:
            data["left"] = _coerce_node(data["left"])
        if right is not None and "right" not in data:
            data["right"] = _coerce_node(right)
        elif "right" in data:
            data["right"] = _coerce_node(data["right"])
        super().__init__(**data)


class Ref(FactorASTNode):
    expr: FactorASTNode
    n: int = Field(gt=0)

    def __init__(self, expr: Any = None, n: int | None = None, **data: Any) -> None:
        if expr is not None and "expr" not in data:
            data["expr"] = _coerce_node(expr)
        elif "expr" in data:
            data["expr"] = _coerce_node(data["expr"])
        if n is not None and "n" not in data:
            data["n"] = n
        super().__init__(**data)


class Mean(FactorASTNode):
    expr: FactorASTNode
    n: int = Field(gt=0)

    def __init__(self, expr: Any = None, n: int | None = None, **data: Any) -> None:
        if expr is not None and "expr" not in data:
            data["expr"] = _coerce_node(expr)
        elif "expr" in data:
            data["expr"] = _coerce_node(data["expr"])
        if n is not None and "n" not in data:
            data["n"] = n
        super().__init__(**data)


class Std(FactorASTNode):
    expr: FactorASTNode
    n: int = Field(gt=0)

    def __init__(self, expr: Any = None, n: int | None = None, **data: Any) -> None:
        if expr is not None and "expr" not in data:
            data["expr"] = _coerce_node(expr)
        elif "expr" in data:
            data["expr"] = _coerce_node(data["expr"])
        if n is not None and "n" not in data:
            data["n"] = n
        super().__init__(**data)


class Rank(FactorASTNode):
    expr: FactorASTNode

    def __init__(self, expr: Any = None, **data: Any) -> None:
        if expr is not None and "expr" not in data:
            data["expr"] = _coerce_node(expr)
        elif "expr" in data:
            data["expr"] = _coerce_node(data["expr"])
        super().__init__(**data)


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
]
