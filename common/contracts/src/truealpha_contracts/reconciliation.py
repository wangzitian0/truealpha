"""Point-in-time multi-source reconciliation contracts."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import (
    MAX_EMAX,
    MIN_EMIN,
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    DivisionByZero,
    Inexact,
    InvalidOperation,
    Overflow,
    Rounded,
    localcontext,
)
from enum import StrEnum
from itertools import combinations
from typing import Any

from pydantic import Field, field_validator

from truealpha_contracts.common import (
    SHA256_PATTERN,
    AwareDatetime,
    ContentHash,
    IdentifiedFrozenModel,
    ImmutableId,
    SafeDecimal,
    Sha256,
    canonical_sha256,
    content_id,
    content_ref,
    require_aware,
    require_stable_and_immutable,
)
from truealpha_contracts.universe import SubjectRef

_CONTENT_ID = r"^[a-z][a-z0-9-]*:[0-9a-f]{64}$"


def _decimal_shape(value: Decimal) -> tuple[int, int]:
    _, digits, exponent = value.as_tuple()
    if not isinstance(exponent, int):
        raise ValueError("exact Decimal arithmetic requires finite values")
    return len(digits), exponent


def _exact_context(precision: int) -> Context:
    return Context(
        prec=max(1, precision),
        rounding=ROUND_HALF_EVEN,
        Emin=MIN_EMIN,
        Emax=MAX_EMAX,
        capitals=1,
        clamp=0,
        flags=[],
        traps=[InvalidOperation, DivisionByZero, Overflow, Inexact, Rounded],
    )


def _exact_additive_context(left: Decimal, right: Decimal) -> Context:
    ld, le = _decimal_shape(left)
    rd, re_exp = _decimal_shape(right)
    common = min(le, re_exp)
    return _exact_context(max(ld + le - common, rd + re_exp - common) + 1)


def _exact_subtract(left: Decimal, right: Decimal) -> Decimal:
    with localcontext(_exact_additive_context(left, right)):
        return left - right


def _exact_add(left: Decimal, right: Decimal) -> Decimal:
    with localcontext(_exact_additive_context(left, right)):
        return left + right


def _exact_multiply(left: Decimal, right: Decimal) -> Decimal:
    ld, _ = _decimal_shape(left)
    rd, _ = _decimal_shape(right)
    with localcontext(_exact_context(ld + rd)):
        return left * right


def _sorted_unique(values: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} must not contain duplicates")
    return tuple(sorted(values))


class ConflictBehavior(StrEnum):
    REPORT_AND_ABSTAIN = "report_and_abstain"
    MEDIAN_CONSENSUS = "median_consensus"


class ReconciliationOutcome(StrEnum):
    AGREED = "agreed"
    INSUFFICIENT_INDEPENDENT_ORIGINS = "insufficient_independent_origins"
    CONFLICT_ABSTAINED = "conflict_abstained"
    CONFLICT_PRIORITY_SERVED = "conflict_priority_served"
    NOT_YET_KNOWABLE = "not_yet_knowable"
    UNAVAILABLE = "unavailable"


_CONFLICT_OUTCOMES = frozenset(
    {ReconciliationOutcome.CONFLICT_ABSTAINED, ReconciliationOutcome.CONFLICT_PRIORITY_SERVED}
)
_ABSTAIN_OR_UNAVAILABLE = frozenset(
    {
        ReconciliationOutcome.CONFLICT_ABSTAINED,
        ReconciliationOutcome.NOT_YET_KNOWABLE,
        ReconciliationOutcome.UNAVAILABLE,
    }
)
_SELECTED_OUTCOMES = frozenset(
    {
        ReconciliationOutcome.AGREED,
        ReconciliationOutcome.INSUFFICIENT_INDEPENDENT_ORIGINS,
        ReconciliationOutcome.CONFLICT_PRIORITY_SERVED,
    }
)

CONSENSUS_RELATIVE_TOLERANCE = Decimal("0.001")
MEDIAN_QUORUM_GROUPS = 3
PAIR_QUORUM_GROUPS = 2


@dataclass(frozen=True)
class ConsensusVote:
    group_id: str
    rank: int
    value: Decimal | str


@dataclass(frozen=True)
class ConsensusGrade:
    served_value: Decimal | str | None
    served_group_id: str | None
    agreeing_group_ids: tuple[str, ...]
    outlier_group_ids: tuple[str, ...]


def _consensus_value(
    values: list[Decimal | str], relative_tolerance: Decimal, absolute_tolerance: Decimal
) -> tuple[Decimal | str, Decimal] | None:
    if all(isinstance(v, str) for v in values):
        return (values[0], Decimal(0)) if len(set(values)) == 1 else None
    numbers = sorted(v for v in values if isinstance(v, Decimal))
    if len(numbers) != len(values):
        return None
    mid = len(numbers) // 2
    median = (
        numbers[mid]
        if len(numbers) % 2 or numbers[mid - 1] == numbers[mid]
        else _exact_add(numbers[mid - 1], numbers[mid]) / Decimal(2)
    )
    bound = _exact_add(absolute_tolerance, _exact_multiply(relative_tolerance, median.copy_abs()))
    spread = max(_exact_subtract(v, median).copy_abs() for v in numbers)
    return (median, spread) if spread <= bound else None


def grade_consensus(
    votes: tuple[ConsensusVote, ...] | list[ConsensusVote],
    *,
    relative_tolerance: Decimal = CONSENSUS_RELATIVE_TOLERANCE,
    absolute_tolerance: Decimal = Decimal(0),
) -> ConsensusGrade:
    if not votes:
        return ConsensusGrade(None, None, (), ())
    group_ids = [v.group_id for v in votes]
    if len(group_ids) != len(set(group_ids)):
        raise ValueError("each independent origin group votes once")
    ordered = sorted(votes, key=lambda v: (v.rank, v.group_id))
    for size in range(len(ordered), PAIR_QUORUM_GROUPS - 1, -1):
        best: tuple[Decimal, Decimal | str, tuple[ConsensusVote, ...]] | None = None
        for members in combinations(ordered, size):
            agreed = _consensus_value([v.value for v in members], relative_tolerance, absolute_tolerance)
            if agreed is not None and (best is None or agreed[1] < best[0]):
                best = (agreed[1], agreed[0], members)
        if best is not None:
            _, served, members = best
            chosen = {v.group_id for v in members}
            return ConsensusGrade(
                served, members[0].group_id, tuple(sorted(chosen)), tuple(sorted(set(group_ids) - chosen))
            )
    p = ordered[0]
    return ConsensusGrade(p.value, p.group_id, (p.group_id,), tuple(sorted(set(group_ids) - {p.group_id})))


class ReconciliationCell(IdentifiedFrozenModel):
    id_field = "cell_id"
    id_prefix = "reconciliation-cell"
    identity_fields = (
        "requirement_id",
        "subject",
        "field_name",
        "field_semantics_id",
        "unit",
        "valid_from",
        "valid_to",
    )
    cell_id: str = content_id("reconciliation-cell")
    content_sha256: ContentHash = ""
    requirement_id: str = Field(pattern=_CONTENT_ID)
    subject: SubjectRef
    field_name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    field_semantics_id: str = Field(pattern=_CONTENT_ID)
    unit: str = Field(min_length=1)
    valid_from: date
    valid_to: date

    def _canonicalize(self) -> None:
        if self.valid_to < self.valid_from:
            raise ValueError("valid_to cannot precede valid_from")


class SourceAssertion(IdentifiedFrozenModel):
    id_field = "assertion_id"
    id_prefix = "source-assertion"
    identity_fields = (
        "cell_id",
        "observation_id",
        "source_id",
        "origin_group_id",
        "knowable_at",
        "normalized_value_sha256",
        "numeric_value",
        "confidence_assessment_id",
    )
    assertion_id: str = content_id("source-assertion")
    content_sha256: ContentHash = ""
    cell_id: str = content_ref("reconciliation-cell")
    observation_id: str = content_ref("normalized-observation")
    source_id: ImmutableId
    origin_group_id: ImmutableId
    knowable_at: AwareDatetime
    normalized_value_sha256: Sha256
    numeric_value: SafeDecimal | None = None
    confidence_assessment_id: str = content_ref("confidence-assessment")
    lineage_node_ids: tuple[str, ...] = Field(min_length=1)
    lineage_complete: bool

    @field_validator("lineage_node_ids")
    @classmethod
    def normalize_lineage(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        values = _sorted_unique(values, "lineage_node_ids")
        for v in values:
            require_stable_and_immutable(v, "lineage_node_ids")
        return values


class ReconciliationPolicy(IdentifiedFrozenModel):
    id_field = "policy_id"
    id_prefix = "reconciliation-policy"
    identity_fields = (
        "policy_version",
        "source_priority",
        "absolute_tolerance",
        "relative_tolerance",
        "minimum_independent_origin_groups",
        "conflict_behavior",
    )
    policy_id: str = content_id("reconciliation-policy")
    content_sha256: ContentHash = ""
    policy_version: ImmutableId
    source_priority: tuple[str, ...] = Field(min_length=1)
    absolute_tolerance: SafeDecimal = Field(ge=0)
    relative_tolerance: SafeDecimal = Field(ge=0)
    minimum_independent_origin_groups: int = Field(default=2, ge=1)
    conflict_behavior: ConflictBehavior = ConflictBehavior.REPORT_AND_ABSTAIN

    @field_validator("source_priority")
    @classmethod
    def normalize_source_priority(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("source_priority must not contain duplicates")
        for v in values:
            require_stable_and_immutable(v, "source_priority")
        return values

    def _canonicalize(self) -> None:
        if (
            self.conflict_behavior is ConflictBehavior.MEDIAN_CONSENSUS
            and self.minimum_independent_origin_groups != PAIR_QUORUM_GROUPS
        ):
            raise ValueError("a median-consensus policy corroborates at exactly two agreeing origin groups")


class ReconciliationResult(IdentifiedFrozenModel):
    id_field = "result_id"
    id_prefix = "reconciliation-result"
    identity_fields = ("cell_id", "policy_id", "cutoff", "assertion_ids")
    result_id: str = content_id("reconciliation-result")
    content_sha256: ContentHash = ""
    cell_id: str = content_ref("reconciliation-cell")
    policy_id: str = content_ref("reconciliation-policy")
    minimum_independent_origin_groups: int = Field(ge=1)
    cutoff: AwareDatetime
    outcome: ReconciliationOutcome
    assertion_ids: tuple[str, ...]
    eligible_assertion_ids: tuple[str, ...]
    future_assertion_ids: tuple[str, ...]
    unregistered_assertion_ids: tuple[str, ...]
    representative_assertion_ids: tuple[str, ...]
    agreeing_assertion_ids: tuple[str, ...]
    conflicting_assertion_ids: tuple[str, ...]
    origin_group_ids: tuple[str, ...]
    comparison_anchor_assertion_id: str | None = content_ref("source-assertion", default=None)
    selected_assertion_id: str | None = content_ref("source-assertion", default=None)
    selected_value_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    selected_numeric_value: SafeDecimal | None = None
    lineage_complete: bool
    reason_codes: tuple[str, ...] = Field(min_length=1)

    @field_validator(
        "assertion_ids",
        "eligible_assertion_ids",
        "future_assertion_ids",
        "unregistered_assertion_ids",
        "representative_assertion_ids",
        "agreeing_assertion_ids",
        "conflicting_assertion_ids",
        "origin_group_ids",
    )
    @classmethod
    def normalize_sets(cls, values: tuple[str, ...], info: Any) -> tuple[str, ...]:
        return _sorted_unique(values, info.field_name)

    @field_validator("reason_codes")
    @classmethod
    def normalize_reasons(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        values = _sorted_unique(values, "reason_codes")
        if not values or any(re.fullmatch(r"^[a-z][a-z0-9_.-]*$", v) is None for v in values):
            raise ValueError("reason_codes must contain stable machine-readable codes")
        return values

    def _validate_selection(self, representative_ids: set[str], agreeing_ids: set[str]) -> None:
        selected = self.selected_assertion_id is not None
        if selected != (self.selected_value_sha256 is not None):
            raise ValueError("selected assertion and value hash must be present together")
        if self.selected_numeric_value is not None and not selected:
            raise ValueError("selected numeric value requires a selected assertion")
        anchored = self.comparison_anchor_assertion_id is not None
        if anchored != bool(representative_ids):
            raise ValueError("comparison anchor must be present exactly when representatives exist")
        if anchored and self.comparison_anchor_assertion_id not in agreeing_ids:
            raise ValueError("comparison anchor must be an agreeing representative")
        if selected and (
            self.selected_assertion_id not in representative_ids or self.selected_assertion_id not in agreeing_ids
        ):
            raise ValueError("selected assertion must be an agreeing representative")
        if selected and self.selected_assertion_id != self.comparison_anchor_assertion_id:
            raise ValueError("selected assertion must equal the comparison anchor")
        if self.outcome in _ABSTAIN_OR_UNAVAILABLE and selected:
            raise ValueError("abstained or unavailable reconciliation cannot select a value")
        if self.outcome in _SELECTED_OUTCOMES and not selected:
            raise ValueError("an agreed, insufficient-origin, or priority-served result must select an assertion")

    def _validate_outcome_rules(
        self,
        eligible_ids: set[str],
        future_ids: set[str],
        representative_ids: set[str],
        agreeing_ids: set[str],
        conflicting_ids: set[str],
    ) -> None:
        if self.outcome is ReconciliationOutcome.AGREED:
            if len(self.origin_group_ids) < self.minimum_independent_origin_groups:
                raise ValueError("agreement does not meet the bound independent-origin threshold")
            if conflicting_ids and len(agreeing_ids) < PAIR_QUORUM_GROUPS:
                raise ValueError(
                    "an agreed result with conflicting assertions needs at least two agreeing origin groups"
                )
        elif self.outcome is ReconciliationOutcome.INSUFFICIENT_INDEPENDENT_ORIGINS:
            if len(self.origin_group_ids) >= self.minimum_independent_origin_groups:
                raise ValueError("an insufficient-origin result meets its bound threshold")
            if conflicting_ids:
                raise ValueError("an insufficient-origin result cannot contain conflicting assertions")
        elif self.outcome in _CONFLICT_OUTCOMES:
            if not agreeing_ids or not conflicting_ids or len(self.origin_group_ids) < 2:
                raise ValueError("a conflict requires an agreeing anchor and a disagreeing origin")
            if self.outcome is ReconciliationOutcome.CONFLICT_PRIORITY_SERVED and len(agreeing_ids) != 1:
                raise ValueError("a priority-served conflict serves one origin group")
        else:
            if eligible_ids or representative_ids or self.origin_group_ids:
                raise ValueError("an unavailable result cannot contain eligible representatives")
            if self.outcome is ReconciliationOutcome.NOT_YET_KNOWABLE and not future_ids:
                raise ValueError("a not-yet-knowable result requires future assertions")
            if self.outcome is ReconciliationOutcome.UNAVAILABLE and future_ids:
                raise ValueError("an unavailable result cannot contain future assertions")

    def _canonicalize(self) -> None:
        assertion_ids = set(self.assertion_ids)
        eligible_ids, future_ids, unregistered_ids = (
            set(self.eligible_assertion_ids),
            set(self.future_assertion_ids),
            set(self.unregistered_assertion_ids),
        )
        representative_ids, agreeing_ids, conflicting_ids = (
            set(self.representative_assertion_ids),
            set(self.agreeing_assertion_ids),
            set(self.conflicting_assertion_ids),
        )
        if future_ids & eligible_ids or future_ids & unregistered_ids or eligible_ids & unregistered_ids:
            raise ValueError("future, eligible, and unregistered assertions must be disjoint")
        if future_ids | eligible_ids | unregistered_ids != assertion_ids:
            raise ValueError("assertion classifications must exactly cover every assertion")
        if not representative_ids <= eligible_ids:
            raise ValueError("representative assertions must be eligible")
        if agreeing_ids & conflicting_ids or agreeing_ids | conflicting_ids != representative_ids:
            raise ValueError("agreeing and conflicting assertions must partition representatives")
        if len(self.origin_group_ids) != len(self.representative_assertion_ids):
            raise ValueError("each representative must correspond to one independent origin group")
        self._validate_selection(representative_ids, agreeing_ids)
        self._validate_outcome_rules(eligible_ids, future_ids, representative_ids, agreeing_ids, conflicting_ids)


def _assertions_agree(left: SourceAssertion, right: SourceAssertion, policy: ReconciliationPolicy) -> bool:
    if left.numeric_value is None or right.numeric_value is None:
        return (
            left.numeric_value is None
            and right.numeric_value is None
            and left.normalized_value_sha256 == right.normalized_value_sha256
        )
    diff = _exact_subtract(left.numeric_value, right.numeric_value).copy_abs()
    scale = max(left.numeric_value.copy_abs(), right.numeric_value.copy_abs())
    return diff <= _exact_add(policy.absolute_tolerance, _exact_multiply(policy.relative_tolerance, scale))


def _build_result(
    cell: ReconciliationCell,
    policy: ReconciliationPolicy,
    cutoff: datetime,
    ordered: Sequence[SourceAssertion],
    eligible: Sequence[SourceAssertion],
    future: Sequence[SourceAssertion],
    unregistered: Sequence[SourceAssertion],
    representatives: Sequence[SourceAssertion],
    origins: tuple[str, ...],
    outcome: ReconciliationOutcome,
    agreeing_ids: tuple[str, ...],
    conflicting_ids: tuple[str, ...],
    anchor_id: str | None,
    selected_id: str | None,
    selected_sha256: str | None,
    selected_numeric: Decimal | None,
    reasons: tuple[str, ...],
) -> ReconciliationResult:
    return ReconciliationResult(
        cell_id=cell.cell_id,
        policy_id=policy.policy_id,
        minimum_independent_origin_groups=policy.minimum_independent_origin_groups,
        cutoff=cutoff,
        outcome=outcome,
        assertion_ids=tuple(i.assertion_id for i in ordered),
        eligible_assertion_ids=tuple(i.assertion_id for i in eligible),
        future_assertion_ids=tuple(i.assertion_id for i in future),
        unregistered_assertion_ids=tuple(i.assertion_id for i in unregistered),
        representative_assertion_ids=tuple(i.assertion_id for i in representatives),
        origin_group_ids=origins,
        lineage_complete=bool(representatives) and all(i.lineage_complete for i in representatives),
        agreeing_assertion_ids=agreeing_ids,
        conflicting_assertion_ids=conflicting_ids,
        comparison_anchor_assertion_id=anchor_id,
        selected_assertion_id=selected_id,
        selected_value_sha256=selected_sha256,
        selected_numeric_value=selected_numeric,
        reason_codes=reasons,
    )


def reconcile_source_assertions(
    *,
    cell: ReconciliationCell,
    assertions: tuple[SourceAssertion, ...],
    policy: ReconciliationPolicy,
    cutoff: datetime,
) -> ReconciliationResult:
    cutoff = require_aware(cutoff, "cutoff")
    if any(a.cell_id != cell.cell_id for a in assertions):
        raise ValueError("every assertion must belong to the reconciled cell")
    if len({a.assertion_id for a in assertions}) != len(assertions):
        raise ValueError("assertions must not contain duplicates")

    priority = {source_id: rank for rank, source_id in enumerate(policy.source_priority)}
    ordered = tuple(sorted(assertions, key=lambda i: i.assertion_id))
    future = tuple(i for i in ordered if i.knowable_at > cutoff)
    knowable = tuple(i for i in ordered if i.knowable_at <= cutoff)
    unregistered = tuple(i for i in knowable if i.source_id not in priority)
    eligible = tuple(i for i in knowable if i.source_id in priority)

    representatives: list[SourceAssertion] = []
    for origin_group_id in sorted({i.origin_group_id for i in eligible}):
        cands = [i for i in eligible if i.origin_group_id == origin_group_id]
        best_rank = min(priority[i.source_id] for i in cands)
        representatives.append(
            max((c for c in cands if priority[c.source_id] == best_rank), key=lambda c: (c.knowable_at, c.assertion_id))
        )

    reasons: list[str] = []
    if future:
        reasons.append("reconciliation.future_knowledge_excluded")
    if unregistered:
        reasons.append("reconciliation.unregistered_source_excluded")

    if not representatives:
        return _build_result(
            cell,
            policy,
            cutoff,
            ordered,
            (),
            future,
            unregistered,
            (),
            (),
            ReconciliationOutcome.NOT_YET_KNOWABLE if future else ReconciliationOutcome.UNAVAILABLE,
            (),
            (),
            None,
            None,
            None,
            None,
            ("reconciliation.no_eligible_assertion", *reasons),
        )

    origins = tuple(i.origin_group_id for i in representatives)
    if len(eligible) > len(representatives):
        reasons.append("reconciliation.same_origin_deduplicated")
    if policy.conflict_behavior is ConflictBehavior.MEDIAN_CONSENSUS:
        return _median_consensus_result(
            representatives, priority, policy, cell, cutoff, ordered, eligible, future, unregistered, origins, reasons
        )

    selected_rank = min(priority[i.source_id] for i in representatives)
    selected = max(
        (i for i in representatives if priority[i.source_id] == selected_rank),
        key=lambda i: (i.knowable_at, i.assertion_id),
    )
    agreeing = [i for i in representatives if _assertions_agree(selected, i, policy)]
    conflicting = [i for i in representatives if i not in agreeing]

    if conflicting:
        outcome, selected_result = ReconciliationOutcome.CONFLICT_ABSTAINED, None
        reasons.append("reconciliation.cross_origin_conflict")
    elif len(origins) < policy.minimum_independent_origin_groups:
        outcome, selected_result = ReconciliationOutcome.INSUFFICIENT_INDEPENDENT_ORIGINS, selected
        reasons.append("reconciliation.insufficient_independent_origins")
    else:
        outcome, selected_result = ReconciliationOutcome.AGREED, selected
        reasons.append("reconciliation.independent_origins_agree")

    return _build_result(
        cell,
        policy,
        cutoff,
        ordered,
        eligible,
        future,
        unregistered,
        representatives,
        origins,
        outcome,
        tuple(i.assertion_id for i in agreeing),
        tuple(i.assertion_id for i in conflicting),
        selected.assertion_id,
        None if selected_result is None else selected_result.assertion_id,
        None if selected_result is None else selected_result.normalized_value_sha256,
        None if selected_result is None else selected_result.numeric_value,
        tuple(reasons or ("reconciliation.selected",)),
    )


def _median_consensus_result(
    representatives: list[SourceAssertion],
    priority: dict[str, int],
    policy: ReconciliationPolicy,
    cell: ReconciliationCell,
    cutoff: datetime,
    ordered: Sequence[SourceAssertion],
    eligible: Sequence[SourceAssertion],
    future: Sequence[SourceAssertion],
    unregistered: Sequence[SourceAssertion],
    origins: tuple[str, ...],
    reasons: list[str],
) -> ReconciliationResult:
    by_group = {i.origin_group_id: i for i in representatives}
    grade = grade_consensus(
        [
            ConsensusVote(
                i.origin_group_id,
                priority[i.source_id],
                i.normalized_value_sha256 if i.numeric_value is None else i.numeric_value,
            )
            for i in representatives
        ],
        relative_tolerance=policy.relative_tolerance,
        absolute_tolerance=policy.absolute_tolerance,
    )
    assert grade.served_group_id is not None
    selected = by_group[grade.served_group_id]
    if len(grade.agreeing_group_ids) >= PAIR_QUORUM_GROUPS:
        outcome = ReconciliationOutcome.AGREED
        reasons.append(
            "reconciliation.three_origins_agree"
            if len(grade.agreeing_group_ids) >= MEDIAN_QUORUM_GROUPS
            else "reconciliation.two_origins_agree"
        )
    elif len(representatives) == 1:
        outcome = ReconciliationOutcome.INSUFFICIENT_INDEPENDENT_ORIGINS
        reasons.append("reconciliation.insufficient_independent_origins")
    else:
        outcome = ReconciliationOutcome.CONFLICT_PRIORITY_SERVED
        reasons.append("reconciliation.cross_origin_conflict")
    if grade.outlier_group_ids and outcome is ReconciliationOutcome.AGREED:
        reasons.append("reconciliation.outlier_recorded")
    served_sha = (
        grade.served_value
        if isinstance(grade.served_value, str)
        else (
            selected.normalized_value_sha256
            if selected.numeric_value is not None and grade.served_value == selected.numeric_value
            else canonical_sha256(str(grade.served_value))
        )
    )
    return _build_result(
        cell,
        policy,
        cutoff,
        ordered,
        eligible,
        future,
        unregistered,
        representatives,
        origins,
        outcome,
        tuple(by_group[g].assertion_id for g in grade.agreeing_group_ids),
        tuple(by_group[g].assertion_id for g in grade.outlier_group_ids),
        selected.assertion_id,
        selected.assertion_id,
        served_sha,
        grade.served_value if isinstance(grade.served_value, Decimal) else None,
        tuple(reasons),
    )
