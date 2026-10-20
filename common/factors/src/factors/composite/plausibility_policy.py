"""Plausibility policy (#544, v4 since #1443): is a published factor physically possible?

Four defects reached production in 2026-07 with every gate green — XOM's operating metric
above NVIDIA's (a revenue proxy), MA's price-to-sales down 7x on a flat price (a share
count), JPM's negative bank metric (a uniform capital charge), V valued on a 2010 share
count — and each was found by re-deriving from the vendor by hand. Every other gate in
this repository compares the system against something the same process authored. This
one compares a run against references the process did not author, in the tick's own
transaction, and refuses an issuer instead of materialising a row nobody would believe on sight.

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

v3 (#1236): `unexplained-valuation-move` accepts a valuation move that a new fiscal year
explains (MU's FY2026 10-K of 2026-10-09). Thresholds are unchanged.

v4 (#1443, #1323): a value is judged only against independent references. v1 to v3 also
compared each run with the previous accepted run. That reference is our own earlier output, so
the check protected whatever was published first. Production MU is the case: its price-to-sales
read 31.30 on FY2025 revenue (37.378B) while the second financial-fact origin reported FY2026
(133.188B, period end 2026-09-03) from 2026-09-30. The gate accepted 31.30, then refused the
correction to 8.74 on every TOPT tick from 2026-10-09 to 2026-10-12 and froze the governed head.
v4 changes three things:

* `independent-reference-disagreement` refuses an issuer whose served revenue or gross profit
  differs by more than `independent_disagreement_ratio` from the independent reference: from the
  reference's figure for the same period, or from its figure for the latest later period (the
  served figure is then superseded). Gross profit is compared only where the served figure is gross profit:
  the non-financial class, without the #533 revenue proxy.
* `universe-max-regression` and `unexplained-valuation-move` read the previous run, so they
  are change detectors (`reviews`). They raise a review flag and never refuse. The shapes they
  caught are now refused at the source: a share count older than 730 days (#529) and revenue
  used as gross profit where the industry does not allow it (#533).
* A refusal names one issuer, and the caller excludes that issuer and publishes the others
  (#1323). The run itself is refused only when more than `refused_share_limit` of its rows
  are refused (a data fault, not a filing) or when nothing is eligible.

The sentinel known answers (#1416 E4) join as a further independent reference when they land.

Pure functions over plain rows, so the same policy can judge a synthetic run in a test,
a live tick in the data engine, and a historical replay in the backtest (#758 H4).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal

from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.strategy import ExclusionReason

from factors.forest import IssuerClass, SignFinding, judge_sign, published_node

POLICY_VERSION = "v4"

RULE_INDEPENDENT_REFERENCE = "independent-reference-disagreement"
RULE_NODE_SIGN_POLICY = "node-sign-policy"
RULE_REFUSED_SHARE = "refused-share-of-run"
RULE_EMPTY_ELIGIBLE = "empty-eligible-set"
#: Rules that refuse one issuer: the caller excludes it and publishes the others.
ISSUER_RULES = (RULE_INDEPENDENT_REFERENCE, RULE_NODE_SIGN_POLICY)
#: Rules that refuse the whole run.
RUN_RULES = (RULE_REFUSED_SHARE, RULE_EMPTY_ELIGIBLE)
RULES = (*ISSUER_RULES, *RUN_RULES)

REVIEW_UNIVERSE_MAX = "universe-max-regression"
REVIEW_VALUATION_MOVE = "unexplained-valuation-move"
#: Change detectors against the previous accepted run: review flags, never refusals.
REVIEWS = (REVIEW_UNIVERSE_MAX, REVIEW_VALUATION_MOVE)

#: The exclusion reason a refused issuer's strategy decision carries, one per issuer rule.
EXCLUSION_REASON: Mapping[str, ExclusionReason] = {
    RULE_INDEPENDENT_REFERENCE: ExclusionReason.PLAUSIBILITY_REFUSED_INDEPENDENT_REFERENCE,
    RULE_NODE_SIGN_POLICY: ExclusionReason.PLAUSIBILITY_REFUSED_NODE_SIGN_POLICY,
}

#: The published table the gate reads (`plausibility_gate._ROWS_SQL`); `Row.operating_efficiency`
#: is its `operating_efficiency` column, and the forest says which node that column carries.
JUDGED_TABLE = "mart.topt_core_results"
JUDGED_COLUMN = "operating_efficiency"


@dataclass(frozen=True)
class Thresholds:
    """Recorded on #544 and #1443; bump `POLICY_VERSION` with any change."""

    #: Change detectors (review flags since v4). Values unchanged since v1.
    universe_max_factor: Decimal = Decimal("1.5")
    valuation_move_ratio: Decimal = Decimal("2")
    #: A price move of at least this ratio "explains" a valuation move of
    #: `valuation_move_ratio` — the square root of 2, i.e. half the move in log terms.
    price_move_floor: Decimal = Decimal("1.4")
    #: v4: the largest ratio between a served figure and its independent reference. Revenue that is
    #: off by this ratio moves price-to-sales by the same ratio, so it is the valuation move ratio.
    #: Measured on production 2026-09-23, 2026-10-08 and 2026-10-19 (TOPT and QQQ, 408 rows): the
    #: largest same-period revenue ratio was 1.42; MU's superseded FY2025 revenue was 3.56.
    independent_disagreement_ratio: Decimal = Decimal("2")
    #: v4 (#1323): more than this share of a run's rows refused is a data fault, and the run is refused.
    refused_share_limit: Decimal = Decimal("0.25")


