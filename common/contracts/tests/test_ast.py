"""Tests for Factor Expression AST nodes."""

from decimal import Decimal

import pytest
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


def test_ast_node_construction_and_coercion():
    f = Feature("close")
    assert isinstance(f, FactorASTNode)
    assert f.name == "close"

    n = Numeric(10.5)
    assert n.value == 10.5

    n_dec = Numeric(Decimal("42.0"))
    assert n_dec.value == 42.0


def test_ast_arithmetic_operators():
    f = Feature("close")

    add_node = f + 5
    assert isinstance(add_node, Add)
    assert isinstance(add_node.left, Feature)
    assert isinstance(add_node.right, Numeric)
    assert add_node.right.value == 5.0

    radd_node = 5 + f
    assert isinstance(radd_node, Add)
    assert isinstance(radd_node.left, Numeric)
    assert isinstance(radd_node.right, Feature)

    sub_node = f - "open"
    assert isinstance(sub_node, Sub)
    assert isinstance(sub_node.left, Feature)
    assert isinstance(sub_node.right, Feature)
    assert sub_node.right.name == "open"

    rsub_node = 10 - f
    assert isinstance(rsub_node, Sub)
    assert isinstance(rsub_node.left, Numeric)
    assert isinstance(rsub_node.right, Feature)

    mul_node = f * 2
    assert isinstance(mul_node, Mul)

    rmul_node = 2 * f
    assert isinstance(rmul_node, Mul)

    div_node = f / 2
    assert isinstance(div_node, Div)

    rdiv_node = 2 / f
    assert isinstance(rdiv_node, Div)


def test_ast_windowing_and_ranking_methods():
    f = Feature("close")

    s = f.shift(1)
    assert isinstance(s, Ref)
    assert s.n == 1
    assert s.expr == f

    m = f.rolling_mean(5)
    assert isinstance(m, Mean)
    assert m.n == 5

    m2 = f.mean(10)
    assert isinstance(m2, Mean)
    assert m2.n == 10

    std = f.rolling_std(20)
    assert isinstance(std, Std)
    assert std.n == 20

    std2 = f.std(15)
    assert isinstance(std2, Std)
    assert std2.n == 15

    r = f.rank()
    assert isinstance(r, Rank)
    assert r.expr == f


def test_ast_invalid_coercion():
    f = Feature("close")
    with pytest.raises(TypeError, match="Cannot coerce"):
        _ = f + [1, 2, 3]
