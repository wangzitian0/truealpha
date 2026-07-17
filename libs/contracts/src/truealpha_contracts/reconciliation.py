"""Point-in-time multi-source reconciliation and DataHub quality reports."""

from __future__ import annotations

import re
from collections import Counter
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.datahub import AssessmentFreshness, ObligationTerminalState
from truealpha_contracts.models import _require_aware
from truealpha_contracts.universe import SubjectRef

_SHA256 = r"^[0-9a-f]{64}$"
_CONTENT_ID = r"^[a-z][a-z0-9-]*:[0-9a-f]{64}$"
_STABLE_COORDINATE = r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-]*$"
_MUTABLE_TOKENS = frozenset({"latest", "current", "default", "stable", "main", "head"})


def _decimal_input(value: Any) -> Any:
    if isinstance(value, float):
        raise ValueError("binary float is forbidden; use Decimal or a base-10 string")
    return value


def _immutable_coordinate(value: str, field_name: str) -> str:
    if re.fullmatch(_STABLE_COORDINATE, value) is None:
        raise ValueError(f"{field_name} must be a stable coordinate")
    tokens = {token for token in re.split(r"[._:/@+\-]", value.lower()) if token}
    if tokens & _MUTABLE_TOKENS:
        raise ValueError(f"{field_name} must name an immutable version")
    return value


def _sorted_unique(values: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} must not contain duplicates")
    return tuple(sorted(values))


def _reason_codes(values: tuple[str, ...]) -> tuple[str, ...]:
    values = _sorted_unique(values, "reason_codes")
    if not values or any(re.fullmatch(r"^[a-z][a-z0-9_.-]*$", value) is None for value in values):
        raise ValueError("reason_codes must contain stable machine-readable codes")
    return values


def _freeze_content(
    model: BaseModel,
    *,
    id_field: str,
    prefix: str,
    identity_fields: tuple[str, ...],
) -> None:
    identity = model.model_dump(mode="json", include=set(identity_fields))
    identity_sha256 = canonical_sha256({"kind": prefix, "identity": identity})
    expected_id = f"{prefix}:{identity_sha256}"
    content = model.model_dump(mode="json", exclude={id_field, "content_sha256"})
    expected_content_sha256 = canonical_sha256(content)
    supplied_id = getattr(model, id_field)
    supplied_hash = getattr(model, "content_sha256")
    if supplied_id and supplied_id != expected_id:
        raise ValueError(f"{id_field} does not match its declared identity")
    if supplied_hash and supplied_hash != expected_content_sha256:
        raise ValueError("content_sha256 does not match the canonical record")
    object.__setattr__(model, id_field, expected_id)
    object.__setattr__(model, "content_sha256", expected_content_sha256)


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ConflictBehavior(StrEnum):
    REPORT_AND_ABSTAIN = "report_and_abstain"


class ReconciliationOutcome(StrEnum):
    AGREED = "agreed"
    INSUFFICIENT_INDEPENDENT_ORIGINS = "insufficient_independent_origins"
    CONFLICT_ABSTAINED = "conflict_abstained"
    NOT_YET_KNOWABLE = "not_yet_knowable"
    UNAVAILABLE = "unavailable"