THRESHOLDS = Thresholds()


@dataclass(frozen=True)
class IndependentReference:
    """What a source independent of the served figures reports for one listing (#1443).

    Provenance-neutral: an opaque reference id and figures by period end, never the vendor
    (init.md, factor inputs). The caller passes only a reference in the served figures' currency.
    """

    reference_id: str
    #: period end -> figure name (`revenue`, `gross_profit`) -> value; None where not reported.
    figures: Mapping[date, Mapping[str, Decimal | None]]


@dataclass(frozen=True)
class Row:
    """One issuer's published core result — the columns the policy judges."""

    listing_id: str
    operating_branch: str
    availability: str
    operating_efficiency: Decimal | None
    current_ps: Decimal | None
    #: The price behind `current_ps` at this cutoff; None when the run carries no price
    #: for the issuer (then the valuation-move review cannot judge it and says nothing).
    last_close: Decimal | None = None
    #: The period end of the revenue behind `current_ps` (#1236).
    revenue_period_end: date | None = None
    #: v4: the served figures behind the row, and the independent reference that judges them.
    revenue: Decimal | None = None
    gross_profit: Decimal | None = None
    operating_period_end: date | None = None
    #: True when the served gross profit is revenue under the #533 proxy: another quantity than
    #: the gross profit a reference reports, so the two are not compared.
    gross_profit_is_revenue_proxy: bool = False
    #: Every independent reference for the listing at this cutoff; empty when none reported.
    references: tuple[IndependentReference, ...] = field(default=(), compare=False)


@dataclass(frozen=True)
class Violation:
    rule: str
    listing_id: str | None
    detail: str


def policy_sha256(thresholds: Thresholds = THRESHOLDS) -> str:
    """The identity of this policy: version, rules, reviews and thresholds (#1323 criterion 4)."""
    return canonical_sha256(
        {
            "policy_version": POLICY_VERSION,
            "rules": list(RULES),
            "reviews": list(REVIEWS),
            "thresholds": {name: str(value) for name, value in asdict(thresholds).items()},
        }
    )


def _available(rows: list[Row]) -> list[Row]:
    return [row for row in rows if row.availability == "available"]


def _ratio(now: Decimal, before: Decimal) -> Decimal | None:
    if now <= 0 or before <= 0:
        return None
    return now / before if now >= before else before / now


def _served_figures(row: Row) -> list[tuple[str, Decimal, date]]:
    """(name, served value, period end) for every served figure a reference can judge."""
    figures: list[tuple[str, Decimal, date]] = []
    if row.revenue is not None and row.revenue_period_end is not None:
        figures.append(("revenue", row.revenue, row.revenue_period_end))
    gross_profit_is_gross_profit = row.operating_branch == IssuerClass.NON_FINANCIAL.value
    if (
        gross_profit_is_gross_profit
        and not row.gross_profit_is_revenue_proxy
        and row.gross_profit is not None
        and row.operating_period_end is not None
    ):
        figures.append(("gross_profit", row.gross_profit, row.operating_period_end))
    return figures


def _latest_later_figure(reference: IndependentReference, name: str, after: date) -> tuple[date, Decimal] | None:
    later = [(end, values[name]) for end, values in reference.figures.items() if end > after and values.get(name)]
    if not later:
        return None
    end, value = max(later, key=lambda item: item[0])
    assert value is not None
    return end, value


