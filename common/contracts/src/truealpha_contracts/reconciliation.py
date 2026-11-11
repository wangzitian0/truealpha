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
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from truealpha_contracts.common import STABLE_ID_PATTERN, canonical_sha256, require_stable_and_immutable
from truealpha_contracts.common import identify_by_grain as _freeze_content
from truealpha_contracts.models import _require_aware
from truealpha_contracts.universe import SubjectRef

_SHA256 = r"^[0-9a-f]{64}$"
_CONTENT_ID = r"^[a-z][a-z0-9-]*:[0-9a-f]{64}$"
_MUTABLE_TOKENS = frozenset({"latest", "current", "default", "stable", "main", "head"})


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
    left_digits, left_exponent = _decimal_shape(left)
    right_digits, right_exponent = _decimal_shape(right)
    common_exponent = min(left_exponent, right_exponent)
    precision = max(left_digits + left_exponent - common_exponent, right_digits + right_exponent - common_exponent) + 1
    return _exact_context(precision)


def _exact_subtract(left: Decimal, right: Decimal) -> Decimal:
    with localcontext(_exact_additive_context(left, right)):
        return left - right


def _exact_add(left: Decimal, right: Decimal) -> Decimal:
    with localcontext(_exact_additive_context(left, right)):
        return left + right


def _exact_multiply(left: Decimal, right: Decimal) -> Decimal:
    left_digits, _ = _decimal_shape(left)
    right_digits, _ = _decimal_shape(right)
    with localcontext(_exact_context(left_digits + right_digits)):
        return left * right


def _decimal_input(value: Any) -> Any:
    if isinstance(value, float):
        raise ValueError("binary float is forbidden; use Decimal or a base-10 string")
    if value is not None:
        try:
            decimal_value = value if isinstance(value, Decimal) else Decimal(value)
        except (InvalidOperation, TypeError, ValueError):
            return value
        if not decimal_value.is_finite():
            raise ValueError("non-finite Decimal values are forbidden")
    return value


def _immutable_coordinate(value: str, field_name: str) -> str:
    return require_stable_and_immutable(value, field_name, mutable_tokens=_MUTABLE_TOKENS)


def _sorted_unique(values: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} must not contain duplicates")
    return tuple(sorted(values))


def _reason_codes(values: tuple[str, ...]) -> tuple[str, ...]:
    values = _sorted_unique(values, "reason_codes")
    if not values or any(re.fullmatch(r"^[a-z][a-z0-9_.-]*$", value) is None for value in values):
        raise ValueError("reason_codes must contain stable machine-readable codes")
    return values


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


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


def _median(values: list[Decimal]) -> Decimal:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2 or ordered[middle - 1] == ordered[middle]:
        return ordered[middle]
    return _exact_add(ordered[middle - 1], ordered[middle]) / Decimal(2)


def _consensus_value(
    values: list[Decimal | str], relative_tolerance: Decimal, absolute_tolerance: Decimal
) -> tuple[Decimal | str, Decimal] | None:
    if all(isinstance(value, str) for value in values):
        return (values[0], Decimal(0)) if len(set(values)) == 1 else None
    numbers = [value for value in values if isinstance(value, Decimal)]
    if len(numbers) != len(values):
        return None
    median = _median(numbers)
    bound = _exact_add(absolute_tolerance, _exact_multiply(relative_tolerance, median.copy_abs()))
    spread = max(_exact_subtract(value, median).copy_abs() for value in numbers)
    return (median, spread) if spread <= bound else None


def grade_consensus(
    votes: tuple[ConsensusVote, ...] | list[ConsensusVote],
    *,
    relative_tolerance: Decimal = CONSENSUS_RELATIVE_TOLERANCE,
    absolute_tolerance: Decimal = Decimal(0),
) -> ConsensusGrade:
    if not votes:
        return ConsensusGrade(None, None, (), ())
    group_ids = [vote.group_id for vote in votes]
    if len(group_ids) != len(set(group_ids)):
        raise ValueError("each independent origin group votes once")
    ordered = sorted(votes, key=lambda vote: (vote.rank, vote.group_id))
    for size in range(len(ordered), PAIR_QUORUM_GROUPS - 1, -1):
        best: tuple[Decimal, Decimal | str, tuple[ConsensusVote, ...]] | None = None
        for members in combinations(ordered, size):
            agreed = _consensus_value([vote.value for vote in members], relative_tolerance, absolute_tolerance)
            if agreed is not None and (best is None or agreed[1] < best[0]):
                best = (agreed[1], agreed[0], members)
        if best is not None:
            _, served, members = best
            chosen = {vote.group_id for vote in members}
            return ConsensusGrade(
                served_value=served,
                served_group_id=members[0].group_id,
                agreeing_group_ids=tuple(sorted(chosen)),
                outlier_group_ids=tuple(sorted(set(group_ids) - chosen)),
            )
    priority = ordered[0]
    return ConsensusGrade(
        served_value=priority.value,
        served_group_id=priority.group_id,
        agreeing_group_ids=(priority.group_id,),
        outlier_group_ids=tuple(sorted(set(group_ids) - {priority.group_id})),
    )


