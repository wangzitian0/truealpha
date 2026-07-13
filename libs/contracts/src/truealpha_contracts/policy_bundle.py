"""Closed policy inputs and deterministic compilation of expected data demand.

The declarations in this module freeze independently-owned policy inputs.  The
compiler is the only place where Catalog, graph, schedule, universe,
applicability, and capture contracts are joined into an executable denominator.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from fnmatch import fnmatchcase
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_serializer, field_validator, model_validator

from truealpha_contracts.capture_contracts import CaptureScope, compile_capture_requirement_bindings
from truealpha_contracts.catalog import ResearchCatalogManifest
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.data_quality import DataDomain
from truealpha_contracts.execution import FactorInvocationTemplate, FactorKind
from truealpha_contracts.models import _require_aware
from truealpha_contracts.readiness import (
    ApplicabilityCatalog,
    ApplicabilityCell,
    ApplicabilityClassification,
    BudgetLine,
    NaturalRefreshReport,
    SourceCoverageCatalog,
    SourceRightsApproval,
    SourceUsagePermission,
)
from truealpha_contracts.registries import RegistrySnapshot, RegistryVersion, SemanticTypeId, SourceId
from truealpha_contracts.universe import (
    SubjectKind,
    SubjectRef,
    UniverseDefinitionKind,
    UniverseManifest,
    UniverseMembership,
    UniverseRef,
)
from truealpha_contracts.usage import (
    DataRequirement,
    DataUsageEvent,
    PlannedDemandCell,
    RequirementLevel,
    UsageEmitterKind,
    UsageStage,
    planned_cell_id_for,
)

_SHA256 = r"^[0-9a-f]{64}$"
_STABLE_ID = r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-]*$"


def _canonical_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _canonical_value(value.model_dump(mode="python"))
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _canonical_value(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        normalized = [_canonical_value(item) for item in value]
        return sorted(normalized, key=lambda item: repr(item))
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    return value


def _content_address(model: BaseModel, *, id_field: str, prefix: str) -> None:
    payload = _canonical_value(model.model_dump(mode="python", exclude={id_field, "content_sha256"}))
    expected_hash = canonical_sha256(payload)
    expected_id = f"{prefix}:{expected_hash}"
    supplied_hash = getattr(model, "content_sha256")
    supplied_id = getattr(model, id_field)
    if supplied_hash and supplied_hash != expected_hash:
        raise ValueError("content_sha256 does not match canonical content")
    if supplied_id and supplied_id != expected_id:
        raise ValueError(f"{id_field} does not match canonical content")
    object.__setattr__(model, "content_sha256", expected_hash)
    object.__setattr__(model, id_field, expected_id)


def _validate_content_ref(reference_id: str, content_sha256: str, label: str) -> None:
    if reference_id.rsplit(":", 1)[-1] != content_sha256:
        raise ValueError(f"{label} ID and hash do not match")


def _require_unique_sorted(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    if len(values) != len(set(values)):
        raise ValueError(f"{label} must be unique")
    return tuple(sorted(values))


class NaturalRefreshSourceRef(BaseModel):
    """Exact source-registry entry selected for a natural-refresh assertion."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: SourceId
    source_version: RegistryVersion
    source_registry_entry_id: str = Field(pattern=r"^source-registry-entry:[0-9a-f]{64}$")
    source_registry_entry_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode="after")
    def validate_exact_entry(self) -> NaturalRefreshSourceRef:
        _validate_content_ref(
            self.source_registry_entry_id,
            self.source_registry_entry_sha256,
            "source registry entry",
        )
        return self


class SourceCapability(BaseModel):
    """A source capability stated before any module applicability decision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_capability_id: str = Field(default="", pattern=r"^(?:|source-capability:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    source: NaturalRefreshSourceRef
    semantic_type_id: SemanticTypeId
    semantic_type_version: RegistryVersion
    domain: DataDomain
    subject_kinds: frozenset[SubjectKind] = Field(min_length=1)
    partition_pattern: str = Field(min_length=1)
    permissions: frozenset[SourceUsagePermission] = Field(min_length=1)
    rights_approval_id: str = Field(pattern=r"^source-rights:[0-9a-f]{64}$")
    rights_approval_sha256: str = Field(pattern=_SHA256)
    budget_lines: tuple[BudgetLine, ...] = Field(min_length=1)

    @field_serializer("subject_kinds", when_used="json")
    def serialize_subject_kinds(self, values: frozenset[SubjectKind]) -> list[str]:
        return sorted(value.value for value in values)

    @field_serializer("permissions", when_used="json")
    def serialize_permissions(self, values: frozenset[SourceUsagePermission]) -> list[str]:
        return sorted(value.value for value in values)

    @model_validator(mode="after")
    def validate_and_identify(self) -> SourceCapability:
        _validate_content_ref(self.rights_approval_id, self.rights_approval_sha256, "rights approval")
        budget_lines = tuple(sorted(self.budget_lines, key=lambda item: item.dimension.value))
        dimensions = [item.dimension for item in budget_lines]
        if len(dimensions) != len(set(dimensions)):
            raise ValueError("source capability budget dimensions must be unique")
        object.__setattr__(self, "budget_lines", budget_lines)
        _content_address(self, id_field="source_capability_id", prefix="source-capability")
        return self


class SourceCapabilityCatalog(BaseModel):
    """Source inventory independent of applicability and execution planning."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_capability_catalog_id: str = Field(
        default="",
        pattern=r"^(?:|source-capability-catalog:[0-9a-f]{64})$",
    )
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    catalog_version: RegistryVersion
    research_catalog_id: str = Field(pattern=r"^research-catalog:[0-9a-f]{64}$")
    research_catalog_sha256: str = Field(pattern=_SHA256)
    universe: UniverseRef
    source_registry_id: str = Field(pattern=r"^source-registry:[0-9a-f]{64}$")
    source_registry_sha256: str = Field(pattern=_SHA256)
    capabilities: tuple[SourceCapability, ...] = Field(min_length=1)
    effective_at: datetime
    owner: str = Field(min_length=1)

    @field_validator("effective_at")
    @classmethod
    def validate_effective_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "effective_at")

    @model_validator(mode="after")
    def validate_and_identify(self) -> SourceCapabilityCatalog:
        _validate_content_ref(self.research_catalog_id, self.research_catalog_sha256, "research catalog")
        _validate_content_ref(self.source_registry_id, self.source_registry_sha256, "source registry")
        capabilities = tuple(sorted(self.capabilities, key=lambda item: item.source_capability_id))
        ids = [item.source_capability_id for item in capabilities]
        if len(ids) != len(set(ids)):
            raise ValueError("source capabilities must be unique")
        if self.owner != self.owner.strip():
            raise ValueError("owner cannot have surrounding whitespace")
        object.__setattr__(self, "capabilities", capabilities)
        _content_address(self, id_field="source_capability_catalog_id", prefix="source-capability-catalog")
        return self


