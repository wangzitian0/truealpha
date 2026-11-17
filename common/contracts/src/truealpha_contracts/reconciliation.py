"""Point-in-time multi-source reconciliation contracts."""

from __future__ import annotations

import decimal
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, localcontext
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
    sorted_unique_coordinates,
)
from truealpha_contracts.common import (
    require_checks as _require_checks,
)
from truealpha_contracts.universe import SubjectRef

_CONTENT_ID = r"^[a-z][a-z0-9-]*:[0-9a-f]{64}$"


def _decimal_shape(value: Decimal) -> tuple[int, int]:
    _, digits, exponent = value.as_tuple()
    if not isinstance(exponent, int):
        raise ValueError("exact Decimal arithmetic requires finite values")
    return len(digits), exponent


_TRAPS = [decimal.InvalidOperation, decimal.DivisionByZero, decimal.Overflow, decimal.Inexact, decimal.Rounded]


def _exact_context(precision: int) -> decimal.Context:
    return decimal.Context(
        prec=max(1, precision),
        rounding=decimal.ROUND_HALF_EVEN,
        Emin=decimal.MIN_EMIN,
        Emax=decimal.MAX_EMAX,
        capitals=1,
        clamp=0,
        flags=[],
        traps=_TRAPS,
    )


def _exact_delta(left: Decimal, right: Decimal, sign: int = 1) -> Decimal:
    ld, le = _decimal_shape(left)
    rd, re_exp = _decimal_shape(right)
    common = min(le, re_exp)
    with localcontext(_exact_context(max(ld + le - common, rd + re_exp - common) + 1)):
        return left + right if sign > 0 else left - right


