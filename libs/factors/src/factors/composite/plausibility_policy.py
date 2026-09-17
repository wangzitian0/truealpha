"""Plausibility policy (#544, v2 since #528): is a published factor physically possible?

Four defects reached production in 2026-07 with every gate green — XOM's operating metric
above NVIDIA's (a revenue proxy), MA's price-to-sales down 7x on a flat price (a share
count), JPM's negative bank metric (a uniform capital charge), V valued on a 2010 share
count — and each was found by re-deriving from the vendor by hand. Every other gate in
this repository compares the system against something the same process authored. This
one compares a run against physical reality and against the previous accepted run, in
the tick's own transaction, and fails the run instead of materialising a row nobody would
believe on sight.

The thresholds are a versioned policy (owner decision 2026-09-07, recorded on #544), not
constants: a change is a new version with its own row on the issue, never a silent edit. Rule
ids are stable so exemptions (`tools/output_invariant_exemptions.json`, issue + expiry) can
name them; `node-sign-policy` shares the nightly suite's invariant of the same id because it
is the same question judged at a different time.

v2 (#528): v1's `sign-per-branch` asserted that a financial or insurance issuer's operating
metric cannot be negative, while the definition it judged (GPPE v0.2.0, frozen by #59) says a
negative value is a valid low signal for every issuer. The two could not both hold, and v1
held only under an expiring exemption. v2 judges each published value against the sign
policy its metric-forest node declares (`factors.forest`): a value a node forbids is refused,
a value published for a class the node does not declare is refused, and a negative value the
node declares `sign-is-signal` passes and is reported by name on every tick (`sign_signals`).
The other three rules and every threshold are unchanged.

Pure functions over plain rows, so the same policy can judge a synthetic run in a test,
a live tick in the data engine, and a historical replay in the backtest (#758 H4).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from factors.forest import SignFinding, judge_sign, published_node

POLICY_VERSION = "v2"

RULE_UNIVERSE_MAX = "universe-max-regression"
RULE_VALUATION_MOVE = "unexplained-valuation-move"
RULE_NODE_SIGN_POLICY = "node-sign-policy"
RULE_EMPTY_ELIGIBLE = "empty-eligible-set"
RULES = (RULE_UNIVERSE_MAX, RULE_VALUATION_MOVE, RULE_NODE_SIGN_POLICY, RULE_EMPTY_ELIGIBLE)

#: The published table the gate reads (`plausibility_gate._ROWS_SQL`); `Row.operating_efficiency`
#: is its `operating_efficiency` column, and the forest says which node that column carries.
JUDGED_TABLE = "mart.topt_core_results"
JUDGED_COLUMN = "operating_efficiency"


@dataclass(frozen=True)
class Thresholds:
    """Unchanged since v1. Recorded on #544; bump `POLICY_VERSION` with any change."""

    universe_max_factor: Decimal = Decimal("1.5")
    valuation_move_ratio: Decimal = Decimal("2")
    #: A price move of at least this ratio "explains" a valuation move of
    #: `valuation_move_ratio` — the square root of 2, i.e. half the move in log terms.
    price_move_floor: Decimal = Decimal("1.4")


THRESHOLDS = Thresholds()


@dataclass(frozen=True)
class Row:
    """One issuer's published core result — the columns the policy judges."""

    listing_id: str
    operating_branch: str
    availability: str
    operating_efficiency: Decimal | None
    current_ps: Decimal | None
    #: The price behind `current_ps` at this cutoff; None when the run carries no price
    #: for the issuer (then rule 2 cannot judge it and says so).
    last_close: Decimal | None = None


@dataclass(frozen=True)
class Violation:
    rule: str
    listing_id: str | None
    detail: str


def _available(rows: list[Row]) -> list[Row]:
    return [row for row in rows if row.availability == "available"]


def universe_max_regression(
    current: list[Row], previous: list[Row], thresholds: Thresholds = THRESHOLDS
) -> list[Violation]:
    """The top operating metric may not exceed the previous accepted run's universe
    maximum by more than `universe_max_factor` (#533: XOM at 4,984,153 above NVIDIA)."""
    prior = [row.operating_efficiency for row in _available(previous) if row.operating_efficiency is not None]
    if not prior:
        return []
    ceiling = max(prior) * thresholds.universe_max_factor
    return [
        Violation(
            RULE_UNIVERSE_MAX,
            row.listing_id,
            f"operating metric {row.operating_efficiency:,.0f} exceeds {thresholds.universe_max_factor}x the previous "
            f"accepted run's universe maximum {max(prior):,.0f}",
        )
        for row in _available(current)
        if row.operating_efficiency is not None and row.operating_efficiency > ceiling
    ]


def _ratio(now: Decimal, before: Decimal) -> Decimal | None:
    if now <= 0 or before <= 0:
        return None
    return now / before if now >= before else before / now


def unexplained_valuation_move(
    current: list[Row], previous: list[Row], thresholds: Thresholds = THRESHOLDS
) -> list[Violation]:
    """An issuer's price-to-sales may not move more than `valuation_move_ratio` run over
    run unless its price moved at least `price_move_floor` (#533 MA 15.55 → 2.17 on a
    flat price; #529 V on a 2010 share count). Without a price on both sides the rule
    cannot judge and stays silent — a missing measurement is not evidence either way."""
    before = {row.listing_id: row for row in _available(previous)}
    violations: list[Violation] = []
    for row in _available(current):
        prior = before.get(row.listing_id)
        if prior is None or row.current_ps is None or prior.current_ps is None:
            continue
        move = _ratio(row.current_ps, prior.current_ps)
        if move is None or move <= thresholds.valuation_move_ratio:
            continue
        if row.last_close is None or prior.last_close is None:
            continue
        price_move = _ratio(row.last_close, prior.last_close)
        if price_move is not None and price_move >= thresholds.price_move_floor:
            continue
        violations.append(
            Violation(
                RULE_VALUATION_MOVE,
                row.listing_id,
                f"price-to-sales moved {move:.2f}x ({prior.current_ps} → {row.current_ps}) while the price moved "
                f"{(price_move or Decimal(1)):.2f}x ({prior.last_close} → {row.last_close}); a valuation move that "
                f"large without a price move is a share-count or revenue error",
            )
        )
    return violations


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
    class the node does not declare, is refused (#528)."""
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
    tick ran no strategy, which is not this rule's business."""
    violations: list[Violation] = []
    if l2_complete is not None and l2_complete == 0:
        violations.append(
            Violation(RULE_EMPTY_ELIGIBLE, None, "L2 complete count is 0: no issuer has every strategy input")
        )
    if outcomes:
        total = sum(outcomes.values())
        excluded = outcomes.get("excluded", 0)
        if total > 0 and excluded == total:
            violations.append(
                Violation(
                    RULE_EMPTY_ELIGIBLE, None, f"all {total} decisions are excluded: the strategy selected nothing"
                )
            )
    return violations


def evaluate(
    current: list[Row],
    previous: list[Row],
    *,
    outcomes: dict[str, int] | None = None,
    l2_complete: int | None = None,
    thresholds: Thresholds = THRESHOLDS,
) -> list[Violation]:
    """Every violation of the policy for a run, judged against the previous accepted run."""
    return [
        *universe_max_regression(current, previous, thresholds),
        *unexplained_valuation_move(current, previous, thresholds),
        *node_sign_policy(current),
        *empty_eligible_set(outcomes, l2_complete),
    ]
