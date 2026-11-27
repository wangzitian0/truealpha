"""Datahub confidence and accuracy report for a governed head."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Protocol

from factors.confidence import evaluate_cell_confidence
from factors.panel import ALL_ISSUER_CLASSES, IssuerClass, snapshot_field_specs
from psycopg import Connection
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.confidence import ContinuousConfidencePolicy, SourceConfidenceEvidence
from truealpha_contracts.universes import key_of_lane, record
from truealpha_runtime.reconciliation import (
    MEDIAN_QUORUM_GROUPS,
    ConflictBehavior,
    ReconciliationOutcome,
    ReconciliationPolicy,
    ReconciliationResult,
    SourceAssertion,
    reconcile_source_assertions,
)
from truealpha_runtime.reconciliation_policies import (
    FIELD_RECONCILIATION_POLICIES,
    FINANCIAL_FACT_RECONCILIATION_POLICY,
    INDEX_MEMBERSHIP_POLICY,
)

from data_engine.datahub._helpers import CAPTURE_OBSERVATION_JOIN, execute_many, fetch_run_cutoff, parse_decimal
from data_engine.datahub.capture_core.executor import SERVED_BY_FAILOVER
from data_engine.datahub.capture_core.headcount import is_eligible_headcount_row
from data_engine.datahub.capture_core.parser_identity import PARSER_VERSION_HISTORY
from data_engine.datahub.capture_core.source_registrations import (
    RELEASE_SEMANTICS,
    SOURCE_BY_PARSER,
    registered_semantic_types,
    registration_for,
    source_reliability_prior,
)
from data_engine.datahub.capture_core.universe_plane import (
    UNIVERSE_SOURCES,
    UniverseSource,
    active_constituent_source,
    resolve_listing_mics,
)
from data_engine.datahub.oracle_outcomes import (
    OracleRowsBySubject,
    canonical_metric,
    load_oracle_confidence_inputs,
    oracle_cell_agreement,
    oracle_outcome_mass_by_origin,
)
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
from data_engine.datahub.question_coverage import GovernedHead, NoHead, declared_environment, governed_head_of
from data_engine.datahub.repository import persist_run_report
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
NPORT_ORIGIN = "origin:nport:v1"
_ORIGIN_LINEAGE: Mapping[str, str] = {
    NPORT_ORIGIN: "sec-nport",
    "origin:moomoo-kline:v1": "moomoo",
    "origin:moomoo-financials:v1": "moomoo",
}
INDEPENDENCE_RULE = (
    "two origins are independent when their lineages differ; a mirror, reseller or second "
    "parser of one document is the same source and never corroborates its original"
)

_CIK_ID = re.compile(r"^issuer:cik:(\d+)$")
_RATIO_PLACES = Decimal("0.0001")
_DELTA_PLACES = Decimal("0.000001")
CONTINUOUS_CONFIDENCE_POLICY = ContinuousConfidencePolicy()
UNSERVED_REASONS = frozenset({"no_origin_value", "not_knowable_at_cutoff", "no_eligible_origin"})
COMPILE_STAMPS: tuple[str, ...] = ("generated_at", "tier_counts")
_COMPARED_OUTCOMES = frozenset({ReconciliationOutcome.AGREED.value, ReconciliationOutcome.CONFLICT_ABSTAINED.value})
_AGGREGATE_COMPARED_OUTCOMES = _COMPARED_OUTCOMES | {ReconciliationOutcome.CONFLICT_PRIORITY_SERVED.value}
_CELL_CONFIDENCE_INSERT_SQL = (
    "insert into mart.cell_confidence "
    "(run_id, subject_id, semantic_type, metric, observation_id, confidence, policy_hash, input_hash, reason_codes) "
    "values (%s, %s, %s, %s, %s, %s, %s, %s, %s) on conflict on constraint cell_confidence_run_subject_metric_unique do nothing"
)


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
    return _ORIGIN_LINEAGE.get(origin_id) or (
        parts[1] if len(parts := origin_id.split(":")) >= 3 and parts[0] == "origin" else origin_id
    )


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
        INDEX_MEMBERSHIP_FAMILY,
        INDEX_MEMBERSHIP_SEMANTIC,
        (MEMBER,),
        INDEX_MEMBERSHIP_POLICY,
        comparison="membership",
        unit="membership",
    ),
    FamilyPolicy(
        ETF_WEIGHT_FAMILY, INDEX_MEMBERSHIP_SEMANTIC, ("weight",), INDEX_MEMBERSHIP_POLICY, unit="percent_of_net_assets"
    ),
)
PLANE_FAMILIES: tuple[str, ...] = (INDEX_MEMBERSHIP_FAMILY, ETF_WEIGHT_FAMILY)
_FAMILY_BY_NAME = {policy.family: policy for policy in FAMILIES}
if _ungraded := set(registered_semantic_types()) - RELEASE_SEMANTICS - {p.semantic_type for p in FAMILIES}:
    raise ValueError(f"registered semantics with no confidence family: {sorted(_ungraded)}")


def family_policy(name: str) -> FamilyPolicy:
    try:
        return _FAMILY_BY_NAME[name]
    except KeyError as error:
        raise LookupError(f"no confidence family named {name!r}; families: {sorted(_FAMILY_BY_NAME)}") from error


_decimal = parse_decimal


def _ratio(numerator: int, denominator: int) -> str | None:
    return None if denominator == 0 else str((Decimal(numerator) / Decimal(denominator)).quantize(_RATIO_PLACES))


def _synthetic_observation_id(family: str, subject_id: str, origin: OriginValue) -> str:
    return "normalized-observation:" + canonical_sha256(
        {"family": family, "subject": subject_id, "origin": origin.origin_id, "value": origin.value}
    )


def _narrow_to_anchor(
    policy: FamilyPolicy,
    origins: Sequence[OriginValue],
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
    anchor = match_key(max(primary_keys) if primary_keys else max(raw_anchor(o) for o in valued))
    kept = [o for o in origins if raw_anchor(o) is None or match_key(raw_anchor(o)) == anchor]
    return kept, [o for o in origins if o not in kept]


def _numeric_for_comparison(
    policy: FamilyPolicy, origins: Sequence[OriginValue]
) -> tuple[str, dict[str, Decimal | None]]:
    if policy.comparison == "membership":
        return "membership", {origin.origin_id: None for origin in origins}
    return "numeric", {origin.origin_id: _decimal(origin.value) for origin in origins}


def _priority_rank(policy: FamilyPolicy, origin: OriginValue) -> tuple[int, str]:
    priority = policy.reconciliation.source_priority if policy.reconciliation else ()
    rank = priority.index(origin.source_id) if origin.source_id in priority else len(priority)
    return rank, origin.origin_id


def _deltas(
    policy: FamilyPolicy, numeric: Mapping[str, Decimal | None], origins: Sequence[OriginValue]
) -> tuple[str | None, str | None]:
    values = {origin_id: value for origin_id, value in numeric.items() if value is not None}
    if len(values) < 2:
        return None, None
    ranks = {origin.origin_id: _priority_rank(policy, origin) for origin in origins}
    anchor_id = min(values, key=lambda oid: ranks.get(oid, (len(ranks), oid)))
    anchor = values[anchor_id]
    worst = max((abs(anchor - value) for oid, value in values.items() if oid != anchor_id), default=Decimal(0))
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
    base = (origin.origin_id, f"origin:{origin.lineage}:v1", origin.source_id, origin.lineage)
    if family is not None:
        can = canonical_metric(family)
        keys = (family, can) if can != family else (family,)
        for c in base:
            for k in keys:
                if (scoped := f"{c}:{k}") in outcome_mass_by_origin:
                    return outcome_mass_by_origin[scoped]
        if any(k.startswith(f"{c}:") for c in base for k in outcome_mass_by_origin):
            return Decimal("0"), Decimal("0")
    for c in base:
        if c in outcome_mass_by_origin:
            return outcome_mass_by_origin[c]
    return Decimal("0"), Decimal("0")


_OUTCOME_REASONS = {
    ReconciliationOutcome.CONFLICT_ABSTAINED: "not_agreed_within_tolerance",
    ReconciliationOutcome.CONFLICT_PRIORITY_SERVED: "cross_origin_conflict",
    ReconciliationOutcome.INSUFFICIENT_INDEPENDENT_ORIGINS: "single_eligible_origin",
    ReconciliationOutcome.NOT_YET_KNOWABLE: "not_knowable_at_cutoff",
    ReconciliationOutcome.UNAVAILABLE: "no_eligible_origin",
}


def _dropped_ids(dropped: Sequence[OriginValue], asserting: set[str]) -> tuple[str, ...]:
    return tuple(sorted({origin.origin_id for origin in dropped if origin.value is not None} - asserting))


def classify_cell(
    policy: FamilyPolicy,
    subject_id: str,
    origins: Sequence[OriginValue],
    cutoff: datetime,
    *,
    outcome_mass_by_origin: Mapping[str, tuple[Decimal, Decimal]] | None = None,
    oracle_outcomes: Sequence[Mapping[str, Any]] | OracleRowsBySubject = (),
    subject_aliases: Sequence[str] = (),
) -> CellGrade:
    """Grade one cell from what its origins asserted."""
    kept, other_day = _narrow_to_anchor(policy, origins, policy.session_bound, lambda o: o.knowable_at, utc_day, False)
    kept, other_period = _narrow_to_anchor(policy, kept, policy.period_bound, lambda o: o.period_end, lambda d: d, True)
    valued = _newest_per_origin([origin for origin in kept if origin.value is not None])
    asserting = {origin.origin_id for origin in valued}
    values = {origin.origin_id: origin.value for origin in _newest_per_origin(origins)}
    values.update({origin.origin_id: origin.value for origin in valued})
    other_day_ids, other_period_ids = _dropped_ids(other_day, asserting), _dropped_ids(other_period, asserting)
    excluded_ids = tuple(sorted(other_day_ids + other_period_ids))
    asserted = tuple(sorted(origin.origin_id for origin in valued))
    independent = len({origin.lineage for origin in valued})

    delta = relative = comparison = outcome = None
    if not valued:
        reason = "no_origin_value"
    elif len(valued) == 1:
        reason = (
            SERVED_BY_FAILOVER
            if valued[0].served_by_failover
            else "second_origin_other_day"
            if other_day_ids
            else "second_origin_other_period"
            if other_period_ids
            else "single_origin"
        )
    else:
        comparison, numeric = _numeric_for_comparison(policy, valued)
        delta, relative = _deltas(policy, numeric, valued)
        if independent < 2:
            reason = "same_lineage"
        elif policy.reconciliation is None:
            reason = "no_agreement_policy"
        else:
            result = _reconcile(policy, subject_id, valued, numeric, cutoff)
            outcome = result.outcome.value
            if result.outcome is ReconciliationOutcome.AGREED:
                pair = (
                    policy.reconciliation.conflict_behavior is ConflictBehavior.MEDIAN_CONSENSUS
                    and len(result.agreeing_assertion_ids) < MEDIAN_QUORUM_GROUPS
                )
                reason = "two_origins_agree" if pair else "independent_origins_agree"
            else:
                reason = _OUTCOME_REASONS.get(result.outcome, "unknown")

    case_id = f"cell:{policy.family}:{subject_id}"
    if policy.semantic_type in RELEASE_SEMANTICS and valued and reason not in UNSERVED_REASONS:
        c_eval = evaluate_cell_confidence(case_id=case_id, definitional=True, policy=CONTINUOUS_CONFIDENCE_POLICY)
    elif not valued or reason in UNSERVED_REASONS:
        c_eval = evaluate_cell_confidence(
            case_id=case_id,
            has_value=False,
            extra_reason_codes=(f"reason:{reason}",),
            policy=CONTINUOUS_CONFIDENCE_POLICY,
        )
    else:
        is_conflict = reason in ("cross_origin_conflict", "not_agreed_within_tolerance")
        active_origins: Sequence[OriginValue] = (
            (min(valued, key=lambda origin: _priority_rank(policy, origin)),) if is_conflict else valued
        )
        sources: list[SourceConfidenceEvidence] = []
        for origin in active_origins:
            succ, fail = _lookup_outcome_mass(outcome_mass_by_origin, origin, family=policy.family)
            prior = next(
                (p for k in (origin.origin_id, origin.source_id, origin.lineage) if (p := source_reliability_prior(k))),
                None,
            )
            freshness = Decimal("1")
            if policy.session_bound and origin.knowable_at is not None:
                lag = max(0, (utc_day(cutoff) - utc_day(origin.knowable_at)).days)
                if lag > 4:
                    freshness = max(Decimal("0.20"), Decimal("1") - Decimal("0.05") * Decimal(lag - 4))
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
                    evidence_ids=(
                        origin.observation_id or _synthetic_observation_id(policy.family, subject_id, origin),
                    ),
                    reason_codes=(f"reason:{reason}",),
                )
            )
        multi_agreement = (
            Decimal("1")
            if (len(valued) == 1 or is_conflict)
            else max(Decimal("0.50"), Decimal("1") - min(Decimal("0.50"), _decimal(relative) or Decimal("0")))
        )
        oracle_agreement, oracle_reasons = oracle_cell_agreement(
            oracle_outcomes, subject_ids=(subject_id, *subject_aliases), field=policy.family, cutoff=cutoff
        )
        c_eval = evaluate_cell_confidence(
            case_id=case_id,
            sources=tuple(sources),
            agreement=min(multi_agreement, oracle_agreement),
            semantic_mapping_quality=Decimal("0.85") if reason == "no_agreement_policy" else Decimal("1"),
            extra_reason_codes=(f"reason:{reason}", *oracle_reasons),
            policy=CONTINUOUS_CONFIDENCE_POLICY,
        )
    return CellGrade(
        policy.family,
        subject_id,
        reason,
        asserted,
        independent,
        outcome,
        delta,
        relative,
        policy.reconciliation.policy_id if policy.reconciliation else None,
        comparison,
        excluded_ids,
        values,
        c_eval.confidence,
        c_eval.policy_hash,
        c_eval.input_hash,
        c_eval.reason_codes,
        next((o.observation_id for o in valued if o.observation_id), None),
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
    assertions: list[SourceAssertion] = []
    for o in origins:
        obs_id = o.observation_id or _synthetic_observation_id(policy.family, subject_id, o)
        cmp_val = numeric[o.origin_id]
        v_sha = canonical_sha256({"value": o.value if cmp_val is None else str(cmp_val)})
        assertions.append(
            SourceAssertion(
                cell_id=cell.cell_id,
                observation_id=obs_id,
                source_id=o.source_id,
                origin_group_id=o.origin_id,
                knowable_at=o.knowable_at or cutoff,
                normalized_value_sha256=v_sha,
                numeric_value=cmp_val,
                confidence_assessment_id=f"confidence-assessment:{v_sha}",
                lineage_node_ids=(obs_id,),
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
    compared = sum(1 for grade in rows if grade.outcome in _AGGREGATE_COMPARED_OUTCOMES)
    agreed = sum(1 for grade in rows if grade.outcome == ReconciliationOutcome.AGREED.value)
    reasons = Counter(grade.reason for grade in rows)
    mean_confidence = (
        str((sum((g.confidence for g in rows), Decimal(0)) / Decimal(total)).quantize(_RATIO_PLACES)) if total else "0"
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
    return (
        None
        if policy is None
        else {
            "policy_id": policy.policy_id,
            "policy_version": policy.policy_version,
            "source_priority": list(policy.source_priority),
            "absolute_tolerance": str(policy.absolute_tolerance),
            "relative_tolerance": str(policy.relative_tolerance),
            "minimum_independent_origin_groups": policy.minimum_independent_origin_groups,
        }
    )


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
    issuer_id: str | None, connection: Connection[Any] | None = None, cutoff: datetime | None = None
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
        if val and str(val).isdigit():
            return int(val)
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


def _headcount_origin(
    producer: str, value: str | None, knowable_at: datetime | None, observation_id: str | None = None
) -> OriginValue:
    return OriginValue(
        origin_id=f"origin:headcount:{producer}",
        source_id=producer,
        lineage=HEADCOUNT_LINEAGE.get(producer, producer),
        value=value,
        knowable_at=knowable_at,
        observation_id=observation_id,
    )


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
                    _headcount_origin(
                        producer, _text(payload.get(name)), observation.knowable_at, observation.observation_id
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


def _is_primary_parser(parser_version: str, *, allow_integration_parser: bool = False) -> bool:
    return parser_version in PARSER_VERSION_HISTORY or (
        allow_integration_parser and parser_version == "production-topt-integration-parser:v1"
    )


def load_run_subjects(
    connection: Connection[Any], run_id: str, cutoff: datetime, *, allow_integration_parser: bool = False
) -> dict[str, _Subject]:
    subjects: dict[str, _Subject] = {
        str(row[0]): _Subject()
        for row in connection.execute(
            "select distinct subject_id from raw.capture_obligations where run_id = %s order by subject_id", (run_id,)
        ).fetchall()
    }
    rows = connection.execute(
        f"select ob.subject_id, o.semantic_type, o.parser_version, o.knowable_at, o.observation_id, p.normalized_payload "
        f"{CAPTURE_OBSERVATION_JOIN} where ob.run_id = %s order by ob.subject_id, o.semantic_type, o.observation_id",
        (run_id,),
    ).fetchall()
    financial: dict[str, list[FinancialObservation]] = {}
    integration = (SEC_COMPANY_FACTS_SOURCE, SEC_COMPANY_FACTS_ORIGIN, "close")
    for sid_raw, sem, pver, know_at, obs_id, payload_raw in rows:
        subject = subjects.setdefault(str(sid_raw), _Subject())
        payload = payload_raw or {}
        if lid := payload.get("listing_id"):
            subject.listing_id = str(lid)
        if iid := payload.get("issuer_id"):
            subject.issuer_id = subject.issuer_id or iid
        if sem in RELEASE_SEMANTICS:
            subject.ticker = subject.ticker or payload.get("ticker")
        elif sem == MARKET_PRICE_SEMANTIC:
            coord = SOURCE_BY_PARSER.get(pver) or (
                integration if allow_integration_parser and pver == "production-topt-integration-parser:v1" else None
            )
            if coord is not None:
                for family, origin in bar_origins(coord, payload, knowable_at=know_at, observation_id=obs_id).items():
                    subject.add(family, origin)
        elif sem == "financial-fact":
            is_primary = _is_primary_parser(pver, allow_integration_parser=allow_integration_parser)
            coord = integration if is_primary else SOURCE_BY_PARSER.get(pver)
            if coord is not None:
                if (raw_branch := payload.get("operating_branch")) is not None and subject.operating_branch is None:
                    try:
                        subject.operating_branch = IssuerClass(str(raw_branch))
                    except ValueError:
                        pass
                financial.setdefault(str(sid_raw), []).append(
                    FinancialObservation(is_primary, coord[0], coord[1], payload, know_at, obs_id)
                )
    for sid, observations in financial.items():
        for family, family_origins in financial_origins(observations).items():
            for origin_value in family_origins:
                subjects[sid].add(family, origin_value)
    _add_headcount_producers(connection, subjects, cutoff)
    return subjects


def _add_headcount_producers(connection: Connection[Any], subjects: Mapping[str, _Subject], cutoff: datetime) -> None:
    ciks = {
        cik: sid
        for sid, subj in subjects.items()
        if (cik := _cik_of(subj.issuer_id, connection=connection, cutoff=cutoff)) is not None
    }
    if not ciks:
        return
    rows = connection.execute(
        "select cik, source, headcount, knowable_at, evidence_ref, period_end from staging.issuer_headcount_facts "
        "where cik = any(%s) and knowable_at <= %s order by cik, source, knowable_at desc, id desc",
        (list(ciks), cutoff),
    ).fetchall()
    for cik, producer, headcount, knowable_at, evidence_ref, period_end in rows:
        if is_eligible_headcount_row(
            knowable_at=knowable_at, source=str(producer), evidence_ref=str(evidence_ref or ""), period_end=period_end
        ):
            subject = subjects[ciks[int(cik)]]
            if not any(origin.source_id == producer for origin in subject.origins.get("headcount", [])):
                subject.add("headcount", _headcount_origin(str(producer), str(headcount), knowable_at))


def _universe_source(universe: str) -> UniverseSource | None:
    return UNIVERSE_SOURCES.get(key_of_lane(universe))


def load_index_membership(
    connection: Connection[Any], source: UniverseSource, cutoff: datetime
) -> tuple[dict[str, dict[str, list[OriginValue]]], dict[str, Any]]:
    constituents = connection.execute(
        "with q as (select ticker, weight, knowable_at, as_of from staging.etf_constituent_facts "
        "where etf_symbol = %s and source = %s and knowable_at <= %s) "
        "select ticker, weight, knowable_at from q where (as_of, knowable_at) = ("
        "select as_of, max(knowable_at) from q where as_of = (select max(as_of) from q) group by as_of) order by ticker",
        (source.etf, active_constituent_source(source), cutoff),
    ).fetchall()
    fund = connection.execute(
        "select e.entity_id::text from staging.entities e "
        "join staging.entity_aliases ea on ea.entity_id = e.entity_id "
        "where e.kind = 'fund' and ea.scheme in ('ticker', 'name', 'legacy-id') "
        "and upper(ea.value) in (%s, %s) "
        "order by (ea.scheme = 'ticker') desc, ea.transaction_time desc, ea.alias_id desc limit 1",
        (str(source.nport_ticker).upper(), f"ETF:SERIES:{str(source.nport_ticker).upper()}"),
    ).fetchone()
    holdings: list[Any] = []
    vintage: dict[str, Any] = {}
    if fund is not None:
        holdings = connection.execute(
            "with vintage as (select report_period, transaction_time from mart.fund_holdings_resolved "
            "where fund_id = %s and transaction_time <= %s order by transaction_time desc, report_period desc limit 1) "
            "select h.listing_id, h.holding_name, h.isin, h.percent_of_net_assets, h.report_period, h.transaction_time "
            "from mart.fund_holdings_resolved h join vintage using (report_period, transaction_time) "
            "where h.fund_id = %s order by h.listing_id nulls last, h.holding_name",
            (fund[0], cutoff, fund[0]),
        ).fetchall()
    origins: dict[str, dict[str, list[OriginValue]]] = {INDEX_MEMBERSHIP_FAMILY: {}, ETF_WEIGHT_FAMILY: {}}

    def assert_route(listing_id: str, origin_id: str, rank: int, weight: Any, knowable_at: datetime) -> None:
        src = INDEX_MEMBERSHIP_POLICY.source_priority[rank]
        lin = lineage_of(origin_id)
        w_val = None if weight is None else str(weight)
        mem_list = origins[INDEX_MEMBERSHIP_FAMILY].setdefault(listing_id, [])
        if not any(o.origin_id == origin_id for o in mem_list):
            mem_list.append(OriginValue(origin_id, src, lin, MEMBER, knowable_at))
        wt_list = origins[ETF_WEIGHT_FAMILY].setdefault(listing_id, [])
        existing_wt = next((i for i, o in enumerate(wt_list) if o.origin_id == origin_id), None)
        if existing_wt is None:
            wt_list.append(OriginValue(origin_id, src, lin, w_val, knowable_at))
        elif wt_list[existing_wt].value is None and w_val is not None:
            wt_list[existing_wt] = OriginValue(origin_id, src, lin, w_val, knowable_at)

    constituent_origin = NPORT_ORIGIN
    constituent_rank = 0
    mics = resolve_listing_mics(
        connection, source, [str(t) for t, _, _ in constituents], valid_at=cutoff.date(), known_at=cutoff
    )
    unresolved_exchange: list[str] = []
    for ticker, weight, knowable_at in constituents:
        if (mic := mics.get(str(ticker))) is None:
            unresolved_exchange.append(str(ticker))
        else:
            assert_route(
                f"listing:{mic}:{str(ticker).lower()}",
                constituent_origin,
                constituent_rank,
                weight,
                knowable_at,
            )
    unresolved: list[str] = []
    for listing_id, holding_name, _isin, weight, report_period, transaction_time in holdings:
        vintage = {"report_period": report_period.isoformat(), "filed": transaction_time.astimezone(UTC).isoformat()}
        if listing_id is None:
            unresolved.append(str(holding_name))
        else:
            assert_route(str(listing_id), NPORT_ORIGIN, 0, weight, transaction_time)
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
    for section, field_names, allow_flat_close in (
        ("reconciliation_cells", PRICE_BAR_FIELDS, True),
        ("financial_fact_reconciliation_cells", FINANCIAL_FACT_FUSION_FIELDS, False),
    ):
        for listing, cell in (payload.get(section) or {}).items():
            fields = cell.get("fields")
            graded = fields if isinstance(fields, Mapping) else ({CLOSE_FAMILY: cell} if allow_flat_close else {})
            for name in field_names:
                if (outcome := (graded.get(name) or {}).get("outcome")) is not None:
                    outcomes[name][str(listing)] = str(outcome)
    return outcomes


def load_quality_report_field_outcomes(
    connection: Connection[Any], run_id: str
) -> tuple[str | None, dict[str, dict[str, str]]]:
    row = connection.execute(
        "select report_id, jsonb_build_object('reconciliation_cells', payload->'reconciliation_cells', "
        "'financial_fact_reconciliation_cells', payload->'financial_fact_reconciliation_cells') "
        "from mart.datahub_quality_report where run_id = %s order by created_at desc limit 1",
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
    mismatches = sorted(k for k, out in persisted.items() if k in cells and _disagrees(cells[k].outcome, out))
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


def _subject_aliases(subject: _Subject | None) -> tuple[str, ...]:
    return (subject.issuer_id,) if subject is not None and subject.issuer_id is not None else ()


def _oracle_context(
    connection: Connection[Any], run_id: str, cutoff: datetime
) -> tuple[OracleRowsBySubject, dict[str, tuple[Decimal, Decimal]]]:
    oracle_rows = load_oracle_confidence_inputs(connection, run_id=run_id, cutoff=cutoff)
    return OracleRowsBySubject(oracle_rows), oracle_outcome_mass_by_origin(oracle_rows, cutoff=cutoff)


def build_report(
    connection: Connection[Any], *, universe: str, head: GovernedHead, executed_at: datetime, environment: str
) -> dict[str, Any]:
    cutoff = head.cutoff
    subjects = load_run_subjects(connection, head.run_id, cutoff)
    source = _universe_source(universe)
    membership_notes: dict[str, Any] | None = None
    plane_origins: dict[str, dict[str, list[OriginValue]]] = {}
    if source is not None and source.nport_ticker is not None:
        plane_origins, membership_notes = load_index_membership(connection, source, cutoff)
    oracle_rows, outcome_mass_by_origin = _oracle_context(connection, head.run_id, cutoff)

    grades: dict[str, dict[str, CellGrade]] = {}
    for policy in FAMILIES:
        if policy.family in PLANE_FAMILIES:
            if membership_notes is None:
                continue
            family_origins = plane_origins[policy.family]
            origins_by_subject = {listing: family_origins[listing] for listing in sorted(family_origins)}
        else:
            origins_by_subject = {sid: subj.origins.get(policy.family, []) for sid, subj in sorted(subjects.items())}
        grades[policy.family] = {
            sid: classify_cell(
                policy,
                sid,
                origins,
                cutoff,
                outcome_mass_by_origin=outcome_mass_by_origin,
                oracle_outcomes=oracle_rows,
                subject_aliases=_subject_aliases(subjects.get(sid)),
            )
            for sid, origins in origins_by_subject.items()
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
    accuracy = {
        name: field_accuracy(
            family_policy(name),
            families.get(name, {}),
            grades.get(name, {}),
            quality_report_id=quality_report_id,
            persisted=persisted_outcomes.get(name, {}),
        )
        for name in CROSS_CHECKED_FAMILIES
    }
    all_origins = [o for s in subjects.values() for fam in s.origins.values() for o in fam]
    all_origins.extend(o for fo in plane_origins.values() for lo in fo.values() for o in lo)
    lineages = sorted({o.lineage for o in all_origins if o.value is not None})
    tier_counts = {
        name: {
            "high": sum(1 for grade in cells.values() if grade.confidence >= Decimal("0.80")),
            "medium": sum(1 for grade in cells.values() if Decimal("0.60") <= grade.confidence < Decimal("0.80")),
            "low": sum(1 for grade in cells.values() if grade.confidence < Decimal("0.60")),
        }
        for name, cells in grades.items()
    }
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
        "tier_counts": tier_counts,
        "cells": {name: {sid: grade.payload() for sid, grade in cells.items()} for name, cells in grades.items()},
        "sample": sample,
        "accuracy": accuracy,
        "metadata": {"quality_report_id": quality_report_id},
    }


def compile_report(connection: Connection[Any], *, universe: str, executed_at: datetime) -> dict[str, Any] | None:
    head = governed_head_of(connection, key_of_lane(universe))
    if isinstance(head, NoHead):
        return None
    return build_report(
        connection, universe=universe, head=head, executed_at=executed_at, environment=declared_environment(connection)
    )


def _cell_confidence_row(
    run_id: str,
    subject_id: str,
    semantic_type: str,
    metric: str,
    observation_id: str | None,
    confidence: Decimal,
    policy_hash: str | None,
    input_hash: str | None,
    reason_codes: Sequence[str],
) -> tuple[Any, ...]:
    return (
        run_id,
        subject_id,
        semantic_type,
        metric,
        observation_id,
        confidence,
        policy_hash or CONTINUOUS_CONFIDENCE_POLICY.content_sha256,
        input_hash or canonical_sha256({"family": metric, "subject_id": subject_id, "confidence": str(confidence)}),
        list(reason_codes),
    )


def persist(connection: Connection[Any], report: Mapping[str, Any]) -> str:
    report_id = persist_run_report(
        connection,
        report,
        table="mart.datahub_confidence_report",
        report_id_prefix=REPORT_ID_PREFIX,
        columns=("universe_id", "run_id", "cutoff"),
        exclude_from_hash=COMPILE_STAMPS,
    )
    cells_by_family = report.get("cells")
    if isinstance(cells_by_family, Mapping) and cells_by_family:
        families_meta: Mapping[str, Any] = report["families"] if isinstance(report.get("families"), Mapping) else {}
        run_id = str(report["run_id"])
        rows = [
            _cell_confidence_row(
                run_id,
                str(sid),
                str(
                    (fmeta.get("semantic_type") if isinstance((fmeta := families_meta.get(fname)), Mapping) else None)
                    or getattr(_FAMILY_BY_NAME.get(fname), "semantic_type", "unknown")
                ),
                str(fname),
                str(cell["observation_id"]) if cell.get("observation_id") else None,
                Decimal(str(cell.get("confidence", "0"))),
                cell.get("policy_hash"),
                cell.get("input_hash"),
                [str(code) for code in (cell.get("reason_codes") or [f"reason:{cell.get('reason', 'unknown')}"])],
            )
            for fname, fcells in sorted(cells_by_family.items())
            if isinstance(fcells, Mapping)
            for sid, cell in sorted(fcells.items())
            if isinstance(cell, Mapping)
        ]
        execute_many(connection, _CELL_CONFIDENCE_INSERT_SQL, rows)
    return report_id


_SINGLE_ORIGIN_FINANCIAL_FIELDS: tuple[str, ...] = tuple(f for f in snapshot_field_specs() if f not in FINANCIAL_FIELDS)


def evaluate_and_persist_run_cell_confidence(
    connection: Connection[Any], run_id: str, *, cutoff: datetime | None = None
) -> dict[tuple[str, str], Decimal]:
    if not hasattr(connection, "cursor"):
        return {}
    if cutoff is None:
        cutoff = fetch_run_cutoff(connection, run_id) or datetime.now(UTC)
    subjects = load_run_subjects(connection, run_id, cutoff, allow_integration_parser=True)
    oracle_rows, outcome_mass_by_origin = _oracle_context(connection, run_id, cutoff)
    out: dict[tuple[str, str], Decimal] = {}
    pending_rows: list[tuple[Any, ...]] = []

    def _record(
        subject_id: str,
        semantic_type: str,
        metric: str,
        grade: Any,
        listing_alias: str | None = None,
        observation_id: str | None = None,
    ) -> None:
        out[(subject_id, metric)] = grade.confidence
        if listing_alias and listing_alias != subject_id:
            out[(listing_alias, metric)] = grade.confidence
        pending_rows.append(
            _cell_confidence_row(
                run_id,
                subject_id,
                semantic_type,
                metric,
                grade.observation_id if observation_id is None else observation_id,
                grade.confidence,
                grade.policy_hash,
                grade.input_hash,
                grade.reason_codes,
            )
        )

    for policy in FAMILIES:
        if policy.family in PLANE_FAMILIES:
            continue
        for sid, subj in sorted(subjects.items()):
            if _subject_applies(subj, policy):
                grd = classify_cell(
                    policy,
                    sid,
                    subj.origins.get(policy.family, []),
                    cutoff,
                    outcome_mass_by_origin=outcome_mass_by_origin,
                    oracle_outcomes=oracle_rows,
                    subject_aliases=_subject_aliases(subj),
                )
                _record(sid, policy.semantic_type, policy.family, grd, subj.listing_id)

    sec_lineage = lineage_of(SEC_COMPANY_FACTS_ORIGIN)
    obs_rows = connection.execute(
        f"select ob.subject_id, o.semantic_type, o.observation_id, o.parser_version, p.normalized_payload "
        f"{CAPTURE_OBSERVATION_JOIN} where ob.run_id = %s and o.semantic_type in ('listing-identity', 'universe-membership', 'financial-fact') "
        "order by ob.subject_id, o.semantic_type, o.observation_id",
        (run_id,),
    ).fetchall()
    for s_raw, sem_raw, obs_raw, pver_raw, p_raw in obs_rows:
        sid, sem, obs_id = str(s_raw), str(sem_raw), str(obs_raw)
        payload = p_raw or {}
        alias = str(payload["listing_id"]) if payload.get("listing_id") else None
        if sem in RELEASE_SEMANTICS:
            _record(
                sid,
                sem,
                sem,
                evaluate_cell_confidence(
                    case_id=f"cell:{sem}:{sid}", definitional=True, policy=CONTINUOUS_CONFIDENCE_POLICY
                ),
                alias,
                obs_id,
            )
        elif sem == "financial-fact" and _is_primary_parser(str(pver_raw), allow_integration_parser=True):
            aliases = _subject_aliases(subjects.get(sid))
            for fld in _SINGLE_ORIGIN_FINANCIAL_FIELDS:
                if payload.get(fld) is None:
                    continue
                orig = OriginValue(
                    SEC_COMPANY_FACTS_ORIGIN,
                    SEC_COMPANY_FACTS_SOURCE,
                    sec_lineage,
                    str(payload[fld]),
                    observation_id=obs_id,
                )
                grd = classify_cell(
                    FamilyPolicy(fld, "financial-fact", (fld,), None),
                    sid,
                    (orig,),
                    cutoff,
                    outcome_mass_by_origin=outcome_mass_by_origin,
                    oracle_outcomes=oracle_rows,
                    subject_aliases=aliases,
                )
                _record(sid, "financial-fact", fld, grd, alias)

    execute_many(connection, _CELL_CONFIDENCE_INSERT_SQL, pending_rows)
    for subject_id_raw, metric_raw, conf_raw in connection.execute(
        "select subject_id, metric, confidence from mart.cell_confidence where run_id = %s", (run_id,)
    ).fetchall():
        out[(str(subject_id_raw), str(metric_raw))] = Decimal(str(conf_raw))
    return out


def summary_line(report: Mapping[str, Any]) -> str:
    parts = [
        f"{name}: {fam['cells']} cells ({', '.join(f'{c} with {n}' for n, c in fam['by_independent_origins'].items())} independent origins), mean confidence {fam['mean_confidence']}"
        + (f", agreement {r}" if (r := fam.get("agreement_rate")) is not None else "")
        for name, fam in report["families"].items()
    ]
    return f"{report['universe_id']} @ {report['cutoff']}: " + "; ".join(parts)