def _exact_multiply(left: Decimal, right: Decimal) -> Decimal:
    ld, _ = _decimal_shape(left)
    rd, _ = _decimal_shape(right)
    with localcontext(_exact_context(ld + rd)):
        return left * right


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
        else _exact_delta(numbers[mid - 1], numbers[mid]) / Decimal(2)
    )
    bound = _exact_delta(absolute_tolerance, _exact_multiply(relative_tolerance, median.copy_abs()))
    spread = max(_exact_delta(v, median, -1).copy_abs() for v in numbers)
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
        return sorted_unique_coordinates(values, "lineage_node_ids", immutable=True)


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
        return sorted_unique_coordinates(values, info.field_name, pattern=None)

    @field_validator("reason_codes")
    @classmethod
    def normalize_reasons(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return sorted_unique_coordinates(values, "reason_codes", allow_empty=False, pattern=r"^[a-z][a-z0-9_.-]*$")

    def _canonicalize(self) -> None:
        sel, anc = self.selected_assertion_id is not None, self.comparison_anchor_assertion_id is not None
        reps, ag, co = (
            set(self.representative_assertion_ids),
            set(self.agreeing_assertion_ids),
            set(self.conflicting_assertion_ids),
        )
        el, fu, un = (
            set(self.eligible_assertion_ids),
            set(self.future_assertion_ids),
            set(self.unregistered_assertion_ids),
        )
        sel_id, anc_id = self.selected_assertion_id, self.comparison_anchor_assertion_id
        n_og, min_og, out = len(self.origin_group_ids), self.minimum_independent_origin_groups, self.outcome
        ro = ReconciliationOutcome
        checks: list[tuple[bool, str]] = [
            (
                sel != (self.selected_value_sha256 is not None),
                "selected assertion and value hash must be present together",
            ),
            (
                self.selected_numeric_value is not None and not sel,
                "selected numeric value requires a selected assertion",
            ),
            (anc != bool(reps), "comparison anchor must be present exactly when representatives exist"),
            (anc and anc_id not in ag, "comparison anchor must be an agreeing representative"),
            (sel and (sel_id not in reps or sel_id not in ag), "selected assertion must be an agreeing representative"),
            (sel and sel_id != anc_id, "selected assertion must equal the comparison anchor"),
            (out in _ABSTAIN_OR_UNAVAILABLE and sel, "abstained or unavailable reconciliation cannot select a value"),
            (
                out in _SELECTED_OUTCOMES and not sel,
                "an agreed, insufficient-origin, or priority-served result must select an assertion",
            ),
            (bool(fu & el or fu & un or el & un), "future, eligible, and unregistered assertions must be disjoint"),
            (fu | el | un != set(self.assertion_ids), "assertion classifications must exactly cover every assertion"),
            (not reps <= el, "representative assertions must be eligible"),
            (bool(ag & co) or ag | co != reps, "agreeing and conflicting assertions must partition representatives"),
            (
                n_og != len(self.representative_assertion_ids),
                "each representative must correspond to one independent origin group",
            ),
        ]
        if out is ro.AGREED:
            checks += [
                (n_og < min_og, "agreement does not meet the bound independent-origin threshold"),
                (
                    bool(co) and len(ag) < PAIR_QUORUM_GROUPS,
                    "an agreed result with conflicting assertions needs at least two agreeing origin groups",
                ),
            ]
        elif out is ro.INSUFFICIENT_INDEPENDENT_ORIGINS:
            checks += [
                (n_og >= min_og, "an insufficient-origin result meets its bound threshold"),
                (bool(co), "an insufficient-origin result cannot contain conflicting assertions"),
            ]
        elif out in _CONFLICT_OUTCOMES:
            checks += [
                (not ag or not co or n_og < 2, "a conflict requires an agreeing anchor and a disagreeing origin"),
                (
                    out is ro.CONFLICT_PRIORITY_SERVED and len(ag) != 1,
                    "a priority-served conflict serves one origin group",
                ),
            ]
        else:
            checks += [
                (
                    bool(el or reps or self.origin_group_ids),
                    "an unavailable result cannot contain eligible representatives",
                ),
                (out is ro.NOT_YET_KNOWABLE and not fu, "a not-yet-knowable result requires future assertions"),
                (out is ro.UNAVAILABLE and bool(fu), "an unavailable result cannot contain future assertions"),
            ]
        _require_checks(*checks)


def _assertions_agree(left: SourceAssertion, right: SourceAssertion, policy: ReconciliationPolicy) -> bool:
    if left.numeric_value is None or right.numeric_value is None:
        return (
            left.numeric_value is None
            and right.numeric_value is None
            and left.normalized_value_sha256 == right.normalized_value_sha256
        )
    diff = _exact_delta(left.numeric_value, right.numeric_value, -1).copy_abs()
    scale = max(left.numeric_value.copy_abs(), right.numeric_value.copy_abs())
    return diff <= _exact_delta(policy.absolute_tolerance, _exact_multiply(policy.relative_tolerance, scale))


_MedianOutcome = tuple[ReconciliationOutcome, tuple[str, ...], tuple[str, ...], str, str, str, Decimal | None]


def _median_decision(
    representatives: list[SourceAssertion],
    priority: dict[str, int],
    policy: ReconciliationPolicy,
    reasons: list[str],
) -> _MedianOutcome:
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
    return (
        outcome,
        tuple(by_group[g].assertion_id for g in grade.agreeing_group_ids),
        tuple(by_group[g].assertion_id for g in grade.outlier_group_ids),
        selected.assertion_id,
        selected.assertion_id,
        served_sha,
        grade.served_value if isinstance(grade.served_value, Decimal) else None,
    )


def _ids(items: Sequence[SourceAssertion]) -> tuple[str, ...]:
    return tuple(i.assertion_id for i in items)


def reconcile_source_assertions(
    *,
    cell: ReconciliationCell,
    assertions: tuple[SourceAssertion, ...],
    policy: ReconciliationPolicy,
    cutoff: datetime,
) -> ReconciliationResult:
    cutoff = require_aware(cutoff, "cutoff")
    _require_checks(
        (any(a.cell_id != cell.cell_id for a in assertions), "every assertion must belong to the reconciled cell"),
        (len({a.assertion_id for a in assertions}) != len(assertions), "assertions must not contain duplicates"),
    )
    priority = {s: rank for rank, s in enumerate(policy.source_priority)}

    def priority_key(assertion: SourceAssertion) -> tuple[int, datetime, str]:
        return (-priority[assertion.source_id], assertion.knowable_at, assertion.assertion_id)

    ordered = tuple(sorted(assertions, key=lambda i: i.assertion_id))
    future = tuple(i for i in ordered if i.knowable_at > cutoff)
    knowable = tuple(i for i in ordered if i.knowable_at <= cutoff)
    unregistered = tuple(i for i in knowable if i.source_id not in priority)
    eligible = tuple(i for i in knowable if i.source_id in priority)

    by_origin: dict[str, list[SourceAssertion]] = {}
    for item in eligible:
        by_origin.setdefault(item.origin_group_id, []).append(item)
    representatives = [max(by_origin[og], key=priority_key) for og in sorted(by_origin)]
    reasons = [
        *(["reconciliation.future_knowledge_excluded"] if future else []),
        *(["reconciliation.unregistered_source_excluded"] if unregistered else []),
    ]
    origins = tuple(i.origin_group_id for i in representatives)
    agreeing_ids: tuple[str, ...] = ()
    conflicting_ids: tuple[str, ...] = ()
    anchor_id: str | None = None
    selected_id: str | None = None
    selected_sha: str | None = None
    selected_numeric: Decimal | None = None
    if not representatives:
        outcome = ReconciliationOutcome.NOT_YET_KNOWABLE if future else ReconciliationOutcome.UNAVAILABLE
        eligible_out: Sequence[SourceAssertion] = ()
        reasons = ["reconciliation.no_eligible_assertion", *reasons]
    else:
        eligible_out = eligible
        if len(eligible) > len(representatives):
            reasons.append("reconciliation.same_origin_deduplicated")
        if policy.conflict_behavior is ConflictBehavior.MEDIAN_CONSENSUS:
            outcome, agreeing_ids, conflicting_ids, anchor_id, selected_id, selected_sha, selected_numeric = (
                _median_decision(representatives, priority, policy, reasons)
            )
        else:
            selected = max(representatives, key=priority_key)
            agreeing = [i for i in representatives if _assertions_agree(selected, i, policy)]
            conflicting = [i for i in representatives if i not in agreeing]
            agreeing_ids, conflicting_ids, anchor_id = _ids(agreeing), _ids(conflicting), selected.assertion_id
            if conflicting:
                outcome, selected_result = ReconciliationOutcome.CONFLICT_ABSTAINED, None
                reasons.append("reconciliation.cross_origin_conflict")
            elif len(origins) < policy.minimum_independent_origin_groups:
                outcome, selected_result = ReconciliationOutcome.INSUFFICIENT_INDEPENDENT_ORIGINS, selected
                reasons.append("reconciliation.insufficient_independent_origins")
            else:
                outcome, selected_result = ReconciliationOutcome.AGREED, selected
                reasons.append("reconciliation.independent_origins_agree")
            if selected_result is not None:
                selected_id, selected_sha, selected_numeric = (
                    selected_result.assertion_id,
                    selected_result.normalized_value_sha256,
                    selected_result.numeric_value,
                )
            reasons = reasons or ["reconciliation.selected"]

    return ReconciliationResult(
        cell_id=cell.cell_id,
        policy_id=policy.policy_id,
        minimum_independent_origin_groups=policy.minimum_independent_origin_groups,
        cutoff=cutoff,
        outcome=outcome,
        assertion_ids=_ids(ordered),
        eligible_assertion_ids=_ids(eligible_out),
        future_assertion_ids=_ids(future),
        unregistered_assertion_ids=_ids(unregistered),
        representative_assertion_ids=_ids(representatives),
        origin_group_ids=origins,
        lineage_complete=bool(representatives) and all(i.lineage_complete for i in representatives),
        agreeing_assertion_ids=agreeing_ids,
        conflicting_assertion_ids=conflicting_ids,
        comparison_anchor_assertion_id=anchor_id,
        selected_assertion_id=selected_id,
        selected_value_sha256=selected_sha,
        selected_numeric_value=selected_numeric,
        reason_codes=tuple(reasons),
    )