class ReconciliationCell(_FrozenModel):
    """One exact semantic grain in the requested DataHub denominator."""

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
    """One normalized assertion plus its independent-origin evidence."""

    assertion_id: str = Field(default="", pattern=r"^(?:|source-assertion:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    cell_id: str = Field(pattern=r"^reconciliation-cell:[0-9a-f]{64}$")
    observation_id: str = Field(pattern=r"^normalized-observation:[0-9a-f]{64}$")
    source_id: str = Field(pattern=_STABLE_COORDINATE)
    origin_group_id: str = Field(pattern=_STABLE_COORDINATE)
    knowable_at: datetime
    normalized_value_sha256: str = Field(pattern=_SHA256)
    numeric_value: Decimal | None = None
    confidence_assessment_id: str = Field(pattern=r"^confidence-assessment:[0-9a-f]{64}$")
    confidence_score: Decimal = Field(ge=0, le=100)
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

    @field_validator("numeric_value", "confidence_score", mode="before")
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
    """Versioned winner selection and Decimal comparison semantics."""

    policy_id: str = Field(default="", pattern=r"^(?:|reconciliation-policy:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    policy_version: str = Field(pattern=_STABLE_COORDINATE)
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


class ReconciliationResult(_FrozenModel):
    result_id: str = Field(default="", pattern=r"^(?:|reconciliation-result:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    cell_id: str = Field(pattern=r"^reconciliation-cell:[0-9a-f]{64}$")
    policy_id: str = Field(pattern=r"^reconciliation-policy:[0-9a-f]{64}$")
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
    selected_assertion_id: str | None = Field(default=None, pattern=r"^source-assertion:[0-9a-f]{64}$")
    selected_value_sha256: str | None = Field(default=None, pattern=_SHA256)
    selected_numeric_value: Decimal | None = None
    selected_confidence_score: Decimal | None = Field(default=None, ge=0, le=100)
    lineage_complete: bool
    reason_codes: tuple[str, ...] = Field(min_length=1)

    @field_validator("cutoff")
    @classmethod
    def validate_cutoff(cls, value: datetime) -> datetime:
        return _require_aware(value, "cutoff")

    @field_validator("selected_numeric_value", "selected_confidence_score", mode="before")
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

    @model_validator(mode="after")
    def identify(self) -> Self:
        selected = self.selected_assertion_id is not None
        if selected != (self.selected_value_sha256 is not None) or selected != (
            self.selected_confidence_score is not None
        ):
            raise ValueError("selected assertion, value hash, and confidence must be present together")
        if self.selected_numeric_value is not None and not selected:
            raise ValueError("selected numeric value requires a selected assertion")
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
        if selected and (
            self.selected_assertion_id not in representative_ids or self.selected_assertion_id not in agreeing_ids
        ):
            raise ValueError("selected assertion must be an agreeing representative")
        if (
            self.outcome
            in {
                ReconciliationOutcome.CONFLICT_ABSTAINED,
                ReconciliationOutcome.NOT_YET_KNOWABLE,
                ReconciliationOutcome.UNAVAILABLE,
            }
            and selected
        ):
            raise ValueError("abstained or unavailable reconciliation cannot select a value")
        selected_outcomes = {
            ReconciliationOutcome.AGREED,
            ReconciliationOutcome.INSUFFICIENT_INDEPENDENT_ORIGINS,
        }
        if self.outcome in selected_outcomes and not selected:
            raise ValueError("an agreed or insufficient-origin result must select an assertion")
        if self.outcome is ReconciliationOutcome.AGREED:
            if len(self.origin_group_ids) < 2:
                raise ValueError("agreement requires at least two independent origin groups")
            if conflicting_ids:
                raise ValueError("an agreed result cannot contain conflicting assertions")
        if self.outcome is ReconciliationOutcome.INSUFFICIENT_INDEPENDENT_ORIGINS and conflicting_ids:
            raise ValueError("an insufficient-origin result cannot contain conflicting assertions")
        if self.outcome is ReconciliationOutcome.CONFLICT_ABSTAINED and not conflicting_ids:
            raise ValueError("a conflict-abstained result requires conflicting assertions")
        if self.outcome in {ReconciliationOutcome.NOT_YET_KNOWABLE, ReconciliationOutcome.UNAVAILABLE}:
            if eligible_ids or representative_ids or self.origin_group_ids:
                raise ValueError("an unavailable result cannot contain eligible representatives")
        if self.outcome is ReconciliationOutcome.NOT_YET_KNOWABLE and not future_ids:
            raise ValueError("a not-yet-knowable result requires future assertions")
        if self.outcome is ReconciliationOutcome.UNAVAILABLE and future_ids:
            raise ValueError("an unavailable result cannot contain future assertions")
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
    difference = abs(left.numeric_value - right.numeric_value)
    scale = max(abs(left.numeric_value), abs(right.numeric_value))
    return difference <= policy.absolute_tolerance + policy.relative_tolerance * scale


def reconcile_source_assertions(
    *,
    cell: ReconciliationCell,
    assertions: tuple[SourceAssertion, ...],
    policy: ReconciliationPolicy,
    cutoff: datetime,
) -> ReconciliationResult:
    """Reconcile assertions without allowing ingestion order or confidence to arbitrate."""

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
        best_source_rank = min(priority[item.source_id] for item in candidates)
        candidates = [item for item in candidates if priority[item.source_id] == best_source_rank]
        representatives.append(max(candidates, key=lambda item: (item.knowable_at, item.assertion_id)))

    if not representatives:
        outcome = ReconciliationOutcome.NOT_YET_KNOWABLE if future else ReconciliationOutcome.UNAVAILABLE
        reasons = ["reconciliation.no_eligible_assertion"]
        if future:
            reasons.append("reconciliation.future_knowledge_excluded")
        if unregistered:
            reasons.append("reconciliation.unregistered_source_excluded")
        return ReconciliationResult(
            cell_id=cell.cell_id,
            policy_id=policy.policy_id,
            cutoff=cutoff,
            outcome=outcome,
            assertion_ids=tuple(item.assertion_id for item in ordered),
            eligible_assertion_ids=(),
            future_assertion_ids=tuple(item.assertion_id for item in future),
            unregistered_assertion_ids=tuple(item.assertion_id for item in unregistered),
            representative_assertion_ids=(),
            agreeing_assertion_ids=(),
            conflicting_assertion_ids=(),
            origin_group_ids=(),
            lineage_complete=False,
            reason_codes=tuple(reasons),
        )

    selected_rank = min(priority[item.source_id] for item in representatives)
    selected = max(
        (item for item in representatives if priority[item.source_id] == selected_rank),
        key=lambda item: (item.knowable_at, item.assertion_id),
    )
    agreeing = [item for item in representatives if _assertions_agree(selected, item, policy)]
    conflicting = [item for item in representatives if item not in agreeing]
    origins = tuple(item.origin_group_id for item in representatives)
    reasons = []
    if future:
        reasons.append("reconciliation.future_knowledge_excluded")
    if unregistered:
        reasons.append("reconciliation.unregistered_source_excluded")
    if len(eligible) > len(representatives):
        reasons.append("reconciliation.same_origin_deduplicated")

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

    return ReconciliationResult(
        cell_id=cell.cell_id,
        policy_id=policy.policy_id,
        cutoff=cutoff,
        outcome=outcome,
        assertion_ids=tuple(item.assertion_id for item in ordered),
        eligible_assertion_ids=tuple(item.assertion_id for item in eligible),
        future_assertion_ids=tuple(item.assertion_id for item in future),
        unregistered_assertion_ids=tuple(item.assertion_id for item in unregistered),
        representative_assertion_ids=tuple(item.assertion_id for item in representatives),
        agreeing_assertion_ids=tuple(item.assertion_id for item in agreeing),
        conflicting_assertion_ids=tuple(item.assertion_id for item in conflicting),
        origin_group_ids=origins,
        selected_assertion_id=None if selected_result is None else selected_result.assertion_id,
        selected_value_sha256=(None if selected_result is None else selected_result.normalized_value_sha256),
        selected_numeric_value=None if selected_result is None else selected_result.numeric_value,
        selected_confidence_score=None if selected_result is None else selected_result.confidence_score,
        lineage_complete=all(item.lineage_complete for item in representatives),
        reason_codes=tuple(reasons or ("reconciliation.selected",)),
    )


class OriginGroupCount(_FrozenModel):
    origin_group_id: str = Field(pattern=_STABLE_COORDINATE)
    cell_count: int = Field(ge=1)

    @field_validator("origin_group_id")
    @classmethod
    def validate_origin_group_id(cls, value: str) -> str:
        return _immutable_coordinate(value, "origin_group_id")


class DataHubQualitySummary(_FrozenModel):
    requested_count: int = Field(ge=1)
    planned_count: int = Field(ge=0)
    terminal_count: int = Field(ge=0)
    available_count: int = Field(ge=0)
    fresh_count: int = Field(ge=0)
    independently_reconciled_count: int = Field(ge=0)
    conflicted_count: int = Field(ge=0)
    complete_lineage_count: int = Field(ge=0)
    planned_coverage: Decimal = Field(ge=0, le=1)
    terminal_coverage: Decimal = Field(ge=0, le=1)
    availability: Decimal = Field(ge=0, le=1)
    freshness: Decimal = Field(ge=0, le=1)
    independent_reconciliation: Decimal = Field(ge=0, le=1)
    lineage_completeness: Decimal = Field(ge=0, le=1)
    denominator_mean_confidence_score: Decimal = Field(ge=0, le=100)
    origin_composition: tuple[OriginGroupCount, ...]

    @field_validator(
        "planned_coverage",
        "terminal_coverage",
        "availability",
        "freshness",
        "independent_reconciliation",
        "lineage_completeness",
        "denominator_mean_confidence_score",
        mode="before",
    )
    @classmethod
    def reject_binary_float(cls, value: Any) -> Any:
        return _decimal_input(value)


class DataHubQualityCell(_FrozenModel):
    quality_cell_id: str = Field(default="", pattern=r"^(?:|datahub-quality-cell:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    cell: ReconciliationCell
    reconciliation_policy_id: str = Field(pattern=r"^reconciliation-policy:[0-9a-f]{64}$")
    planned: bool
    terminal_state: ObligationTerminalState | None = None
    reconciliation: ReconciliationResult | None = None
    freshness: AssessmentFreshness = AssessmentFreshness.UNKNOWN
    lineage_complete: bool
    attempt_count: int = Field(default=0, ge=0)
    retry_count: int = Field(default=0, ge=0)
    unchanged_response_count: int = Field(default=0, ge=0)
    reason_codes: tuple[str, ...] = Field(min_length=1)

    @field_validator("reason_codes")
    @classmethod
    def normalize_reasons(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _reason_codes(values)

    @model_validator(mode="after")
    def validate_and_identify(self) -> Self:
        if not self.planned and (
            self.terminal_state is not None
            or self.reconciliation is not None
            or self.lineage_complete
            or self.attempt_count
            or self.retry_count
            or self.unchanged_response_count
        ):
            raise ValueError("an unplanned requested cell cannot have execution evidence")
        if self.retry_count > self.attempt_count or self.unchanged_response_count > self.attempt_count:
            raise ValueError("retry and unchanged counts cannot exceed attempts")
        if self.reconciliation is not None and self.reconciliation.cell_id != self.cell.cell_id:
            raise ValueError("reconciliation must target the report cell")
        if self.reconciliation is not None and self.reconciliation.policy_id != self.reconciliation_policy_id:
            raise ValueError("reconciliation must use the cell-level field policy")
        if self.reconciliation is not None and self.lineage_complete != self.reconciliation.lineage_complete:
            raise ValueError("cell lineage completeness must match its reconciliation evidence")
        selected = self.reconciliation is not None and self.reconciliation.selected_assertion_id is not None
        if not selected and self.freshness is not AssessmentFreshness.UNKNOWN:
            raise ValueError("freshness is unknown when no assertion is selected")
        if self.terminal_state in {ObligationTerminalState.SUCCESS, ObligationTerminalState.UNCHANGED}:
            if self.reconciliation is None:
                raise ValueError("successful and unchanged cells require reconciliation evidence")
        attempted_terminal_states = {
            ObligationTerminalState.SUCCESS,
            ObligationTerminalState.UNCHANGED,
            ObligationTerminalState.UNAVAILABLE,
            ObligationTerminalState.FAILED,
        }
        if self.terminal_state in attempted_terminal_states and self.attempt_count == 0:
            raise ValueError("an attempted terminal outcome requires at least one attempt")
        if self.terminal_state is ObligationTerminalState.UNCHANGED and self.unchanged_response_count == 0:
            raise ValueError("an unchanged terminal outcome requires an unchanged response")
        if (
            self.terminal_state
            in {
                ObligationTerminalState.UNAVAILABLE,
                ObligationTerminalState.FAILED,
                ObligationTerminalState.SKIPPED_BY_POLICY,
            }
            and selected
        ):
            raise ValueError("unavailable, failed, or skipped cells cannot select an assertion")
        _freeze_content(
            self,
            id_field="quality_cell_id",
            prefix="datahub-quality-cell",
            identity_fields=("cell",),
        )
        return self


def _ratio(numerator: int, denominator: int) -> Decimal:
    return Decimal(numerator) / Decimal(denominator)


def _summarize(cells: tuple[DataHubQualityCell, ...]) -> DataHubQualitySummary:
    denominator = len(cells)
    planned = sum(cell.planned for cell in cells)
    terminal = sum(cell.terminal_state is not None for cell in cells)
    selected = tuple(
        cell
        for cell in cells
        if cell.reconciliation is not None and cell.reconciliation.selected_assertion_id is not None
    )
    fresh = sum(cell.freshness is AssessmentFreshness.FRESH for cell in selected)
    independently_reconciled = sum(
        cell.reconciliation is not None and cell.reconciliation.outcome is ReconciliationOutcome.AGREED
        for cell in cells
    )
    conflicted = sum(
        cell.reconciliation is not None and cell.reconciliation.outcome is ReconciliationOutcome.CONFLICT_ABSTAINED
        for cell in cells
    )
    lineage_complete = sum(cell.lineage_complete for cell in cells)
    confidence_sum = sum(
        (
            cell.reconciliation.selected_confidence_score
            if cell.reconciliation is not None and cell.reconciliation.selected_confidence_score is not None
            else Decimal(0)
        )
        for cell in cells
    )
    origin_counts: Counter[str] = Counter()
    for cell in cells:
        if cell.reconciliation is not None:
            origin_counts.update(set(cell.reconciliation.origin_group_ids))
    return DataHubQualitySummary(
        requested_count=denominator,
        planned_count=planned,
        terminal_count=terminal,
        available_count=len(selected),
        fresh_count=fresh,
        independently_reconciled_count=independently_reconciled,
        conflicted_count=conflicted,
        complete_lineage_count=lineage_complete,
        planned_coverage=_ratio(planned, denominator),
        terminal_coverage=_ratio(terminal, denominator),
        availability=_ratio(len(selected), denominator),
        freshness=_ratio(fresh, denominator),
        independent_reconciliation=_ratio(independently_reconciled, denominator),
        lineage_completeness=_ratio(lineage_complete, denominator),
        denominator_mean_confidence_score=confidence_sum / Decimal(denominator),
        origin_composition=tuple(
            OriginGroupCount(origin_group_id=origin_group_id, cell_count=count)
            for origin_group_id, count in sorted(origin_counts.items())
        ),
    )


class VersionedDataHubQualityReport(_FrozenModel):
    """Row-complete service report; all ratios use the requested-cell denominator."""

    report_id: str = Field(default="", pattern=r"^(?:|datahub-quality-report:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    report_schema_version: str = Field(pattern=_STABLE_COORDINATE)
    service_demand_id: str = Field(pattern=_CONTENT_ID)
    reconciliation_policy_ids: tuple[str, ...] = Field(min_length=1)
    cutoff: datetime
    generated_at: datetime
    cells: tuple[DataHubQualityCell, ...] = Field(min_length=1)
    summary: DataHubQualitySummary | None = None

    @field_validator("report_schema_version")
    @classmethod
    def validate_schema_version(cls, value: str) -> str:
        return _immutable_coordinate(value, "report_schema_version")

    @field_validator("reconciliation_policy_ids")
    @classmethod
    def normalize_policy_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        values = _sorted_unique(values, "reconciliation_policy_ids")
        if any(re.fullmatch(r"^reconciliation-policy:[0-9a-f]{64}$", value) is None for value in values):
            raise ValueError("reconciliation_policy_ids must contain content addresses")
        return values

    @field_validator("cutoff", "generated_at")
    @classmethod
    def validate_datetimes(cls, value: datetime, info: Any) -> datetime:
        return _require_aware(value, info.field_name)

    @model_validator(mode="after")
    def validate_and_identify(self) -> Self:
        cells = tuple(sorted(self.cells, key=lambda item: item.cell.cell_id))
        if len({cell.cell.cell_id for cell in cells}) != len(cells):
            raise ValueError("quality report must contain exactly one row per requested cell")
        cell_policy_ids = tuple(sorted({cell.reconciliation_policy_id for cell in cells}))
        if self.reconciliation_policy_ids != cell_policy_ids:
            raise ValueError("report policy IDs must exactly cover the cell-level field policies")
        if any(cell.reconciliation is not None and cell.reconciliation.cutoff != self.cutoff for cell in cells):
            raise ValueError("every reconciliation must use the report cutoff")
        if self.generated_at < self.cutoff:
            raise ValueError("quality report cannot be generated before its cutoff")
        expected_summary = _summarize(cells)
        if self.summary is not None and self.summary != expected_summary:
            raise ValueError("summary does not match the fixed report denominator")
        object.__setattr__(self, "cells", cells)
        object.__setattr__(self, "summary", expected_summary)
        _freeze_content(
            self,
            id_field="report_id",
            prefix="datahub-quality-report",
            identity_fields=(
                "report_schema_version",
                "service_demand_id",
                "reconciliation_policy_ids",
                "cutoff",
                "cells",
            ),
        )
        return self
