"""#1518 criterion 3b: the definition pins the labor-efficiency tree, and every engine binds that pin.

`bound_tree(definition, issuer_class)` returns the registered tree that the definition names for
the class. It raises when the pin does not match the registered tree. The tests drive the same
function that the composite calls (`evaluate_cutoff`), so a table of trees that ignores the
definition fails here.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from factors.composite.strategy_evaluator import IssuerInput, evaluate_cutoff, required_input_keys
from factors.forest import (
    FOREST,
    GPPE_BANKING_TCE_TREE,
    GPPE_V0_TREE,
    AliasKind,
    Forest,
    IssuerClass,
    MetricTree,
    NodeAlias,
    UnpinnedClassError,
    bound_tree,
    capital_adjusted_node,
    operating_numerator_node,
    published_node,
    uniform_tree_pins,
)
from truealpha_contracts.strategy import (
    LIVE_STRATEGY_DEFINITION_RESOURCE,
    LargeModelValueV0Definition,
    load_live_strategy_definition,
    load_strategy_definition_corpus,
)

_CUTOFF = datetime(2026, 3, 31, tzinfo=UTC)
_TCE_LEAVES = frozenset(
    {"stockholders_equity", "preferred_stock_value", "goodwill", "intangible_assets_net_excluding_goodwill"}
)


def _payload() -> dict[str, Any]:
    return json.loads(
        json.dumps(load_strategy_definition_corpus(LIVE_STRATEGY_DEFINITION_RESOURCE)["strategy_definition"])
    )


def _definition(payload: dict[str, Any]) -> LargeModelValueV0Definition:
    return LargeModelValueV0Definition.model_validate_json(json.dumps(payload))


def _bank() -> IssuerInput:
    """JPM FY2025: equity 362,438M, preferred 20,045M, goodwill 52,731M, intangibles 2,600M, assets 4,424,900M."""
    values = {
        "gross_profit": "86807000000",
        "total_assets": "4424900000000",
        "headcount": "318512",
        "revenue": "182447000000",
        "shares_outstanding": "2697032375",
        "last_close": "294.16",
        "stockholders_equity": "362438000000",
        "preferred_stock_value": "20045000000",
        "goodwill": "52731000000",
        "intangible_assets_net_excluding_goodwill": "2600000000",
    }
    records = {key: (Decimal(value), Decimal("0.9")) for key, value in values.items()}
    return IssuerInput(
        issuer_id="issuer:bank",
        records=records,
        knowable_at=dict.fromkeys(records, _CUTOFF - timedelta(days=3)),
        issuer_class=IssuerClass.FINANCIAL,
    )


@pytest.mark.parametrize("issuer_class", sorted(IssuerClass))
def test_each_live_pin_equals_the_hash_of_the_registered_tree(issuer_class: IssuerClass) -> None:
    live = load_live_strategy_definition()
    assert live.labor_efficiency_tree_keys is not None
    pin = live.labor_efficiency_tree_keys[issuer_class.value]
    tree = bound_tree(live, issuer_class)
    assert (tree.key, tree.version) == (pin.tree_key, pin.tree_version)
    assert FOREST.tree_sha256(tree) == pin.tree_sha256


def test_the_financial_class_binds_the_banking_tree_and_every_other_class_the_uniform_tree() -> None:
    live = load_live_strategy_definition()
    assert bound_tree(live, IssuerClass.FINANCIAL) is GPPE_BANKING_TCE_TREE
    assert bound_tree(live, IssuerClass.NON_FINANCIAL) is GPPE_V0_TREE
    assert bound_tree(live, IssuerClass.INSURANCE) is GPPE_V0_TREE


def test_an_edited_alias_of_a_reached_node_raises() -> None:
    # An alias is part of the tree identity. A forest whose banking leaf names another snapshot field
    # is another tree, so the pin of the live definition no longer matches it.
    edited_nodes = tuple(
        node.model_copy(
            update={
                "aliases": tuple(
                    NodeAlias(kind=AliasKind.TOPT_SNAPSHOT_FIELD, value="goodwill_edited")
                    if alias.kind is AliasKind.TOPT_SNAPSHOT_FIELD
                    else alias
                    for alias in node.aliases
                )
            }
        )
        if node.key == "goodwill"
        else node
        for node in FOREST.nodes
    )
    edited = Forest(nodes=edited_nodes, trees=FOREST.trees)
    with pytest.raises(ValueError, match="registered tree has hash"):
        bound_tree(load_live_strategy_definition(), IssuerClass.FINANCIAL, forest=edited)


def test_a_pin_of_an_unregistered_version_raises() -> None:
    payload = _payload()
    payload["labor_efficiency_tree_keys"]["financial"]["tree_version"] = "v1"
    with pytest.raises(ValueError, match="does not register"):
        bound_tree(_definition(payload), IssuerClass.FINANCIAL)


def test_a_class_without_a_pin_raises() -> None:
    payload = _payload()
    del payload["labor_efficiency_tree_keys"]["insurance"]
    with pytest.raises(ValueError, match="pins no tree for the class insurance"):
        bound_tree(_definition(payload), IssuerClass.INSURANCE)


def test_the_frozen_replay_pins_hold_the_registered_hash_of_the_uniform_tree() -> None:
    pins = uniform_tree_pins()
    assert set(pins) == {issuer_class.value for issuer_class in IssuerClass}
    assert {pin.tree_sha256 for pin in pins.values()} == {FOREST.tree_sha256(GPPE_V0_TREE)}


def test_a_changed_pin_changes_the_decision() -> None:
    # Mutation proof: pin the financial class to the uniform tree and the same bank gets the
    # total-assets value. A decision that ignored the definition would give the same value twice.
    live = load_live_strategy_definition()
    uniform_pin = uniform_tree_pins()["financial"].model_dump()
    payload = _payload()
    payload["labor_efficiency_tree_keys"]["financial"] = uniform_pin
    repinned = _definition(payload)
    [tce] = evaluate_cutoff([_bank()], definition=live, cutoff_at=_CUTOFF, risk_free_rate=Decimal("0.05"))
    [uniform] = evaluate_cutoff([_bank()], definition=repinned, cutoff_at=_CUTOFF, risk_free_rate=Decimal("0.05"))
    assert tce.capital_adjusted_labor_efficiency == Decimal("227476.20")
    assert uniform.capital_adjusted_labor_efficiency == Decimal("-422081.43")
    assert (tce.labor_efficiency_metric, uniform.labor_efficiency_metric) == (
        "gppe_banking_tce_v1",
        "gppe_uniform_charge_v0",
    )


def test_the_required_inputs_follow_the_pinned_trees() -> None:
    live = load_live_strategy_definition()
    bank = required_input_keys(live, IssuerClass.FINANCIAL)
    other = required_input_keys(live, IssuerClass.NON_FINANCIAL)
    assert _TCE_LEAVES <= bank and "total_assets" not in bank
    assert "total_assets" in other and not _TCE_LEAVES & other
    assert required_input_keys(live) == bank | other
    # Re-pin the bank to the uniform tree: its required inputs change with it.
    payload = _payload()
    payload["labor_efficiency_tree_keys"]["financial"] = uniform_tree_pins()["financial"].model_dump()
    repinned = required_input_keys(_definition(payload), IssuerClass.FINANCIAL)
    assert "total_assets" in repinned and not _TCE_LEAVES & repinned


def test_an_unpinned_class_is_an_exclusion_in_the_composite() -> None:
    # The composite and the core agree (#1518 review): a class with no pin is never an abort. The core gives an
    # unavailable row (`test_gppe_tree.py`). The composite excludes the issuer with `missing_issuer_class`.
    payload = _payload()
    del payload["labor_efficiency_tree_keys"]["financial"]
    partial = _definition(payload)
    with pytest.raises(UnpinnedClassError):
        bound_tree(partial, IssuerClass.FINANCIAL)
    [decision] = evaluate_cutoff([_bank()], definition=partial, cutoff_at=_CUTOFF, risk_free_rate=Decimal("0.05"))
    assert decision.eligible is False
    assert decision.exclusion_reason is not None and decision.exclusion_reason.value == "missing_issuer_class"


@pytest.mark.parametrize(
    ("tree", "issuer_class", "field"),
    [
        (GPPE_V0_TREE, IssuerClass.NON_FINANCIAL, "gross_profit"),
        (GPPE_V0_TREE, IssuerClass.INSURANCE, "gross_profit"),
        (GPPE_V0_TREE, IssuerClass.FINANCIAL, "pre_provision_profit"),
        (GPPE_BANKING_TCE_TREE, IssuerClass.FINANCIAL, "pre_provision_profit"),
    ],
)
def test_the_numerator_of_p_over_gp_is_the_captured_input_of_the_pinned_tree(
    tree: MetricTree, issuer_class: IssuerClass, field: str
) -> None:
    # Hand reading of the trees: the uniform tree binds `operating_gross_profit` to gross profit, or to a bank's
    # pre-provision profit. The banking tree subtracts its charge from the pre-provision profit.
    node = operating_numerator_node(tree, issuer_class)
    assert node.alias(AliasKind.TOPT_SNAPSHOT_FIELD) == field


@pytest.mark.parametrize("issuer_class", sorted(IssuerClass))
def test_the_published_column_is_judged_by_a_node_that_has_the_sign_policy_of_the_pinned_root(
    issuer_class: IssuerClass,
) -> None:
    """The mart column `gppe` holds the root of the PINNED tree of the row's class (#1518). The registry ties the
    column to one node for the sign policy (`PUBLISHED_COLUMNS`). That is one statement only if the pinned root of
    every class declares the same sign policy as that node, and so does the numerator column."""
    live = load_live_strategy_definition()
    tree = bound_tree(live, issuer_class)
    judged = published_node("mart.topt_gppe_results", "gppe")
    assert FOREST.node(tree.root).sign_policy[issuer_class] == judged.sign_policy[issuer_class]
    judged_numerator = published_node("mart.topt_gppe_results", "capital_adjusted_gross_profit")
    numerator = FOREST.node(capital_adjusted_node(tree, issuer_class))
    assert numerator.sign_policy[issuer_class] == judged_numerator.sign_policy[issuer_class]
