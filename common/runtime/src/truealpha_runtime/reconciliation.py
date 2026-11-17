"""Point-in-time multi-source reconciliation contracts."""

from __future__ import annotations

import decimal
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
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
CONSENSUS_RELATIVE_TOLERANCE, MEDIAN_QUORUM_GROUPS, PAIR_QUORUM_GROUPS = Decimal("0.001"), 3, 2


def _shape(v: Decimal) -> tuple[int, int]:
    _, d, exp = v.as_tuple()
    if not isinstance(exp, int):
        raise ValueError("exact Decimal arithmetic requires finite values")
    return len(d), exp


def _ctx(prec: int) -> decimal.Context:
    return decimal.Context(
        prec=max(1, prec),
        rounding=decimal.ROUND_HALF_EVEN,
        Emin=decimal.MIN_EMIN,
        Emax=decimal.MAX_EMAX,
        capitals=1,
        clamp=0,
        flags=[],
        traps=[decimal.InvalidOperation, decimal.DivisionByZero, decimal.Overflow, decimal.Inexact, decimal.Rounded],
    )


def _exact_add(a: Decimal, b: Decimal, *, sign: int = 1) -> Decimal:
    (ad, ae), (bd, be) = _shape(a), _shape(b)
    c = min(ae, be)
    with decimal.localcontext(_ctx(max(ad + ae - c, bd + be - c) + 1)):
        return a + b if sign > 0 else a - b


def _exact_mul(a: Decimal, b: Decimal) -> Decimal:
    with decimal.localcontext(_ctx(_shape(a)[0] + _shape(b)[0])):
        return a * b


def _uniq(vals: tuple[str, ...], name: str) -> tuple[str, ...]:
    if len(vals) != len(set(vals)):
        raise ValueError(f"{name} must not contain duplicates")
    return tuple(sorted(vals))


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


_CONFLICT = frozenset({ReconciliationOutcome.CONFLICT_ABSTAINED, ReconciliationOutcome.CONFLICT_PRIORITY_SERVED})
_ABSTAIN = frozenset(
    {
        ReconciliationOutcome.CONFLICT_ABSTAINED,
        ReconciliationOutcome.NOT_YET_KNOWABLE,
        ReconciliationOutcome.UNAVAILABLE,
    }
)
_SELECTED = frozenset(
    {
        ReconciliationOutcome.AGREED,
        ReconciliationOutcome.INSUFFICIENT_INDEPENDENT_ORIGINS,
        ReconciliationOutcome.CONFLICT_PRIORITY_SERVED,
    }
)


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


def _consensus_val(vals: list[Decimal | str], rel: Decimal, abs_tol: Decimal) -> tuple[Decimal | str, Decimal] | None:
    if all(isinstance(v, str) for v in vals):
        return (vals[0], Decimal(0)) if len(set(vals)) == 1 else None
    nums = sorted(v for v in vals if isinstance(v, Decimal))
    if len(nums) != len(vals):
        return None
    mid = len(nums) // 2
    med = (
        nums[mid] if len(nums) % 2 or nums[mid - 1] == nums[mid] else _exact_add(nums[mid - 1], nums[mid]) / Decimal(2)
    )
    spread = max(_exact_add(v, med, sign=-1).copy_abs() for v in nums)
    return (med, spread) if spread <= _exact_add(abs_tol, _exact_mul(rel, med.copy_abs())) else None