def independent_reference_disagreement(current: list[Row], thresholds: Thresholds = THRESHOLDS) -> list[Violation]:
    """A served revenue or gross profit may not differ by more than `independent_disagreement_ratio`
    from any independent reference: from the reference's figure for the same period, nor from its
    figure for the latest later period (#1443: MU's FY2025 revenue agreed with the reference's
    FY2025 and was 3.56x off its FY2026). Every row is judged, an unavailable one too: its served
    figures still reach the strategy decision. A row without a reference, or a figure the reference
    does not report, is not judged: a missing measurement is not evidence either way."""
    limit = thresholds.independent_disagreement_ratio
    violations: list[Violation] = []
    for row, reference in ((row, reference) for row in current for reference in row.references):
        for name, served, period in _served_figures(row):
            same = reference.figures.get(period, {}).get(name)
            if same is not None:
                ratio = _ratio(served, same)
                if ratio is not None and ratio > limit:
                    violations.append(
                        Violation(
                            RULE_INDEPENDENT_REFERENCE,
                            row.listing_id,
                            f"{name} {served:,.0f} for the period ending {period} is {ratio:.2f}x the independent "
                            f"reference {same:,.0f} for the same period ({reference.reference_id})",
                        )
                    )
                    continue
            # Agreement on the served period says the figure is right for that period, not that the
            # period is current: a later period the reference reports supersedes it.
            later = _latest_later_figure(reference, name, period)
            if later is None:
                continue
            end, value = later
            ratio = _ratio(served, value)
            if ratio is not None and ratio > limit:
                violations.append(
                    Violation(
                        RULE_INDEPENDENT_REFERENCE,
                        row.listing_id,
                        f"{name} {served:,.0f} is for the period ending {period}; the independent reference reports "
                        f"{value:,.0f} for the later period ending {end} ({ratio:.2f}x), so the served figure is "
                        f"superseded ({reference.reference_id})",
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


def evaluate_issuers(current: list[Row], thresholds: Thresholds = THRESHOLDS) -> list[Violation]:
    """Every issuer refusal of a run. Each names one listing; none reads the previous run."""
    return [*independent_reference_disagreement(current, thresholds), *node_sign_policy(current)]


def refused_share_of_run(
    current: list[Row], refused_listings: set[str], thresholds: Thresholds = THRESHOLDS
) -> list[Violation]:
    """More than `refused_share_limit` of a run's rows refused is a data fault, not a filing or a
    split: the run is refused as a whole (#1323: 6 of 20 refuses the run, 5 of 20 does not)."""
    if not current:
        return []
    share = Decimal(len(refused_listings)) / Decimal(len(current))
    if share <= thresholds.refused_share_limit:
        return []
    return [
        Violation(
            RULE_REFUSED_SHARE,
            None,
            f"{len(refused_listings)} of {len(current)} rows refused ({share:.0%}), more than the limit "
            f"{thresholds.refused_share_limit:.0%}: a data fault, not one issuer's filing",
        )
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


def universe_max_regression(
    current: list[Row], previous: list[Row], thresholds: Thresholds = THRESHOLDS
) -> list[Violation]:
    """Review flag: the top operating metric exceeds the previous accepted run's universe
    maximum by more than `universe_max_factor` (#533: XOM at 4,984,153 above NVIDIA)."""
    prior = [row.operating_efficiency for row in _available(previous) if row.operating_efficiency is not None]
    if not prior:
        return []
    ceiling = max(prior) * thresholds.universe_max_factor
    return [
        Violation(
            REVIEW_UNIVERSE_MAX,
            row.listing_id,
            f"operating metric {row.operating_efficiency:,.0f} exceeds {thresholds.universe_max_factor}x the previous "
            f"accepted run's universe maximum {max(prior):,.0f}",
        )
        for row in _available(current)
        if row.operating_efficiency is not None and row.operating_efficiency > ceiling
    ]


def unexplained_valuation_move(
    current: list[Row], previous: list[Row], thresholds: Thresholds = THRESHOLDS
) -> list[Violation]:
    """Review flag: an issuer's price-to-sales moved more than `valuation_move_ratio` run over
    run while its price moved less than `price_move_floor` and its revenue period end did not
    advance (#533 MA 15.55 → 2.17 on a flat price; #1236 MU's FY2026 10-K). Without a price on
    both sides it cannot judge and stays silent."""
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
        if _new_fiscal_year(row, prior):
            continue
        violations.append(
            Violation(
                REVIEW_VALUATION_MOVE,
                row.listing_id,
                f"price-to-sales moved {move:.2f}x versus the previous run ({prior.current_ps} → {row.current_ps}) "
                f"while the price moved {(price_move or Decimal(1)):.2f}x ({prior.last_close} → {row.last_close}) "
                f"and the revenue period end did not advance ({prior.revenue_period_end} → {row.revenue_period_end})",
            )
        )
    return violations


def _new_fiscal_year(row: Row, prior: Row) -> bool:
    """True when the row's revenue period end is later than the previous row's."""
    if row.revenue_period_end is None or prior.revenue_period_end is None:
        return False
    return row.revenue_period_end > prior.revenue_period_end


def reviews(current: list[Row], previous: list[Row], thresholds: Thresholds = THRESHOLDS) -> list[Violation]:
    """Change detectors against the previous accepted run. A review flag asks a person to look;
    it never refuses an issuer or a run, because the previous run is not an independent reference."""
    return [
        *universe_max_regression(current, previous, thresholds),
        *unexplained_valuation_move(current, previous, thresholds),
    ]


def evaluate(
    current: list[Row],
    *,
    outcomes: dict[str, int] | None = None,
    l2_complete: int | None = None,
    thresholds: Thresholds = THRESHOLDS,
) -> list[Violation]:
    """Every refusal of a run: the issuer refusals, then the run refusals. No refusal reads the
    previous accepted run (see `reviews`)."""
    issuers = evaluate_issuers(current, thresholds)
    refused = {violation.listing_id for violation in issuers if violation.listing_id is not None}
    return [
        *issuers,
        *refused_share_of_run(current, refused, thresholds),
        *empty_eligible_set(outcomes, l2_complete),
    ]
