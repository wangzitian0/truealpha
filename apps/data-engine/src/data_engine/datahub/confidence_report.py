"""Datahub CONFIDENCE & ACCURACY report for a governed head (owner standard, 2026-09-15)."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from factors.confidence import evaluate_cell_confidence
from factors.panel import ALL_ISSUER_CLASSES, IssuerClass, snapshot_field_specs
from psycopg import Connection
from psycopg.types.json import Jsonb
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.confidence import ContinuousConfidencePolicy, SourceConfidenceEvidence
from truealpha_contracts.reconciliation import (
    MEDIAN_QUORUM_GROUPS,
    ConflictBehavior,
    ReconciliationOutcome,
    ReconciliationPolicy,
    ReconciliationResult,
    SourceAssertion,
    reconcile_source_assertions,
)
from truealpha_contracts.reconciliation_policies import (
    FIELD_RECONCILIATION_POLICIES,
    FINANCIAL_FACT_RECONCILIATION_POLICY,
    INDEX_MEMBERSHIP_POLICY,
)
from truealpha_contracts.universes import key_of_lane, record

from data_engine.datahub.oracle_outcomes import (
    canonical_metric,
    load_oracle_confidence_inputs,
    oracle_cell_agreement,
    oracle_outcome_mass_by_origin,
)
from data_engine.datahub.production_topt.executor import SERVED_BY_FAILOVER
from data_engine.datahub.production_topt.headcount import is_eligible_headcount_row
from data_engine.datahub.production_topt.parser_identity import PARSER_VERSION_HISTORY
from data_engine.datahub.production_topt.source_registrations import (
    RELEASE_SEMANTICS,
    SOURCE_BY_PARSER,
    registered_semantic_types,
    registration_for,
    source_reliability_prior,
)
from data_engine.datahub.production_topt.universe_plane import UNIVERSE_SOURCES, UniverseSource, resolve_listing_mics
from data_engine.datahub.quality_report import (
    FIELD_UNITS,
    FINANCIAL_FACT_FUSION_FIELDS,
    PRICE_BAR_FIELDS,
    _reconciliation_cell,
    corroborating_financial_value,
    financial_fact_unit,
    primary_financial_fields,
    utc_day,
)
from data_engine.datahub.question_coverage import (
    GovernedHead,
    NoHead,
    declared_environment,
    governed_head_of,
)
from data_engine.datahub.resolve_coordinates import alias_of, is_uuid

REPORT_VERSION = "datahub-confidence-report:v1"
REPORT_ID_PREFIX = "datahub-confidence-report"
MARKET_PRICE_SEMANTIC = "market-price"
CLOSE_FAMILY = "close"
INDEX_MEMBERSHIP_FAMILY = "index_membership"
ETF_WEIGHT_FAMILY = "etf_weight"
INDEX_MEMBERSHIP_SEMANTIC = "index-membership"
MEMBER = "member"

FINANCIAL_FIELDS: tuple[str, ...] = (
    "revenue",
    "gross_profit",
    "pre_provision_profit",
    "total_assets",
    "shares_outstanding",
    "net_income",
    "headcount",
)
CROSS_CHECKED_FAMILIES: tuple[str, ...] = (*PRICE_BAR_FIELDS, *FINANCIAL_FACT_FUSION_FIELDS)

SEC_COMPANY_FACTS_ORIGIN = "origin:sec-company-facts:v1"
SEC_COMPANY_FACTS_SOURCE = "sec-company-facts:v1"
HEADCOUNT_LINEAGE: Mapping[str, str] = {"10k-extraction": "sec-10k", "manual-review": "sec-10k"}
NASDAQ_INDEX_ORIGIN = "origin:nasdaq-index:v1"
NPORT_ORIGIN = "origin:nport:v1"
MEMBERSHIP_LINEAGE: Mapping[str, str] = {NASDAQ_INDEX_ORIGIN: "nasdaq-index", NPORT_ORIGIN: "sec-nport"}
VENDOR_LINEAGE: Mapping[str, str] = {"origin:moomoo-kline:v1": "moomoo", "origin:moomoo-financials:v1": "moomoo"}

INDEPENDENCE_RULE = (
    "two origins are independent when their lineages differ; a mirror, reseller or second "
    "parser of one document is the same source and never corroborates its original"
)

_CIK_ID = re.compile(r"^issuer:cik:(\d+)$")
_RATIO_PLACES = Decimal("0.0001")
_DELTA_PLACES = Decimal("0.000001")
CONTINUOUS_CONFIDENCE_POLICY = ContinuousConfidencePolicy()
UNSERVED_REASONS = frozenset({"no_origin_value", "not_knowable_at_cutoff", "no_eligible_origin"})
COMPILE_STAMPS: tuple[str, ...] = ("generated_at",)
_COMPARED_OUTCOMES = frozenset({ReconciliationOutcome.AGREED.value, ReconciliationOutcome.CONFLICT_ABSTAINED.value})


@dataclass(frozen=True)
class OriginValue:
    origin_id: str
    source_id: str
    lineage: str
    value: str | None
    knowable_at: datetime | None = None
    observation_id: str | None = None
    period_end: date | None = None
    served_by_failover: bool = False


@dataclass(frozen=True)
class FamilyPolicy:
    family: str
    semantic_type: str
    value_keys: tuple[str, ...]
    reconciliation: ReconciliationPolicy | None
    comparison: str = "numeric"
    session_bound: bool = False
    period_bound: bool = False
    unit: str = "USD"
    applicability: frozenset[IssuerClass] = ALL_ISSUER_CLASSES


@dataclass(frozen=True)
class CellGrade:
    family: str
    subject_id: str
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
    confidence: Decimal = Decimal("0")
    policy_hash: str = ""
    input_hash: str = ""
    reason_codes: tuple[str, ...] = ()
    observation_id: str | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "origins": list(self.origins),
            "independent_origins": self.independent_origins,
            "outcome": self.outcome,
            "delta": self.delta,
            "relative_delta": self.relative_delta,
            "comparison": self.comparison,
            "excluded": list(self.excluded),
            "confidence": str(self.confidence),
            "policy_hash": self.policy_hash,
            "input_hash": self.input_hash,
            "reason_codes": list(self.reason_codes),
            "observation_id": self.observation_id,
        }


def _close_origins() -> dict[str, tuple[str, str, str]]:
    market = {origin.origin_id for origin in registration_for(MARKET_PRICE_SEMANTIC).origins}
    out: dict[str, tuple[str, str, str]] = {}
    for coordinate in SOURCE_BY_PARSER.values():
        if coordinate[1] in market:
            out.setdefault(coordinate[1], coordinate)
    return out


CLOSE_ORIGINS: Mapping[str, tuple[str, str, str]] = _close_origins()


def lineage_of(origin_id: str) -> str:
    if origin_id in MEMBERSHIP_LINEAGE:
        return MEMBERSHIP_LINEAGE[origin_id]
    if origin_id in VENDOR_LINEAGE:
        return VENDOR_LINEAGE[origin_id]
    parts = origin_id.split(":")
    return parts[1] if len(parts) >= 3 and parts[0] == "origin" else origin_id


def _market_price_families() -> tuple[FamilyPolicy, ...]:
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


_FIELD_APPLICABILITY: Mapping[str, frozenset[IssuerClass]] = {
    field_name: node.applicability for field_name, node in snapshot_field_specs().items()
}

FAMILIES: tuple[FamilyPolicy, ...] = (
    *_market_price_families(),
    *(
        FamilyPolicy(
            family=name,
            semantic_type="financial-fact",
            value_keys=(name,),
            reconciliation=FINANCIAL_FACT_RECONCILIATION_POLICY if name in FINANCIAL_FACT_FUSION_FIELDS else None,
            period_bound=name in FINANCIAL_FACT_FUSION_FIELDS,
            applicability=_FIELD_APPLICABILITY.get(name, ALL_ISSUER_CLASSES),
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
        unit="percent_of_net_assets",
    ),
)
PLANE_FAMILIES: tuple[str, ...] = (INDEX_MEMBERSHIP_FAMILY, ETF_WEIGHT_FAMILY)
_FAMILY_BY_NAME = {policy.family: policy for policy in FAMILIES}
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
    return None if denominator == 0 else str((Decimal(numerator) / Decimal(denominator)).quantize(_RATIO_PLACES))


def _synthetic_observation_id(family: str, subject_id: str, origin: OriginValue) -> str:
    return "normalized-observation:" + canonical_sha256(
        {"family": family, "subject": subject_id, "origin": origin.origin_id, "value": origin.value}
    )


def _narrow_to_anchor(
    policy: FamilyPolicy,
    origins: Sequence[OriginValue],
    *,
    bound: bool,
    raw_anchor: Callable[[OriginValue], Any],
    match_key: Callable[[Any], Any],
    require_valued: bool,
) -> tuple[list[OriginValue], list[OriginValue]]:
    if not bound or policy.reconciliation is None:
        return list(origins), []
    dated = [o for o in origins if raw_anchor(o) is not None and (not require_valued or o.value is not None)]
    if not dated:
        return list(origins), []
    valued = [o for o in dated if o.value is not None] or dated
    primary = policy.reconciliation.source_priority[0]
    primary_keys = [raw_anchor(o) for o in valued if o.source_id == primary and raw_anchor(o) is not None]
    anchor = match_key(max(primary_keys) if primary_keys else max(raw_anchor(o) for o in valued if raw_anchor(o)))
    kept = [o for o in origins if raw_anchor(o) is None or match_key(raw_anchor(o)) == anchor]
    return kept, [o for o in origins if o not in kept]


def _served_day(policy: FamilyPolicy, origins: Sequence[OriginValue]) -> tuple[list[OriginValue], list[OriginValue]]:
    return _narrow_to_anchor(
        policy,
        origins,
        bound=policy.session_bound,
        raw_anchor=lambda o: o.knowable_at,
        match_key=utc_day,
        require_valued=False,
    )


def _served_period(policy: FamilyPolicy, origins: Sequence[OriginValue]) -> tuple[list[OriginValue], list[OriginValue]]:
    return _narrow_to_anchor(
        policy,
        origins,
        bound=policy.period_bound,
        raw_anchor=lambda o: o.period_end,
        match_key=lambda d: d,
        require_valued=True,
    )


def _numeric_for_comparison(
    policy: FamilyPolicy, origins: Sequence[OriginValue]
) -> tuple[str, dict[str, Decimal | None]]:
    if policy.comparison == "membership":
        return "membership", {origin.origin_id: None for origin in origins}
    return "numeric", {origin.origin_id: _decimal(origin.value) for origin in origins}


def _deltas(
    policy: FamilyPolicy, numeric: Mapping[str, Decimal | None], origins: Sequence[OriginValue]
) -> tuple[str | None, str | None]:
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
    integral = worst.to_integral_value()
    return str(integral if worst == integral else worst), str(relative)


def _lookup_outcome_mass(
    outcome_mass_by_origin: Mapping[str, tuple[Decimal, Decimal]] | None,
    origin: OriginValue,
    family: str | None = None,
) -> tuple[Decimal, Decimal]:
    if not outcome_mass_by_origin:
        return Decimal("0"), Decimal("0")
    base_candidates = (origin.origin_id, f"origin:{origin.lineage}:v1", origin.source_id, origin.lineage)
    if family is not None:
        canonical = canonical_metric(family)
        field_keys = (family, canonical) if canonical != family else (family,)
        for candidate in base_candidates:
            for field_key in field_keys:
                scoped = f"{candidate}:{field_key}"
                if scoped in outcome_mass_by_origin:
                    return outcome_mass_by_origin[scoped]
        if any(key.startswith(f"{candidate}:") for candidate in base_candidates for key in outcome_mass_by_origin):
            return Decimal("0"), Decimal("0")
    for candidate in base_candidates:
        if candidate in outcome_mass_by_origin:
            return outcome_mass_by_origin[candidate]
    return Decimal("0"), Decimal("0")


def _evaluate_valued_cell(
    policy: FamilyPolicy,
    subject_id: str,
    valued: Sequence[OriginValue],
    *,
    reason: str,
    relative: str | None,
    cutoff: datetime,
    outcome_mass_by_origin: Mapping[str, tuple[Decimal, Decimal]] | None,
    oracle_outcomes: Sequence[Mapping[str, Any]],
    subject_aliases: Sequence[str],
):
    case_id = f"cell:{policy.family}:{subject_id}"
    is_conflict = reason in ("cross_origin_conflict", "not_agreed_within_tolerance")
    if is_conflict and valued:
        priority = policy.reconciliation.source_priority if policy.reconciliation else ()
        primary = min(
            valued,
            key=lambda origin: (
                priority.index(origin.source_id) if origin.source_id in priority else len(priority),
                origin.origin_id,
            ),
        )
        active_origins: Sequence[OriginValue] = (primary,)
    else:
        active_origins = valued
    sources: list[SourceConfidenceEvidence] = []
    for origin in active_origins:
        succ, fail = _lookup_outcome_mass(outcome_mass_by_origin, origin, family=policy.family)
        prior = (
            source_reliability_prior(origin.origin_id)
            or source_reliability_prior(origin.source_id)
            or source_reliability_prior(origin.lineage)
        )
        freshness = Decimal("1")
        if policy.session_bound and origin.knowable_at is not None:
            lag_days = max(0, (utc_day(cutoff) - utc_day(origin.knowable_at)).days)
            if lag_days > 4:
                freshness = max(Decimal("0.20"), Decimal("1") - Decimal("0.05") * Decimal(lag_days - 4))
        evidence_id = origin.observation_id or _synthetic_observation_id(policy.family, subject_id, origin)
        sources.append(
            SourceConfidenceEvidence(
                provider_id=origin.origin_id,
                origin_group_id=f"lineage:{origin.lineage}",
                independence_weight=Decimal("1"),
                reliability_prior=prior,
                successful_outcome_mass=succ,
                failed_outcome_mass=fail,
                freshness=freshness,
                sample_conformance=Decimal("0.90") if origin.served_by_failover else Decimal("1"),
                transport_integrity=Decimal("1"),
                evidence_ids=(evidence_id,),
                reason_codes=(f"reason:{reason}",),
            )
        )
    if len(valued) == 1 or is_conflict:
        multi_agreement = Decimal("1")
    else:
        rel = _decimal(relative) or Decimal("0")
        multi_agreement = max(Decimal("0.50"), Decimal("1") - min(Decimal("0.50"), rel))
    oracle_agreement, oracle_reasons = oracle_cell_agreement(
        oracle_outcomes,
        subject_ids=(subject_id, *subject_aliases),
        field=policy.family,
        cutoff=cutoff,
    )
    return evaluate_cell_confidence(
        case_id=case_id,
        sources=tuple(sources),
        agreement=min(multi_agreement, oracle_agreement),
        semantic_mapping_quality=Decimal("0.85") if reason == "no_agreement_policy" else Decimal("1"),
        lineage_completeness=Decimal("1"),
        required_component_completeness=Decimal("1"),
        extra_reason_codes=(f"reason:{reason}", *oracle_reasons),
        policy=CONTINUOUS_CONFIDENCE_POLICY,
    )


def classify_cell(
    policy: FamilyPolicy,
    subject_id: str,
    origins: Sequence[OriginValue],
    cutoff: datetime,
    *,
    outcome_mass_by_origin: Mapping[str, tuple[Decimal, Decimal]] | None = None,
    oracle_outcomes: Sequence[Mapping[str, Any]] = (),
    subject_aliases: Sequence[str] = (),
) -> CellGrade:
    """Grade one cell from what its origins asserted."""
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
        reason: str,
        origins_asserting: tuple[str, ...],
        independent: int,
        *,
        outcome: str | None = None,
        delta: str | None = None,
        relative: str | None = None,
        comparison: str | None = None,
    ) -> CellGrade:
        case_id = f"cell:{policy.family}:{subject_id}"
        observation_id = next((origin.observation_id for origin in valued if origin.observation_id), None)
        if policy.semantic_type in RELEASE_SEMANTICS and valued and reason not in UNSERVED_REASONS:
            cell_eval = evaluate_cell_confidence(
                case_id=case_id, definitional=True, policy=CONTINUOUS_CONFIDENCE_POLICY
            )
        elif not valued or reason in UNSERVED_REASONS:
            cell_eval = evaluate_cell_confidence(
                case_id=case_id,
                has_value=False,
                extra_reason_codes=(f"reason:{reason}",),
                policy=CONTINUOUS_CONFIDENCE_POLICY,
            )
        else:
            cell_eval = _evaluate_valued_cell(
                policy,
                subject_id,
                valued,
                reason=reason,
                relative=relative,
                cutoff=cutoff,
                outcome_mass_by_origin=outcome_mass_by_origin,
                oracle_outcomes=oracle_outcomes,
                subject_aliases=subject_aliases,
            )
        return CellGrade(
            family=policy.family,
            subject_id=subject_id,
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
            confidence=cell_eval.confidence,
            policy_hash=cell_eval.policy_hash,
            input_hash=cell_eval.input_hash,
            reason_codes=cell_eval.reason_codes,
            observation_id=observation_id,
        )

    if not valued:
        return grade("no_origin_value", (), 0)
    asserted = tuple(sorted(origin.origin_id for origin in valued))
    independent = len({origin.lineage for origin in valued})
    if len(valued) == 1:
        if valued[0].served_by_failover:
            reason = SERVED_BY_FAILOVER
        elif other_day_ids:
            reason = "second_origin_other_day"
        elif other_period_ids:
            reason = "second_origin_other_period"
        else:
            reason = "single_origin"
        return grade(reason, asserted, 1)
    comparison, numeric = _numeric_for_comparison(policy, valued)
    delta, relative = _deltas(policy, numeric, valued)
    if independent < 2:
        return grade("same_lineage", asserted, independent, delta=delta, relative=relative, comparison=comparison)
    if policy.reconciliation is None:
        return grade(
            "no_agreement_policy", asserted, independent, delta=delta, relative=relative, comparison=comparison
        )
    result = _reconcile(policy, subject_id, valued, numeric, cutoff)
    if result.outcome is ReconciliationOutcome.AGREED:
        pair = (
            policy.reconciliation.conflict_behavior is ConflictBehavior.MEDIAN_CONSENSUS
            and len(result.agreeing_assertion_ids) < MEDIAN_QUORUM_GROUPS
        )
        reason = "two_origins_agree" if pair else "independent_origins_agree"
    else:
        reason = {
            ReconciliationOutcome.CONFLICT_ABSTAINED: "not_agreed_within_tolerance",
            ReconciliationOutcome.CONFLICT_PRIORITY_SERVED: "cross_origin_conflict",
            ReconciliationOutcome.INSUFFICIENT_INDEPENDENT_ORIGINS: "single_eligible_origin",
            ReconciliationOutcome.NOT_YET_KNOWABLE: "not_knowable_at_cutoff",
            ReconciliationOutcome.UNAVAILABLE: "no_eligible_origin",
        }.get(result.outcome, "unknown")
    return grade(
        reason,
        asserted,
        independent,
        outcome=result.outcome.value,
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
) -> ReconciliationResult:
    assert policy.reconciliation is not None
    day = utc_day(cutoff)
    cell = _reconciliation_cell(
        f"{policy.semantic_type}:v1", subject_id, policy.family, policy.semantic_type, policy.unit, day, day
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
                numeric_value=compared,
                confidence_assessment_id=f"confidence-assessment:{value_sha}",
                lineage_node_ids=(observation_id,),
                lineage_complete=True,
            )
        )
    return reconcile_source_assertions(
        cell=cell, assertions=tuple(assertions), policy=policy.reconciliation, cutoff=cutoff
    )


def aggregate(policy: FamilyPolicy, grades: Iterable[CellGrade]) -> dict[str, Any]:
    rows = list(grades)
    by_origins = Counter(grade.independent_origins for grade in rows)
    total = len(rows)
    compared = sum(
        1
        for grade in rows
        if grade.outcome
        in (
            ReconciliationOutcome.AGREED.value,
            ReconciliationOutcome.CONFLICT_ABSTAINED.value,
            ReconciliationOutcome.CONFLICT_PRIORITY_SERVED.value,
        )
    )
    agreed = sum(1 for grade in rows if grade.outcome == ReconciliationOutcome.AGREED.value)
    reasons = Counter(grade.reason for grade in rows)
    mean_confidence = (
        str((sum((grade.confidence for grade in rows), Decimal(0)) / Decimal(total)).quantize(_RATIO_PLACES))
        if total
        else "0"
    )
    return {
        "semantic_type": policy.semantic_type,
        "cells": total,
        "by_independent_origins": {str(origins): by_origins[origins] for origins in sorted(by_origins)},
        "mean_confidence": mean_confidence,
        "compared": compared,
        "agreed": agreed,
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


def content_address(payload: Mapping[str, Any]) -> tuple[str, str]:
    digest = canonical_sha256({key: value for key, value in payload.items() if key not in COMPILE_STAMPS})
    return f"{REPORT_ID_PREFIX}:{digest}", digest


@dataclass
class _Subject:
    ticker: str | None = None
    issuer_id: str | None = None
    listing_id: str | None = None
    operating_branch: IssuerClass | None = None
    origins: dict[str, list[OriginValue]] = field(default_factory=dict)

    def add(self, family: str, origin: OriginValue) -> None:
        self.origins.setdefault(family, []).append(origin)


def _subject_applies(subject: _Subject, policy: FamilyPolicy) -> bool:
    return subject.operating_branch is None or subject.operating_branch in policy.applicability


def _cik_of(
    issuer_id: str | None,
    connection: Connection[Any] | None = None,
    cutoff: datetime | None = None,
) -> int | None:
    if not issuer_id:
        return None
    if match := _CIK_ID.match(issuer_id):
        return int(match.group(1))
    if is_uuid(issuer_id) and connection is not None:
        val = alias_of(
            connection,
            issuer_id,
            "cik",
            valid_at=cutoff.date() if cutoff else date.today(),
            known_at=cutoff or datetime.now(UTC),
        )
        if val is not None:
            try:
                return int(val)
            except (ValueError, TypeError):
                return None
    return None


def bar_origins(
    coordinate: tuple[str, str, str],
    payload: Mapping[str, Any],
    *,
    knowable_at: datetime | None = None,
    observation_id: str | None = None,
) -> dict[str, OriginValue]:
    origin_source, origin_id, value_key = coordinate
    close = payload.get(value_key)
    served_by_failover = payload.get(SERVED_BY_FAILOVER) is not None
    return {
        family: OriginValue(
            origin_id=origin_id,
            source_id=origin_source,
            lineage=lineage_of(origin_id),
            value=None
            if (val := (close if family == CLOSE_FAMILY or close is None else payload.get(family))) is None
            else str(val),
            knowable_at=knowable_at,
            observation_id=observation_id,
            served_by_failover=served_by_failover,
        )
        for family in PRICE_BAR_FIELDS
    }


@dataclass(frozen=True)
class FinancialObservation:
    primary: bool
    origin_source: str
    origin_id: str
    payload: Mapping[str, Any]
    knowable_at: datetime | None = None
    observation_id: str | None = None


def _text(value: Any) -> str | None:
    return None if value is None else str(value)


def _newest_period_value(payload: Mapping[str, Any], name: str) -> tuple[str | None, date | None]:
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
    @property
    def origin_id(self) -> str: ...

    @property
    def knowable_at(self) -> datetime | None: ...

    @property
    def observation_id(self) -> str | None: ...


def _recency(observation: _Observed) -> tuple[datetime, str]:
    return observation.knowable_at or datetime.min.replace(tzinfo=UTC), observation.observation_id or ""


def _newest_per_origin[ObservedT: _Observed](observations: Sequence[ObservedT]) -> list[ObservedT]:
    newest: dict[str, ObservedT] = {}
    for observation in observations:
        current = newest.get(observation.origin_id)
        if current is None or _recency(observation) > _recency(current):
            newest[observation.origin_id] = observation
    return [newest[origin_id] for origin_id in sorted(newest)]


def _corroborating_fused_value(
    payload: Mapping[str, Any],
    name: str,
    *,
    primary_payload: Mapping[str, Any],
    anchors: Mapping[str, tuple[Decimal, date]],
    unit: str | None,
) -> tuple[str | None, date | None]:
    if unit is None or financial_fact_unit(payload) != unit:
        return None, None
    if name in anchors:
        aligned = corroborating_financial_value(payload, name, anchors[name][1])
        return (str(aligned), anchors[name][1]) if aligned is not None else _newest_period_value(payload, name)
    return (None, None) if primary_payload.get(name) is not None else (_text(payload.get(name)), None)


def financial_origins(observations: Sequence[FinancialObservation]) -> dict[str, list[OriginValue]]:
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
                producer = str(((payload.get("vintage") or {}).get("headcount") or {}).get("source") or "unknown")
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
                value, period = _corroborating_fused_value(
                    payload, name, primary_payload=primary.payload, anchors=anchors, unit=unit
                )
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


def _resolve_financial_observation(
    parser_version: str,
    payload: Mapping[str, Any],
    knowable_at: datetime | None,
    observation_id: str | None,
    *,
    allow_integration_parser: bool = False,
) -> FinancialObservation | None:
    if parser_version in PARSER_VERSION_HISTORY or (
        allow_integration_parser and parser_version == "production-topt-integration-parser:v1"
    ):
        return FinancialObservation(
            True, SEC_COMPANY_FACTS_SOURCE, SEC_COMPANY_FACTS_ORIGIN, payload, knowable_at, observation_id
        )
    coordinate = SOURCE_BY_PARSER.get(parser_version)
    return (
        None
        if coordinate is None
        else FinancialObservation(False, coordinate[0], coordinate[1], payload, knowable_at, observation_id)
    )


def _resolve_market_price_coordinate(
    parser_version: str,
    *,
    allow_integration_parser: bool = False,
) -> tuple[str, str, str] | None:
    return SOURCE_BY_PARSER.get(parser_version) or (
        (SEC_COMPANY_FACTS_SOURCE, SEC_COMPANY_FACTS_ORIGIN, "close")
        if allow_integration_parser and parser_version == "production-topt-integration-parser:v1"
        else None
    )


def load_run_subjects(
    connection: Connection[Any],
    run_id: str,
    cutoff: datetime,
    *,
    allow_integration_parser: bool = False,
) -> dict[str, _Subject]:
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
        if payload.get("listing_id"):
            subject.listing_id = str(payload["listing_id"])
        if payload.get("issuer_id"):
            subject.issuer_id = subject.issuer_id or payload.get("issuer_id")
        if semantic_type in RELEASE_SEMANTICS:
            subject.ticker = subject.ticker or payload.get("ticker")
        elif semantic_type == MARKET_PRICE_SEMANTIC:
            coordinate = _resolve_market_price_coordinate(
                parser_version, allow_integration_parser=allow_integration_parser
            )
            if coordinate is not None:
                for family, origin in bar_origins(
                    coordinate, payload, knowable_at=knowable_at, observation_id=observation_id
                ).items():
                    subject.add(family, origin)
        elif semantic_type == "financial-fact":
            raw_branch = payload.get("operating_branch")
            if raw_branch is not None and subject.operating_branch is None:
                try:
                    subject.operating_branch = IssuerClass(str(raw_branch))
                except ValueError:
                    pass
            fin_obs = _resolve_financial_observation(
                parser_version,
                payload,
                knowable_at,
                observation_id,
                allow_integration_parser=allow_integration_parser,
            )
            if fin_obs is not None:
                financial.setdefault(str(subject_id), []).append(fin_obs)
    for subject_id, observations in financial.items():
        for family, family_origins in financial_origins(observations).items():
            for origin_value in family_origins:
                subjects[subject_id].add(family, origin_value)
    _add_headcount_producers(connection, subjects, cutoff)
    return subjects


def _add_headcount_producers(connection: Connection[Any], subjects: Mapping[str, _Subject], cutoff: datetime) -> None:
    ciks = {
        cik: subject_id
        for subject_id, subject in subjects.items()
        if (cik := _cik_of(subject.issuer_id, connection=connection, cutoff=cutoff)) is not None
    }
    if not ciks:
        return
    rows = connection.execute(
        """
        select cik, source, headcount, knowable_at, evidence_ref, period_end
        from staging.issuer_headcount_facts
        where cik = any(%s) and knowable_at <= %s
        order by cik, source, knowable_at desc, id desc
        """,
        (list(ciks), cutoff),
    ).fetchall()
    for cik, producer, headcount, knowable_at, evidence_ref, period_end in rows:
        if not is_eligible_headcount_row(
            knowable_at=knowable_at,
            source=str(producer),
            evidence_ref=str(evidence_ref or ""),
            period_end=period_end,
        ):
            continue
        subject = subjects[ciks[int(cik)]]
        if any(origin.source_id == producer for origin in subject.origins.get("headcount", [])):
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
    return UNIVERSE_SOURCES.get(key_of_lane(universe))


def load_index_membership(
    connection: Connection[Any], source: UniverseSource, cutoff: datetime
) -> tuple[dict[str, dict[str, list[OriginValue]]], dict[str, Any]]:
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

    mics = resolve_listing_mics(
        connection,
        source,
        [str(ticker) for ticker, _weight, _knowable_at in constituents],
        valid_at=cutoff.date(),
        known_at=cutoff,
    )
    unresolved_exchange: list[str] = []
    for ticker, weight, knowable_at in constituents:
        mic = mics[str(ticker)]
        if mic is None:
            unresolved_exchange.append(str(ticker))
            continue
        assert_route(f"listing:{mic}:{str(ticker).lower()}", NASDAQ_INDEX_ORIGIN, 0, weight, knowable_at)
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
        "constituents_without_listing_exchange": sorted(unresolved_exchange),
        "constituents_with_weight": sum(1 for _, weight, _ in constituents if weight is not None),
        "fund_id": None if fund is None else str(fund[0]),
        "holdings_vintage": vintage or None,
        "holding_lines": len(holdings),
        "holdings_unresolved_to_listing": sorted(unresolved),
        "denominator": "union of both routes' listings — a line held but not in the index is a real cell",
    }
    return origins, notes


def quality_report_field_outcomes(payload: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    outcomes: dict[str, dict[str, str]] = {name: {} for name in CROSS_CHECKED_FAMILIES}
    for listing, cell in (payload.get("reconciliation_cells") or {}).items():
        fields = cell.get("fields")
        graded = fields if isinstance(fields, Mapping) else {CLOSE_FAMILY: cell}
        for name in PRICE_BAR_FIELDS:
            if (outcome := (graded.get(name) or {}).get("outcome")) is not None:
                outcomes[name][str(listing)] = str(outcome)
    for listing, cell in (payload.get("financial_fact_reconciliation_cells") or {}).items():
        fields = cell.get("fields") or {}
        for name in FINANCIAL_FACT_FUSION_FIELDS:
            if (outcome := (fields.get(name) or {}).get("outcome")) is not None:
                outcomes[name][str(listing)] = str(outcome)
    return outcomes


def load_quality_report_field_outcomes(
    connection: Connection[Any], run_id: str
) -> tuple[str | None, dict[str, dict[str, str]]]:
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
    return (None, {}) if row is None else (str(row[0]), quality_report_field_outcomes(row[1] or {}))


def _disagrees(graded: str | None, persisted: str) -> bool:
    return persisted in _COMPARED_OUTCOMES if graded is None else graded != persisted


def field_accuracy(
    policy: FamilyPolicy,
    summary: Mapping[str, Any],
    cells: Mapping[str, CellGrade],
    *,
    quality_report_id: str | None,
    persisted: Mapping[str, str],
) -> dict[str, Any]:
    mismatches = sorted(
        listing
        for listing, outcome in persisted.items()
        if listing in cells and _disagrees(cells[listing].outcome, outcome)
    )
    return {
        "origins": summary.get("origins", []),
        "compared": summary.get("compared", 0),
        "agreed": summary.get("agreed", 0),
        "agreement_rate": summary.get("agreement_rate"),
        "tolerance_policy": _policy_payload(policy.reconciliation),
        "quality_report_id": quality_report_id,
        "quality_report_cells": len(persisted),
        "matches_quality_report": None if quality_report_id is None or not persisted else not mismatches,
        "quality_report_mismatches": mismatches,
    }


def _sample_entry(grade: CellGrade) -> dict[str, Any]:
    return {
        "values": dict(sorted(grade.values.items())),
        "delta": grade.delta,
        "relative_delta": grade.relative_delta,
        "comparison": grade.comparison,
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
) -> dict[str, Any]:
    cutoff = head.cutoff
    subjects = load_run_subjects(connection, head.run_id, cutoff)
    source = _universe_source(universe)
    membership_notes: dict[str, Any] | None = None
    plane_origins: dict[str, dict[str, list[OriginValue]]] = {}
    if source is not None and source.nport_ticker is not None:
        plane_origins, membership_notes = load_index_membership(connection, source, cutoff)

    oracle_rows = load_oracle_confidence_inputs(connection, run_id=head.run_id, cutoff=cutoff)
    outcome_mass_by_origin = oracle_outcome_mass_by_origin(oracle_rows, cutoff=cutoff)

    grades: dict[str, dict[str, CellGrade]] = {}
    for policy in FAMILIES:
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
            subject_id: classify_cell(
                policy,
                subject_id,
                origins,
                cutoff,
                outcome_mass_by_origin=outcome_mass_by_origin,
                oracle_outcomes=oracle_rows,
                subject_aliases=tuple(
                    alias
                    for alias in (subjects[subject_id].issuer_id if subject_id in subjects else None,)
                    if alias is not None
                ),
            )
            for subject_id, origins in origins_by_subject.items()
        }

    families = {name: aggregate(family_policy(name), family_cells.values()) for name, family_cells in grades.items()}
    if membership_notes is not None:
        for family in PLANE_FAMILIES:
            families[family]["routes"] = membership_notes

    sample: dict[str, Any] = {}
    for subject_id in record(key_of_lane(universe)).report_sample:
        entry: dict[str, Any] = {"ticker": subjects[subject_id].ticker if subject_id in subjects else None}
        for name, family_cells in grades.items():
            if subject_id in family_cells:
                entry[name] = _sample_entry(family_cells[subject_id])
        entry["in_universe"] = subject_id in subjects
        sample[subject_id] = entry

    quality_report_id, persisted_outcomes = load_quality_report_field_outcomes(connection, head.run_id)
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
        "independence_rule": INDEPENDENCE_RULE,
        "subjects": len(subjects),
        "sources_connected": lineages,
        "families": families,
        "cells": {
            name: {subject_id: grade.payload() for subject_id, grade in cells.items()} for name, cells in grades.items()
        },
        "sample": sample,
        "accuracy": accuracy,
        "metadata": {"quality_report_id": quality_report_id},
    }


def compile_report(
    connection: Connection[Any],
    *,
    universe: str,
    executed_at: datetime,
) -> dict[str, Any] | None:
    head = governed_head_of(connection, key_of_lane(universe))
    if isinstance(head, NoHead):
        return None
    return build_report(
        connection,
        universe=universe,
        head=head,
        executed_at=executed_at,
        environment=declared_environment(connection),
    )


def _insert_cell_confidence_row(
    connection: Connection[Any],
    *,
    run_id: str,
    subject_id: str,
    semantic_type: str,
    metric: str,
    observation_id: str | None,
    confidence: Decimal,
    policy_hash: str,
    input_hash: str,
    reason_codes: Sequence[str],
) -> None:
    connection.execute(
        """
        insert into mart.cell_confidence
            (run_id, subject_id, semantic_type, metric, observation_id, confidence, policy_hash, input_hash, reason_codes)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        on conflict on constraint cell_confidence_run_subject_metric_unique do nothing
        """,
        (
            run_id,
            subject_id,
            semantic_type,
            metric,
            observation_id,
            confidence,
            policy_hash,
            input_hash,
            list(reason_codes),
        ),
    )


def _record_cell_confidence(
    connection: Connection[Any],
    *,
    run_id: str,
    subject_id: str,
    semantic_type: str,
    metric: str,
    observation_id: str | None,
    confidence: Decimal,
    policy_hash: str | None,
    input_hash: str | None,
    reason_codes: Sequence[str],
    out: dict[tuple[str, str], Decimal] | None = None,
    listing_alias: str | None = None,
) -> None:
    if out is not None:
        out[(subject_id, metric)] = confidence
        if listing_alias and listing_alias != subject_id:
            out[(listing_alias, metric)] = confidence
    _insert_cell_confidence_row(
        connection,
        run_id=run_id,
        subject_id=subject_id,
        semantic_type=semantic_type,
        metric=metric,
        observation_id=observation_id,
        confidence=confidence,
        policy_hash=policy_hash or CONTINUOUS_CONFIDENCE_POLICY.content_sha256,
        input_hash=input_hash
        or canonical_sha256({"family": metric, "subject_id": subject_id, "confidence": str(confidence)}),
        reason_codes=reason_codes,
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
    cells_by_family = report.get("cells")
    if isinstance(cells_by_family, Mapping) and cells_by_family:
        families_meta = report.get("families") if isinstance(report.get("families"), Mapping) else {}
        run_id = str(report["run_id"])
        for family_name, family_cells in sorted(cells_by_family.items()):
            if not isinstance(family_cells, Mapping):
                continue
            fam_info = families_meta.get(family_name) if isinstance(families_meta, Mapping) else None
            semantic_type = (
                str(fam_info["semantic_type"])
                if isinstance(fam_info, Mapping) and fam_info.get("semantic_type")
                else (_FAMILY_BY_NAME[family_name].semantic_type if family_name in _FAMILY_BY_NAME else "unknown")
            )
            for subject_id, cell in sorted(family_cells.items()):
                if not isinstance(cell, Mapping):
                    continue
                obs_id = cell.get("observation_id")
                _record_cell_confidence(
                    connection,
                    run_id=run_id,
                    subject_id=str(subject_id),
                    semantic_type=semantic_type,
                    metric=str(family_name),
                    observation_id=str(obs_id) if obs_id else None,
                    confidence=Decimal(str(cell.get("confidence", "0"))),
                    policy_hash=str(cell.get("policy_hash") or ""),
                    input_hash=str(cell.get("input_hash") or ""),
                    reason_codes=[
                        str(code) for code in (cell.get("reason_codes") or [f"reason:{cell.get('reason', 'unknown')}"])
                    ],
                )
    return report_id


_SINGLE_ORIGIN_FINANCIAL_FIELDS: tuple[str, ...] = tuple(
    field_name for field_name in snapshot_field_specs() if field_name not in FINANCIAL_FIELDS
)
_TCE_FIELDS: tuple[str, ...] = _SINGLE_ORIGIN_FINANCIAL_FIELDS


def _persist_release_and_tce_cell_confidences(
    connection: Connection[Any],
    run_id: str,
    cutoff: datetime,
    subjects: Mapping[str, _Subject],
    outcome_mass_by_origin: Mapping[str, tuple[Decimal, Decimal]],
    oracle_rows: Sequence[Mapping[str, Any]],
    out: dict[tuple[str, str], Decimal],
) -> None:
    obs_rows = connection.execute(
        """
        select ob.subject_id, o.semantic_type, o.observation_id, o.parser_version, p.normalized_payload
        from raw.capture_obligations ob
        join staging.capture_observation_obligations oo on oo.capture_obligation_id = ob.obligation_id
        join staging.capture_normalized_observations o on o.observation_id = oo.observation_id
        join staging.capture_observation_payloads p on p.observation_id = o.observation_id
        where ob.run_id = %s and o.semantic_type in ('listing-identity', 'universe-membership', 'financial-fact')
        order by ob.subject_id, o.semantic_type, o.observation_id
        """,
        (run_id,),
    ).fetchall()
    for subject_id_raw, semantic_type_raw, observation_id_raw, parser_version_raw, payload_raw in obs_rows:
        subject_id, semantic_type, observation_id = str(subject_id_raw), str(semantic_type_raw), str(observation_id_raw)
        payload = payload_raw or {}
        listing_alias = str(payload["listing_id"]) if payload.get("listing_id") else None
        if semantic_type in RELEASE_SEMANTICS:
            cell_eval = evaluate_cell_confidence(
                case_id=f"cell:{semantic_type}:{subject_id}",
                definitional=True,
                policy=CONTINUOUS_CONFIDENCE_POLICY,
            )
            _record_cell_confidence(
                connection,
                run_id=run_id,
                subject_id=subject_id,
                semantic_type=semantic_type,
                metric=semantic_type,
                observation_id=observation_id,
                confidence=cell_eval.confidence,
                policy_hash=cell_eval.policy_hash,
                input_hash=cell_eval.input_hash,
                reason_codes=cell_eval.reason_codes,
                out=out,
                listing_alias=listing_alias,
            )
        elif semantic_type == "financial-fact" and (
            parser_version_raw in PARSER_VERSION_HISTORY
            or parser_version_raw == "production-topt-integration-parser:v1"
        ):
            aliases = tuple(
                alias
                for alias in (subjects[subject_id].issuer_id if subject_id in subjects else None,)
                if alias is not None
            )
            for tce_field in _TCE_FIELDS:
                if payload.get(tce_field) is None:
                    continue
                tce_policy = FamilyPolicy(
                    family=tce_field,
                    semantic_type="financial-fact",
                    value_keys=(tce_field,),
                    reconciliation=None,
                    period_bound=False,
                )
                tce_origin = OriginValue(
                    origin_id=SEC_COMPANY_FACTS_ORIGIN,
                    source_id=SEC_COMPANY_FACTS_SOURCE,
                    lineage=lineage_of(SEC_COMPANY_FACTS_ORIGIN),
                    value=str(payload[tce_field]),
                    observation_id=observation_id,
                )
                grade = classify_cell(
                    tce_policy,
                    subject_id,
                    (tce_origin,),
                    cutoff,
                    outcome_mass_by_origin=outcome_mass_by_origin,
                    oracle_outcomes=oracle_rows,
                    subject_aliases=aliases,
                )
                _record_cell_confidence(
                    connection,
                    run_id=run_id,
                    subject_id=subject_id,
                    semantic_type="financial-fact",
                    metric=tce_field,
                    observation_id=observation_id,
                    confidence=grade.confidence,
                    policy_hash=grade.policy_hash,
                    input_hash=grade.input_hash,
                    reason_codes=grade.reason_codes,
                    out=out,
                    listing_alias=listing_alias,
                )


def evaluate_and_persist_run_cell_confidence(
    connection: Connection[Any],
    run_id: str,
    *,
    cutoff: datetime | None = None,
) -> dict[tuple[str, str], Decimal]:
    if not hasattr(connection, "cursor"):
        return {}
    if cutoff is None:
        row = connection.execute("select cutoff from mart.topt_capture_status where run_id = %s", (run_id,)).fetchone()
        cutoff = row[0] if row is not None and row[0] is not None else datetime.now(UTC)

    subjects = load_run_subjects(connection, run_id, cutoff, allow_integration_parser=True)
    oracle_rows = load_oracle_confidence_inputs(connection, run_id=run_id, cutoff=cutoff)
    outcome_mass_by_origin = oracle_outcome_mass_by_origin(oracle_rows, cutoff=cutoff)

    out: dict[tuple[str, str], Decimal] = {}
    for policy in FAMILIES:
        if policy.family in PLANE_FAMILIES:
            continue
        for subject_id, subject in sorted(subjects.items()):
            if not _subject_applies(subject, policy):
                continue
            grade = classify_cell(
                policy,
                subject_id,
                subject.origins.get(policy.family, []),
                cutoff,
                outcome_mass_by_origin=outcome_mass_by_origin,
                oracle_outcomes=oracle_rows,
                subject_aliases=tuple(alias for alias in (subject.issuer_id,) if alias is not None),
            )
            _record_cell_confidence(
                connection,
                run_id=run_id,
                subject_id=subject_id,
                semantic_type=policy.semantic_type,
                metric=policy.family,
                observation_id=grade.observation_id,
                confidence=grade.confidence,
                policy_hash=grade.policy_hash,
                input_hash=grade.input_hash,
                reason_codes=grade.reason_codes or (f"reason:{grade.reason}",),
                out=out,
                listing_alias=subject.listing_id,
            )

    _persist_release_and_tce_cell_confidences(
        connection, run_id, cutoff, subjects, outcome_mass_by_origin, oracle_rows, out
    )

    for subject_id_raw, metric_raw, conf_raw in connection.execute(
        "select subject_id, metric, confidence from mart.cell_confidence where run_id = %s",
        (run_id,),
    ).fetchall():
        out[(str(subject_id_raw), str(metric_raw))] = Decimal(str(conf_raw))
    return out


def summary_line(report: Mapping[str, Any]) -> str:
    parts = []
    for name, family in report["families"].items():
        rate = family.get("agreement_rate")
        origins = ", ".join(f"{count} with {number}" for number, count in family["by_independent_origins"].items())
        parts.append(
            f"{name}: {family['cells']} cells ({origins} independent origins), mean confidence "
            f"{family['mean_confidence']}" + (f", agreement {rate}" if rate is not None else "")
        )
    return f"{report['universe_id']} @ {report['cutoff']}: " + "; ".join(parts)
