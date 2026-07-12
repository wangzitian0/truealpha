"""Source-neutral demand and infrastructure-owned data-usage accountability."""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_serializer,
    field_validator,
    model_validator,
)

from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.data_quality import DataDomain
from truealpha_contracts.models import _require_aware
from truealpha_contracts.registries import RegistrySnapshot, SemanticTypeId
from truealpha_contracts.universe import SubjectKind, SubjectRef, UniverseRef

_SHA256 = r"^[0-9a-f]{64}$"
_STABLE_ID = r"^[a-zA-Z0-9][a-zA-Z0-9._:/@+-]*$"


def _identify(model: BaseModel, *, id_field: str, hash_field: str, prefix: str) -> None:
    payload = model.model_dump(mode="json", exclude={id_field, hash_field})
    expected_hash = canonical_sha256(payload)
    expected_id = f"{prefix}:{expected_hash}"
    supplied_hash = getattr(model, hash_field)
    supplied_id = getattr(model, id_field)
    if supplied_hash and supplied_hash != expected_hash:
        raise ValueError(f"{hash_field} does not match canonical content")
    if supplied_id and supplied_id != expected_id:
        raise ValueError(f"{id_field} does not match canonical content")
    object.__setattr__(model, hash_field, expected_hash)
    object.__setattr__(model, id_field, expected_id)


def planned_cell_id_for(
    *,
    requirement_id: str,
    capture_requirement_id: str,
    semantic_type_id: str,
    domain: DataDomain,
    subject: SubjectRef,
    partition_key: str,
) -> str:
    """Return the stable cross-stage identity for one compiled demand cell."""

    digest = canonical_sha256(
        {
            "requirement_id": requirement_id,
            "capture_requirement_id": capture_requirement_id,
            "semantic_type_id": semantic_type_id,
            "domain": domain.value,
            "subject": subject.model_dump(mode="json"),
            "partition_key": partition_key,
        }
    )
    return f"planned-demand-cell:{digest}"


class RequirementLevel(StrEnum):
    REQUIRED = "required"
    OPTIONAL = "optional"


class UsageStage(StrEnum):
    CAPTURE = "capture"
    NORMALIZATION = "normalization"
    SNAPSHOT_SELECTION = "snapshot_selection"
    FACTOR_CONSUMPTION = "factor_consumption"
    STRATEGY_CONSUMPTION = "strategy_consumption"
    STATE_TRANSITION = "state_transition"
    TRADE_EXECUTION = "trade_execution"
    VALUATION = "valuation"
    METRIC = "metric"


class UsageEmitterKind(StrEnum):
    CAPTURE_MANIFEST_EVALUATOR = "capture_manifest_evaluator"
    INSTRUMENTED_RUNNER = "instrumented_runner"