def grade_consensus(
    votes: tuple[ConsensusVote, ...] | list[ConsensusVote],
    *,
    relative_tolerance: Decimal = CONSENSUS_RELATIVE_TOLERANCE,
    absolute_tolerance: Decimal = Decimal(0),
) -> ConsensusGrade:
    if not votes:
        return ConsensusGrade(None, None, (), ())
    gids = [v.group_id for v in votes]
    if len(gids) != len(set(gids)):
        raise ValueError("each independent origin group votes once")
    ordered = sorted(votes, key=lambda v: (v.rank, v.group_id))
    for size in range(len(ordered), PAIR_QUORUM_GROUPS - 1, -1):
        best: tuple[Decimal, Decimal | str, tuple[ConsensusVote, ...]] | None = None
        for m in combinations(ordered, size):
            if (ag := _consensus_val([v.value for v in m], relative_tolerance, absolute_tolerance)) is not None and (
                best is None or ag[1] < best[0]
            ):
                best = (ag[1], ag[0], m)
        if best is not None:
            chosen = {v.group_id for v in best[2]}
            return ConsensusGrade(
                best[1], best[2][0].group_id, tuple(sorted(chosen)), tuple(sorted(set(gids) - chosen))
            )
    p = ordered[0]
    return ConsensusGrade(p.value, p.group_id, (p.group_id,), tuple(sorted(set(gids) - {p.group_id})))


class ReconciliationCell(IdentifiedFrozenModel):
    id_field, id_prefix = "cell_id", "reconciliation-cell"
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
    id_field, id_prefix = "assertion_id", "source-assertion"
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
        for v in _uniq(values, "lineage_node_ids"):
            require_stable_and_immutable(v, "lineage_node_ids")
        return tuple(sorted(values))


