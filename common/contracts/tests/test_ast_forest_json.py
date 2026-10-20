"""Standing checks for F1 (#1413): typed, JSON-loadable AST and metric-forest nodes.

Every registered tree and AST node serialises to JSON and loads back equal;
a dict without a type tag (or unknown type tag) is refused.
"""

from __future__ import annotations

import json

import pytest
from factors.forest import FOREST, Decomposition, Forest, MetricNode, MetricTree
from pydantic import ValidationError
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


def test_every_ast_node_round_trips_through_json_and_base_validator() -> None:
    nodes: tuple[FactorASTNode, ...] = (
        Feature(name="gross_profit"),
        Numeric(value=100),
        Numeric(value=0.05),
        Add(left=Feature(name="gross_profit"), right=Numeric(value=10)),
        Sub(
            left=Feature(name="gross_profit"),
            right=Mul(left=Feature(name="total_assets"), right=Feature(name="risk_free_rate")),
        ),
        Div(
            left=Sub(
                left=Feature(name="gross_profit"),
                right=Mul(left=Feature(name="total_assets"), right=Feature(name="risk_free_rate")),
            ),
            right=Feature(name="employees_total"),
        ),
        Ref(expr=Feature(name="close"), n=5),
        Mean(expr=Feature(name="close"), n=10),
        Std(expr=Feature(name="close"), n=20),
        Rank(expr=Div(left=Feature(name="close"), right=Feature(name="revenue"))),
    )
    for node in nodes:
        raw_json = node.model_dump_json()
        payload = json.loads(raw_json)
        assert "type" in payload
        reloaded_specific = type(node).model_validate_json(raw_json)
        reloaded_base = FactorASTNode.model_validate_json(raw_json)
        reloaded_dict = FactorASTNode.model_validate(payload)
        assert reloaded_specific == node
        assert reloaded_base == node
        assert reloaded_dict == node


def test_ast_dict_without_type_tag_or_unknown_tag_is_refused() -> None:
    with pytest.raises((ValidationError, ValueError), match="type"):
        FactorASTNode.model_validate({"name": "gross_profit"})

    with pytest.raises((ValidationError, ValueError), match="type"):
        Add(left={"name": "gross_profit"}, right={"value": 1})

    with pytest.raises((ValidationError, ValueError), match="Unknown AST node type tag"):
        FactorASTNode.model_validate({"type": "nonexistent_op", "name": "gross_profit"})


def test_every_registered_forest_node_and_tree_round_trips_through_json() -> None:
    for node in FOREST.nodes:
        raw_json = node.model_dump_json()
        reloaded = MetricNode.model_validate_json(raw_json)
        assert reloaded == node

    for tree in FOREST.trees:
        raw_json = tree.model_dump_json()
        reloaded = MetricTree.model_validate_json(raw_json)
        assert reloaded == tree
        assert FOREST.tree_sha256(reloaded) == FOREST.tree_sha256(tree)
        for decomp in tree.decompositions:
            assert Decomposition.model_validate_json(decomp.model_dump_json()) == decomp

    forest_json = FOREST.model_dump_json()
    reloaded_forest = Forest.model_validate_json(forest_json)
    assert reloaded_forest == FOREST


def test_forest_node_dict_without_kind_tag_is_refused() -> None:
    node_payload = FOREST.node("gppe_uniform_charge_v0").model_dump(mode="json")
    del node_payload["kind"]
    with pytest.raises(ValidationError):
        MetricNode.model_validate(node_payload)