class DataRequirement(BaseModel):
    """A typed factor demand declaration that cannot select a vendor."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    requirement_id: str = ""
    content_sha256: str = ""
    capture_requirement_id: str = Field(pattern=r"^capture-requirement:[0-9a-f]{64}$")
    semantic_type_id: SemanticTypeId
    domain: DataDomain
    metric: str | None = None
    subject_kinds: frozenset[SubjectKind] = Field(min_length=1)
    level: RequirementLevel
    lookback: timedelta | None = None
    valid_period_rule_id: str = Field(pattern=_STABLE_ID)
    maximum_age: timedelta
    cadence: timedelta

    @field_serializer("subject_kinds", when_used="json")
    def serialize_subject_kinds(self, values: frozenset[SubjectKind]) -> list[str]:
        return sorted(value.value for value in values)

    @model_validator(mode="after")
    def freeze_and_identify(self) -> DataRequirement:
        if self.lookback is not None and self.lookback <= timedelta(0):
            raise ValueError("lookback must be positive")
        if self.maximum_age <= timedelta(0) or self.cadence <= timedelta(0):
            raise ValueError("maximum age and cadence must be positive")
        _identify(self, id_field="requirement_id", hash_field="content_sha256", prefix="data-requirement")
        return self


class PlannedDemandCell(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    planned_cell_id: str = Field(default="", pattern=r"^(?:|planned-demand-cell:[0-9a-f]{64})$")
    requirement_id: str = Field(pattern=r"^data-requirement:[0-9a-f]{64}$")
    capture_requirement_id: str = Field(pattern=r"^capture-requirement:[0-9a-f]{64}$")
    semantic_type_id: SemanticTypeId
    domain: DataDomain
    subject: SubjectRef
    partition_key: str = Field(min_length=1)
    level: RequirementLevel
    expected_stages: frozenset[UsageStage] = Field(min_length=1)

    @field_serializer("expected_stages", when_used="json")
    def serialize_expected_stages(self, values: frozenset[UsageStage]) -> list[str]:
        return sorted(value.value for value in values)

    @model_validator(mode="after")
    def identify(self) -> PlannedDemandCell:
        expected_id = planned_cell_id_for(
            requirement_id=self.requirement_id,
            capture_requirement_id=self.capture_requirement_id,
            semantic_type_id=self.semantic_type_id,
            domain=self.domain,
            subject=self.subject,
            partition_key=self.partition_key,
        )
        if self.planned_cell_id and self.planned_cell_id != expected_id:
            raise ValueError("planned_cell_id does not match frozen demand")
        object.__setattr__(self, "planned_cell_id", expected_id)
        return self

    @property
    def key(self) -> tuple[str, str, str, str]:
        return self.requirement_id, self.subject.kind.value, self.subject.id, self.partition_key


class DataUsageEvent(BaseModel):
    """Idempotent append-only evidence emitted outside factor computation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    usage_event_id: str = ""
    content_sha256: str = ""
    operation_id: str = Field(pattern=_STABLE_ID)
    emitter_kind: UsageEmitterKind
    emitter_id: str = Field(pattern=_STABLE_ID)
    stage: UsageStage
    planned_cell_id: str = Field(default="", pattern=r"^(?:|planned-demand-cell:[0-9a-f]{64})$")
    requirement_id: str = Field(pattern=r"^data-requirement:[0-9a-f]{64}$")
    capture_requirement_id: str = Field(pattern=r"^capture-requirement:[0-9a-f]{64}$")
    semantic_type_id: SemanticTypeId
    domain: DataDomain
    subject: SubjectRef
    partition_key: str = Field(min_length=1)
    run_id: str = Field(pattern=_STABLE_ID)
    trace_id: str = Field(pattern=_STABLE_ID)
    normalized_record_ids: tuple[str, ...] = ()
    consumed_market_event_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = Field(min_length=1)
    occurred_at: datetime
    recorded_at: datetime
    retained_until: datetime

    @field_validator("occurred_at", "recorded_at", "retained_until")
    @classmethod
    def validate_datetimes(cls, value: datetime, info) -> datetime:
        return _require_aware(value, info.field_name)

    @model_validator(mode="after")
    def freeze_and_identify(self) -> DataUsageEvent:
        if self.recorded_at < self.occurred_at:
            raise ValueError("recorded_at cannot precede occurred_at")
        if self.retained_until < self.recorded_at:
            raise ValueError("retained_until cannot precede recorded_at")
        manifest_stages = {UsageStage.CAPTURE, UsageStage.NORMALIZATION}
        expected_emitter = (
            UsageEmitterKind.CAPTURE_MANIFEST_EVALUATOR
            if self.stage in manifest_stages
            else UsageEmitterKind.INSTRUMENTED_RUNNER
        )
        if self.emitter_kind is not expected_emitter:
            raise ValueError(f"{self.stage.value} usage must be emitted by {expected_emitter.value}")
        normalized_ids = tuple(sorted(self.normalized_record_ids))
        if len(normalized_ids) != len(set(normalized_ids)):
            raise ValueError("normalized record IDs must be unique")
        evidence_ids = tuple(sorted(self.evidence_ids))
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("usage evidence IDs must be unique")
        market_event_ids = tuple(sorted(self.consumed_market_event_ids))
        if len(market_event_ids) != len(set(market_event_ids)):
            raise ValueError("consumed market event IDs must be unique")
        if self.stage is not UsageStage.CAPTURE and not (normalized_ids or market_event_ids):
            raise ValueError(f"{self.stage.value} requires exact normalized records or consumed market events")
        expected_planned_cell_id = planned_cell_id_for(
            requirement_id=self.requirement_id,
            capture_requirement_id=self.capture_requirement_id,
            semantic_type_id=self.semantic_type_id,
            domain=self.domain,
            subject=self.subject,
            partition_key=self.partition_key,
        )
        if self.planned_cell_id and self.planned_cell_id != expected_planned_cell_id:
            raise ValueError("planned_cell_id does not match usage demand")
        object.__setattr__(self, "planned_cell_id", expected_planned_cell_id)
        object.__setattr__(self, "normalized_record_ids", normalized_ids)
        object.__setattr__(self, "consumed_market_event_ids", market_event_ids)
        object.__setattr__(self, "evidence_ids", evidence_ids)
        identity = self.model_dump(
            mode="json",
            include={
                "operation_id",
                "emitter_kind",
                "emitter_id",
                "stage",
                "planned_cell_id",
                "requirement_id",
                "capture_requirement_id",
                "semantic_type_id",
                "domain",
                "subject",
                "partition_key",
                "run_id",
                "trace_id",
                "normalized_record_ids",
                "consumed_market_event_ids",
                "evidence_ids",
            },
        )
        expected_hash = canonical_sha256(identity)
        expected_id = f"data-usage:{expected_hash}"
        if self.content_sha256 and self.content_sha256 != expected_hash:
            raise ValueError("content_sha256 does not match usage-event identity")
        if self.usage_event_id and self.usage_event_id != expected_id:
            raise ValueError("usage_event_id does not match usage-event identity")
        object.__setattr__(self, "content_sha256", expected_hash)
        object.__setattr__(self, "usage_event_id", expected_id)
        return self

    @property
    def cell_key(self) -> tuple[str, str, str, str]:
        return self.requirement_id, self.subject.kind.value, self.subject.id, self.partition_key


