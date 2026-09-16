"""The datahub CONFIDENCE & ACCURACY report for a governed head (owner standard, 2026-09-15).

The standard, verbatim: "HIGH confidence = multiple sources agree, MEDIUM = multiple sources
present, LOW = covered by one source". This module grades every (metric family, subject)
cell of the run the governed pointer heads into one of four bands and persists the result
as one append-only, content-addressed `mart.datahub_confidence_report` row:

* `high`     — at least two INDEPENDENT origins asserted a value and the accepted fusion
               engine (`reconcile_source_assertions`, #343) graded them `agreed` under the
               family's declared, content-addressed tolerance policy;
* `medium`   — at least two origins asserted a value but they are not two sources: no
               agreement policy exists for the family, or they disagree beyond tolerance,
               or they share one lineage (two readers of one 10-K are one source);
* `low`      — exactly one origin asserted a value;
* `missing`  — no origin asserted a value for the cell.

Independence is a property of LINEAGE, not of origin id: a mirror, a reseller, or a second
parser of the same document never corroborates its original
(docs/datahub-quality-report.md, reconciliation step 3). The bands are computed from what
each origin actually asserted; the stored `confidence` column on
`staging.capture_normalized_observations` is a per-semantic constant and takes no part in
them — the report says so in its metadata, from a measurement rather than a remark.

Every field of the session's bar is its own family (#865): open, high, low and close under
the deployed market-price policy (`quality_report.RECONCILIATION_POLICY`), volume under its
own (`VOLUME_RECONCILIATION_POLICY`), each read from the payload key the origin wrote it
under — so a field's HIGH is exactly what the quality report's `field_reconciliation` calls
agreed (#850), and the close's HIGH is what the pointer gate calls corroborated. An origin
whose payload lacks a field (Twelve Data v2 carried the close alone) asserts nothing for
that family: it is present with no value, never a conflict. Index membership gets the
policy it never had (`INDEX_MEMBERSHIP_POLICY`): the index operator's constituent list
against the fund's own N-PORT holdings, membership matched exactly, weights compared at a
stated tolerance whenever both routes carry one. The fundamentals the quality report fuses
(revenue, gross profit, net income, total assets; #854) reconcile here under the same
`FINANCIAL_FACT_RECONCILIATION_POLICY` (#866): the primary's figure at its fiscal period
end against a second origin's figure at that same period, in the primary's currency —
agreed is HIGH, beyond tolerance MEDIUM, a second origin that never published the
primary's period LOW (`second_origin_other_period`, the financial analogue of #622).
Headcount, pre-provision profit and shares outstanding have no policy and grade `medium`
at most with two origins.

The ACCURACY section is the half a self-consistent warehouse cannot supply: for every bar
field and every fused fundamental, the agreement the engine already computed,
cross-checked field by field against the persisted quality report; for revenue and gross
profit, `quality.vendor_oracle`'s deliberately independent SEC re-derivation over a
sample of issuers, fetched live at report time through the source gateway.
"""

from __future__ import annotations

import re
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Protocol

from psycopg import Connection
from psycopg.types.json import Jsonb
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.reconciliation import (
    ReconciliationCell,
    ReconciliationOutcome,
    ReconciliationPolicy,
    SourceAssertion,
    reconcile_source_assertions,
)
from truealpha_contracts.universe import SubjectKind, SubjectRef

from data_engine.datahub.production_topt.parser_identity import PARSER_VERSION_HISTORY
from data_engine.datahub.production_topt.source_registrations import (
    RELEASE_SEMANTICS,
    SOURCE_BY_PARSER,
    registered_semantic_types,
    registration_for,
)
from data_engine.datahub.production_topt.universe_plane import UNIVERSE_SOURCES, UniverseSource
from data_engine.datahub.quality_report import (
    FIELD_RECONCILIATION_POLICIES,
    FIELD_UNITS,
    FINANCIAL_FACT_FUSION_FIELDS,
    FINANCIAL_FACT_RECONCILIATION_POLICY,
    PRICE_BAR_FIELDS,
    corroborating_financial_value,
    financial_fact_unit,
    primary_financial_fields,
    utc_day,
)
from data_engine.datahub.question_coverage import UNIVERSE_PREFIXES, GovernedHead, governed_head
from data_engine.quality import vendor_oracle

REPORT_VERSION = "datahub-confidence-report:v1"
REPORT_ID_PREFIX = "datahub-confidence-report"

MARKET_PRICE_SEMANTIC = "market-price"
#: The served field of the bar. Every other bar field (`quality_report.PRICE_BAR_FIELDS`) is
#: its own family under its own name, because one family cannot honestly be HIGH for the
#: close and unknown for the volume (#865).
CLOSE_FAMILY = "close"
INDEX_MEMBERSHIP_FAMILY = "index_membership"
#: The fund weight is its own family: membership can be corroborated by two routes today
#: while the weight has one (the operator route carries none yet), and one family
#: cannot honestly be HIGH and LOW at once.
ETF_WEIGHT_FAMILY = "etf_weight"
#: The membership plane is not a capture semantic (no registration owns it — it is
#: published by the weekly universe refresh, #539/#63), so it carries its own name here.
INDEX_MEMBERSHIP_SEMANTIC = "index-membership"
MEMBER = "member"

#: Every payload key of the financial-fact semantic that carries a measured figure the
#: factor or the strategy consumes (`materialization.FinancialFactPayload`). Each is its own
#: family because each can gain a second origin on its own schedule.
FINANCIAL_FIELDS: tuple[str, ...] = (
    "revenue",
    "gross_profit",
    "pre_provision_profit",
    "total_assets",
    "shares_outstanding",
    "net_income",
    "headcount",
)
#: The families whose cells are cross-checked against the persisted quality report's grade
#: of the same field: every bar field (`reconciliation_cells[*].fields`, #850) and every
#: fused fundamental (`financial_fact_reconciliation_cells[*].fields`, #854).
CROSS_CHECKED_FAMILIES: tuple[str, ...] = (*PRICE_BAR_FIELDS, *FINANCIAL_FACT_FUSION_FIELDS)

SEC_COMPANY_FACTS_ORIGIN = "origin:sec-company-facts:v1"
SEC_COMPANY_FACTS_SOURCE = "sec-company-facts:v1"
#: The headcount plane's producers all read the issuer's 10-K (the extractor's evidence span
#: and the reviewed seed's citation are the same document), so they are one lineage. A
#: producer not listed here — a vendor feed — keeps its own name and counts as independent.
HEADCOUNT_LINEAGE: Mapping[str, str] = {"10k-extraction": "sec-10k", "manual-review": "sec-10k"}
NASDAQ_INDEX_ORIGIN = "origin:nasdaq-index:v1"
NPORT_ORIGIN = "origin:nport:v1"
MEMBERSHIP_LINEAGE: Mapping[str, str] = {NASDAQ_INDEX_ORIGIN: "nasdaq-index", NPORT_ORIGIN: "sec-nport"}

#: Index membership: the operator's constituent list is the pinned primary (it is what the
#: governed universe is published from); the fund's N-PORT holdings are the independent
#: second route. Membership is compared exactly (both routes list the listing or not).
#: The fund WEIGHT is the `etf_weight` family under the same policy: a quarter-end filed
#: weight and an operator weight taken weeks later differ by price drift, a few percent;
#: the tolerance exists to catch a mis-joined line (NVDA's 7.6% landing on a 0.3% name),
#: which is an order of magnitude, not drift. Today the operator route carries no weight
#: at all (`staging.etf_constituent_facts.weight` is NULL by design until #63 fills it),
#: so `etf_weight` is single-origin and grades `low` until it does.
INDEX_MEMBERSHIP_POLICY = ReconciliationPolicy(
    policy_version="index-membership-fusion:v1",
    source_priority=("nasdaq-index:v1", "nport:v1"),
    absolute_tolerance=Decimal("0.05"),
    relative_tolerance=Decimal("0.10"),
    minimum_independent_origin_groups=2,
)

