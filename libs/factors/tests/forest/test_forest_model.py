"""The forest's structural rules (#528, docs/metric-forest.md §2): each is a sentence of the
design, and each is shown failing on the shape it forbids."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest
from factors.forest import (
    FOREST,
    GPPE_V0_TREE,
    PUBLISHED_COLUMNS,
    AliasKind,
    ConfidenceBand,
    ConfidenceRule,
    Decomposition,
    Forest,
    IssuerClass,
    NodeKind,
    SignPolicy,
    judge_sign,
    published_node,
)
from pydantic import ValidationError
from truealpha_contracts.metrics import METRICS


def _node(key: str):
    return FOREST.node(key)


def test_every_node_and_tree_has_a_unique_uuid_and_typed_aliases_that_resolve() -> None:
    ids = [node.node_id for node in FOREST.nodes] + [tree.tree_id for tree in FOREST.trees]
    assert len(set(ids)) == len(ids) and all(isinstance(value, UUID) for value in ids)
    for node in FOREST.nodes:
        metric = node.alias(AliasKind.METRIC)
        assert metric is None or metric in METRICS
        assert node.alias(AliasKind.KEY) == node.key


#: node key -> UUID, as minted. A UUID is an identity: the graph and embedding stores key on it
#: (docs/metric-forest.md §8), so it never changes, even when a key is renamed.
MINTED = {
    "labor_efficiency": "8f78ed40-9625-58eb-a866-4fd42015b43d",
    "gross_profit": "c682a9c8-c41d-5262-899b-f2d62000e10f",
    "pre_provision_profit": "75bf2b7a-1600-5bcf-9d86-648fb16a18ae",
    "total_assets": "c1d19aea-eb4a-525e-b62c-2fc9efe3bf77",
    "employees_total": "c7db1ab2-d0fa-5437-800e-34f3be376420",
    "risk_free_rate": "c6ae2f22-4b96-5610-8482-71c6ccd132ed",
    "operating_gross_profit": "34a38955-e69e-559b-a483-91f81b9a6ab0",
    "capital_charge": "6b4ce481-f045-5503-9675-921160e2bcd2",
    "capital_adjusted_gross_profit": "c3428286-eb85-5107-a32a-b8757ad5b340",
    "gppe": "6143cf17-ceba-5f78-a2d3-a7cc423c49a7",
}


def test_minted_uuids_never_change() -> None:
    assert {node.key: str(node.node_id) for node in FOREST.nodes} == MINTED
    assert str(GPPE_V0_TREE.tree_id) == "ef13b940-b695-5f5f-846a-a36face5cc3e"


def test_captured_inputs_name_a_confidence_family_and_nothing_else_does() -> None:
    for node in FOREST.nodes:
        if node.kind is NodeKind.INPUT:
            assert node.confidence is not None and node.confidence.family, node.key
        elif node.confidence is not None:
            assert node.confidence.family is None, node.key
    with pytest.raises(ValidationError, match="confidence-report family"):
        ConfidenceBand(rule=ConfidenceRule.MINIMUM_CONSUMED, family="gross_profit")
    with pytest.raises(ValidationError, match="confidence-report family"):
        ConfidenceBand(rule=ConfidenceRule.AS_CAPTURED)


def test_a_node_declares_a_sign_policy_for_exactly_its_classes() -> None:
    with pytest.raises(ValidationError, match="exactly its applicable classes"):
        _node("gppe").model_validate(
            {**_node("gppe").model_dump(), "sign_policy": {IssuerClass.FINANCIAL: SignPolicy.SIGN_IS_SIGNAL}}
        )


def test_a_derived_node_has_one_decomposition_across_the_forest() -> None:
    """Otherwise its value would depend on which tree reached it, and one wide-row column per
    node would be meaningless. A different formula is a different node."""
    other = GPPE_V0_TREE.model_copy(
        update={
            "version": "rival",
            "tree_id": UUID(int=1),
            "decompositions": (
                Decomposition(
                    output="gppe",
                    formula_id="ratio",
                    formula_version=1,
                    operands={c: ("operating_gross_profit", "employees_total") for c in IssuerClass},
                ),
                GPPE_V0_TREE.decompositions[3],
            ),
        }
    )
    with pytest.raises(ValidationError, match="decomposed two ways"):
        Forest(nodes=FOREST.nodes, trees=(*FOREST.trees, other))


def test_a_tree_is_acyclic_reachable_and_binds_every_class() -> None:
    cyclic = GPPE_V0_TREE.model_copy(
        update={
            "decompositions": (
                *GPPE_V0_TREE.decompositions[:3],
                Decomposition(
                    output="operating_gross_profit",
                    formula_id="identity",
                    formula_version=1,
                    operands={c: ("gppe",) for c in IssuerClass},
                ),
            )
        }
    )
    with pytest.raises(ValidationError, match="cyclic"):
        Forest(nodes=FOREST.nodes, trees=(cyclic,))
    partial = GPPE_V0_TREE.model_copy(
        update={
            "decompositions": (
                Decomposition(
                    output="gppe",
                    formula_id="ratio",
                    formula_version=1,
                    operands={IssuerClass.FINANCIAL: ("capital_adjusted_gross_profit", "employees_total")},
                ),
                *GPPE_V0_TREE.decompositions[1:],
            )
        }
    )
    with pytest.raises(ValidationError, match="every class"):
        Forest(nodes=FOREST.nodes, trees=(partial,))
    # a bank has no gross-profit input to bind
    wrong_proxy = GPPE_V0_TREE.model_copy(
        update={
            "decompositions": (
                *GPPE_V0_TREE.decompositions[:3],
                Decomposition(
                    output="operating_gross_profit",
                    formula_id="identity",
                    formula_version=1,
                    operands={c: ("gross_profit",) for c in IssuerClass},
                ),
            )
        }
    )
    with pytest.raises(ValidationError, match="does not apply to financial"):
        Forest(nodes=FOREST.nodes, trees=(wrong_proxy,))


def test_a_formula_is_registered_and_takes_its_arity() -> None:
    with pytest.raises(ValidationError, match="cannot take 3 operand"):
        Decomposition(
            output="gppe",
            formula_id="ratio",
            formula_version=1,
            operands={IssuerClass.FINANCIAL: ("a", "b", "c")},
        )
    with pytest.raises(ValidationError, match="not registered"):
        Decomposition(
            output="gppe", formula_id="ratio", formula_version=2, operands={IssuerClass.FINANCIAL: ("a", "b")}
        )


def test_the_tree_identity_does_not_depend_on_set_order() -> None:
    """Applicability is a set; a set dumps in hash order, which varies between processes."""
    reordered = GPPE_V0_TREE.model_copy(update={"applicability": frozenset(reversed(list(IssuerClass)))})
    assert FOREST.tree_sha256(reordered) == FOREST.tree_sha256(GPPE_V0_TREE)


@pytest.mark.parametrize(
    ("policy", "value", "violation", "signal", "silent"),
    [
        (SignPolicy.SIGN_IS_SIGNAL, "-1", False, True, False),
        (SignPolicy.SIGN_IS_SIGNAL, "1", False, False, True),
        (SignPolicy.MUST_BE_NON_NEGATIVE, "-1", True, False, False),
        (SignPolicy.MUST_BE_NON_NEGATIVE, "0", False, False, True),
        (SignPolicy.MAY_BE_NEGATIVE, "-1", False, False, True),
    ],
)
def test_judge_sign(policy: SignPolicy, value: str, violation: bool, signal: bool, silent: bool) -> None:
    node = _node("gppe").model_copy(update={"sign_policy": {c: policy for c in IssuerClass}})
    finding = judge_sign(node, issuer_class="financial", value=Decimal(value), subject="listing:x")
    assert (finding is None) == silent
    if finding is not None:
        assert (finding.violation, finding.signal) == (violation, signal)


def test_a_class_the_node_does_not_declare_is_a_violation_whatever_the_sign() -> None:
    finding = judge_sign(_node("gppe"), issuer_class="sovereign_fund", value=Decimal("5"), subject="listing:x")
    assert finding is not None and finding.violation and finding.policy is None
    assert judge_sign(_node("gppe"), issuer_class="sovereign_fund", value=None, subject="listing:x") is None


def test_gppe_v020_declares_a_negative_value_a_signal_for_every_class() -> None:
    """#59's frozen reading, recorded where the invariants read it (#528)."""
    for key in ("gppe", "capital_adjusted_gross_profit"):
        assert dict(_node(key).sign_policy) == {c: SignPolicy.SIGN_IS_SIGNAL for c in IssuerClass}
    assert _node("capital_charge").sign_policy[IssuerClass.FINANCIAL] is SignPolicy.MUST_BE_NON_NEGATIVE


def test_published_columns_name_registered_nodes() -> None:
    for table, columns in PUBLISHED_COLUMNS.items():
        for column in columns:
            assert published_node(table, column).kind is NodeKind.DERIVED
