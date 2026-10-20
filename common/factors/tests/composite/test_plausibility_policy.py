"""#544 acceptance 1: each rule fails against a synthetic run reproducing the real defect it
targets.

v2 (#528): the sign rule judges the metric-forest node's declared policy instead of v1's
"a financial branch cannot be negative", which contradicted the definition it judged.

v4 (#1443): no rule reads the previous accepted run."""

from __future__ import annotations

import inspect
from decimal import Decimal

import pytest
from factors.composite import plausibility_policy as policy
from factors.composite.plausibility_policy import Row, evaluate, sign_signals
from factors.forest import IssuerClass, SignPolicy, published_node
from truealpha_contracts.strategy import ExclusionReason

D = Decimal


def row(listing: str, op: str | None, *, branch: str = "non_financial", availability: str = "available") -> Row:
    return Row(
        listing_id=listing,
        operating_branch=branch,
        availability=availability,
        operating_efficiency=D(op) if op is not None else None,
    )


# The 2026-09-06 production head, abbreviated.
PREVIOUS = [
    row("listing:xnas:nvda", "3272605"),
    row("listing:xnas:meta", "1804263"),
    row("listing:xnys:ma", "751418"),
    row("listing:xnys:jpm", "600000", branch="financial"),
]


def test_a_negative_bank_metric_is_a_declared_signal_not_a_violation() -> None:
    """#528: JPM at -514,726 is what GPPE v0.2.0 (#59) defines as a valid low signal. v1
    refused it under an expiring exemption; v2 accepts it and names it."""
    jpm = row("listing:xnys:jpm", "-514726", branch="financial")
    assert evaluate([jpm]) == []
    (signal,) = sign_signals([jpm])
    assert signal.rule == policy.RULE_NODE_SIGN_POLICY and signal.listing_id == "listing:xnys:jpm"
    assert "sign-is-signal" in signal.detail and "-514,726.00" in signal.detail
    # the same holds for every class the node declares, and an unavailable row is not judged
    assert sign_signals([row("listing:xnas:tsla", "-1")])
    assert sign_signals([row("listing:xnys:jpm", "-1", branch="financial", availability="unavailable")]) == []
    # a non-negative value says nothing
    assert sign_signals(PREVIOUS) == []


@pytest.fixture
def strict_financial(monkeypatch: pytest.MonkeyPatch) -> None:
    node = published_node(policy.JUDGED_TABLE, policy.JUDGED_COLUMN)
    strict = node.model_copy(
        update={"sign_policy": {**node.sign_policy, IssuerClass.FINANCIAL: SignPolicy.MUST_BE_NON_NEGATIVE}}
    )
    monkeypatch.setattr(policy, "published_node", lambda table, column: strict)


def test_a_sign_the_node_forbids_is_caught(strict_financial: None) -> None:
    """The rule is only as strict as the definition: where a node forbids a negative value,
    the v1 shape is caught exactly as before."""
    current = [row("listing:xnys:jpm", "-514726", branch="financial")]
    (violation,) = evaluate(current)
    assert violation.rule == policy.RULE_NODE_SIGN_POLICY and violation.listing_id == "listing:xnys:jpm"
    assert "must-be-non-negative" in violation.detail
    assert sign_signals(current) == []
    # the non-financial class still declares a signal, and an unavailable row is not judged
    assert evaluate([row("listing:xnas:tsla", "-1")]) == []
    assert evaluate([row("listing:xnys:jpm", "-1", branch="financial", availability="unavailable")]) == []


def test_a_value_for_a_class_the_node_does_not_declare_is_caught() -> None:
    """No policy vouches for a branch the forest does not know, whatever the sign."""
    (violation,) = evaluate([row("listing:xnys:new", "10", branch="sovereign_fund")])
    assert violation.rule == policy.RULE_NODE_SIGN_POLICY and "no policy for this issuer class" in violation.detail


def test_an_empty_eligible_set_is_caught() -> None:
    """#527: 100 % excluded decisions, or an L2 complete count of zero, recorded SUCCESS."""
    all_excluded = evaluate(PREVIOUS, outcomes={"excluded": 20}, l2_complete=0)
    assert [v.rule for v in all_excluded] == [policy.RULE_EMPTY_ELIGIBLE, policy.RULE_EMPTY_ELIGIBLE]
    assert evaluate(PREVIOUS, outcomes={"selected": 2, "excluded": 18}, l2_complete=17) == []
    # a tick without a strategy passes nothing here and is not judged
    assert evaluate(PREVIOUS) == []
    # #544: outcomes={} (a strategy run that produced 0 decisions) must not be exempted
    assert [v.rule for v in evaluate(PREVIOUS, outcomes={}, l2_complete=5)] == [policy.RULE_EMPTY_ELIGIBLE]
    assert [v.rule for v in policy.empty_eligible_set({}, 5)] == [policy.RULE_EMPTY_ELIGIBLE]


def test_no_rule_reads_the_previous_run() -> None:
    """#1443 acceptance 2: no refusal names "moved Nx versus the previous run". The policy takes no
    previous run, and a row carries nothing a previous run could be compared with."""
    assert "previous" not in inspect.signature(evaluate).parameters
    assert set(Row.__dataclass_fields__) == {"listing_id", "operating_branch", "availability", "operating_efficiency"}
    for deleted in ("universe_max_regression", "unexplained_valuation_move", "Thresholds"):
        assert not hasattr(policy, deleted), deleted


def test_the_policy_is_v4_with_named_rules() -> None:
    assert policy.POLICY_VERSION == "v4"
    assert set(policy.RULES) == {"node-sign-policy", "empty-eligible-set"}
    # #1323: the issuer rule names the exclusion reason its refused decision carries
    assert policy.EXCLUSION_REASON == {
        policy.RULE_NODE_SIGN_POLICY: ExclusionReason.PLAUSIBILITY_REFUSED_NODE_SIGN_POLICY
    }
    # the gate judges the published column the forest ties to GPPE v0.2.0 (#1176: the uniform node)
    assert published_node(policy.JUDGED_TABLE, policy.JUDGED_COLUMN).key == "gppe_uniform_charge_v0"