class ReconciliationCell(_FrozenModel):
    cell_id: str = Field(default="", pattern=r"^(?:|reconciliation-cell:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    requirement_id: str = Field(pattern=_CONTENT_ID)
    subject: SubjectRef
    field_name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    field_semantics_id: str = Field(pattern=_CONTENT_ID)
    unit: str = Field(min_length=1)
    valid_from: date
    valid_to: date

    @model_validator(mode="after")
    def identify(self) -> Self:
        if self.valid_to < self.valid_from:
            raise ValueError("valid_to cannot precede valid_from")
        _freeze_content(
            self,
            id_field="cell_id",
            prefix="reconciliation-cell",
            identity_fields=(
                "requirement_id",
                "subject",
                "field_name",
                "field_semantics_id",
                "unit",
                "valid_from",
                "valid_to",
            ),
        )
        return self


class SourceAssertion(_FrozenModel):
    assertion_id: str = Field(default="", pattern=r"^(?:|source-assertion:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    cell_id: str = Field(pattern=r"^reconciliation-cell:[0-9a-f]{64}$")
    observation_id: str = Field(pattern=r"^normalized-observation:[0-9a-f]{64}$")
    source_id: str = Field(pattern=STABLE_ID_PATTERN)
    origin_group_id: str = Field(pattern=STABLE_ID_PATTERN)
    knowable_at: datetime
    normalized_value_sha256: str = Field(pattern=_SHA256)
    numeric_value: Decimal | None = None
    confidence_assessment_id: str = Field(pattern=r"^confidence-assessment:[0-9a-f]{64}$")
    lineage_node_ids: tuple[str, ...] = Field(min_length=1)
    lineage_complete: bool

    @field_validator("source_id", "origin_group_id")
    @classmethod
    def validate_coordinates(cls, value: str, info: Any) -> str:
        return _immutable_coordinate(value, info.field_name)

    @field_validator("knowable_at")
    @classmethod
    def validate_knowable_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "knowable_at")

    @field_validator("numeric_value", mode="before")
    @classmethod
    def reject_binary_float(cls, value: Any) -> Any:
        return _decimal_input(value)

    @field_validator("lineage_node_ids")
    @classmethod
    def normalize_lineage(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        values = _sorted_unique(values, "lineage_node_ids")
        for value in values:
            _immutable_coordinate(value, "lineage_node_ids")
        return values

    @model_validator(mode="after")
    def identify(self) -> Self:
        _freeze_content(
            self,
            id_field="assertion_id",
            prefix="source-assertion",
            identity_fields=(
                "cell_id",
                "observation_id",
                "source_id",
                "origin_group_id",
                "knowable_at",
                "normalized_value_sha256",
                "numeric_value",
                "confidence_assessment_id",
            ),
        )
        return self


class ReconciliationPolicy(_FrozenModel):
    policy_id: str = Field(default="", pattern=r"^(?:|reconciliation-policy:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    policy_version: str = Field(pattern=STABLE_ID_PATTERN)
    source_priority: tuple[str, ...] = Field(min_length=1)
    absolute_tolerance: Decimal = Field(ge=0)
    relative_tolerance: Decimal = Field(ge=0)
    minimum_independent_origin_groups: int = Field(default=2, ge=1)
    conflict_behavior: ConflictBehavior = ConflictBehavior.REPORT_AND_ABSTAIN

    @field_validator("policy_version")
    @classmethod
    def validate_policy_version(cls, value: str) -> str:
        return _immutable_coordinate(value, "policy_version")

    @field_validator("source_priority")
    @classmethod
    def normalize_source_priority(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("source_priority must not contain duplicates")
        for value in values:
            _immutable_coordinate(value, "source_priority")
        return values

    @field_validator("absolute_tolerance", "relative_tolerance", mode="before")
    @classmethod
    def reject_binary_float(cls, value: Any) -> Any:
        return _decimal_input(value)

    @model_validator(mode="after")
    def identify(self) -> Self:
        if (
            self.conflict_behavior is ConflictBehavior.MEDIAN_CONSENSUS
            and self.minimum_independent_origin_groups != PAIR_QUORUM_GROUPS
        ):
            raise ValueError("a median-consensus policy corroborates at exactly two agreeing origin groups")
        _freeze_content(
            self,
            id_field="policy_id",
            prefix="reconciliation-policy",
            identity_fields=(
                "policy_version",
                "source_priority",
                "absolute_tolerance",
                "relative_tolerance",
                "minimum_independent_origin_groups",
                "conflict_behavior",
            ),
        )
        return self


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


class ReconciliationResult(_FrozenModel):
    result_id: str = Field(default="", pattern=r"^(?:|reconciliation-result:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    cell_id: str = Field(pattern=r"^reconciliation-cell:[0-9a-f]{64}$")
    policy_id: str = Field(pattern=r"^reconciliation-policy:[0-9a-f]{64}$")
    minimum_independent_origin_groups: int = Field(ge=1)
    cutoff: datetime
    outcome: ReconciliationOutcome
    assertion_ids: tuple[str, ...]
    eligible_assertion_ids: tuple[str, ...]
    future_assertion_ids: tuple[str, ...]
    unregistered_assertion_ids: tuple[str, ...]
    representative_assertion_ids: tuple[str, ...]
    agreeing_assertion_ids: tuple[str, ...]
    conflicting_assertion_ids: tuple[str, ...]
    origin_group_ids: tuple[str, ...]
    comparison_anchor_assertion_id: str | None = Field(default=None, pattern=r"^source-assertion:[0-9a-f]{64}$")
    selected_assertion_id: str | None = Field(default=None, pattern=r"^source-assertion:[0-9a-f]{64}$")
    selected_value_sha256: str | None = Field(default=None, pattern=_SHA256)
    selected_numeric_value: Decimal | None = None
    lineage_complete: bool
    reason_codes: tuple[str, ...] = Field(min_length=1)

    @field_validator("cutoff")
    @classmethod
    def validate_cutoff(cls, value: datetime) -> datetime:
        return _require_aware(value, "cutoff")

    @field_validator("selected_numeric_value", mode="before")
    @classmethod
    def reject_binary_float(cls, value: Any) -> Any:
        return _decimal_input(value)

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
        return _reason_codes(values)

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

    def _validate_empty_outcome(
        self, eligible_ids: set[str], future_ids: set[str], representative_ids: set[str]
    ) -> None:
        if eligible_ids or representative_ids or self.origin_group_ids:
            raise ValueError("an unavailable result cannot contain eligible representatives")
        if self.outcome is ReconciliationOutcome.NOT_YET_KNOWABLE and not future_ids:
            raise ValueError("a not-yet-knowable result requires future assertions")
        if self.outcome is ReconciliationOutcome.UNAVAILABLE and future_ids:
            raise ValueError("an unavailable result cannot contain future assertions")

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
            self._validate_empty_outcome(eligible_ids, future_ids, representative_ids)

    @model_validator(mode="after")
    def identify(self) -> Self:
        assertion_ids = set(self.assertion_ids)
        eligible_ids = set(self.eligible_assertion_ids)
        future_ids = set(self.future_assertion_ids)
        unregistered_ids = set(self.unregistered_assertion_ids)
        representative_ids = set(self.representative_assertion_ids)
        agreeing_ids = set(self.agreeing_assertion_ids)
        conflicting_ids = set(self.conflicting_assertion_ids)
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
        _freeze_content(
            self,
            id_field="result_id",
            prefix="reconciliation-result",
            identity_fields=("cell_id", "policy_id", "cutoff", "assertion_ids"),
        )
        return self


def _assertions_agree(left: SourceAssertion, right: SourceAssertion, policy: ReconciliationPolicy) -> bool:
    if left.numeric_value is None or right.numeric_value is None:
        return (
            left.numeric_value is None
            and right.numeric_value is None
            and left.normalized_value_sha256 == right.normalized_value_sha256
        )
    difference = _exact_subtract(left.numeric_value, right.numeric_value).copy_abs()
    scale = max(left.numeric_value.copy_abs(), right.numeric_value.copy_abs())
    tolerance_bound = _exact_add(policy.absolute_tolerance, _exact_multiply(policy.relative_tolerance, scale))
    return difference <= tolerance_bound


def _build_result(
    *,
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
        assertion_ids=tuple(item.assertion_id for item in ordered),
        eligible_assertion_ids=tuple(item.assertion_id for item in eligible),
        future_assertion_ids=tuple(item.assertion_id for item in future),
        unregistered_assertion_ids=tuple(item.assertion_id for item in unregistered),
        representative_assertion_ids=tuple(item.assertion_id for item in representatives),
        origin_group_ids=origins,
        lineage_complete=bool(representatives) and all(item.lineage_complete for item in representatives),
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
    cutoff = _require_aware(cutoff, "cutoff")
    if any(assertion.cell_id != cell.cell_id for assertion in assertions):
        raise ValueError("every assertion must belong to the reconciled cell")
    if len({assertion.assertion_id for assertion in assertions}) != len(assertions):
        raise ValueError("assertions must not contain duplicates")

    priority = {source_id: rank for rank, source_id in enumerate(policy.source_priority)}
    ordered = tuple(sorted(assertions, key=lambda item: item.assertion_id))
    future = tuple(item for item in ordered if item.knowable_at > cutoff)
    knowable = tuple(item for item in ordered if item.knowable_at <= cutoff)
    unregistered = tuple(item for item in knowable if item.source_id not in priority)
    eligible = tuple(item for item in knowable if item.source_id in priority)

    representatives: list[SourceAssertion] = []
    for origin_group_id in sorted({item.origin_group_id for item in eligible}):
        candidates = [item for item in eligible if item.origin_group_id == origin_group_id]
        best_rank = min(priority[item.source_id] for item in candidates)
        representatives.append(
            max(
                (c for c in candidates if priority[c.source_id] == best_rank),
                key=lambda c: (c.knowable_at, c.assertion_id),
            )
        )

    reasons: list[str] = []
    if future:
        reasons.append("reconciliation.future_knowledge_excluded")
    if unregistered:
        reasons.append("reconciliation.unregistered_source_excluded")

    if not representatives:
        return _build_result(
            cell=cell,
            policy=policy,
            cutoff=cutoff,
            ordered=ordered,
            eligible=(),
            future=future,
            unregistered=unregistered,
            representatives=(),
            origins=(),
            outcome=ReconciliationOutcome.NOT_YET_KNOWABLE if future else ReconciliationOutcome.UNAVAILABLE,
            agreeing_ids=(),
            conflicting_ids=(),
            anchor_id=None,
            selected_id=None,
            selected_sha256=None,
            selected_numeric=None,
            reasons=("reconciliation.no_eligible_assertion", *reasons),
        )

    origins = tuple(item.origin_group_id for item in representatives)
    if len(eligible) > len(representatives):
        reasons.append("reconciliation.same_origin_deduplicated")
    if policy.conflict_behavior is ConflictBehavior.MEDIAN_CONSENSUS:
        return _median_consensus_result(
            representatives, priority, policy, cell, cutoff, ordered, eligible, future, unregistered, origins, reasons
        )

    selected_rank = min(priority[item.source_id] for item in representatives)
    selected = max(
        (item for item in representatives if priority[item.source_id] == selected_rank),
        key=lambda item: (item.knowable_at, item.assertion_id),
    )
    agreeing = [item for item in representatives if _assertions_agree(selected, item, policy)]
    conflicting = [item for item in representatives if item not in agreeing]

    if conflicting:
        outcome = ReconciliationOutcome.CONFLICT_ABSTAINED
        reasons.append("reconciliation.cross_origin_conflict")
        selected_result: SourceAssertion | None = None
    elif len(origins) < policy.minimum_independent_origin_groups:
        outcome = ReconciliationOutcome.INSUFFICIENT_INDEPENDENT_ORIGINS
        reasons.append("reconciliation.insufficient_independent_origins")
        selected_result = selected
    else:
        outcome = ReconciliationOutcome.AGREED
        reasons.append("reconciliation.independent_origins_agree")
        selected_result = selected

    return _build_result(
        cell=cell,
        policy=policy,
        cutoff=cutoff,
        ordered=ordered,
        eligible=eligible,
        future=future,
        unregistered=unregistered,
        representatives=representatives,
        origins=origins,
        outcome=outcome,
        agreeing_ids=tuple(item.assertion_id for item in agreeing),
        conflicting_ids=tuple(item.assertion_id for item in conflicting),
        anchor_id=selected.assertion_id,
        selected_id=None if selected_result is None else selected_result.assertion_id,
        selected_sha256=None if selected_result is None else selected_result.normalized_value_sha256,
        selected_numeric=None if selected_result is None else selected_result.numeric_value,
        reasons=tuple(reasons or ("reconciliation.selected",)),
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
    by_group = {item.origin_group_id: item for item in representatives}
    grade = grade_consensus(
        [
            ConsensusVote(
                group_id=item.origin_group_id,
                rank=priority[item.source_id],
                value=item.normalized_value_sha256 if item.numeric_value is None else item.numeric_value,
            )
            for item in representatives
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
        cell=cell,
        policy=policy,
        cutoff=cutoff,
        ordered=ordered,
        eligible=eligible,
        future=future,
        unregistered=unregistered,
        representatives=representatives,
        origins=origins,
        outcome=outcome,
        agreeing_ids=tuple(by_group[group].assertion_id for group in grade.agreeing_group_ids),
        conflicting_ids=tuple(by_group[group].assertion_id for group in grade.outlier_group_ids),
        anchor_id=selected.assertion_id,
        selected_id=selected.assertion_id,
        selected_sha256=served_sha,
        selected_numeric=grade.served_value if isinstance(grade.served_value, Decimal) else None,
        reasons=tuple(reasons),
    )
