"""Plausibility policy (#544, v4 since #1443): is a published factor physically possible?

The gate runs in the tick's own transaction and judges each published row against its
definition. Whether a published value is right is a question for an independent answer: the
sentinel known answers check that every night on a sample (#1416 E4). Strong dashboards, weak
gates (AGENTS.md rule 4).

The rules are a versioned policy (owner decision 2026-09-07, recorded on #544): a change is a
new version, never a silent edit. Rule ids are stable so exemptions
(`tools/output_invariant_exemptions.json`, issue + expiry) can name them; `node-sign-policy`
shares the nightly suite's invariant of the same id because it is the same question judged at a
different time.

v2 (#528): each published value is judged against the sign policy its metric-forest node
declares (`factors.forest`). A value a node forbids is refused, a value published for a class
the node does not declare is refused, and a negative value the node declares `sign-is-signal`
passes and is reported by name on every tick (`sign_signals`).

v4 (#1443, #1323): v1 to v3 also compared each run with the previous accepted run. That
reference is our own earlier output, so it protected whatever was published first. It accepted
MU's price-to-sales of 31.30 and then refused the correction to 8.74 on every TOPT tick from
2026-10-09 to 2026-10-12, which froze the governed head. v4 deletes the two rules that read the
previous run (`universe-max-regression`, `unexplained-valuation-move`). The source now refuses
the shapes they caught: a share count older than 730 days (#529), and revenue used as gross
profit where the industry does not allow it (#533). A refusal names one issuer: the caller
excludes that issuer and publishes the others (#1323). Only an empty eligible set refuses the run.

Pure functions over plain rows, so the same policy can judge a synthetic run in a test,
a live tick in the data engine, and a historical replay in the backtest (#758 H4).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from truealpha_contracts.strategy import ExclusionReason

from factors.forest import SignFinding, judge_sign, published_node

POLICY_VERSION = "v4"

RULE_NODE_SIGN_POLICY = "node-sign-policy"
RULE_EMPTY_ELIGIBLE = "empty-eligible-set"
RULES = (RULE_NODE_SIGN_POLICY, RULE_EMPTY_ELIGIBLE)

#: The exclusion reason a refused issuer's strategy decision carries, one per issuer rule.
EXCLUSION_REASON: Mapping[str, ExclusionReason] = {
    RULE_NODE_SIGN_POLICY: ExclusionReason.PLAUSIBILITY_REFUSED_NODE_SIGN_POLICY,
}

#: The published table the gate reads (`plausibility_gate._ROWS_SQL`); `Row.operating_efficiency`
#: is its `operating_efficiency` column, and the forest says which node that column carries.
JUDGED_TABLE = "mart.topt_core_results"
JUDGED_COLUMN = "operating_efficiency"


@dataclass(frozen=True)
class Row:
    """One issuer's published core result — the columns the policy judges."""

    listing_id: str
    operating_branch: str
    availability: str
    operating_efficiency: Decimal | None


@dataclass(frozen=True)
class Violation:
    rule: str
    listing_id: str | None
    detail: str


def _available(rows: list[Row]) -> list[Row]:
    return [row for row in rows if row.availability == "available"]


def _sign_findings(current: list[Row]) -> list[SignFinding]:
    node = published_node(JUDGED_TABLE, JUDGED_COLUMN)
    findings = (
        judge_sign(node, issuer_class=row.operating_branch, value=row.operating_efficiency, subject=row.listing_id)
        for row in _available(current)
    )
    return [finding for finding in findings if finding is not None]


def _describe(finding: SignFinding, branch: str) -> str:
    policy = finding.policy.value if finding.policy is not None else "no policy for this issuer class"
    return f"{branch} {finding.node_key} {finding.value:,.2f} ({policy}, node {finding.node_id})"


def node_sign_policy(current: list[Row]) -> list[Violation]:
    """A published operating metric must respect the sign policy of the forest node it
    carries: negative where the node is `must-be-non-negative`, or any value for an issuer
    class the node does not declare, is refused (#528). Each refusal names one issuer."""
    branches = {row.listing_id: row.operating_branch for row in current}
    return [
        Violation(RULE_NODE_SIGN_POLICY, finding.subject, _describe(finding, branches[finding.subject]))
        for finding in _sign_findings(current)
        if finding.violation
    ]


def sign_signals(current: list[Row]) -> list[Violation]:
    """Negative values the node declares `sign-is-signal`: not violations, and never silent.
    The gate prints each one so a published negative number is always visible by name."""
    branches = {row.listing_id: row.operating_branch for row in current}
    return [
        Violation(RULE_NODE_SIGN_POLICY, finding.subject, _describe(finding, branches[finding.subject]))
        for finding in _sign_findings(current)
        if finding.signal
    ]


def empty_eligible_set(outcomes: dict[str, int] | None, l2_complete: int | None) -> list[Violation]:
    """A strategy run whose decisions are all excluded, or whose L2 complete count is
    zero, publishes nothing and must not be recorded as SUCCESS (#527). None means the
    tick ran no strategy, which is not this rule's business. This is the only run refusal."""
    violations: list[Violation] = []
    if l2_complete is not None and l2_complete == 0:
        violations.append(
            Violation(RULE_EMPTY_ELIGIBLE, None, "L2 complete count is 0: no issuer has every strategy input")
        )
    if outcomes is not None:
        total = sum(outcomes.values())
        excluded = outcomes.get("excluded", 0)
        if total == 0:
            violations.append(
                Violation(RULE_EMPTY_ELIGIBLE, None, "strategy produced 0 decisions: the strategy selected nothing")
            )
        elif excluded == total:
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
    """Every refusal of a run: the issuer refusals, then the run refusals. None reads another run."""
    return [*node_sign_policy(current), *empty_eligible_set(outcomes, l2_complete)]
