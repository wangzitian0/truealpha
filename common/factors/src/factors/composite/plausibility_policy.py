"""Plausibility policy (#544, v4 since #1443): check published factors against physical invariants."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal

from truealpha_contracts.strategy import ExclusionReason

from factors.panel import SignFinding, judge_sign, published_node

POLICY_VERSION = "v4"
RULE_NODE_SIGN_POLICY = "node-sign-policy"
RULE_EMPTY_ELIGIBLE = "empty-eligible-set"
RULES = (RULE_NODE_SIGN_POLICY, RULE_EMPTY_ELIGIBLE)

EXCLUSION_REASON: Mapping[str, ExclusionReason] = {
    RULE_NODE_SIGN_POLICY: ExclusionReason.PLAUSIBILITY_REFUSED_NODE_SIGN_POLICY,
}

JUDGED_TABLE = "mart.topt_core_results"
JUDGED_COLUMN = "operating_efficiency"


@dataclass(frozen=True)
class Row:
    listing_id: str
    operating_branch: str
    availability: str
    operating_efficiency: Decimal | None


@dataclass(frozen=True)
class Violation:
    rule: str
    listing_id: str | None
    detail: str


def _collect_sign_violations(current: list[Row], predicate: Callable[[SignFinding], bool]) -> list[Violation]:
    node = published_node(JUDGED_TABLE, JUDGED_COLUMN)
    violations: list[Violation] = []
    for row in current:
        if row.availability != "available":
            continue
        finding = judge_sign(
            node, issuer_class=row.operating_branch, value=row.operating_efficiency, subject=row.listing_id
        )
        if finding is not None and predicate(finding):
            policy = finding.policy.value if finding.policy is not None else "no policy for this issuer class"
            detail = (
                f"{row.operating_branch} {finding.node_key} {finding.value:,.2f} ({policy}, node {finding.node_id})"
            )
            violations.append(Violation(RULE_NODE_SIGN_POLICY, finding.subject, detail))
    return violations


def node_sign_policy(current: list[Row]) -> list[Violation]:
    return _collect_sign_violations(current, lambda finding: finding.violation)


def sign_signals(current: list[Row]) -> list[Violation]:
    return _collect_sign_violations(current, lambda finding: finding.signal)


def empty_eligible_set(outcomes: dict[str, int] | None, l2_complete: int | None) -> list[Violation]:
    violations: list[Violation] = []
    if l2_complete is not None and l2_complete == 0:
        violations.append(
            Violation(RULE_EMPTY_ELIGIBLE, None, "L2 complete count is 0: no issuer has every strategy input")
        )
    if outcomes is not None:
        total = sum(outcomes.values())
        if total == 0:
            violations.append(
                Violation(RULE_EMPTY_ELIGIBLE, None, "strategy produced 0 decisions: the strategy selected nothing")
            )
        elif outcomes.get("excluded", 0) == total:
            violations.append(
                Violation(
                    RULE_EMPTY_ELIGIBLE, None, f"all {total} decisions are excluded: the strategy selected nothing"
                )
            )
    return violations


def evaluate(
    current: list[Row],
    *,
    outcomes: dict[str, int] | None = None,
    l2_complete: int | None = None,
) -> list[Violation]:
    return [*node_sign_policy(current), *empty_eligible_set(outcomes, l2_complete)]