#: Five well-known QQQ names and five TOPT issuers spanning every operating branch (JPM is
#: the depository institution, BRK.B the insurer, V the multi-class share-count case).
DEFAULT_SAMPLE_SUBJECTS: Mapping[str, tuple[str, ...]] = {
    "universe-list:qqq": (
        "listing:xnas:aapl",
        "listing:xnas:msft",
        "listing:xnas:nvda",
        "listing:xnas:amzn",
        "listing:xnas:googl",
    ),
    "topt": ("listing:xnys:jpm", "listing:xnys:brk.b", "listing:xnys:xom", "listing:xnys:v", "listing:xnas:cost"),
}
DEFAULT_ORACLE_ISSUERS = 5

BAND_DEFINITIONS: Mapping[str, str] = {
    "high": "at least two independent origins asserted a value and the family's declared policy graded them agreed",
    "medium": "at least two origins asserted a value but no policy exists, or they disagree beyond tolerance, or they share one lineage",
    "low": "exactly one origin asserted a value",
    "missing": "no origin asserted a value",
}
INDEPENDENCE_RULE = (
    "two origins are independent when their lineages differ; a mirror, reseller or second "
    "parser of one document is the same source and grades medium at most"
)

_CIK_ID = re.compile(r"^issuer:cik:(\d+)$")
_RATIO_PLACES = Decimal("0.0001")
_DELTA_PLACES = Decimal("0.000001")