class MissingDemand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy_run_id: str = Field(pattern=_STABLE_ID)
    planned_cell_id: str = Field(pattern=r"^planned-demand-cell:[0-9a-f]{64}$")
    requirement_id: str = Field(pattern=r"^data-requirement:[0-9a-f]{64}$")
    capture_requirement_id: str = Field(pattern=r"^capture-requirement:[0-9a-f]{64}$")
    subject: SubjectRef
    partition_key: str = Field(min_length=1)
    missing_stages: tuple[UsageStage, ...] = Field(min_length=1)


class ReverseLineageEdge(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reverse_lineage_edge_id: str = ""
    content_sha256: str = ""
    downstream_id: str = Field(min_length=1)
    upstream_id: str = Field(min_length=1)
    relation: str = Field(pattern=_STABLE_ID)

    @model_validator(mode="after")
    def freeze_and_identify(self) -> ReverseLineageEdge:
        if self.downstream_id == self.upstream_id:
            raise ValueError("reverse lineage cannot contain self edges")
        _identify(
            self,
            id_field="reverse_lineage_edge_id",
            hash_field="content_sha256",
            prefix="reverse-lineage-edge",
        )
        return self


def _reconcile_usage(
    *,
    strategy_run_id: str,
    planned_cells: tuple[PlannedDemandCell, ...],
    events: tuple[DataUsageEvent, ...],
) -> tuple[
    tuple[PlannedDemandCell, ...],
    tuple[DataUsageEvent, ...],
    tuple[MissingDemand, ...],
    dict[UsageStage, int],
]:
    planned = tuple(sorted(planned_cells, key=lambda item: item.planned_cell_id))
    planned_by_id = {cell.planned_cell_id: cell for cell in planned}
    if len(planned_by_id) != len(planned):
        raise ValueError("planned demand cells must be unique")
    ordered_events = tuple(sorted(events, key=lambda item: item.usage_event_id))
    if len({event.usage_event_id for event in ordered_events}) != len(ordered_events):
        raise ValueError("duplicate idempotent usage events cannot be counted twice")
    event_stages: dict[str, set[UsageStage]] = {cell_id: set() for cell_id in planned_by_id}
    for event in ordered_events:
        if event.run_id != strategy_run_id:
            raise ValueError("usage event belongs to another strategy run")
        cell = planned_by_id.get(event.planned_cell_id)
        if cell is None:
            raise ValueError(f"usage event is not declared by planned demand: {event.planned_cell_id}")
        if (
            event.requirement_id != cell.requirement_id
            or event.capture_requirement_id != cell.capture_requirement_id
            or event.semantic_type_id != cell.semantic_type_id
            or event.domain is not cell.domain
            or event.subject != cell.subject
            or event.partition_key != cell.partition_key
        ):
            raise ValueError("usage event demand binding does not match planned demand")
        if event.stage not in cell.expected_stages:
            raise ValueError("usage event stage is undeclared for the planned demand cell")
        event_stages[cell.planned_cell_id].add(event.stage)

    missing = tuple(
        MissingDemand(
            strategy_run_id=strategy_run_id,
            planned_cell_id=cell.planned_cell_id,
            requirement_id=cell.requirement_id,
            capture_requirement_id=cell.capture_requirement_id,
            subject=cell.subject,
            partition_key=cell.partition_key,
            missing_stages=tuple(
                sorted(
                    cell.expected_stages - event_stages[cell.planned_cell_id],
                    key=lambda item: item.value,
                )
            ),
        )
        for cell in planned
        if cell.level is RequirementLevel.REQUIRED and cell.expected_stages - event_stages[cell.planned_cell_id]
    )
    counts = {stage: sum(event.stage is stage for event in ordered_events) for stage in UsageStage}
    return planned, ordered_events, missing, counts


class StrategyUsageAudit(BaseModel):
    """Complete runner-owned usage and reverse-lineage evidence for one exact run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy_usage_audit_id: str = ""
    content_sha256: str = ""
    strategy_run_id: str = Field(pattern=_STABLE_ID)
    research_catalog_id: str = Field(pattern=r"^research-catalog:[0-9a-f]{64}$")
    research_catalog_sha256: str = Field(pattern=_SHA256)
    universe: UniverseRef
    applicability_catalog_id: str = Field(pattern=r"^applicability:[0-9a-f]{64}$")
    applicability_catalog_sha256: str = Field(pattern=_SHA256)
    slo_catalog_id: str = Field(min_length=1)
    slo_catalog_sha256: str = Field(pattern=_SHA256)
    release_manifest_id: str = Field(pattern=r"^release-manifest:[0-9a-f]{64}$")
    registry_snapshot: RegistrySnapshot
    run_started_at: datetime
    run_completed_at: datetime
    audited_at: datetime
    planned_cells: tuple[PlannedDemandCell, ...] = Field(min_length=1)
    usage_events: tuple[DataUsageEvent, ...] = ()
    trace_bundle_ids: tuple[str, ...] = Field(min_length=1)
    reverse_lineage: tuple[ReverseLineageEdge, ...] = Field(min_length=1)
    affected_decision_ids: tuple[str, ...] = Field(min_length=1)
    affected_state_transition_ids: tuple[str, ...] = ()
    affected_trade_ids: tuple[str, ...] = ()
    affected_valuation_ids: tuple[str, ...] = ()
    affected_metric_ids: tuple[str, ...] = ()
    auditor_id: str = Field(pattern=_STABLE_ID)
    auditor_version: str = Field(pattern=_STABLE_ID)
    auditor_implementation_sha256: str = Field(pattern=_SHA256)
    derivation_input_ids: tuple[str, ...] = ()
    missing_required: tuple[MissingDemand, ...] = ()
    counts_by_stage: dict[UsageStage, int] = Field(default_factory=dict)
    telemetry_complete: bool = False

    @field_validator("run_started_at", "run_completed_at", "audited_at")
    @classmethod
    def validate_datetimes(cls, value: datetime, info) -> datetime:
        return _require_aware(value, info.field_name)

    @model_validator(mode="after")
    def freeze_and_identify(self) -> StrategyUsageAudit:
        if self.run_completed_at < self.run_started_at:
            raise ValueError("strategy run cannot complete before it starts")
        if self.audited_at < self.run_completed_at:
            raise ValueError("strategy usage cannot be audited before the run completes")
        planned, events, missing, counts = _reconcile_usage(
            strategy_run_id=self.strategy_run_id,
            planned_cells=self.planned_cells,
            events=self.usage_events,
        )
        if any(event.recorded_at > self.audited_at for event in events):
            raise ValueError("strategy usage audit cannot contain future telemetry")
        trace_bundles = tuple(sorted(self.trace_bundle_ids))
        if len(trace_bundles) != len(set(trace_bundles)) or any(
            not item.startswith("trace-bundle:") for item in trace_bundles
        ):
            raise ValueError("strategy usage audit requires unique content-addressed trace bundles")
        edges = tuple(
            sorted(
                self.reverse_lineage,
                key=lambda item: item.reverse_lineage_edge_id,
            )
        )
        if len({edge.reverse_lineage_edge_id for edge in edges}) != len(edges):
            raise ValueError("strategy usage audit reverse-lineage edges must be unique")
        affected_fields = (
            "affected_decision_ids",
            "affected_state_transition_ids",
            "affected_trade_ids",
            "affected_valuation_ids",
            "affected_metric_ids",
        )
        affected: set[str] = set()
        for field_name in affected_fields:
            values = tuple(sorted(getattr(self, field_name)))
            if len(values) != len(set(values)):
                raise ValueError(f"{field_name} must contain unique identities")
            object.__setattr__(self, field_name, values)
            affected.update(values)
        adjacency: dict[str, set[str]] = {}
        for edge in edges:
            adjacency.setdefault(edge.downstream_id, set()).add(edge.upstream_id)
        reachable = {self.strategy_run_id}
        changed = True
        while changed:
            changed = False
            for node in tuple(reachable):
                for upstream in adjacency.get(node, set()):
                    if upstream not in reachable:
                        reachable.add(upstream)
                        changed = True
        trace_ids = {event.trace_id for event in events}
        consumed_ids = {
            item
            for event in events
            for item in (
                *event.normalized_record_ids,
                *event.consumed_market_event_ids,
                *event.evidence_ids,
            )
        }
        unresolved = (affected | trace_ids | consumed_ids) - reachable
        if unresolved:
            raise ValueError(f"strategy usage reverse lineage is incomplete: {sorted(unresolved)}")
        derivation_inputs = tuple(
            sorted(
                {
                    self.research_catalog_id,
                    f"universe-ref:{self.universe.content_sha256}",
                    self.applicability_catalog_id,
                    self.slo_catalog_id,
                    self.release_manifest_id,
                    self.registry_snapshot.registry_snapshot_id,
                    self.registry_snapshot.source_registry_snapshot_id,
                    self.registry_snapshot.semantic_type_registry_snapshot_id,
                    *(cell.planned_cell_id for cell in planned),
                    *(cell.requirement_id for cell in planned),
                    *(cell.capture_requirement_id for cell in planned),
                    *(event.usage_event_id for event in events),
                    *(edge.reverse_lineage_edge_id for edge in edges),
                    *trace_bundles,
                }
            )
        )
        if self.derivation_input_ids and self.derivation_input_ids != derivation_inputs:
            raise ValueError("derivation_input_ids must be derived from exact audit inputs")
        if self.missing_required and self.missing_required != missing:
            raise ValueError("missing_required must be derived from planned demand and events")
        if self.counts_by_stage and self.counts_by_stage != counts:
            raise ValueError("counts_by_stage must be derived from usage events")
        expected_complete = not missing and bool(events)
        if "telemetry_complete" in self.model_fields_set and self.telemetry_complete != expected_complete:
            raise ValueError("telemetry_complete must be derived from the complete run audit")
        object.__setattr__(self, "planned_cells", planned)
        object.__setattr__(self, "usage_events", events)
        object.__setattr__(self, "trace_bundle_ids", trace_bundles)
        object.__setattr__(self, "reverse_lineage", edges)
        object.__setattr__(self, "derivation_input_ids", derivation_inputs)
        object.__setattr__(self, "missing_required", missing)
        object.__setattr__(self, "counts_by_stage", counts)
        object.__setattr__(self, "telemetry_complete", expected_complete)
        _identify(
            self,
            id_field="strategy_usage_audit_id",
            hash_field="content_sha256",
            prefix="strategy-usage-audit",
        )
        return self


def build_strategy_usage_audit(
    *,
    strategy_run_id: str,
    planned_cells: tuple[PlannedDemandCell, ...],
    events: tuple[DataUsageEvent, ...],
    trace_bundle_ids: tuple[str, ...],
    reverse_lineage: tuple[ReverseLineageEdge, ...],
    affected_decision_ids: tuple[str, ...],
    affected_state_transition_ids: tuple[str, ...] = (),
    affected_trade_ids: tuple[str, ...] = (),
    affected_valuation_ids: tuple[str, ...] = (),
    affected_metric_ids: tuple[str, ...] = (),
    research_catalog_id: str,
    research_catalog_sha256: str,
    universe: UniverseRef,
    applicability_catalog_id: str,
    applicability_catalog_sha256: str,
    slo_catalog_id: str,
    slo_catalog_sha256: str,
    release_manifest_id: str,
    registry_snapshot: RegistrySnapshot,
    run_started_at: datetime,
    run_completed_at: datetime,
    audited_at: datetime,
    auditor_id: str,
    auditor_version: str,
    auditor_implementation_sha256: str,
) -> StrategyUsageAudit:
    return StrategyUsageAudit(
        strategy_run_id=strategy_run_id,
        research_catalog_id=research_catalog_id,
        research_catalog_sha256=research_catalog_sha256,
        universe=universe,
        applicability_catalog_id=applicability_catalog_id,
        applicability_catalog_sha256=applicability_catalog_sha256,
        slo_catalog_id=slo_catalog_id,
        slo_catalog_sha256=slo_catalog_sha256,
        release_manifest_id=release_manifest_id,
        registry_snapshot=registry_snapshot,
        run_started_at=run_started_at,
        run_completed_at=run_completed_at,
        audited_at=audited_at,
        planned_cells=planned_cells,
        usage_events=events,
        trace_bundle_ids=trace_bundle_ids,
        reverse_lineage=reverse_lineage,
        affected_decision_ids=affected_decision_ids,
        affected_state_transition_ids=affected_state_transition_ids,
        affected_trade_ids=affected_trade_ids,
        affected_valuation_ids=affected_valuation_ids,
        affected_metric_ids=affected_metric_ids,
        auditor_id=auditor_id,
        auditor_version=auditor_version,
        auditor_implementation_sha256=auditor_implementation_sha256,
    )


class UsageFrequencySlice(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    usage_frequency_slice_id: str = ""
    content_sha256: str = ""
    research_catalog_id: str = Field(pattern=r"^research-catalog:[0-9a-f]{64}$")
    research_catalog_sha256: str = Field(pattern=_SHA256)
    universe: UniverseRef
    applicability_catalog_id: str = Field(pattern=r"^applicability:[0-9a-f]{64}$")
    applicability_catalog_sha256: str = Field(pattern=_SHA256)
    slo_catalog_id: str = Field(min_length=1)
    slo_catalog_sha256: str = Field(pattern=_SHA256)
    release_manifest_id: str = Field(pattern=r"^release-manifest:[0-9a-f]{64}$")
    registry_snapshot_id: str = Field(pattern=r"^registry-snapshot:[0-9a-f]{64}$")
    registry_snapshot_sha256: str = Field(pattern=_SHA256)
    source_registry_id: str = Field(pattern=r"^source-registry:[0-9a-f]{64}$")
    source_registry_sha256: str = Field(pattern=_SHA256)
    semantic_type_registry_id: str = Field(pattern=r"^semantic-type-registry:[0-9a-f]{64}$")
    semantic_type_registry_sha256: str = Field(pattern=_SHA256)
    window_start: datetime
    window_end: datetime
    strategy_usage_audits: tuple[StrategyUsageAudit, ...] = Field(min_length=1)
    strategy_usage_audit_ids: tuple[str, ...] = ()
    planned_cells: tuple[PlannedDemandCell, ...] = ()
    usage_event_ids: tuple[str, ...] = ()
    trace_bundle_ids: tuple[str, ...] = ()
    trace_ids: tuple[str, ...] = ()
    counts_by_stage: dict[UsageStage, int] = Field(default_factory=dict)
    distinct_run_ids: tuple[str, ...] = ()
    first_used_at: datetime | None = None
    last_used_at: datetime | None = None
    missing_required: tuple[MissingDemand, ...] = ()
    telemetry_complete: bool = False

    @field_validator("window_start", "window_end", "first_used_at", "last_used_at")
    @classmethod
    def validate_datetimes(cls, value: datetime | None, info) -> datetime | None:
        return None if value is None else _require_aware(value, info.field_name)

    @model_validator(mode="after")
    def freeze_and_identify(self) -> UsageFrequencySlice:
        if self.window_end <= self.window_start:
            raise ValueError("usage window must be positive")
        audits = tuple(sorted(self.strategy_usage_audits, key=lambda item: item.strategy_run_id))
        audit_ids = tuple(audit.strategy_usage_audit_id for audit in audits)
        run_ids = tuple(audit.strategy_run_id for audit in audits)
        if len(audit_ids) != len(set(audit_ids)) or len(run_ids) != len(set(run_ids)):
            raise ValueError("usage frequency requires unique audits and strategy runs")
        if any(not self.window_start <= audit.run_started_at < self.window_end for audit in audits):
            raise ValueError("strategy usage audit falls outside the bounded frequency window")
        first = audits[0]
        identity = (
            first.research_catalog_id,
            first.research_catalog_sha256,
            first.universe,
            first.applicability_catalog_id,
            first.applicability_catalog_sha256,
            first.slo_catalog_id,
            first.slo_catalog_sha256,
            first.release_manifest_id,
            first.registry_snapshot.registry_snapshot_id,
            first.registry_snapshot.content_sha256,
        )
        expected_identity = (
            self.research_catalog_id,
            self.research_catalog_sha256,
            self.universe,
            self.applicability_catalog_id,
            self.applicability_catalog_sha256,
            self.slo_catalog_id,
            self.slo_catalog_sha256,
            self.release_manifest_id,
            self.registry_snapshot_id,
            self.registry_snapshot_sha256,
        )
        if identity != expected_identity or any(
            (
                audit.research_catalog_id,
                audit.research_catalog_sha256,
                audit.universe,
                audit.applicability_catalog_id,
                audit.applicability_catalog_sha256,
                audit.slo_catalog_id,
                audit.slo_catalog_sha256,
                audit.release_manifest_id,
                audit.registry_snapshot.registry_snapshot_id,
                audit.registry_snapshot.content_sha256,
            )
            != identity
            for audit in audits[1:]
        ):
            raise ValueError("usage frequency can aggregate only identity-matching strategy audits")
        registry = first.registry_snapshot
        if (
            self.source_registry_id != registry.source_registry_snapshot_id
            or self.source_registry_sha256 != registry.source_registry_sha256
            or self.semantic_type_registry_id != registry.semantic_type_registry_snapshot_id
            or self.semantic_type_registry_sha256 != registry.semantic_type_registry_sha256
        ):
            raise ValueError("usage frequency registry projections do not match the audited registry")
        planned = first.planned_cells
        if any(audit.planned_cells != planned for audit in audits[1:]):
            raise ValueError("usage frequency audits must share exact planned demand")
        events = tuple(event for audit in audits for event in audit.usage_events)
        event_ids = tuple(sorted(event.usage_event_id for event in events))
        if len(event_ids) != len(set(event_ids)):
            raise ValueError("usage events cannot be counted by more than one strategy audit")
        trace_bundles = tuple(sorted({item for audit in audits for item in audit.trace_bundle_ids}))
        trace_ids = tuple(sorted({event.trace_id for event in events}))
        counts = {stage: sum(audit.counts_by_stage[stage] for audit in audits) for stage in UsageStage}
        missing = tuple(
            sorted(
                (item for audit in audits for item in audit.missing_required),
                key=lambda item: (item.strategy_run_id, item.planned_cell_id),
            )
        )
        used_at = sorted(event.occurred_at for event in events)
        expected_values = {
            "strategy_usage_audit_ids": audit_ids,
            "planned_cells": planned,
            "usage_event_ids": event_ids,
            "trace_bundle_ids": trace_bundles,
            "trace_ids": trace_ids,
            "counts_by_stage": counts,
            "distinct_run_ids": run_ids,
            "missing_required": missing,
        }
        for field_name, expected in expected_values.items():
            supplied = getattr(self, field_name)
            if supplied and supplied != expected:
                raise ValueError(f"{field_name} must be derived from complete strategy audits")
            object.__setattr__(self, field_name, expected)
        first_used_at = used_at[0] if used_at else None
        last_used_at = used_at[-1] if used_at else None
        if self.first_used_at is not None and self.first_used_at != first_used_at:
            raise ValueError("first_used_at must be derived from audited events")
        if self.last_used_at is not None and self.last_used_at != last_used_at:
            raise ValueError("last_used_at must be derived from audited events")
        expected_complete = all(audit.telemetry_complete for audit in audits)
        if "telemetry_complete" in self.model_fields_set and self.telemetry_complete != expected_complete:
            raise ValueError("telemetry_complete must be derived from complete strategy audits")
        object.__setattr__(self, "strategy_usage_audits", audits)
        object.__setattr__(self, "first_used_at", first_used_at)
        object.__setattr__(self, "last_used_at", last_used_at)
        object.__setattr__(self, "telemetry_complete", expected_complete)
        _identify(
            self,
            id_field="usage_frequency_slice_id",
            hash_field="content_sha256",
            prefix="usage-frequency",
        )
        return self


def build_usage_frequency_slice(
    *,
    audits: tuple[StrategyUsageAudit, ...],
    window_start: datetime,
    window_end: datetime,
) -> UsageFrequencySlice:
    if not audits:
        raise ValueError("usage frequency requires at least one complete strategy audit")
    first = audits[0]
    registry = first.registry_snapshot
    return UsageFrequencySlice(
        research_catalog_id=first.research_catalog_id,
        research_catalog_sha256=first.research_catalog_sha256,
        universe=first.universe,
        applicability_catalog_id=first.applicability_catalog_id,
        applicability_catalog_sha256=first.applicability_catalog_sha256,
        slo_catalog_id=first.slo_catalog_id,
        slo_catalog_sha256=first.slo_catalog_sha256,
        release_manifest_id=first.release_manifest_id,
        registry_snapshot_id=registry.registry_snapshot_id,
        registry_snapshot_sha256=registry.content_sha256,
        source_registry_id=registry.source_registry_snapshot_id,
        source_registry_sha256=registry.source_registry_sha256,
        semantic_type_registry_id=registry.semantic_type_registry_snapshot_id,
        semantic_type_registry_sha256=registry.semantic_type_registry_sha256,
        window_start=window_start,
        window_end=window_end,
        strategy_usage_audits=audits,
    )


class QualityState(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"


class PlannedCellQuality(BaseModel):
    """Exact source/type/freshness/rights evidence for one planned demand cell."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    planned_cell: PlannedDemandCell
    source_coverage_entry_ids: tuple[str, ...] = Field(min_length=1)
    source_readiness_report_id: str = Field(pattern=r"^source-readiness:[0-9a-f]{64}$")
    source_readiness_report_sha256: str = Field(pattern=_SHA256)
    semantic_quality_evidence_ids: tuple[str, ...] = Field(min_length=1)
    source_state: QualityState
    semantic_type_state: QualityState
    freshness_state: QualityState
    rights_state: QualityState

    @model_validator(mode="after")
    def normalize(self) -> PlannedCellQuality:
        entries = tuple(sorted(self.source_coverage_entry_ids))
        evidence = tuple(sorted(self.semantic_quality_evidence_ids))
        if len(entries) != len(set(entries)) or len(evidence) != len(set(evidence)):
            raise ValueError("cell quality evidence IDs must be unique")
        if any(not item.startswith("source-coverage-entry:") for item in entries):
            raise ValueError("cell quality must bind content-addressed source coverage entries")
        if not self.source_readiness_report_id.endswith(f":{self.source_readiness_report_sha256}"):
            raise ValueError("source readiness report ID and hash do not match")
        object.__setattr__(self, "source_coverage_entry_ids", entries)
        object.__setattr__(self, "semantic_quality_evidence_ids", evidence)
        return self

    @property
    def states(self) -> tuple[QualityState, ...]:
        return self.source_state, self.semantic_type_state, self.freshness_state, self.rights_state


class StrategyDataQualityReview(BaseModel):
    """Immutable reverse review derived from usage, lineage, and quality evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    review_id: str = ""
    content_sha256: str = ""
    strategy_run_id: str = Field(pattern=_STABLE_ID)
    strategy_usage_audit_id: str = Field(pattern=r"^strategy-usage-audit:[0-9a-f]{64}$")
    usage_audit: StrategyUsageAudit
    cell_quality: tuple[PlannedCellQuality, ...] = Field(min_length=1)
    evaluator_id: str = Field(pattern=_STABLE_ID)
    evaluator_version: str = Field(pattern=_STABLE_ID)
    evaluator_implementation_sha256: str = Field(pattern=_SHA256)
    evaluated_at: datetime

    @field_validator("evaluated_at")
    @classmethod
    def validate_evaluated_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "evaluated_at")

    @model_validator(mode="after")
    def freeze_and_identify(self) -> StrategyDataQualityReview:
        if self.strategy_run_id != self.usage_audit.strategy_run_id:
            raise ValueError("strategy review belongs to another audited run")
        if self.strategy_usage_audit_id != self.usage_audit.strategy_usage_audit_id:
            raise ValueError("strategy review does not bind the exact usage audit")
        if self.evaluated_at < self.usage_audit.audited_at:
            raise ValueError("strategy review cannot precede its complete usage audit")
        qualities = tuple(sorted(self.cell_quality, key=lambda item: item.planned_cell.planned_cell_id))
        if len({item.planned_cell.planned_cell_id for item in qualities}) != len(qualities):
            raise ValueError("strategy review cell quality rows must be unique")
        if {item.planned_cell.planned_cell_id for item in qualities} != {
            item.planned_cell_id for item in self.usage_audit.planned_cells
        }:
            raise ValueError("strategy review must be row-complete over planned demand")
        object.__setattr__(self, "cell_quality", qualities)
        _identify(
            self,
            id_field="review_id",
            hash_field="content_sha256",
            prefix="strategy-data-quality-review",
        )
        return self

    def _blockers(self) -> tuple[str, ...]:
        blockers: set[str] = set()
        if not self.usage_audit.telemetry_complete:
            blockers.add("usage.telemetry_incomplete")
        for missing in self.usage_audit.missing_required:
            blockers.add(
                "usage.required_zero_or_under_use:"
                f"{missing.strategy_run_id}/{missing.requirement_id}/"
                f"{missing.subject.kind.value}/{missing.subject.id}/{missing.partition_key}"
            )
        for quality in self.cell_quality:
            if quality.planned_cell.level is RequirementLevel.REQUIRED:
                for dimension, state in zip(
                    ("source", "semantic_type", "freshness", "rights"),
                    quality.states,
                    strict=True,
                ):
                    if state is not QualityState.PASS:
                        blockers.add(
                            f"quality.{dimension}_{state.value}:"
                            f"{quality.planned_cell.requirement_id}/{quality.planned_cell.subject.id}"
                        )

        adjacency: dict[str, set[str]] = {}
        for edge in self.usage_audit.reverse_lineage:
            adjacency.setdefault(edge.downstream_id, set()).add(edge.upstream_id)
        reachable = {self.strategy_run_id}
        changed = True
        while changed:
            changed = False
            for node in tuple(reachable):
                for upstream in adjacency.get(node, set()):
                    if upstream not in reachable:
                        reachable.add(upstream)
                        changed = True
        required_evidence = {
            evidence_id
            for quality in self.cell_quality
            for evidence_id in (
                quality.source_readiness_report_id,
                *quality.semantic_quality_evidence_ids,
            )
        }
        missing_paths = required_evidence - reachable
        if missing_paths:
            blockers.add("lineage.quality_evidence_unreachable")
        return tuple(sorted(blockers))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def blocking_reason_codes(self) -> tuple[str, ...]:
        return self._blockers()

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ready(self) -> bool:
        return not self._blockers()


__all__ = [
    "DataRequirement",
    "DataUsageEvent",
    "MissingDemand",
    "PlannedCellQuality",
    "PlannedDemandCell",
    "QualityState",
    "RequirementLevel",
    "ReverseLineageEdge",
    "StrategyDataQualityReview",
    "StrategyUsageAudit",
    "UsageEmitterKind",
    "UsageFrequencySlice",
    "UsageStage",
    "build_strategy_usage_audit",
    "build_usage_frequency_slice",
    "planned_cell_id_for",
]