class SourceCapabilityCoverageReport(BaseModel):
    """Mechanical proof that every coverage row derives from one capability."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_capability_coverage_report_id: str = Field(
        default="",
        pattern=r"^(?:|source-capability-coverage:[0-9a-f]{64})$",
    )
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    source_capability_catalog_id: str = Field(pattern=r"^source-capability-catalog:[0-9a-f]{64}$")
    source_coverage_catalog_id: str = Field(pattern=r"^source-coverage:[0-9a-f]{64}$")
    registry_snapshot_id: str = Field(pattern=r"^registry-snapshot:[0-9a-f]{64}$")
    evaluated_at: datetime
    blocking_reason_codes: tuple[str, ...]

    @field_validator("evaluated_at")
    @classmethod
    def validate_evaluated_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "evaluated_at")

    @model_validator(mode="after")
    def normalize_and_identify(self) -> SourceCapabilityCoverageReport:
        object.__setattr__(self, "blocking_reason_codes", tuple(sorted(set(self.blocking_reason_codes))))
        _content_address(
            self,
            id_field="source_capability_coverage_report_id",
            prefix="source-capability-coverage",
        )
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ready(self) -> bool:
        return not self.blocking_reason_codes


def evaluate_source_capability_coverage(
    *,
    capability_catalog: SourceCapabilityCatalog,
    coverage_catalog: SourceCoverageCatalog,
    registry_snapshot: RegistrySnapshot,
    rights_approvals: tuple[SourceRightsApproval, ...],
    evaluated_at: datetime,
) -> SourceCapabilityCoverageReport:
    """Join projected source rows back to immutable capability, registry, and rights inputs."""

    evaluated_at = _require_aware(evaluated_at, "evaluated_at")
    blockers: set[str] = set()
    if (
        capability_catalog.research_catalog_id != coverage_catalog.research_catalog_id
        or capability_catalog.research_catalog_sha256 != coverage_catalog.research_catalog_sha256
        or capability_catalog.universe != coverage_catalog.universe
    ):
        blockers.add("binding.capability_coverage_scope_mismatch")
    if (
        capability_catalog.source_registry_id != coverage_catalog.source_registry_id
        or capability_catalog.source_registry_sha256 != coverage_catalog.source_registry_sha256
        or capability_catalog.source_registry_id != registry_snapshot.source_registry_snapshot_id
        or capability_catalog.source_registry_sha256 != registry_snapshot.source_registry_sha256
    ):
        blockers.add("binding.source_registry_mismatch")
    if capability_catalog.effective_at > evaluated_at or coverage_catalog.effective_at > evaluated_at:
        blockers.add("binding.catalog_postdates_evaluation")

    registry_sources = {(item.source_id, item.version): item for item in registry_snapshot.sources}
    approvals = {item.rights_approval_id: item for item in rights_approvals}
    if len(approvals) != len(rights_approvals):
        blockers.add("rights.duplicate_approval")
    for capability in capability_catalog.capabilities:
        source = capability.source
        registry_entry = registry_sources.get((source.source_id, source.source_version))
        prefix = capability.source_capability_id
        if registry_entry is None:
            blockers.add(f"{prefix}: registry entry is missing")
        elif (
            registry_entry.source_registry_entry_id != source.source_registry_entry_id
            or registry_entry.content_sha256 != source.source_registry_entry_sha256
            or capability.semantic_type_id not in registry_entry.supported_type_ids
            or capability.domain not in registry_entry.supported_domains
        ):
            blockers.add(f"{prefix}: registry capability binding mismatch")
        approval = approvals.get(capability.rights_approval_id)
        if approval is None:
            blockers.add(f"{prefix}: rights approval is missing")
        elif (
            approval.content_sha256 != capability.rights_approval_sha256
            or approval.source_id != source.source_id
            or approval.source_version != source.source_version
            or approval.source_registry_entry_id != source.source_registry_entry_id
            or approval.source_registry_entry_sha256 != source.source_registry_entry_sha256
        ):
            blockers.add(f"{prefix}: rights approval binds different source content")
        else:
            decisions = approval.permission_map()
            denied = [permission for permission in capability.permissions if not decisions[permission].permitted]
            if denied:
                blockers.add(f"{prefix}: declared capability contains denied permissions")
            if approval.expires_at <= evaluated_at or (
                approval.revoked_at is not None and approval.revoked_at <= evaluated_at
            ):
                blockers.add(f"{prefix}: rights approval is expired or revoked")
        if any(
            line.projected_monthly_use > line.approved_monthly_limit
            or line.projected_annual_use > line.approved_annual_limit
            or line.bounded_probe_use > line.approved_monthly_limit
            for line in capability.budget_lines
        ):
            blockers.add(f"{prefix}: approved capability budget is insufficient")

    requirements = {item.key: item for item in coverage_catalog.requirements}
    for entry in coverage_catalog.entries:
        requirement = requirements.get(entry.cell_key)
        if requirement is None:
            blockers.add(f"{entry.source_coverage_entry_id}: coverage row has no requirement")
            continue
        matches = [
            capability
            for capability in capability_catalog.capabilities
            if capability.source.source_id == entry.source_id
            and capability.source.source_version == entry.source_version
            and capability.source.source_registry_entry_id == entry.source_registry_entry_id
            and capability.source.source_registry_entry_sha256 == entry.source_registry_entry_sha256
            and capability.semantic_type_id == entry.semantic_type_id
            and capability.semantic_type_version == entry.semantic_type_version
            and capability.domain is entry.domain
            and entry.subject.kind in capability.subject_kinds
            and fnmatchcase(entry.partition_key, capability.partition_pattern)
            and requirement.required_permissions <= capability.permissions
            and capability.rights_approval_id == entry.rights_approval_id
            and capability.rights_approval_sha256 == entry.rights_approval_sha256
            and capability.budget_lines == entry.budget_lines
        ]
        if len(matches) != 1:
            blockers.add(f"{entry.source_coverage_entry_id}: coverage row must match exactly one source capability")

    return SourceCapabilityCoverageReport(
        source_capability_catalog_id=capability_catalog.source_capability_catalog_id,
        source_coverage_catalog_id=coverage_catalog.source_coverage_catalog_id,
        registry_snapshot_id=registry_snapshot.registry_snapshot_id,
        evaluated_at=evaluated_at,
        blocking_reason_codes=tuple(blockers),
    )


class NaturalRefreshSourceBinding(BaseModel):
    """Exact registry versions permitted by one existing natural-refresh policy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    natural_refresh_source_binding_id: str = Field(
        default="",
        pattern=r"^(?:|natural-refresh-source-binding:[0-9a-f]{64})$",
    )
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    natural_refresh_requirement_id: str = Field(pattern=r"^natural-refresh:[0-9a-f]{64}$")
    natural_refresh_requirement_sha256: str = Field(pattern=_SHA256)
    sources: tuple[NaturalRefreshSourceRef, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_and_identify(self) -> NaturalRefreshSourceBinding:
        _validate_content_ref(
            self.natural_refresh_requirement_id,
            self.natural_refresh_requirement_sha256,
            "natural refresh requirement",
        )
        sources = tuple(sorted(self.sources, key=lambda item: (item.source_id, item.source_version)))
        keys = [(item.source_id, item.source_version) for item in sources]
        if len(keys) != len(set(keys)):
            raise ValueError("natural refresh source versions must be unique")
        object.__setattr__(self, "sources", sources)
        _content_address(
            self,
            id_field="natural_refresh_source_binding_id",
            prefix="natural-refresh-source-binding",
        )
        return self


class ExactNaturalRefreshReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    exact_natural_refresh_report_id: str = Field(
        default="",
        pattern=r"^(?:|exact-natural-refresh-report:[0-9a-f]{64})$",
    )
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    natural_refresh_report_id: str = Field(pattern=r"^natural-refresh-report:[0-9a-f]{64}$")
    natural_refresh_source_binding_id: str = Field(pattern=r"^natural-refresh-source-binding:[0-9a-f]{64}$")
    registry_snapshot_id: str = Field(pattern=r"^registry-snapshot:[0-9a-f]{64}$")
    blocking_reason_codes: tuple[str, ...]

    @model_validator(mode="after")
    def normalize_and_identify(self) -> ExactNaturalRefreshReport:
        object.__setattr__(self, "blocking_reason_codes", tuple(sorted(set(self.blocking_reason_codes))))
        _content_address(self, id_field="exact_natural_refresh_report_id", prefix="exact-natural-refresh-report")
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ready(self) -> bool:
        return not self.blocking_reason_codes


def evaluate_exact_natural_refresh(
    *,
    report: NaturalRefreshReport,
    source_binding: NaturalRefreshSourceBinding,
    registry_snapshot: RegistrySnapshot,
) -> ExactNaturalRefreshReport:
    """Require every accepted refresh transition to bind an exact registry version."""

    blockers = set(report.blockers)
    requirement = report.requirement
    if (
        source_binding.natural_refresh_requirement_id != requirement.natural_refresh_requirement_id
        or source_binding.natural_refresh_requirement_sha256 != requirement.content_sha256
    ):
        blockers.add("binding.natural_refresh_requirement_mismatch")
    refs = {(item.source_id, item.source_version): item for item in source_binding.sources}
    if {item.source_id for item in source_binding.sources} != set(requirement.source_ids):
        blockers.add("binding.natural_refresh_source_set_mismatch")
    registry_sources = {(item.source_id, item.version): item for item in registry_snapshot.sources}
    for key, source_ref in refs.items():
        registry_entry = registry_sources.get(key)
        if registry_entry is None or (
            registry_entry.source_registry_entry_id != source_ref.source_registry_entry_id
            or registry_entry.content_sha256 != source_ref.source_registry_entry_sha256
        ):
            blockers.add(f"binding.natural_refresh_registry_mismatch:{key[0]}@{key[1]}")
    for transition in report.transitions:
        if (transition.source_id, transition.source_version) not in refs:
            blockers.add(
                f"binding.natural_refresh_transition_version_mismatch:"
                f"{transition.source_id}@{transition.source_version}"
            )
    return ExactNaturalRefreshReport(
        natural_refresh_report_id=report.natural_refresh_report_id,
        natural_refresh_source_binding_id=source_binding.natural_refresh_source_binding_id,
        registry_snapshot_id=registry_snapshot.registry_snapshot_id,
        blocking_reason_codes=tuple(blockers),
    )


class RequirementGraphNode(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    node_id: str = Field(pattern=_STABLE_ID)
    factor_template: FactorInvocationTemplate
    module_id: str = Field(pattern=_STABLE_ID)
    emitter_id: str = Field(pattern=_STABLE_ID)
    data_requirement_ids: tuple[str, ...] = Field(min_length=1)
    upstream_node_ids: tuple[str, ...] = ()
    usage_stages: frozenset[UsageStage] = frozenset()

    @field_validator("data_requirement_ids", "upstream_node_ids")
    @classmethod
    def validate_ids(cls, values: tuple[str, ...], info: Any) -> tuple[str, ...]:
        normalized = _require_unique_sorted(values, info.field_name)
        if info.field_name == "data_requirement_ids" and any(
            re.fullmatch(r"data-requirement:[0-9a-f]{64}", value) is None for value in normalized
        ):
            raise ValueError("data_requirement_ids must be content-addressed")
        if info.field_name == "upstream_node_ids" and any(
            re.fullmatch(_STABLE_ID, value) is None for value in normalized
        ):
            raise ValueError("upstream_node_ids must contain stable identifiers")
        return normalized

    @field_serializer("usage_stages", when_used="json")
    def serialize_usage_stages(self, values: frozenset[UsageStage]) -> list[str]:
        return sorted(value.value for value in values)

    @model_validator(mode="after")
    def derive_mandatory_stages(self) -> RequirementGraphNode:
        terminal_stage = (
            UsageStage.STRATEGY_CONSUMPTION
            if self.factor_template.factor_kind is FactorKind.STRATEGY
            else UsageStage.FACTOR_CONSUMPTION
        )
        mandatory = {
            UsageStage.CAPTURE,
            UsageStage.NORMALIZATION,
            UsageStage.SNAPSHOT_SELECTION,
            terminal_stage,
        }
        object.__setattr__(self, "usage_stages", frozenset(set(self.usage_stages) | mandatory))
        return self


class CatalogRootBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    catalog_entry_id: str = Field(pattern=r"^catalog-entry:[0-9a-f]{64}$")
    node_id: str = Field(pattern=_STABLE_ID)


class RequirementGraphManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    requirement_graph_id: str = Field(default="", pattern=r"^(?:|requirement-graph:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    graph_version: RegistryVersion
    research_catalog_id: str = Field(pattern=r"^research-catalog:[0-9a-f]{64}$")
    research_catalog_sha256: str = Field(pattern=_SHA256)
    roots: tuple[CatalogRootBinding, ...] = Field(min_length=1)
    nodes: tuple[RequirementGraphNode, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_and_identify(self) -> RequirementGraphManifest:
        _validate_content_ref(self.research_catalog_id, self.research_catalog_sha256, "research catalog")
        roots = tuple(sorted(self.roots, key=lambda item: item.catalog_entry_id))
        nodes = tuple(sorted(self.nodes, key=lambda item: item.node_id))
        if len({item.catalog_entry_id for item in roots}) != len(roots):
            raise ValueError("Catalog roots must have unique catalog entries")
        if len({item.node_id for item in roots}) != len(roots):
            raise ValueError("Catalog roots must have unique graph nodes")
        if len({item.node_id for item in nodes}) != len(nodes):
            raise ValueError("requirement graph node IDs must be unique")
        object.__setattr__(self, "roots", roots)
        object.__setattr__(self, "nodes", nodes)
        _content_address(self, id_field="requirement_graph_id", prefix="requirement-graph")
        return self


class ScheduledRequirementPartitions(BaseModel):
    """Exact finite partition-resolver output for one requirement and cutoff."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    partition_selection_id: str = Field(
        default="",
        pattern=r"^(?:|scheduled-requirement-partitions:[0-9a-f]{64})$",
    )
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    data_requirement_id: str = Field(pattern=r"^data-requirement:[0-9a-f]{64}$")
    valid_period_rule_id: str = Field(pattern=_STABLE_ID)
    window_start: datetime | None = None
    window_end: datetime
    partition_keys: tuple[str, ...] = Field(min_length=1)
    resolver_id: str = Field(pattern=_STABLE_ID)
    resolver_version: RegistryVersion
    resolver_implementation_sha256: str = Field(pattern=_SHA256)

    @field_validator("window_start", "window_end")
    @classmethod
    def validate_datetimes(cls, value: datetime | None, info: Any) -> datetime | None:
        return None if value is None else _require_aware(value, info.field_name)

    @field_validator("partition_keys")
    @classmethod
    def validate_partition_keys(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = _require_unique_sorted(values, "partition_keys")
        if any(not value.strip() for value in normalized):
            raise ValueError("partition_keys cannot contain blank values")
        return normalized

    @model_validator(mode="after")
    def validate_and_identify(self) -> ScheduledRequirementPartitions:
        if self.window_start is not None and self.window_start > self.window_end:
            raise ValueError("partition window_start cannot postdate window_end")
        _content_address(
            self,
            id_field="partition_selection_id",
            prefix="scheduled-requirement-partitions",
        )
        return self


class ScheduledCatalogInvocation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scheduled_invocation_id: str = Field(
        default="",
        pattern=r"^(?:|scheduled-invocation:[0-9a-f]{64})$",
    )
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    run_id: str = Field(pattern=_STABLE_ID)
    catalog_entry_id: str = Field(pattern=r"^catalog-entry:[0-9a-f]{64}$")
    scheduled_for: datetime
    as_of: datetime
    valid_on: date
    requirement_partitions: tuple[ScheduledRequirementPartitions, ...] = Field(min_length=1)

    @field_validator("scheduled_for", "as_of")
    @classmethod
    def validate_datetimes(cls, value: datetime, info: Any) -> datetime:
        return _require_aware(value, info.field_name)

    @model_validator(mode="after")
    def validate_and_identify(self) -> ScheduledCatalogInvocation:
        if self.as_of > self.scheduled_for:
            raise ValueError("as_of cannot postdate scheduled_for")
        selections = tuple(sorted(self.requirement_partitions, key=lambda item: item.data_requirement_id))
        requirement_ids = [item.data_requirement_id for item in selections]
        if len(requirement_ids) != len(set(requirement_ids)):
            raise ValueError("scheduled requirement partition selections must be unique")
        object.__setattr__(self, "requirement_partitions", selections)
        _content_address(self, id_field="scheduled_invocation_id", prefix="scheduled-invocation")
        return self


class DemandSchedule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    demand_schedule_id: str = Field(default="", pattern=r"^(?:|demand-schedule:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    schedule_version: RegistryVersion
    research_catalog_id: str = Field(pattern=r"^research-catalog:[0-9a-f]{64}$")
    research_catalog_sha256: str = Field(pattern=_SHA256)
    universe: UniverseRef
    applicability_catalog_id: str = Field(pattern=r"^applicability:[0-9a-f]{64}$")
    applicability_catalog_sha256: str = Field(pattern=_SHA256)
    invocations: tuple[ScheduledCatalogInvocation, ...] = Field(min_length=1)
    effective_at: datetime

    @field_validator("effective_at")
    @classmethod
    def validate_effective_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "effective_at")

    @model_validator(mode="after")
    def validate_and_identify(self) -> DemandSchedule:
        _validate_content_ref(self.research_catalog_id, self.research_catalog_sha256, "research catalog")
        _validate_content_ref(
            self.applicability_catalog_id,
            self.applicability_catalog_sha256,
            "applicability catalog",
        )
        invocations = tuple(sorted(self.invocations, key=lambda item: item.scheduled_invocation_id))
        ids = [item.scheduled_invocation_id for item in invocations]
        if len(ids) != len(set(ids)):
            raise ValueError("scheduled invocations must be unique")
        if any(item.scheduled_for < self.effective_at for item in invocations):
            raise ValueError("scheduled invocation predates schedule effective_at")
        object.__setattr__(self, "invocations", invocations)
        _content_address(self, id_field="demand_schedule_id", prefix="demand-schedule")
        return self


class PlannedUsageRequirement(BaseModel):
    """One independently accountable run/module/emitter/stage expectation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    planned_usage_requirement_id: str = Field(
        default="",
        pattern=r"^(?:|planned-usage-requirement:[0-9a-f]{64})$",
    )
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    run_id: str = Field(pattern=_STABLE_ID)
    scheduled_invocation_id: str = Field(pattern=r"^scheduled-invocation:[0-9a-f]{64}$")
    catalog_entry_id: str = Field(pattern=r"^catalog-entry:[0-9a-f]{64}$")
    catalog_alias: str = Field(pattern=_STABLE_ID)
    graph_node_id: str = Field(pattern=_STABLE_ID)
    module_id: str = Field(pattern=_STABLE_ID)
    planned_cell_id: str = Field(pattern=r"^planned-demand-cell:[0-9a-f]{64}$")
    level: RequirementLevel
    stage: UsageStage
    emitter_kind: UsageEmitterKind
    emitter_id: str = Field(pattern=_STABLE_ID)

    @model_validator(mode="after")
    def validate_and_identify(self) -> PlannedUsageRequirement:
        manifest_stages = {UsageStage.CAPTURE, UsageStage.NORMALIZATION}
        expected_emitter = (
            UsageEmitterKind.CAPTURE_MANIFEST_EVALUATOR
            if self.stage in manifest_stages
            else UsageEmitterKind.INSTRUMENTED_RUNNER
        )
        if self.emitter_kind is not expected_emitter:
            raise ValueError(f"{self.stage.value} requires {expected_emitter.value}")
        _content_address(
            self,
            id_field="planned_usage_requirement_id",
            prefix="planned-usage-requirement",
        )
        return self


class CompiledRunDemand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    compiled_run_demand_id: str = Field(default="", pattern=r"^(?:|compiled-run-demand:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    run_id: str = Field(pattern=_STABLE_ID)
    input_cells: tuple[PlannedDemandCell, ...]
    usage_requirements: tuple[PlannedUsageRequirement, ...]
    not_applicable_cell_ids: tuple[str, ...] = ()

    @field_validator("not_applicable_cell_ids")
    @classmethod
    def validate_not_applicable_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = _require_unique_sorted(values, "not_applicable_cell_ids")
        if any(re.fullmatch(r"applicability-cell:[0-9a-f]{64}", value) is None for value in normalized):
            raise ValueError("not_applicable_cell_ids must be content-addressed")
        return normalized

    @model_validator(mode="after")
    def validate_and_identify(self) -> CompiledRunDemand:
        cells = tuple(sorted(self.input_cells, key=lambda item: item.planned_cell_id))
        usages = tuple(sorted(self.usage_requirements, key=lambda item: item.planned_usage_requirement_id))
        cell_ids = [item.planned_cell_id for item in cells]
        usage_ids = [item.planned_usage_requirement_id for item in usages]
        if len(cell_ids) != len(set(cell_ids)):
            raise ValueError("compiled input cells must be unique")
        if len(usage_ids) != len(set(usage_ids)):
            raise ValueError("planned usage requirements must be unique")
        if any(item.run_id != self.run_id for item in usages):
            raise ValueError("planned usage requirement belongs to a different run")
        if any(item.planned_cell_id not in set(cell_ids) for item in usages):
            raise ValueError("planned usage requirement references a missing input cell")
        object.__setattr__(self, "input_cells", cells)
        object.__setattr__(self, "usage_requirements", usages)
        _content_address(self, id_field="compiled_run_demand_id", prefix="compiled-run-demand")
        return self


class ExpectedDemandPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    expected_demand_plan_id: str = Field(default="", pattern=r"^(?:|expected-demand-plan:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    research_catalog_id: str = Field(pattern=r"^research-catalog:[0-9a-f]{64}$")
    research_catalog_sha256: str = Field(pattern=_SHA256)
    requirement_graph_id: str = Field(pattern=r"^requirement-graph:[0-9a-f]{64}$")
    requirement_graph_sha256: str = Field(pattern=_SHA256)
    demand_schedule_id: str = Field(pattern=r"^demand-schedule:[0-9a-f]{64}$")
    demand_schedule_sha256: str = Field(pattern=_SHA256)
    universe: UniverseRef
    applicability_catalog_id: str = Field(pattern=r"^applicability:[0-9a-f]{64}$")
    applicability_catalog_sha256: str = Field(pattern=_SHA256)
    capture_scope_id: str = Field(pattern=r"^capture-scope:[0-9a-f]{64}$")
    capture_scope_sha256: str = Field(pattern=_SHA256)
    runs: tuple[CompiledRunDemand, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_and_identify(self) -> ExpectedDemandPlan:
        for reference_id, content_sha256, label in (
            (self.research_catalog_id, self.research_catalog_sha256, "research catalog"),
            (self.requirement_graph_id, self.requirement_graph_sha256, "requirement graph"),
            (self.demand_schedule_id, self.demand_schedule_sha256, "demand schedule"),
            (self.applicability_catalog_id, self.applicability_catalog_sha256, "applicability catalog"),
            (self.capture_scope_id, self.capture_scope_sha256, "capture scope"),
        ):
            _validate_content_ref(reference_id, content_sha256, label)
        runs = tuple(sorted(self.runs, key=lambda item: item.run_id))
        if len({item.run_id for item in runs}) != len(runs):
            raise ValueError("compiled runs must be unique")
        object.__setattr__(self, "runs", runs)
        _content_address(self, id_field="expected_demand_plan_id", prefix="expected-demand-plan")
        return self


class PlannedUsageEvidence(BaseModel):
    """Explicit one-to-one binding from an observed event to every planned coordinate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    planned_usage_evidence_id: str = Field(
        default="",
        pattern=r"^(?:|planned-usage-evidence:[0-9a-f]{64})$",
    )
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    planned_usage_requirement_id: str = Field(pattern=r"^planned-usage-requirement:[0-9a-f]{64}$")
    run_id: str = Field(pattern=_STABLE_ID)
    scheduled_invocation_id: str = Field(pattern=r"^scheduled-invocation:[0-9a-f]{64}$")
    module_id: str = Field(pattern=_STABLE_ID)
    emitter_id: str = Field(pattern=_STABLE_ID)
    stage: UsageStage
    planned_cell_id: str = Field(pattern=r"^planned-demand-cell:[0-9a-f]{64}$")
    usage_event: DataUsageEvent

    @model_validator(mode="after")
    def validate_and_identify(self) -> PlannedUsageEvidence:
        event = self.usage_event
        if (
            event.run_id != self.run_id
            or event.emitter_id != self.emitter_id
            or event.stage is not self.stage
            or event.planned_cell_id != self.planned_cell_id
        ):
            raise ValueError("usage event does not match the explicit planned-usage evidence coordinates")
        _content_address(self, id_field="planned_usage_evidence_id", prefix="planned-usage-evidence")
        return self


class ExpectedUsageReconciliation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    expected_usage_reconciliation_id: str = Field(
        default="",
        pattern=r"^(?:|expected-usage-reconciliation:[0-9a-f]{64})$",
    )
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    expected_demand_plan_id: str = Field(pattern=r"^expected-demand-plan:[0-9a-f]{64}$")
    planned_usage_evidence_ids: tuple[str, ...]
    blocking_reason_codes: tuple[str, ...]

    @model_validator(mode="after")
    def normalize_and_identify(self) -> ExpectedUsageReconciliation:
        evidence_ids = _require_unique_sorted(self.planned_usage_evidence_ids, "planned_usage_evidence_ids")
        object.__setattr__(self, "planned_usage_evidence_ids", evidence_ids)
        object.__setattr__(self, "blocking_reason_codes", tuple(sorted(set(self.blocking_reason_codes))))
        _content_address(
            self,
            id_field="expected_usage_reconciliation_id",
            prefix="expected-usage-reconciliation",
        )
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ready(self) -> bool:
        return not self.blocking_reason_codes


def reconcile_expected_usage(
    *,
    expected_demand: ExpectedDemandPlan,
    evidence: tuple[PlannedUsageEvidence, ...],
) -> ExpectedUsageReconciliation:
    """Reconcile exact usage identities; one event can satisfy at most one requirement."""

    requirements = {
        item.planned_usage_requirement_id: item for run in expected_demand.runs for item in run.usage_requirements
    }
    blockers: set[str] = set()
    evidence_by_requirement: dict[str, list[PlannedUsageEvidence]] = defaultdict(list)
    event_ids: list[str] = []
    evidence_ids: list[str] = []
    for item in evidence:
        evidence_ids.append(item.planned_usage_evidence_id)
        event_ids.append(item.usage_event.usage_event_id)
        requirement = requirements.get(item.planned_usage_requirement_id)
        if requirement is None:
            blockers.add(f"usage.unexpected_requirement:{item.planned_usage_requirement_id}")
            continue
        evidence_by_requirement[item.planned_usage_requirement_id].append(item)
        event = item.usage_event
        if (
            item.run_id != requirement.run_id
            or item.scheduled_invocation_id != requirement.scheduled_invocation_id
            or item.module_id != requirement.module_id
            or item.emitter_id != requirement.emitter_id
            or item.stage is not requirement.stage
            or item.planned_cell_id != requirement.planned_cell_id
            or event.emitter_kind is not requirement.emitter_kind
        ):
            blockers.add(f"usage.binding_mismatch:{item.planned_usage_requirement_id}")
    if len(evidence_ids) != len(set(evidence_ids)):
        blockers.add("usage.duplicate_evidence_identity")
    if len(event_ids) != len(set(event_ids)):
        blockers.add("usage.event_reused_across_requirements")
    for requirement_id, requirement in requirements.items():
        matches = evidence_by_requirement.get(requirement_id, [])
        if len(matches) > 1:
            blockers.add(f"usage.multiple_events_for_requirement:{requirement_id}")
        if requirement.level is RequirementLevel.REQUIRED and not matches:
            blockers.add(f"usage.missing_required:{requirement_id}")
    return ExpectedUsageReconciliation(
        expected_demand_plan_id=expected_demand.expected_demand_plan_id,
        planned_usage_evidence_ids=tuple(evidence_ids),
        blocking_reason_codes=tuple(blockers),
    )


def _validate_requirement_graph(
    research_catalog: ResearchCatalogManifest,
    requirement_graph: RequirementGraphManifest,
    data_requirements: tuple[DataRequirement, ...],
) -> tuple[dict[str, RequirementGraphNode], dict[str, str]]:
    entry_by_id = {item.catalog_entry_id: item for item in research_catalog.entries}
    node_by_id = {item.node_id: item for item in requirement_graph.nodes}
    root_by_entry = {item.catalog_entry_id: item.node_id for item in requirement_graph.roots}
    if set(root_by_entry) != set(entry_by_id):
        raise ValueError("requirement graph roots must exactly cover Research Catalog entries")
    for entry_id, node_id in root_by_entry.items():
        node = node_by_id.get(node_id)
        if node is None:
            raise ValueError(f"Catalog root references an unknown graph node: {node_id}")
        factor_template = entry_by_id[entry_id].invocation_template.factor_template
        if node.factor_template.factor_template_id != factor_template.factor_template_id:
            raise ValueError("Catalog root graph node binds a different factor template")

    for node in requirement_graph.nodes:
        unknown = set(node.upstream_node_ids) - set(node_by_id)
        if unknown:
            raise ValueError(f"requirement graph references unknown upstream nodes: {sorted(unknown)}")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in visiting:
            raise ValueError("requirement graph contains a cycle")
        if node_id in visited:
            return
        visiting.add(node_id)
        for upstream_id in node_by_id[node_id].upstream_node_ids:
            visit(upstream_id)
        visiting.remove(node_id)
        visited.add(node_id)

    for node_id in node_by_id:
        visit(node_id)

    for node in requirement_graph.nodes:
        if set(node.data_requirement_ids) != set(node.factor_template.data_requirement_ids):
            raise ValueError("graph node requirements drift from its factor template")
        expected_dependencies = {dependency.template_id for dependency in node.factor_template.dependencies}
        actual_dependencies = {
            node_by_id[upstream_id].factor_template.factor_template_id for upstream_id in node.upstream_node_ids
        }
        if actual_dependencies != expected_dependencies:
            raise ValueError("graph node dependencies drift from its factor template")

    reachable: set[str] = set()

    def mark_reachable(node_id: str) -> None:
        if node_id in reachable:
            return
        reachable.add(node_id)
        for upstream_id in node_by_id[node_id].upstream_node_ids:
            mark_reachable(upstream_id)

    for node_id in root_by_entry.values():
        mark_reachable(node_id)
    orphaned = set(node_by_id) - reachable
    if orphaned:
        raise ValueError(f"requirement graph contains orphan nodes: {sorted(orphaned)}")

    declared_requirement_ids = {item.requirement_id for item in data_requirements}
    graph_requirement_ids = {item for node in requirement_graph.nodes for item in node.data_requirement_ids}
    missing = graph_requirement_ids - declared_requirement_ids
    extra = declared_requirement_ids - graph_requirement_ids
    if missing or extra:
        raise ValueError(
            f"data requirements do not exactly cover graph demand; missing={sorted(missing)}, extra={sorted(extra)}"
        )
    return node_by_id, root_by_entry


def _active_membership_subjects(
    *,
    universe_memberships: tuple[UniverseMembership, ...],
    invocation: ScheduledCatalogInvocation,
) -> set[SubjectRef]:
    active: set[SubjectRef] = set()
    for membership in universe_memberships:
        if membership.knowable_at > invocation.as_of:
            continue
        if membership.valid_from <= invocation.valid_on and (
            membership.valid_to is None or invocation.valid_on <= membership.valid_to
        ):
            active.add(membership.subject)
    if not active:
        raise ValueError(f"scheduled invocation has no point-in-time universe membership: {invocation.run_id}")
    return active


def _reachable_nodes(root_id: str, node_by_id: dict[str, RequirementGraphNode]) -> tuple[RequirementGraphNode, ...]:
    seen: set[str] = set()

    def collect(node_id: str) -> None:
        if node_id in seen:
            return
        seen.add(node_id)
        for upstream_id in node_by_id[node_id].upstream_node_ids:
            collect(upstream_id)

    collect(root_id)
    return tuple(node_by_id[node_id] for node_id in sorted(seen))


def _applicability_cell_id(cell: ApplicabilityCell) -> str:
    return f"applicability-cell:{canonical_sha256(cell.model_dump(mode='json'))}"


def compile_expected_demand(
    *,
    research_catalog: ResearchCatalogManifest,
    requirement_graph: RequirementGraphManifest,
    schedule: DemandSchedule,
    universe_manifest: UniverseManifest,
    universe_memberships: tuple[UniverseMembership, ...],
    applicability: ApplicabilityCatalog,
    capture_scope: CaptureScope,
    data_requirements: tuple[DataRequirement, ...],
) -> ExpectedDemandPlan:
    """Compile the closed expected-demand denominator or reject any drift."""

    for actual_id, actual_hash, expected_id, expected_hash, label in (
        (
            requirement_graph.research_catalog_id,
            requirement_graph.research_catalog_sha256,
            research_catalog.research_catalog_id,
            research_catalog.content_sha256,
            "requirement graph Research Catalog",
        ),
        (
            schedule.research_catalog_id,
            schedule.research_catalog_sha256,
            research_catalog.research_catalog_id,
            research_catalog.content_sha256,
            "schedule Research Catalog",
        ),
        (
            applicability.research_catalog_id,
            applicability.research_catalog_sha256,
            research_catalog.research_catalog_id,
            research_catalog.content_sha256,
            "applicability Research Catalog",
        ),
        (
            capture_scope.research_catalog_id,
            capture_scope.research_catalog_sha256,
            research_catalog.research_catalog_id,
            research_catalog.content_sha256,
            "capture-scope Research Catalog",
        ),
        (
            schedule.applicability_catalog_id,
            schedule.applicability_catalog_sha256,
            applicability.applicability_catalog_id,
            applicability.content_sha256,
            "schedule applicability catalog",
        ),
        (
            capture_scope.applicability_catalog_id,
            capture_scope.applicability_catalog_sha256,
            applicability.applicability_catalog_id,
            applicability.content_sha256,
            "capture-scope applicability catalog",
        ),
    ):
        if (actual_id, actual_hash) != (expected_id, expected_hash):
            raise ValueError(f"{label} binding does not match exact content")

    universe = universe_manifest.ref
    if any(
        candidate != universe
        for candidate in (
            research_catalog.scope_floor.universe,
            schedule.universe,
            applicability.universe,
            capture_scope.universe,
        )
    ):
        raise ValueError("policy inputs do not bind the exact UniverseRef")
    if universe_manifest.definition_kind is not UniverseDefinitionKind.FIXED_COHORT:
        raise ValueError(
            "this finite descriptive-demand compiler accepts only an exact fixed cohort; "
            "PIT/survivorship claims require a content-addressed resolver-output manifest"
        )

    membership_by_id = {item.membership_id: item for item in universe_memberships}
    if len(membership_by_id) != len(universe_memberships):
        raise ValueError("universe memberships must be unique")
    if set(membership_by_id) != set(universe_manifest.membership_ids):
        raise ValueError("universe memberships do not exactly cover the fixed cohort")
    if any(item.universe_id != universe.universe_id for item in universe_memberships):
        raise ValueError("universe membership binds a different universe")

    node_by_id, root_by_entry = _validate_requirement_graph(
        research_catalog,
        requirement_graph,
        data_requirements,
    )
    requirement_by_id = {item.requirement_id: item for item in data_requirements}
    if len(requirement_by_id) != len(data_requirements):
        raise ValueError("data requirements must be unique")
    compile_capture_requirement_bindings(data_requirements, capture_scope.requirements)

    entry_by_id = {item.catalog_entry_id: item for item in research_catalog.entries}
    scheduled_entry_ids = {item.catalog_entry_id for item in schedule.invocations}
    unknown_scheduled = scheduled_entry_ids - set(entry_by_id)
    if unknown_scheduled:
        raise ValueError(f"schedule references unknown Catalog entries: {sorted(unknown_scheduled)}")
    if scheduled_entry_ids != set(entry_by_id):
        raise ValueError("schedule must contain at least one invocation for every Research Catalog entry")
    earliest_execution = min(item.scheduled_for for item in schedule.invocations)
    for effective_at, label in (
        (research_catalog.effective_at, "Research Catalog"),
        (universe_manifest.effective_at, "universe manifest"),
        (applicability.effective_at, "applicability catalog"),
        (capture_scope.effective_at, "capture scope"),
    ):
        if effective_at > earliest_execution:
            raise ValueError(f"{label} postdates scheduled execution")

    expected_cells: dict[tuple[str, str, str, str, str, DataDomain, str], list[ScheduledCatalogInvocation]] = (
        defaultdict(list)
    )
    invocation_nodes: dict[str, tuple[RequirementGraphNode, ...]] = {}
    invocation_partitions: dict[str, dict[str, tuple[str, ...]]] = {}
    for invocation in schedule.invocations:
        entry = entry_by_id[invocation.catalog_entry_id]
        active_subjects = _active_membership_subjects(
            universe_memberships=universe_memberships,
            invocation=invocation,
        )
        missing_subjects = set(entry.subject_scope) - active_subjects
        if missing_subjects:
            raise ValueError(
                f"Catalog subject is outside point-in-time universe membership: {sorted(map(str, missing_subjects))}"
            )
        nodes = _reachable_nodes(root_by_entry[entry.catalog_entry_id], node_by_id)
        invocation_nodes[invocation.scheduled_invocation_id] = nodes
        required_ids = {requirement_id for node in nodes for requirement_id in node.data_requirement_ids}
        selections = {item.data_requirement_id: item for item in invocation.requirement_partitions}
        if set(selections) != required_ids:
            raise ValueError("scheduled partition selections must exactly cover reachable DataRequirements")
        invocation_partitions[invocation.scheduled_invocation_id] = {}
        for requirement_id, selection in selections.items():
            requirement = requirement_by_id[requirement_id]
            if selection.valid_period_rule_id != requirement.valid_period_rule_id:
                raise ValueError("scheduled partition selection binds a different valid-period rule")
            expected_start = None if requirement.lookback is None else invocation.as_of - requirement.lookback
            if selection.window_start != expected_start or selection.window_end != invocation.as_of:
                raise ValueError("scheduled partition selection does not cover the exact requirement lookback window")
            invocation_partitions[invocation.scheduled_invocation_id][requirement_id] = selection.partition_keys
        for node in nodes:
            for requirement_id in node.data_requirement_ids:
                requirement = requirement_by_id[requirement_id]
                incompatible = {subject.kind for subject in entry.subject_scope} - set(requirement.subject_kinds)
                if incompatible:
                    raise ValueError("graph DataRequirement is incompatible with the Catalog subject scope")
                for partition_key in invocation_partitions[invocation.scheduled_invocation_id][requirement_id]:
                    for subject in entry.subject_scope:
                        key = (
                            node.module_id,
                            entry.catalog_alias,
                            requirement_id,
                            subject.kind.value,
                            subject.id,
                            requirement.domain,
                            partition_key,
                        )
                        expected_cells[key].append(invocation)

    applicability_by_key = applicability.cell_map()
    missing_applicability = set(expected_cells) - set(applicability_by_key)
    extra_applicability = set(applicability_by_key) - set(expected_cells)
    if missing_applicability or extra_applicability:
        raise ValueError(
            "applicability does not exactly cover scheduled graph demand; "
            f"missing={len(missing_applicability)}, extra={len(extra_applicability)}"
        )
    for key, invocations in expected_cells.items():
        if applicability_by_key[key].effective_at > min(item.scheduled_for for item in invocations):
            raise ValueError("applicability cell postdates scheduled execution")

    run_invocations: dict[str, list[ScheduledCatalogInvocation]] = defaultdict(list)
    for invocation in schedule.invocations:
        run_invocations[invocation.run_id].append(invocation)

    compiled_runs: list[CompiledRunDemand] = []
    for run_id, invocations in sorted(run_invocations.items()):
        cell_specs: dict[str, dict[str, Any]] = {}
        usages: list[PlannedUsageRequirement] = []
        not_applicable_ids: set[str] = set()
        for invocation in sorted(invocations, key=lambda item: item.scheduled_invocation_id):
            entry = entry_by_id[invocation.catalog_entry_id]
            for node in invocation_nodes[invocation.scheduled_invocation_id]:
                for requirement_id in node.data_requirement_ids:
                    requirement = requirement_by_id[requirement_id]
                    for partition_key in invocation_partitions[invocation.scheduled_invocation_id][requirement_id]:
                        for subject in entry.subject_scope:
                            applicability_key = (
                                node.module_id,
                                entry.catalog_alias,
                                requirement_id,
                                subject.kind.value,
                                subject.id,
                                requirement.domain,
                                partition_key,
                            )
                            applicability_cell = applicability_by_key[applicability_key]
                            if applicability_cell.classification is ApplicabilityClassification.NOT_APPLICABLE:
                                not_applicable_ids.add(_applicability_cell_id(applicability_cell))
                                continue
                            level = (
                                RequirementLevel.REQUIRED
                                if applicability_cell.classification is ApplicabilityClassification.REQUIRED
                                else RequirementLevel.OPTIONAL
                            )
                            planned_cell_id = planned_cell_id_for(
                                requirement_id=requirement.requirement_id,
                                capture_requirement_id=requirement.capture_requirement_id,
                                semantic_type_id=requirement.semantic_type_id,
                                domain=requirement.domain,
                                subject=subject,
                                partition_key=partition_key,
                            )
                            spec = cell_specs.setdefault(
                                planned_cell_id,
                                {
                                    "requirement": requirement,
                                    "subject": subject,
                                    "partition_key": partition_key,
                                    "level": level,
                                    "stages": set(),
                                },
                            )
                            if level is RequirementLevel.REQUIRED:
                                spec["level"] = RequirementLevel.REQUIRED
                            spec["stages"].update(node.usage_stages)
                            for stage in sorted(node.usage_stages, key=lambda item: item.value):
                                emitter_kind = (
                                    UsageEmitterKind.CAPTURE_MANIFEST_EVALUATOR
                                    if stage in {UsageStage.CAPTURE, UsageStage.NORMALIZATION}
                                    else UsageEmitterKind.INSTRUMENTED_RUNNER
                                )
                                usages.append(
                                    PlannedUsageRequirement(
                                        run_id=run_id,
                                        scheduled_invocation_id=invocation.scheduled_invocation_id,
                                        catalog_entry_id=entry.catalog_entry_id,
                                        catalog_alias=entry.catalog_alias,
                                        graph_node_id=node.node_id,
                                        module_id=node.module_id,
                                        planned_cell_id=planned_cell_id,
                                        level=level,
                                        stage=stage,
                                        emitter_kind=emitter_kind,
                                        emitter_id=node.emitter_id,
                                    )
                                )

        input_cells = tuple(
            PlannedDemandCell(
                planned_cell_id=planned_cell_id,
                requirement_id=spec["requirement"].requirement_id,
                capture_requirement_id=spec["requirement"].capture_requirement_id,
                semantic_type_id=spec["requirement"].semantic_type_id,
                domain=spec["requirement"].domain,
                subject=spec["subject"],
                partition_key=spec["partition_key"],
                level=spec["level"],
                expected_stages=frozenset(spec["stages"]),
            )
            for planned_cell_id, spec in sorted(cell_specs.items())
        )
        compiled_runs.append(
            CompiledRunDemand(
                run_id=run_id,
                input_cells=input_cells,
                usage_requirements=tuple(usages),
                not_applicable_cell_ids=tuple(not_applicable_ids),
            )
        )

    return ExpectedDemandPlan(
        research_catalog_id=research_catalog.research_catalog_id,
        research_catalog_sha256=research_catalog.content_sha256,
        requirement_graph_id=requirement_graph.requirement_graph_id,
        requirement_graph_sha256=requirement_graph.content_sha256,
        demand_schedule_id=schedule.demand_schedule_id,
        demand_schedule_sha256=schedule.content_sha256,
        universe=universe,
        applicability_catalog_id=applicability.applicability_catalog_id,
        applicability_catalog_sha256=applicability.content_sha256,
        capture_scope_id=capture_scope.capture_scope_id,
        capture_scope_sha256=capture_scope.content_sha256,
        runs=tuple(compiled_runs),
    )