class Band(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    MISSING = "missing"


@dataclass(frozen=True)
class OriginValue:
    """What one origin asserted for one cell. `value` is a base-10 string (never a float);
    None means the origin was present but carried no value for the cell."""

    origin_id: str
    source_id: str
    lineage: str
    value: str | None
    knowable_at: datetime | None = None
    observation_id: str | None = None
    #: The fiscal period end the value describes, for a period-bound family: the primary's
    #: own, or the period a corroborating figure was read at (#866).
    period_end: date | None = None


@dataclass(frozen=True)
class FamilyPolicy:
    """One metric family: the registered semantic it reads, the payload keys carrying its
    value, and the reconciliation policy (if any) that can grade two origins `agreed`."""

    family: str
    semantic_type: str
    value_keys: tuple[str, ...]
    reconciliation: ReconciliationPolicy | None
    #: `numeric` compares Decimal values under the policy's tolerance; `membership` compares
    #: presence exactly and falls through to a numeric weight comparison when both routes
    #: carry a weight.
    comparison: str = "numeric"
    #: A session close is comparable only across assertions of the same trading day (#622).
    session_bound: bool = False
    #: A financial fact is comparable only across assertions of the same fiscal period end:
    #: the second origin's figure is read at the primary's period (#866), and one it
    #: published for other periods only is excluded, never a conflict.
    period_bound: bool = False
    #: The unit the family's cell is declared in — part of the content-addressed cell
    #: identity, so it must be the family's own (a fund weight is a percent of net assets,
    #: never a dollar figure).
    unit: str = "USD"


@dataclass(frozen=True)
class CellGrade:
    family: str
    subject_id: str
    band: Band
    reason: str
    origins: tuple[str, ...]
    independent_origins: int
    outcome: str | None = None
    delta: str | None = None
    relative_delta: str | None = None
    tolerance: str | None = None
    comparison: str | None = None
    excluded: tuple[str, ...] = ()
    values: Mapping[str, str | None] = field(default_factory=dict)

    def payload(self) -> dict[str, Any]:
        return {
            "band": self.band.value,
            "reason": self.reason,
            "origins": list(self.origins),
            "independent_origins": self.independent_origins,
            "outcome": self.outcome,
            "delta": self.delta,
            "relative_delta": self.relative_delta,
            "comparison": self.comparison,
            "excluded": list(self.excluded),
        }


def _close_origins() -> dict[str, tuple[str, str, str]]:
    """origin_id -> (origin_source, origin_id, value_key), the CURRENT coordinate of each
    market-price origin (a historical vintage of the same origin never overrides it)."""
    # Only origins registered under the market-price semantic: the parser map also
    # carries the financial-fact corroborator (moomoo statements), which asserts no close.
    market = {origin.origin_id for origin in registration_for(MARKET_PRICE_SEMANTIC).origins}
    out: dict[str, tuple[str, str, str]] = {}
    for coordinate in SOURCE_BY_PARSER.values():
        if coordinate[1] in market:
            out.setdefault(coordinate[1], coordinate)
    return out


CLOSE_ORIGINS: Mapping[str, tuple[str, str, str]] = _close_origins()


#: One vendor behind two origins: moomoo's K-line and its statements are the same source
#: (independent of Yahoo/Twelve Data and of SEC respectively), so the lineage — and the
#: `sources_connected` roll-up — names the vendor, not the endpoint.
VENDOR_LINEAGE: Mapping[str, str] = {"origin:moomoo-kline:v1": "moomoo", "origin:moomoo-financials:v1": "moomoo"}


def lineage_of(origin_id: str) -> str:
    """The canonical original source behind an origin id: `origin:<lineage>:<version>`."""
    if origin_id in MEMBERSHIP_LINEAGE:
        return MEMBERSHIP_LINEAGE[origin_id]
    if origin_id in VENDOR_LINEAGE:
        return VENDOR_LINEAGE[origin_id]
    parts = origin_id.split(":")
    return parts[1] if len(parts) >= 3 and parts[0] == "origin" else origin_id


def _market_price_families() -> tuple[FamilyPolicy, ...]:
    """One family per field of the session's bar, in the bar's order. The close is read
    under the registered origins' value keys (the v1 second origin wrote `price`); every
    other field under its own name, which is how every bar-carrying vintage writes it
    (`market_price_adapter.bar_payload`). Each field reuses the quality report's policy
    and unit for that field, so a HIGH here is exactly what `field_reconciliation[<field>]`
    calls agreed; all are session-bound like the close (#622)."""
    registration = registration_for(MARKET_PRICE_SEMANTIC)
    close_keys = tuple(dict.fromkeys(origin.value_key for origin in registration.origins))
    return tuple(
        FamilyPolicy(
            family=name,
            semantic_type=MARKET_PRICE_SEMANTIC,
            value_keys=close_keys if name == CLOSE_FAMILY else (name,),
            reconciliation=FIELD_RECONCILIATION_POLICIES[name],
            session_bound=registration.session_bound,
            unit=FIELD_UNITS[name],
        )
        for name in PRICE_BAR_FIELDS
    )


FAMILIES: tuple[FamilyPolicy, ...] = (
    *_market_price_families(),
    # The fields the quality report fuses reconcile under its policy, aligned on the primary's
    # fiscal period (#866); the others (headcount, pre-provision profit, shares outstanding)
    # have no policy and stay MEDIUM at most with two origins.
    *(
        FamilyPolicy(
            family=name,
            semantic_type="financial-fact",
            value_keys=(name,),
            reconciliation=FINANCIAL_FACT_RECONCILIATION_POLICY if name in FINANCIAL_FACT_FUSION_FIELDS else None,
            period_bound=name in FINANCIAL_FACT_FUSION_FIELDS,
        )
        for name in FINANCIAL_FIELDS
    ),
    FamilyPolicy(
        family=INDEX_MEMBERSHIP_FAMILY,
        semantic_type=INDEX_MEMBERSHIP_SEMANTIC,
        value_keys=(MEMBER,),
        reconciliation=INDEX_MEMBERSHIP_POLICY,
        comparison="membership",
        unit="membership",
    ),
    FamilyPolicy(
        family=ETF_WEIGHT_FAMILY,
        semantic_type=INDEX_MEMBERSHIP_SEMANTIC,
        value_keys=("weight",),
        reconciliation=INDEX_MEMBERSHIP_POLICY,
        # N-PORT `pctVal`: the holding as a percent of the fund's net assets.
        unit="percent_of_net_assets",
    ),
)
PLANE_FAMILIES: tuple[str, ...] = (INDEX_MEMBERSHIP_FAMILY, ETF_WEIGHT_FAMILY)
_FAMILY_BY_NAME = {policy.family: policy for policy in FAMILIES}
# Every registered value-bearing semantic must be graded by some family (identity
# semantics carry scope, not a measurement). Refused at import, not discovered in a report.
_ungraded = set(registered_semantic_types()) - RELEASE_SEMANTICS - {policy.semantic_type for policy in FAMILIES}
if _ungraded:
    raise ValueError(f"registered semantics with no confidence family: {sorted(_ungraded)}")


def family_policy(name: str) -> FamilyPolicy:
    try:
        return _FAMILY_BY_NAME[name]
    except KeyError as error:
        raise LookupError(f"no confidence family named {name!r}; families: {sorted(_FAMILY_BY_NAME)}") from error


def _decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except InvalidOperation:
        return None
    return parsed if parsed.is_finite() else None


def _ratio(numerator: int, denominator: int) -> str | None:
    if denominator == 0:
        return None
    return str((Decimal(numerator) / Decimal(denominator)).quantize(_RATIO_PLACES))


def _synthetic_observation_id(family: str, subject_id: str, origin: OriginValue) -> str:
    """Membership rows carry no normalized observation; the assertion id is content-addressed
    from the row's own facts so the engine's identity checks still hold."""
    return "normalized-observation:" + canonical_sha256(
        {"family": family, "subject": subject_id, "origin": origin.origin_id, "value": origin.value}
    )


def _served_day(policy: FamilyPolicy, origins: Sequence[OriginValue]) -> tuple[list[OriginValue], list[OriginValue]]:
    """Narrow a session-bound family to the served bar's day (#622): the primary's newest
    day on which it asserted a value, else the newest day any origin asserted a value. A
    dated row without a value (Yahoo's overnight null-close window) is not an assertion
    and never moves the anchor away from the day the origins priced. Returns
    (kept, excluded)."""
    if not policy.session_bound or policy.reconciliation is None:
        return list(origins), []
    dated = [origin for origin in origins if origin.knowable_at is not None]
    if not dated:
        return list(origins), []
    valued = [origin for origin in dated if origin.value is not None] or dated
    primary = policy.reconciliation.source_priority[0]
    primary_days = [origin.knowable_at for origin in valued if origin.source_id == primary and origin.knowable_at]
    anchor = max(primary_days) if primary_days else max(origin.knowable_at for origin in valued if origin.knowable_at)
    # The instants' UTC days, never the session zone's (#885, `utc_day`).
    kept = [
        origin for origin in origins if origin.knowable_at is None or utc_day(origin.knowable_at) == utc_day(anchor)
    ]
    excluded = [origin for origin in origins if origin not in kept]
    return kept, excluded


def _served_period(policy: FamilyPolicy, origins: Sequence[OriginValue]) -> tuple[list[OriginValue], list[OriginValue]]:
    """Narrow a period-bound family to the primary's fiscal period (#866): the second
    origin's figure is comparable only at the period end the primary's figure describes.
    An origin that published the field for other periods only has not corroborated the
    served figure — it is excluded, never a conflict (the quality report's alignment,
    `reconcile_financial_fact_entries`). Anchor: the primary's newest asserted period,
    else the newest period any origin asserted; an undated assertion is never excluded.
    Returns (kept, excluded)."""
    if not policy.period_bound or policy.reconciliation is None:
        return list(origins), []
    dated = [origin for origin in origins if origin.period_end is not None and origin.value is not None]
    if not dated:
        return list(origins), []
    primary = policy.reconciliation.source_priority[0]
    primary_periods = [origin.period_end for origin in dated if origin.source_id == primary and origin.period_end]
    anchor = (
        max(primary_periods) if primary_periods else max(origin.period_end for origin in dated if origin.period_end)
    )
    kept = [origin for origin in origins if origin.period_end is None or origin.period_end == anchor]
    excluded = [origin for origin in origins if origin not in kept]
    return kept, excluded


def _numeric_for_comparison(
    policy: FamilyPolicy, origins: Sequence[OriginValue]
) -> tuple[str, dict[str, Decimal | None]]:
    """Which comparison the engine runs, and the numeric value per origin under it: presence
    is compared by exact value, everything else as a Decimal under the policy's tolerance."""
    if policy.comparison == "membership":
        return "membership", {origin.origin_id: None for origin in origins}
    return "numeric", {origin.origin_id: _decimal(origin.value) for origin in origins}


def _deltas(
    policy: FamilyPolicy, numeric: Mapping[str, Decimal | None], origins: Sequence[OriginValue]
) -> tuple[str | None, str | None]:
    """Largest absolute and relative distance from the anchor (the highest-priority origin)."""
    values = {origin_id: value for origin_id, value in numeric.items() if value is not None}
    if len(values) < 2:
        return None, None
    priority = policy.reconciliation.source_priority if policy.reconciliation else ()
    rank = {
        origin.origin_id: priority.index(origin.source_id) if origin.source_id in priority else len(priority)
        for origin in origins
    }
    anchor_id = min(values, key=lambda origin_id: (rank.get(origin_id, len(priority)), origin_id))
    anchor = values[anchor_id]
    worst = max(
        (abs(anchor - value) for origin_id, value in values.items() if origin_id != anchor_id), default=Decimal(0)
    )
    scale = max(abs(value) for value in values.values())
    relative = (worst / scale).quantize(_DELTA_PLACES) if scale else Decimal(0)
    # An integral distance is rendered as the integer (3.00 -> 3), never in the exponent
    # form `normalize()` gives a volume-sized figure (13686700 -> 1.36867E+7).
    integral = worst.to_integral_value()
    return str(integral if worst == integral else worst), str(relative)


def classify_cell(policy: FamilyPolicy, subject_id: str, origins: Sequence[OriginValue], cutoff: datetime) -> CellGrade:
    """Grade one cell from what its origins asserted. Pure: no clock, no database.

    An origin asserts once (#885 item 9): two rows of one origin — a retried capture and
    a reused binding — are one origin, never `same_lineage` with itself. Of the rows the
    served day and period kept, each origin asserts from its newest valued one
    (`_newest_per_origin`), the representative the fusion engine itself would pick, so
    the grade still matches the quality report's cell by cell. An origin that asserted
    on the served day is not also "another day's" second origin.
    """
    kept, other_day = _served_day(policy, origins)
    kept, other_period = _served_period(policy, kept)
    valued = _newest_per_origin([origin for origin in kept if origin.value is not None])
    asserting = {origin.origin_id for origin in valued}
    values = {origin.origin_id: origin.value for origin in _newest_per_origin(origins)}
    values.update({origin.origin_id: origin.value for origin in valued})
    other_day_ids = tuple(sorted({origin.origin_id for origin in other_day if origin.value is not None} - asserting))
    other_period_ids = tuple(
        sorted({origin.origin_id for origin in other_period if origin.value is not None} - asserting)
    )
    excluded_ids = tuple(sorted(other_day_ids + other_period_ids))
    tolerance = policy.reconciliation.policy_id if policy.reconciliation else None

    def grade(
        band: Band,
        reason: str,
        origins_asserting: tuple[str, ...],
        independent: int,
        *,
        outcome: str | None = None,
        delta: str | None = None,
        relative: str | None = None,
        comparison: str | None = None,
    ) -> CellGrade:
        return CellGrade(
            family=policy.family,
            subject_id=subject_id,
            band=band,
            reason=reason,
            origins=origins_asserting,
            independent_origins=independent,
            outcome=outcome,
            delta=delta,
            relative_delta=relative,
            tolerance=tolerance,
            comparison=comparison,
            excluded=excluded_ids,
            values=values,
        )

    if not valued:
        return grade(Band.MISSING, "no_origin_value", (), 0)
    asserted = tuple(sorted(origin.origin_id for origin in valued))
    independent = len({origin.lineage for origin in valued})
    if len(valued) == 1:
        if other_day_ids:
            reason = "second_origin_other_day"
        elif other_period_ids:
            reason = "second_origin_other_period"
        else:
            reason = "single_origin"
        return grade(Band.LOW, reason, asserted, 1)
    comparison, numeric = _numeric_for_comparison(policy, valued)
    delta, relative = _deltas(policy, numeric, valued)
    if independent < 2:
        return grade(
            Band.MEDIUM, "same_lineage", asserted, independent, delta=delta, relative=relative, comparison=comparison
        )
    if policy.reconciliation is None:
        return grade(
            Band.MEDIUM,
            "no_agreement_policy",
            asserted,
            independent,
            delta=delta,
            relative=relative,
            comparison=comparison,
        )
    outcome = _reconcile(policy, subject_id, valued, numeric, cutoff)
    graded = {
        ReconciliationOutcome.AGREED: (Band.HIGH, "independent_origins_agree"),
        ReconciliationOutcome.CONFLICT_ABSTAINED: (Band.MEDIUM, "not_agreed_within_tolerance"),
        ReconciliationOutcome.INSUFFICIENT_INDEPENDENT_ORIGINS: (Band.LOW, "single_eligible_origin"),
        ReconciliationOutcome.NOT_YET_KNOWABLE: (Band.MISSING, "not_knowable_at_cutoff"),
        ReconciliationOutcome.UNAVAILABLE: (Band.MISSING, "no_eligible_origin"),
    }
    band, reason = graded[outcome]
    return grade(
        band,
        reason,
        asserted,
        independent,
        outcome=outcome.value,
        delta=delta,
        relative=relative,
        comparison=comparison,
    )


def _reconcile(
    policy: FamilyPolicy,
    subject_id: str,
    origins: Sequence[OriginValue],
    numeric: Mapping[str, Decimal | None],
    cutoff: datetime,
) -> ReconciliationOutcome:
    """Run the accepted fusion engine over the origins' assertions under the family policy."""
    assert policy.reconciliation is not None
    cell = ReconciliationCell(
        requirement_id=f"data-requirement:{canonical_sha256({'requirement': f'{policy.semantic_type}:v1'})}",
        subject=SubjectRef(kind=SubjectKind.LISTING, id=subject_id),
        field_name=policy.family,
        field_semantics_id=f"field-semantics:{canonical_sha256({'field': f'{policy.semantic_type}-{policy.family}:v1'})}",
        unit=policy.unit,
        valid_from=utc_day(cutoff),
        valid_to=utc_day(cutoff),
    )
    assertions = []
    for origin in origins:
        observation_id = origin.observation_id or _synthetic_observation_id(policy.family, subject_id, origin)
        compared = numeric[origin.origin_id]
        value_sha = canonical_sha256({"value": origin.value if compared is None else str(compared)})
        assertions.append(
            SourceAssertion(
                cell_id=cell.cell_id,
                observation_id=observation_id,
                source_id=origin.source_id,
                origin_group_id=origin.origin_id,
                knowable_at=origin.knowable_at or cutoff,
                normalized_value_sha256=value_sha,
                numeric_value=numeric[origin.origin_id],
                confidence_assessment_id=f"confidence-assessment:{value_sha}",
                # The engine never arbitrates on confidence (rule 12) and the bands never
                # read it; a full score here is a placeholder the result does not carry.
                confidence_score=Decimal("1"),
                lineage_node_ids=(observation_id,),
                lineage_complete=True,
            )
        )
    result = reconcile_source_assertions(
        cell=cell, assertions=tuple(assertions), policy=policy.reconciliation, cutoff=cutoff
    )
    return result.outcome


def aggregate(policy: FamilyPolicy, grades: Iterable[CellGrade]) -> dict[str, Any]:
    """Per-family counts, shares, the agreement rate over COMPARED cells (two independent
    origins under a policy), the tolerance used and the origins involved."""
    rows = list(grades)
    counts = Counter(grade.band.value for grade in rows)
    total = len(rows)
    compared = sum(
        1
        for grade in rows
        if grade.outcome in (ReconciliationOutcome.AGREED.value, ReconciliationOutcome.CONFLICT_ABSTAINED.value)
    )
    agreed = sum(1 for grade in rows if grade.band is Band.HIGH)
    reasons = Counter(grade.reason for grade in rows)
    return {
        "semantic_type": policy.semantic_type,
        "cells": total,
        **{band.value: counts.get(band.value, 0) for band in Band},
        "share": {band.value: _ratio(counts.get(band.value, 0), total) or "0" for band in Band},
        "compared": compared,
        "agreement_rate": _ratio(agreed, compared),
        "tolerance": policy.reconciliation.policy_id if policy.reconciliation else None,
        "tolerance_policy": _policy_payload(policy.reconciliation),
        "origins": sorted({origin for grade in rows for origin in grade.origins}),
        "reasons": dict(sorted(reasons.items())),
    }


def _policy_payload(policy: ReconciliationPolicy | None) -> dict[str, Any] | None:
    if policy is None:
        return None
    return {
        "policy_id": policy.policy_id,
        "policy_version": policy.policy_version,
        "source_priority": list(policy.source_priority),
        "absolute_tolerance": str(policy.absolute_tolerance),
        "relative_tolerance": str(policy.relative_tolerance),
        "minimum_independent_origin_groups": policy.minimum_independent_origin_groups,
    }


#: Stamps of the compile, not of what it graded. They travel in the payload and stay out of
#: the content address, so a re-compile over identical content is the SAME row (#885).
COMPILE_STAMPS: tuple[str, ...] = ("generated_at",)


def content_address(payload: Mapping[str, Any]) -> tuple[str, str]:
    """(report_id, content_sha256) over the canonical payload without its compile stamps.

    The quality report's way, with one deliberate difference: this payload carries the
    time it was compiled (`generated_at`), and hashing that made every nightly compile a
    new row that `on conflict do nothing` could never collapse — the table grew one row
    per night per universe over an unchanged head. The identity is the graded content
    (the migration's promise: a replay over the same head is the same row, a changed
    grade a new one); the stored `generated_at` is the compile that first produced it.
    """
    digest = canonical_sha256({key: value for key, value in payload.items() if key not in COMPILE_STAMPS})
    return f"{REPORT_ID_PREFIX}:{digest}", digest


def stored_confidence_metadata(rows: Iterable[tuple[str, Decimal, int]]) -> dict[str, Any]:
    """What the stored `confidence` column holds in this run, measured — and the statement
    that the bands do not read it."""
    by_semantic: dict[str, dict[str, int]] = {}
    for semantic, value, count in rows:
        by_semantic.setdefault(semantic, {})[str(value)] = int(count)
    return {
        "used_for_bands": False,
        "values_by_semantic": dict(sorted((k, dict(sorted(v.items()))) for k, v in by_semantic.items())),
        "constant_per_semantic": all(len(values) == 1 for values in by_semantic.values()),
        "note": (
            "staging.capture_normalized_observations.confidence is stamped per semantic by the parser, "
            "not computed by the formula in docs/confidence-calibration.md; the bands above are derived "
            "from origin counts, lineage and the reconciliation outcome only"
        ),
    }


# -- accuracy: the SEC oracle --------------------------------------------------------------------

ORACLE_FIELDS: tuple[str, ...] = ("revenue", "gross_profit")


def sec_oracle_section(
    issuers: Sequence[tuple[str, str, Decimal | None, Decimal | None]],
    *,
    cutoff: date,
    ticker_index: Callable[[], Mapping[str, int]] | None = None,
    facts_for: Callable[[int], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Re-derive revenue and gross profit for each (subject, ticker, revenue, gross_profit)
    through `quality.vendor_oracle`'s independent route and report agreement per field.

    Pure given its two fetchers; without them (no SEC user agent configured) it reports
    that nothing was compared rather than comparing against nothing.
    """
    section: dict[str, Any] = {
        "oracle": "quality.vendor_oracle",
        "fields": list(ORACLE_FIELDS),
        "cutoff": cutoff.isoformat(),
        "issuers_requested": len(issuers),
        "issuers_compared": 0,
        "rows": [],
        "skipped": {},
        "per_field": {},
    }
    if ticker_index is None or facts_for is None:
        section["reason"] = "no_sec_user_agent"
        return section
    index = ticker_index()
    facts_cache: dict[int, Mapping[str, Any]] = {}
    per_field: dict[str, dict[str, int]] = {name: {"compared": 0, "agreed": 0} for name in ORACLE_FIELDS}
    for subject_id, ticker, mart_revenue, mart_gross_profit in issuers:
        cik = index.get(ticker.upper().replace(".", "-"))
        if cik is None:
            section["skipped"][subject_id] = "no_cik"
            continue
        if cik not in facts_cache:
            facts_cache[cik] = facts_for(cik)
        facts = dict(facts_cache[cik])
        drifts = {
            "revenue": vendor_oracle.Drift(
                ticker=ticker,
                field="revenue",
                mart_value=mart_revenue,
                vendor=vendor_oracle.latest_across_variants(facts, vendor_oracle._REVENUE_CONCEPTS, "USD", cutoff),
                cutoff=cutoff,
                mart_period_end=vendor_oracle.period_reporting(
                    facts, vendor_oracle._REVENUE_CONCEPTS, mart_revenue, cutoff
                ),
            ),
            "gross_profit": vendor_oracle.Drift(
                ticker=ticker,
                field="gross_profit",
                mart_value=mart_gross_profit,
                vendor=vendor_oracle.gross_profit(facts, cutoff),
                cutoff=cutoff,
                mart_period_end=vendor_oracle.period_reporting(facts, ("GrossProfit",), mart_gross_profit, cutoff),
            ),
        }
        row: dict[str, Any] = {"subject_id": subject_id, "ticker": ticker, "cik": cik}
        for name, drift in drifts.items():
            compared = drift.vendor is not None
            if compared:
                per_field[name]["compared"] += 1
                per_field[name]["agreed"] += int(drift.agrees)
            delta = (
                str(abs(drift.mart_value - drift.vendor.value))
                if drift.vendor is not None and drift.mart_value is not None
                else None
            )
            row[name] = {
                "mart_value": None if drift.mart_value is None else str(drift.mart_value),
                "vendor_value": None if drift.vendor is None else str(drift.vendor.value),
                "vendor_period_end": None if drift.vendor is None else drift.vendor.period_end.isoformat(),
                "vendor_concept": None if drift.vendor is None else drift.vendor.concept,
                "mart_period_end": None if drift.mart_period_end is None else drift.mart_period_end.isoformat(),
                "compared": compared,
                "agrees": drift.agrees if compared else None,
                "delta": delta,
                "staleness_years": drift.staleness_years,
            }
        section["rows"].append(row)
        section["issuers_compared"] += 1
    section["per_field"] = {
        name: {**counts, "agreement_rate": _ratio(counts["agreed"], counts["compared"])}
        for name, counts in per_field.items()
    }
    return section


@dataclass(frozen=True)
class SecOracle:
    """The live SEC fetchers behind the accuracy section, paced as `vendor_oracle` paces."""

    user_agent: str

    def ticker_index(self) -> Mapping[str, int]:
        index = vendor_oracle.ticker_cik_index(self.user_agent)
        time.sleep(vendor_oracle._PACE_SECONDS)
        return index

    def facts_for(self, cik: int) -> Mapping[str, Any]:
        facts = vendor_oracle.company_facts(cik, self.user_agent)
        time.sleep(vendor_oracle._PACE_SECONDS)
        return facts


# -- loaders: the persisted observations of one run --------------------------------------------


@dataclass
class _Subject:
    ticker: str | None = None
    issuer_id: str | None = None
    origins: dict[str, list[OriginValue]] = field(default_factory=dict)
    financial: dict[str, Decimal | None] = field(default_factory=dict)

    def add(self, family: str, origin: OriginValue) -> None:
        self.origins.setdefault(family, []).append(origin)


def _cik_of(issuer_id: str | None) -> int | None:
    match = _CIK_ID.match(issuer_id or "")
    return int(match.group(1)) if match else None


def bar_origins(
    coordinate: tuple[str, str, str],
    payload: Mapping[str, Any],
    *,
    knowable_at: datetime | None = None,
    observation_id: str | None = None,
) -> dict[str, OriginValue]:
    """What one market-price observation asserts, per bar family.

    `coordinate` is the origin's registry entry, (origin_source, origin_id, value_key): the
    close is read under the declared key (the v1 second origin wrote `price`), every other
    field under its own name, which is how every bar-carrying vintage writes it
    (`market_price_adapter.bar_payload`). A payload without a field — Twelve Data v2
    carried the close alone — asserts nothing for that family: the origin is present with
    no value, never a conflict. A payload without a close asserts no bar at all, the way
    the quality report reads it (`_reconcile_market_price_cells` skips the observation),
    so the two reports grade the same assertions field by field.
    """
    origin_source, origin_id, value_key = coordinate
    close = payload.get(value_key)
    origins: dict[str, OriginValue] = {}
    for family in PRICE_BAR_FIELDS:
        value = close if family == CLOSE_FAMILY or close is None else payload.get(family)
        origins[family] = OriginValue(
            origin_id=origin_id,
            source_id=origin_source,
            lineage=lineage_of(origin_id),
            value=None if value is None else str(value),
            knowable_at=knowable_at,
            observation_id=observation_id,
        )
    return origins


@dataclass(frozen=True)
class FinancialObservation:
    """One financial-fact observation of a subject, as the loader read it: the shared
    primary vintage (`primary`) or a registered corroborating origin's."""

    primary: bool
    origin_source: str
    origin_id: str
    payload: Mapping[str, Any]
    knowable_at: datetime | None = None
    observation_id: str | None = None


def _text(value: Any) -> str | None:
    return None if value is None else str(value)


def _newest_period_value(payload: Mapping[str, Any], name: str) -> tuple[str | None, date | None]:
    """A corroborating origin's newest figure for `name` across `by_period_end`, with the
    period it describes — what the origin asserts when it never published the primary's
    period, so the classifier can exclude it as another period rather than compare it."""
    periods = payload.get("by_period_end")
    if not isinstance(periods, Mapping):
        return None, None
    for period in sorted(periods, reverse=True):
        values = periods.get(period)
        if not isinstance(values, Mapping) or values.get(name) is None:
            continue
        try:
            return str(values[name]), date.fromisoformat(str(period))
        except ValueError:
            continue
    return None, None


class _Observed(Protocol):
    """What `_newest_per_origin` reads: a financial observation or a graded origin value."""

    @property
    def origin_id(self) -> str: ...

    @property
    def knowable_at(self) -> datetime | None: ...

    @property
    def observation_id(self) -> str | None: ...


def _recency(observation: _Observed) -> tuple[datetime, str]:
    return observation.knowable_at or datetime.min.replace(tzinfo=UTC), observation.observation_id or ""


def _newest_per_origin[ObservedT: _Observed](observations: Sequence[ObservedT]) -> list[ObservedT]:
    """One observation per origin — the newest by knowable_at, then observation id: the
    quality report's selection of the primary, applied to every origin. An origin then
    asserts once, whichever order its rows arrived in, and never counts as two. Serves
    the financial loader (#875) and every family's grade (#885)."""
    newest: dict[str, ObservedT] = {}
    for observation in observations:
        current = newest.get(observation.origin_id)
        if current is None or _recency(observation) > _recency(current):
            newest[observation.origin_id] = observation
    return [newest[origin_id] for origin_id in sorted(newest)]


def financial_origins(observations: Sequence[FinancialObservation]) -> dict[str, list[OriginValue]]:
    """What each financial-fact origin asserts per family, aligned the way the quality
    report's fusion aligns them (#866, `reconcile_financial_fact_entries`). Each origin
    asserts from its newest observation alone (`_newest_per_origin`), so the grade cannot
    depend on the order the rows were read in.

    The primary asserts its own figure, dated with its own fiscal period end where the
    payload dates it (`primary_financial_fields`). For a fused field, a corroborating
    origin asserts its figure AT THE PRIMARY'S period end (`corroborating_financial_value`)
    in the primary's reporting currency (`financial_fact_unit`): an origin reporting in
    another currency is present with no comparable value; one that published the field
    for other periods only asserts its newest such figure, dated with that period, which
    the classifier excludes (`second_origin_other_period`) rather than compares; a primary
    figure without a dated period cannot be aligned, so nothing corroborates it and the
    cell is single-origin — the quality report compares it no more. A field the primary
    does not assert, or a subject without a primary, has no period to align on: every
    origin asserts its headline figure and the cell is single-origin at most.

    Headcount asserts under its producer (the plane's other producers are added by
    `_add_headcount_producers`); fields no policy fuses are read by name from every origin.
    """
    selected = _newest_per_origin(observations)
    primaries = [observation for observation in selected if observation.primary]
    primary = max(primaries, key=_recency) if primaries else None
    anchors = primary_financial_fields(primary.payload) if primary is not None else {}
    unit = financial_fact_unit(primary.payload) if primary is not None else None
    origins: dict[str, list[OriginValue]] = {}
    for observation in selected:
        payload = observation.payload
        for name in FINANCIAL_FIELDS:
            if name == "headcount":
                if not observation.primary:
                    continue
                vintage = payload.get("vintage") or {}
                producer = str((vintage.get("headcount") or {}).get("source") or "unknown")
                origins.setdefault(name, []).append(
                    OriginValue(
                        origin_id=f"origin:headcount:{producer}",
                        source_id=producer,
                        lineage=HEADCOUNT_LINEAGE.get(producer, producer),
                        value=_text(payload.get(name)),
                        knowable_at=observation.knowable_at,
                        observation_id=observation.observation_id,
                    )
                )
                continue
            value, period = _text(payload.get(name)), None
            if name in FINANCIAL_FACT_FUSION_FIELDS and observation.primary:
                period = anchors[name][1] if name in anchors else None
            elif name in FINANCIAL_FACT_FUSION_FIELDS and primary is not None:
                if financial_fact_unit(payload) != unit:
                    value = None
                elif name in anchors:
                    aligned = corroborating_financial_value(payload, name, anchors[name][1])
                    if aligned is not None:
                        value, period = str(aligned), anchors[name][1]
                    else:
                        value, period = _newest_period_value(payload, name)
                elif primary.payload.get(name) is not None:
                    value = None
            origins.setdefault(name, []).append(
                OriginValue(
                    origin_id=observation.origin_id,
                    source_id=observation.origin_source,
                    lineage=lineage_of(observation.origin_id),
                    value=value,
                    knowable_at=observation.knowable_at,
                    observation_id=observation.observation_id,
                    period_end=period,
                )
            )
    return origins


def load_run_subjects(connection: Connection[Any], run_id: str, cutoff: datetime) -> dict[str, _Subject]:
    """Every subject the run requested, with what each origin asserted per family."""
    subjects: dict[str, _Subject] = {
        str(row[0]): _Subject()
        for row in connection.execute(
            "select distinct subject_id from raw.capture_obligations where run_id = %s order by subject_id", (run_id,)
        ).fetchall()
    }
    rows = connection.execute(
        """
        select ob.subject_id, o.semantic_type, o.parser_version, o.knowable_at, o.observation_id, p.normalized_payload
        from raw.capture_obligations ob
        join staging.capture_observation_obligations oo on oo.capture_obligation_id = ob.obligation_id
        join staging.capture_normalized_observations o on o.observation_id = oo.observation_id
        join staging.capture_observation_payloads p on p.observation_id = o.observation_id
        where ob.run_id = %s
        order by ob.subject_id, o.semantic_type, o.observation_id
        """,
        (run_id,),
    ).fetchall()
    financial: dict[str, list[FinancialObservation]] = {}
    for subject_id, semantic_type, parser_version, knowable_at, observation_id, payload in rows:
        subject = subjects.setdefault(str(subject_id), _Subject())
        payload = payload or {}
        if semantic_type in RELEASE_SEMANTICS:
            subject.ticker = subject.ticker or payload.get("ticker")
            subject.issuer_id = subject.issuer_id or payload.get("issuer_id")
        elif semantic_type == MARKET_PRICE_SEMANTIC:
            coordinate = SOURCE_BY_PARSER.get(parser_version)
            if coordinate is None:
                continue
            asserted = bar_origins(coordinate, payload, knowable_at=knowable_at, observation_id=observation_id)
            for family, origin in asserted.items():
                subject.add(family, origin)
        elif semantic_type == "financial-fact":
            subject.issuer_id = subject.issuer_id or payload.get("issuer_id")
            # The primary's parser vintage is the shared primary identity (as the quality
            # report classifies it); any other vintage is a registered corroborating origin
            # (moomoo statements) and asserts under ITS origin, never as the primary's.
            if parser_version in PARSER_VERSION_HISTORY:
                primary, origin_source, origin_id = True, SEC_COMPANY_FACTS_SOURCE, SEC_COMPANY_FACTS_ORIGIN
            else:
                coordinate = SOURCE_BY_PARSER.get(parser_version)
                if coordinate is None:
                    continue
                primary, origin_source, origin_id = False, coordinate[0], coordinate[1]
            if primary:
                # The served figures (and the factor inputs) are the primary's alone.
                for name in FINANCIAL_FIELDS:
                    subject.financial[name] = _decimal(_text(payload.get(name)))
            # Aligned once the subject's observations are all read: a corroborating origin
            # asserts at the primary's period, whichever row came first.
            financial.setdefault(str(subject_id), []).append(
                FinancialObservation(primary, origin_source, origin_id, payload, knowable_at, observation_id)
            )
    for subject_id, observations in financial.items():
        for family, family_origins in financial_origins(observations).items():
            for origin_value in family_origins:
                subjects[subject_id].add(family, origin_value)
    _add_headcount_producers(connection, subjects, cutoff)
    return subjects


def _add_headcount_producers(connection: Connection[Any], subjects: Mapping[str, _Subject], cutoff: datetime) -> None:
    """Every producer that wrote a headcount knowable at the cutoff for the run's issuers
    (`staging.issuer_headcount_facts`, #70): the fused payload carries one winner, the plane
    holds all of them, and the standard counts sources present."""
    ciks = {
        cik: subject_id for subject_id, subject in subjects.items() if (cik := _cik_of(subject.issuer_id)) is not None
    }
    if not ciks:
        return
    rows = connection.execute(
        """
        select distinct on (cik, source) cik, source, headcount, knowable_at
        from staging.issuer_headcount_facts
        where cik = any(%s) and knowable_at <= %s
        order by cik, source, knowable_at desc, id desc
        """,
        (list(ciks), cutoff),
    ).fetchall()
    for cik, producer, headcount, knowable_at in rows:
        subject = subjects[ciks[int(cik)]]
        present = subject.origins.get("headcount", [])
        if any(origin.source_id == producer for origin in present):
            continue
        subject.add(
            "headcount",
            OriginValue(
                origin_id=f"origin:headcount:{producer}",
                source_id=str(producer),
                lineage=HEADCOUNT_LINEAGE.get(str(producer), str(producer)),
                value=str(headcount),
                knowable_at=knowable_at,
            ),
        )


def _universe_source(universe: str) -> UniverseSource | None:
    return next((source for source in UNIVERSE_SOURCES.values() if source.head_kind == universe), None)


def load_index_membership(
    connection: Connection[Any], source: UniverseSource, cutoff: datetime
) -> tuple[dict[str, dict[str, list[OriginValue]]], dict[str, Any]]:
    """Both membership routes at the cutoff: the operator's newest constituent refresh and
    the fund's newest N-PORT vintage filed by then. Returns (origins by family by listing,
    notes): the membership family asserts presence, the weight family a filed weight."""
    constituents = connection.execute(
        """
        select ticker, weight, knowable_at from staging.etf_constituent_facts
        where etf_symbol = %s and (as_of, knowable_at) = (
            select as_of, max(knowable_at) from staging.etf_constituent_facts
            where etf_symbol = %s and knowable_at <= %s and as_of = (
                select max(as_of) from staging.etf_constituent_facts where etf_symbol = %s and knowable_at <= %s
            )
            group by as_of
        )
        order by ticker
        """,
        (source.etf, source.etf, cutoff, source.etf, cutoff),
    ).fetchall()
    fund = connection.execute(
        """
        select entity_id from staging.kg_identifiers
        where identifier_type = 'ticker' and identifier_value = %s and entity_id like 'etf:series:%%'
        order by transaction_time desc, id desc limit 1
        """,
        (str(source.nport_ticker).upper(),),
    ).fetchone()
    holdings: list[Any] = []
    vintage: dict[str, Any] = {}
    if fund is not None:
        holdings = connection.execute(
            """
            with vintage as (
                select report_period, transaction_time from mart.fund_holdings_resolved
                where fund_id = %s and transaction_time <= %s
                order by transaction_time desc, report_period desc limit 1
            )
            select h.listing_id, h.holding_name, h.isin, h.percent_of_net_assets, h.report_period, h.transaction_time
            from mart.fund_holdings_resolved h
            join vintage using (report_period, transaction_time)
            where h.fund_id = %s
            order by h.listing_id nulls last, h.holding_name
            """,
            (fund[0], cutoff, fund[0]),
        ).fetchall()
    origins: dict[str, dict[str, list[OriginValue]]] = {INDEX_MEMBERSHIP_FAMILY: {}, ETF_WEIGHT_FAMILY: {}}

    def assert_route(listing_id: str, origin_id: str, rank: int, weight: Any, knowable_at: datetime) -> None:
        for family, value in (
            (INDEX_MEMBERSHIP_FAMILY, MEMBER),
            (ETF_WEIGHT_FAMILY, None if weight is None else str(weight)),
        ):
            origins[family].setdefault(listing_id, []).append(
                OriginValue(
                    origin_id=origin_id,
                    source_id=INDEX_MEMBERSHIP_POLICY.source_priority[rank],
                    lineage=lineage_of(origin_id),
                    value=value,
                    knowable_at=knowable_at,
                )
            )

    for ticker, weight, knowable_at in constituents:
        assert_route(f"listing:{source.mic}:{str(ticker).lower()}", NASDAQ_INDEX_ORIGIN, 0, weight, knowable_at)
    unresolved: list[str] = []
    for listing_id, holding_name, _isin, weight, report_period, transaction_time in holdings:
        vintage = {"report_period": report_period.isoformat(), "filed": transaction_time.astimezone(UTC).isoformat()}
        if listing_id is None:
            unresolved.append(str(holding_name))
            continue
        assert_route(str(listing_id), NPORT_ORIGIN, 1, weight, transaction_time)
    notes = {
        "constituents_as_of": None if not constituents else constituents[0][2].astimezone(UTC).isoformat(),
        "constituent_count": len(constituents),
        "constituents_with_weight": sum(1 for _, weight, _ in constituents if weight is not None),
        "fund_id": None if fund is None else str(fund[0]),
        "holdings_vintage": vintage or None,
        "holding_lines": len(holdings),
        "holdings_unresolved_to_listing": sorted(unresolved),
        "denominator": "union of both routes' listings — a line held but not in the index is a real cell",
    }
    return origins, notes


def load_stored_confidence(connection: Connection[Any], run_id: str) -> list[tuple[str, Decimal, int]]:
    rows = connection.execute(
        """
        select o.semantic_type, o.confidence, count(*)
        from raw.capture_obligations ob
        join staging.capture_observation_obligations oo on oo.capture_obligation_id = ob.obligation_id
        join staging.capture_normalized_observations o on o.observation_id = oo.observation_id
        where ob.run_id = %s
        group by o.semantic_type, o.confidence
        order by o.semantic_type, o.confidence
        """,
        (run_id,),
    ).fetchall()
    return [(str(semantic), Decimal(str(confidence)), int(count)) for semantic, confidence, count in rows]


_COMPARED_OUTCOMES = frozenset({ReconciliationOutcome.AGREED.value, ReconciliationOutcome.CONFLICT_ABSTAINED.value})


def quality_report_field_outcomes(payload: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    """family -> listing -> outcome, read from a persisted quality report payload: every bar
    field's grade under `reconciliation_cells[*].fields` (#850) and every fused
    fundamental's under `financial_fact_reconciliation_cells[*].fields` (#854; a field the
    primary left undated is not graded there and is not compared here). A report written
    before the bar was fused per field carries the close's grade alone, under the cell's
    headline keys, and grades no other field."""
    outcomes: dict[str, dict[str, str]] = {name: {} for name in CROSS_CHECKED_FAMILIES}
    for listing, cell in (payload.get("reconciliation_cells") or {}).items():
        fields = cell.get("fields")
        graded = fields if isinstance(fields, Mapping) else {CLOSE_FAMILY: cell}
        for name in PRICE_BAR_FIELDS:
            outcome = (graded.get(name) or {}).get("outcome")
            if outcome is not None:
                outcomes[name][str(listing)] = str(outcome)
    for listing, cell in (payload.get("financial_fact_reconciliation_cells") or {}).items():
        fields = cell.get("fields") or {}
        for name in FINANCIAL_FACT_FUSION_FIELDS:
            outcome = (fields.get(name) or {}).get("outcome")
            if outcome is not None:
                outcomes[name][str(listing)] = str(outcome)
    return outcomes


def load_quality_report_field_outcomes(
    connection: Connection[Any], run_id: str
) -> tuple[str | None, dict[str, dict[str, str]]]:
    """The persisted quality report for this run and its per-listing outcome per bar field
    and fused fundamental, so the report can prove it grades each field the same day, at
    the same period, and the same way the pointer gate's report graded it."""
    # Only the cells the cross-check reads, not the whole payload (Copilot on #872).
    row = connection.execute(
        """
        select report_id,
               jsonb_build_object(
                   'reconciliation_cells', payload->'reconciliation_cells',
                   'financial_fact_reconciliation_cells', payload->'financial_fact_reconciliation_cells'
               )
        from mart.datahub_quality_report
        where run_id = %s order by created_at desc limit 1
        """,
        (run_id,),
    ).fetchone()
    if row is None:
        return None, {}
    return str(row[0]), quality_report_field_outcomes(row[1] or {})


def _disagrees(graded: str | None, persisted: str) -> bool:
    # A cell this report never compared (one origin on the served day) disagrees with a
    # persisted comparison: the quality report saw two origins this report did not.
    if graded is None:
        return persisted in _COMPARED_OUTCOMES
    return graded != persisted


def field_accuracy(
    policy: FamilyPolicy,
    summary: Mapping[str, Any],
    cells: Mapping[str, CellGrade],
    *,
    quality_report_id: str | None,
    persisted: Mapping[str, str],
) -> dict[str, Any]:
    """One family's accuracy entry: the agreement the engine computed, and the cross-check
    of every cell's outcome against the persisted quality report's grade of the same
    field. `matches_quality_report` is None when there is no persisted report or it graded
    no cell of this field (a report from before the bar was fused per field)."""
    mismatches = sorted(
        listing
        for listing, outcome in persisted.items()
        if listing in cells and _disagrees(cells[listing].outcome, outcome)
    )
    return {
        "origins": summary.get("origins", []),
        "compared": summary.get("compared", 0),
        "agreed": summary.get("high", 0),
        "agreement_rate": summary.get("agreement_rate"),
        "tolerance_policy": _policy_payload(policy.reconciliation),
        "quality_report_id": quality_report_id,
        "quality_report_cells": len(persisted),
        "matches_quality_report": None if quality_report_id is None or not persisted else not mismatches,
        "quality_report_mismatches": mismatches,
    }


# -- the report ---------------------------------------------------------------------------------


def _sample_entry(grade: CellGrade) -> dict[str, Any]:
    return {
        "values": {origin: value for origin, value in sorted(grade.values.items())},
        "delta": grade.delta,
        "relative_delta": grade.relative_delta,
        "comparison": grade.comparison,
        "verdict": grade.band.value,
        "reason": grade.reason,
        "excluded": list(grade.excluded),
    }


def build_report(
    connection: Connection[Any],
    *,
    universe: str,
    head: GovernedHead,
    executed_at: datetime,
    environment: str,
    sample_subjects: Sequence[str] | None = None,
    oracle_issuers: int = DEFAULT_ORACLE_ISSUERS,
    oracle: SecOracle | None = None,
) -> dict[str, Any]:
    """Grade the head's cells, sample them, and run the accuracy oracle."""
    cutoff = head.cutoff
    subjects = load_run_subjects(connection, head.run_id, cutoff)
    source = _universe_source(universe)
    membership_notes: dict[str, Any] | None = None
    plane_origins: dict[str, dict[str, list[OriginValue]]] = {}
    if source is not None and source.nport_ticker is not None:
        plane_origins, membership_notes = load_index_membership(connection, source, cutoff)

    grades: dict[str, dict[str, CellGrade]] = {}
    for policy in FAMILIES:
        origins_by_subject: dict[str, list[OriginValue]]
        if policy.family in PLANE_FAMILIES:
            if membership_notes is None:
                continue
            family_origins = plane_origins[policy.family]
            origins_by_subject = {listing: family_origins[listing] for listing in sorted(family_origins)}
        else:
            origins_by_subject = {
                subject_id: subject.origins.get(policy.family, []) for subject_id, subject in sorted(subjects.items())
            }
        grades[policy.family] = {
            subject_id: classify_cell(policy, subject_id, origins, cutoff)
            for subject_id, origins in origins_by_subject.items()
        }

    families = {name: aggregate(family_policy(name), family_cells.values()) for name, family_cells in grades.items()}
    if membership_notes is not None:
        for family in PLANE_FAMILIES:
            families[family]["routes"] = membership_notes

    samples = list(sample_subjects) if sample_subjects else list(DEFAULT_SAMPLE_SUBJECTS.get(universe, ()))
    sample: dict[str, Any] = {}
    for subject_id in samples:
        entry: dict[str, Any] = {"ticker": subjects[subject_id].ticker if subject_id in subjects else None}
        for name, family_cells in grades.items():
            if subject_id in family_cells:
                entry[name] = _sample_entry(family_cells[subject_id])
        entry["in_universe"] = subject_id in subjects
        sample[subject_id] = entry

    quality_report_id, persisted_outcomes = load_quality_report_field_outcomes(connection, head.run_id)
    # The oracle's sample: the configured sample subjects first, then the universe in
    # order, each once, bounded by `oracle_issuers` — only issuers whose head carries a
    # revenue figure, because a mart gap is availability's finding, not accuracy's.
    oracle_issuers_selected: list[tuple[str, str, Decimal | None, Decimal | None]] = []
    seen: set[str] = set()
    for subject_id in [*samples, *sorted(subjects)]:
        subject = subjects.get(subject_id)
        if subject is None or subject_id in seen or not subject.ticker or subject.financial.get("revenue") is None:
            continue
        seen.add(subject_id)
        oracle_issuers_selected.append(
            (subject_id, subject.ticker, subject.financial.get("revenue"), subject.financial.get("gross_profit"))
        )
        if len(oracle_issuers_selected) >= max(0, oracle_issuers):
            break
    # Every bar field and fused fundamental is cross-checked against the quality report's
    # grade of that field, so "how many metrics are HIGH" is answered by two reports that
    # agree, not one.
    accuracy: dict[str, Any] = {
        name: field_accuracy(
            family_policy(name),
            families.get(name, {}),
            grades.get(name, {}),
            quality_report_id=quality_report_id,
            persisted=persisted_outcomes.get(name, {}),
        )
        for name in CROSS_CHECKED_FAMILIES
    }
    accuracy["sec_oracle"] = sec_oracle_section(
        oracle_issuers_selected,
        cutoff=utc_day(cutoff),
        ticker_index=None if oracle is None else oracle.ticker_index,
        facts_for=None if oracle is None else oracle.facts_for,
    )
    lineages = sorted(
        {
            origin.lineage
            for subject in subjects.values()
            for family in subject.origins.values()
            for origin in family
            if origin.value is not None
        }
        | {
            origin.lineage
            for family_origins in plane_origins.values()
            for listing_origins in family_origins.values()
            for origin in listing_origins
            if origin.value is not None
        }
    )
    return {
        "report_version": REPORT_VERSION,
        "universe": universe,
        "universe_id": head.universe_id,
        "run_id": head.run_id,
        "cutoff": cutoff.astimezone(UTC).isoformat(),
        "generated_at": executed_at.astimezone(UTC).isoformat(),
        "environment": environment,
        "bands": dict(BAND_DEFINITIONS),
        "independence_rule": INDEPENDENCE_RULE,
        "subjects": len(subjects),
        "sources_connected": lineages,
        "families": families,
        "cells": {
            name: {subject_id: grade.payload() for subject_id, grade in cells.items()} for name, cells in grades.items()
        },
        "sample": sample,
        "accuracy": accuracy,
        "metadata": {
            "stored_confidence": stored_confidence_metadata(load_stored_confidence(connection, head.run_id)),
            "quality_report_id": quality_report_id,
            "oracle_issuers_requested": oracle_issuers,
        },
    }


def compile_report(
    connection: Connection[Any],
    *,
    universe: str,
    executed_at: datetime,
    environment: str = "production",
    sample_subjects: Sequence[str] | None = None,
    oracle_issuers: int = DEFAULT_ORACLE_ISSUERS,
    oracle: SecOracle | None = None,
) -> dict[str, Any] | None:
    """The report for one lane universe's governed head, or None when it has no head yet."""
    prefix = UNIVERSE_PREFIXES.get(universe, universe)
    head = governed_head(connection, universe_prefix=prefix, environment=environment)
    if head is None:
        return None
    return build_report(
        connection,
        universe=universe,
        head=head,
        executed_at=executed_at,
        environment=environment,
        sample_subjects=sample_subjects,
        oracle_issuers=oracle_issuers,
        oracle=oracle,
    )


def persist(connection: Connection[Any], report: Mapping[str, Any]) -> str:
    report_id, content_sha256 = content_address(report)
    connection.execute(
        """
        insert into mart.datahub_confidence_report
            (report_id, content_sha256, universe_id, run_id, cutoff, payload)
        values (%s, %s, %s, %s, %s, %s)
        on conflict (report_id) do nothing
        """,
        (report_id, content_sha256, report["universe_id"], report["run_id"], report["cutoff"], Jsonb(dict(report))),
    )
    return report_id


def summary_line(report: Mapping[str, Any]) -> str:
    parts = []
    for name, family in report["families"].items():
        rate = family.get("agreement_rate")
        parts.append(
            f"{name}: high {family['high']} / medium {family['medium']} / low {family['low']} / missing {family['missing']}"
            + (f" (agreement {rate})" if rate is not None else "")
        )
    return f"{report['universe_id']} @ {report['cutoff']}: " + "; ".join(parts)