class ReconciliationPolicy(IdentifiedFrozenModel):
    id_field, id_prefix = "policy_id", "reconciliation-policy"
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
        _uniq(values, "source_priority")
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
    id_field, id_prefix = "result_id", "reconciliation-result"
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
        return _uniq(values, info.field_name)

    @field_validator("reason_codes")
    @classmethod
    def normalize_reasons(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values or any(re.fullmatch(r"^[a-z][a-z0-9_.-]*$", v) is None for v in _uniq(values, "reason_codes")):
            raise ValueError("reason_codes must contain stable machine-readable codes")
        return tuple(sorted(values))

    def _canonicalize(self) -> None:
        a, el, ft, un, rp, ag, cf = map(
            set,
            (
                self.assertion_ids,
                self.eligible_assertion_ids,
                self.future_assertion_ids,
                self.unregistered_assertion_ids,
                self.representative_assertion_ids,
                self.agreeing_assertion_ids,
                self.conflicting_assertion_ids,
            ),
        )
        if (
            (ft & el)
            or (ft & un)
            or (el & un)
            or (ft | el | un != a)
            or not (rp <= el)
            or (ag & cf)
            or (ag | cf != rp)
            or len(self.origin_group_ids) != len(rp)
        ):
            raise ValueError("assertion partitions invalid")
        sel, anc = self.selected_assertion_id is not None, self.comparison_anchor_assertion_id is not None
        if (
            sel != (self.selected_value_sha256 is not None)
            or (self.selected_numeric_value is not None and not sel)
            or (anc != bool(rp))
            or (anc and self.comparison_anchor_assertion_id not in ag)
        ):
            raise ValueError("selected assertion and comparison anchor mismatch")
        if sel and (
            self.selected_assertion_id not in rp
            or self.selected_assertion_id not in ag
            or self.selected_assertion_id != self.comparison_anchor_assertion_id
        ):
            raise ValueError("selected assertion must equal comparison anchor")
        if (self.outcome in _ABSTAIN and sel) or (self.outcome in _SELECTED and not sel):
            raise ValueError("abstained cannot select or selected outcome must select an assertion")
        if (
            self.outcome is ReconciliationOutcome.AGREED
            and len(self.origin_group_ids) < self.minimum_independent_origin_groups
        ):
            raise ValueError("agreement does not meet the bound independent-origin threshold")
        if self.outcome is ReconciliationOutcome.AGREED and cf and len(ag) < PAIR_QUORUM_GROUPS:
            raise ValueError("an agreed result with conflicting assertions needs at least two agreeing origin groups")
        if self.outcome is ReconciliationOutcome.INSUFFICIENT_INDEPENDENT_ORIGINS and (
            len(self.origin_group_ids) >= self.minimum_independent_origin_groups or cf
        ):
            raise ValueError("insufficient origins invalid")
        if self.outcome in _CONFLICT and (
            not ag
            or not cf
            or len(self.origin_group_ids) < 2
            or (self.outcome is ReconciliationOutcome.CONFLICT_PRIORITY_SERVED and len(ag) != 1)
        ):
            raise ValueError("conflict outcome invalid: serves one origin group")
        if self.outcome not in (
            {ReconciliationOutcome.AGREED, ReconciliationOutcome.INSUFFICIENT_INDEPENDENT_ORIGINS} | _CONFLICT
        ) and (
            el
            or rp
            or self.origin_group_ids
            or (self.outcome is ReconciliationOutcome.NOT_YET_KNOWABLE and not ft)
            or (self.outcome is ReconciliationOutcome.UNAVAILABLE and ft)
        ):
            raise ValueError("unavailable/future result invalid")


def _agrees(a: SourceAssertion, b: SourceAssertion, policy: ReconciliationPolicy) -> bool:
    if a.numeric_value is None or b.numeric_value is None:
        return (
            a.numeric_value is None
            and b.numeric_value is None
            and a.normalized_value_sha256 == b.normalized_value_sha256
        )
    return _exact_add(a.numeric_value, b.numeric_value, sign=-1).copy_abs() <= _exact_add(
        policy.absolute_tolerance,
        _exact_mul(policy.relative_tolerance, max(a.numeric_value.copy_abs(), b.numeric_value.copy_abs())),
    )


def _mk_res(
    cell: ReconciliationCell,
    policy: ReconciliationPolicy,
    cutoff: datetime,
    ordered: Sequence[SourceAssertion],
    eligible: Sequence[SourceAssertion],
    future: Sequence[SourceAssertion],
    unregistered: Sequence[SourceAssertion],
    reps: Sequence[SourceAssertion],
    origins: tuple[str, ...],
    outcome: ReconciliationOutcome,
    agr: Sequence[str],
    conf: Sequence[str],
    anc_id: str | None,
    sel_id: str | None,
    sha: str | None,
    num: Decimal | None,
    reasons: Sequence[str],
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
        representative_assertion_ids=tuple(i.assertion_id for i in reps),
        origin_group_ids=origins,
        lineage_complete=bool(reps) and all(i.lineage_complete for i in reps),
        agreeing_assertion_ids=tuple(agr),
        conflicting_assertion_ids=tuple(conf),
        comparison_anchor_assertion_id=anc_id,
        selected_assertion_id=sel_id,
        selected_value_sha256=sha,
        selected_numeric_value=num,
        reason_codes=tuple(reasons),
    )


def reconcile_source_assertions(
    *, cell: ReconciliationCell, assertions: tuple[SourceAssertion, ...], policy: ReconciliationPolicy, cutoff: datetime
) -> ReconciliationResult:
    cutoff = require_aware(cutoff, "cutoff")
    if any(a.cell_id != cell.cell_id for a in assertions) or len({a.assertion_id for a in assertions}) != len(
        assertions
    ):
        raise ValueError("assertions must belong to cell and be unique")
    prio, ordered = (
        {sid: r for r, sid in enumerate(policy.source_priority)},
        tuple(sorted(assertions, key=lambda i: i.assertion_id)),
    )
    future, knowable = (
        tuple(i for i in ordered if i.knowable_at > cutoff),
        tuple(i for i in ordered if i.knowable_at <= cutoff),
    )
    unreg, elig = (
        tuple(i for i in knowable if i.source_id not in prio),
        tuple(i for i in knowable if i.source_id in prio),
    )
    reps = [
        max(
            (i for i in elig if i.origin_group_id == gid),
            key=lambda c: (-prio[c.source_id], c.knowable_at, c.assertion_id),
        )
        for gid in sorted({i.origin_group_id for i in elig})
    ]
    reasons = (["reconciliation.future_knowledge_excluded"] if future else []) + (
        ["reconciliation.unregistered_source_excluded"] if unreg else []
    )
    if not reps:
        return _mk_res(
            cell,
            policy,
            cutoff,
            ordered,
            (),
            future,
            unreg,
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
    origins = tuple(i.origin_group_id for i in reps)
    if len(elig) > len(reps):
        reasons.append("reconciliation.same_origin_deduplicated")
    if policy.conflict_behavior is ConflictBehavior.MEDIAN_CONSENSUS:
        by_g = {i.origin_group_id: i for i in reps}
        grade = grade_consensus(
            [
                ConsensusVote(
                    i.origin_group_id,
                    prio[i.source_id],
                    i.normalized_value_sha256 if i.numeric_value is None else i.numeric_value,
                )
                for i in reps
            ],
            relative_tolerance=policy.relative_tolerance,
            absolute_tolerance=policy.absolute_tolerance,
        )
        assert grade.served_group_id is not None
        sel = by_g[grade.served_group_id]
        if len(grade.agreeing_group_ids) >= PAIR_QUORUM_GROUPS:
            outcome = ReconciliationOutcome.AGREED
            reasons.append(
                "reconciliation.three_origins_agree"
                if len(grade.agreeing_group_ids) >= MEDIAN_QUORUM_GROUPS
                else "reconciliation.two_origins_agree"
            )
            if grade.outlier_group_ids:
                reasons.append("reconciliation.outlier_recorded")
        elif len(reps) == 1:
            outcome = ReconciliationOutcome.INSUFFICIENT_INDEPENDENT_ORIGINS
            reasons.append("reconciliation.insufficient_independent_origins")
        else:
            outcome = ReconciliationOutcome.CONFLICT_PRIORITY_SERVED
            reasons.append("reconciliation.cross_origin_conflict")
        sha = (
            grade.served_value
            if isinstance(grade.served_value, str)
            else (
                sel.normalized_value_sha256
                if sel.numeric_value is not None and grade.served_value == sel.numeric_value
                else canonical_sha256(str(grade.served_value))
            )
        )
        return _mk_res(
            cell,
            policy,
            cutoff,
            ordered,
            elig,
            future,
            unreg,
            reps,
            origins,
            outcome,
            [by_g[g].assertion_id for g in grade.agreeing_group_ids],
            [by_g[g].assertion_id for g in grade.outlier_group_ids],
            sel.assertion_id,
            sel.assertion_id,
            sha,
            grade.served_value if isinstance(grade.served_value, Decimal) else None,
            reasons,
        )
    min_r = min(prio[i.source_id] for i in reps)
    sel = max((i for i in reps if prio[i.source_id] == min_r), key=lambda i: (i.knowable_at, i.assertion_id))
    agr = [i for i in reps if _agrees(sel, i, policy)]
    conf = [i for i in reps if i not in agr]
    if conf:
        outcome, sel_res, tag = ReconciliationOutcome.CONFLICT_ABSTAINED, None, "reconciliation.cross_origin_conflict"
    elif len(origins) < policy.minimum_independent_origin_groups:
        outcome, sel_res, tag = (
            ReconciliationOutcome.INSUFFICIENT_INDEPENDENT_ORIGINS,
            sel,
            "reconciliation.insufficient_independent_origins",
        )
    else:
        outcome, sel_res, tag = ReconciliationOutcome.AGREED, sel, "reconciliation.independent_origins_agree"
    return _mk_res(
        cell,
        policy,
        cutoff,
        ordered,
        elig,
        future,
        unreg,
        reps,
        origins,
        outcome,
        [i.assertion_id for i in agr],
        [i.assertion_id for i in conf],
        sel.assertion_id,
        None if sel_res is None else sel_res.assertion_id,
        None if sel_res is None else sel_res.normalized_value_sha256,
        None if sel_res is None else sel_res.numeric_value,
        [*reasons, tag],
    )
