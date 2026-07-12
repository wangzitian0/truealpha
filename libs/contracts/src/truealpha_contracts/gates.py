"""Fail-closed operational and Production-graduation gates.

The models in this module join the immutable contracts owned by the registry,
source-readiness, capture, release, and SLO layers.  They intentionally do not
accept caller-supplied readiness or graduation flags.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

from truealpha_contracts.capture_contracts import CaptureEvaluationReport, CaptureScope
from truealpha_contracts.catalog import ResearchCatalogManifest
from truealpha_contracts.common import CaptureEnvironment, canonical_sha256
from truealpha_contracts.data_quality import DataDomain
from truealpha_contracts.models import _require_aware
from truealpha_contracts.readiness import (
    ApplicabilityCatalog,
    BudgetDimension,
    ConsumerSloReport,
    EvaluationStatus,
    FallbackPolicy,
    ModuleSloReport,
    NaturalRefreshReport,
    SourceCoverageCatalog,
    SourceCoverageEntry,
    SourceReadinessReport,
    SourceRole,
    SourceUsagePermission,
    UsageTelemetryReport,
)
from truealpha_contracts.registries import RegistrySnapshot, RegistryVersion, SourceId
from truealpha_contracts.release import ReleaseManifest
from truealpha_contracts.universe import (
    SubjectKind,
    SubjectRef,
    UniverseDefinitionKind,
    UniverseManifest,
    UniverseMembership,
    UniverseRef,
)

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_CONTENT_SHA256_PATTERN = r"^(?:|[0-9a-f]{64})$"
_STABLE_ID_PATTERN = r"^[a-z0-9][A-Za-z0-9._:/@+\-]*$"
_SIGNATURE_ID_PATTERN = r"^[a-z][a-z0-9._:/-]*$"
_SHA256 = re.compile(_SHA256_PATTERN)


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _content_address(model: BaseModel, *, id_field: str, prefix: str) -> None:
    payload = model.model_dump(mode="json", exclude={id_field, "content_sha256"})
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


def _reference_matches(reference_id: str, content_sha256: str) -> bool:
    return reference_id.rsplit(":", 1)[-1] == content_sha256


def _normalize_unique(values: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} must not contain duplicates")
    return tuple(sorted(values))


class RegistryCallState(StrEnum):
    ENABLED = "enabled"
    DISABLED = "disabled"


class SourceRegistryOperationalState(_StrictFrozenModel):
    """Append-only call state for an entry retained in an immutable registry."""

    operational_state_id: str = Field(
        default="",
        pattern=r"^(?:|source-operational-state:[0-9a-f]{64})$",
    )
    content_sha256: str = Field(default="", pattern=_CONTENT_SHA256_PATTERN)
    registry_snapshot_id: str = Field(pattern=r"^registry-snapshot:[0-9a-f]{64}$")
    registry_snapshot_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_registry_entry_id: str = Field(pattern=r"^source-registry-entry:[0-9a-f]{64}$")
    source_registry_entry_sha256: str = Field(pattern=_SHA256_PATTERN)
    call_state: RegistryCallState
    effective_at: datetime
    recorded_by: str = Field(min_length=1)
    decision_evidence_id: str = Field(min_length=1)
    decision_evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    disabled_reason: str | None = None

    @field_validator("effective_at")
    @classmethod
    def validate_effective_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "effective_at")

    @model_validator(mode="after")
    def identify(self) -> SourceRegistryOperationalState:
        if not _reference_matches(self.registry_snapshot_id, self.registry_snapshot_sha256):
            raise ValueError("registry snapshot ID and hash do not match")
        if not _reference_matches(self.source_registry_entry_id, self.source_registry_entry_sha256):
            raise ValueError("source registry entry ID and hash do not match")
        if self.call_state is RegistryCallState.DISABLED and not self.disabled_reason:
            raise ValueError("disabled source entries require a reason")
        if self.call_state is RegistryCallState.ENABLED and self.disabled_reason is not None:
            raise ValueError("enabled source entries cannot carry a disabled reason")
        _content_address(self, id_field="operational_state_id", prefix="source-operational-state")
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def callable(self) -> bool:
        return self.call_state is RegistryCallState.ENABLED


class SourceCallIntent(_StrictFrozenModel):
    """One exact source operation that must be preflighted before execution."""

    source_call_intent_id: str = Field(default="", pattern=r"^(?:|source-call-intent:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=_CONTENT_SHA256_PATTERN)
    operation_id: str = Field(pattern=_STABLE_ID_PATTERN)
    registry_snapshot_id: str = Field(pattern=r"^registry-snapshot:[0-9a-f]{64}$")
    registry_snapshot_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_id: SourceId
    source_version: RegistryVersion
    source_registry_entry_id: str = Field(pattern=r"^source-registry-entry:[0-9a-f]{64}$")
    source_registry_entry_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_coverage_entry_id: str = Field(pattern=r"^source-coverage-entry:[0-9a-f]{64}$")
    environment: CaptureEnvironment
    subject: SubjectRef
    domain: DataDomain
    partition_key: str = Field(min_length=1)
    required_permissions: frozenset[SourceUsagePermission] = Field(min_length=1)
    intended_call_at: datetime
    maximum_preflight_age: timedelta

    @field_validator("intended_call_at")
    @classmethod
    def validate_intended_call_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "intended_call_at")

    @model_validator(mode="after")
    def validate_intent(self) -> SourceCallIntent:
        if self.maximum_preflight_age <= timedelta(0):
            raise ValueError("maximum_preflight_age must be positive")
        if not _reference_matches(self.registry_snapshot_id, self.registry_snapshot_sha256):
            raise ValueError("registry snapshot ID and hash do not match")
        if not _reference_matches(self.source_registry_entry_id, self.source_registry_entry_sha256):
            raise ValueError("source registry entry ID and hash do not match")
        publication_permissions = {
            SourceUsagePermission.PUBLIC_REPORTS,
            SourceUsagePermission.PUBLIC_CARDS,
        }
        if SourceUsagePermission.RAW_RETENTION not in self.required_permissions:
            raise ValueError("source calls must preflight raw-retention rights")
        if not publication_permissions.intersection(self.required_permissions):
            raise ValueError("source calls must preflight a publication permission")
        _content_address(self, id_field="source_call_intent_id", prefix="source-call-intent")
        return self


class SourceCallPreflightReport(_StrictFrozenModel):
    """Derived authorization for a single future source call."""

    intent: SourceCallIntent
    registry_snapshot: RegistrySnapshot
    source_readiness: SourceReadinessReport
    operational_states: tuple[SourceRegistryOperationalState, ...] = Field(min_length=1)
    evaluated_at: datetime

    @field_validator("evaluated_at")
    @classmethod
    def validate_evaluated_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "evaluated_at")

    @model_validator(mode="after")
    def normalize_states(self) -> SourceCallPreflightReport:
        states = tuple(sorted(self.operational_states, key=lambda item: item.operational_state_id))
        state_ids = [item.operational_state_id for item in states]
        entry_ids = [item.source_registry_entry_id for item in states]
        if len(state_ids) != len(set(state_ids)) or len(entry_ids) != len(set(entry_ids)):
            raise ValueError("operational states must contain one unique state per registry entry")
        object.__setattr__(self, "operational_states", states)
        return self

    def _blockers(self) -> tuple[str, ...]:
        blockers: set[str] = set()
        intent = self.intent
        call_at = intent.intended_call_at
        if self.evaluated_at > call_at:
            blockers.add("preflight.after_intended_call")
        if call_at - self.evaluated_at > intent.maximum_preflight_age:
            blockers.add("preflight.stale")
        if (
            self.registry_snapshot.registry_snapshot_id != intent.registry_snapshot_id
            or self.registry_snapshot.content_sha256 != intent.registry_snapshot_sha256
        ):
            blockers.add("registry.snapshot_binding_mismatch")

        registry_entries = {entry.source_registry_entry_id: entry for entry in self.registry_snapshot.sources}
        registry_entry = registry_entries.get(intent.source_registry_entry_id)
        if registry_entry is None:
            blockers.add("registry.entry_missing")
        elif (
            registry_entry.content_sha256 != intent.source_registry_entry_sha256
            or registry_entry.source_id != intent.source_id
            or registry_entry.version != intent.source_version
        ):
            blockers.add("registry.entry_binding_mismatch")

        state_by_entry = {state.source_registry_entry_id: state for state in self.operational_states}
        for state in self.operational_states:
            if (
                state.registry_snapshot_id != self.registry_snapshot.registry_snapshot_id
                or state.registry_snapshot_sha256 != self.registry_snapshot.content_sha256
            ):
                blockers.add(f"operational_state.snapshot_mismatch:{state.source_registry_entry_id}")
            entry = registry_entries.get(state.source_registry_entry_id)
            if entry is None or entry.content_sha256 != state.source_registry_entry_sha256:
                blockers.add(f"operational_state.entry_missing:{state.source_registry_entry_id}")
            if state.effective_at > self.evaluated_at:
                blockers.add(f"operational_state.postdated:{state.source_registry_entry_id}")
        selected_state = state_by_entry.get(intent.source_registry_entry_id)
        if selected_state is None:
            blockers.add("operational_state.selected_entry_missing")
        elif not selected_state.callable:
            blockers.add("operational_state.selected_entry_disabled")

        readiness = self.source_readiness
        if readiness.evaluated_at != self.evaluated_at:
            blockers.add("readiness.not_evaluated_at_preflight")
        if not readiness.ready:
            blockers.add("readiness.failed")
        if (
            readiness.registry_snapshot.registry_snapshot_id != self.registry_snapshot.registry_snapshot_id
            or readiness.registry_snapshot.content_sha256 != self.registry_snapshot.content_sha256
        ):
            blockers.add("readiness.registry_binding_mismatch")

        coverage_entries = [
            entry
            for entry in readiness.catalog.entries
            if entry.source_coverage_entry_id == intent.source_coverage_entry_id
        ]
        if len(coverage_entries) != 1:
            blockers.add("coverage.selected_entry_missing_or_duplicate")
            return tuple(sorted(blockers))
        selected = coverage_entries[0]
        if (
            selected.source_id != intent.source_id
            or selected.source_version != intent.source_version
            or selected.source_registry_entry_id != intent.source_registry_entry_id
            or selected.source_registry_entry_sha256 != intent.source_registry_entry_sha256
        ):
            blockers.add("coverage.registry_binding_mismatch")
        if (
            selected.environment is not intent.environment
            or selected.subject != intent.subject
            or selected.domain is not intent.domain
            or selected.partition_key != intent.partition_key
        ):
            blockers.add("coverage.call_scope_mismatch")
        if selected.review_expires_at <= call_at:
            blockers.add("coverage.review_expired")
        if not selected.credential_owner.strip():
            blockers.add("coverage.credential_owner_missing")
        if selected.cadence <= timedelta(0):
            blockers.add("coverage.cadence_missing")
        if not selected.coverage.artifact_sha256 or not selected.knowability.evidence_sha256:
            blockers.add("coverage.evidence_missing")
        dimensions = {line.dimension for line in selected.budget_lines}
        for dimension in (BudgetDimension.API_CALLS, BudgetDimension.VENDOR_FEES):
            if dimension not in dimensions:
                blockers.add(f"coverage.{dimension.value}_budget_missing")

        requirement = next(
            (item for item in readiness.catalog.requirements if item.key == selected.cell_key),
            None,
        )
        if requirement is None:
            blockers.add("coverage.requirement_missing")
        else:
            if not intent.required_permissions.issubset(requirement.required_permissions):
                blockers.add("rights.intent_permissions_outside_frozen_requirement")
            if requirement.fallback_policy is FallbackPolicy.REQUIRED:
                fallbacks = [
                    entry
                    for entry in readiness.catalog.entries
                    if entry.cell_key == selected.cell_key and entry.role is SourceRole.FALLBACK
                ]
                enabled_fallbacks = [
                    entry
                    for entry in fallbacks
                    if entry.source_registry_entry_id in state_by_entry
                    and state_by_entry[entry.source_registry_entry_id].callable
                    and state_by_entry[entry.source_registry_entry_id].effective_at <= self.evaluated_at
                ]
                if not enabled_fallbacks:
                    blockers.add("coverage.enabled_fallback_missing")

        approvals = {item.rights_approval_id: item for item in readiness.rights_approvals}
        approval = approvals.get(selected.rights_approval_id)
        if approval is None:
            blockers.add("rights.approval_missing")
        else:
            if (
                approval.content_sha256 != selected.rights_approval_sha256
                or approval.source_registry_entry_id != intent.source_registry_entry_id
                or approval.source_registry_entry_sha256 != intent.source_registry_entry_sha256
            ):
                blockers.add("rights.approval_binding_mismatch")
            if approval.approved_at > self.evaluated_at:
                blockers.add("rights.approval_postdated")
            if approval.expires_at <= call_at:
                blockers.add("rights.approval_expired")
            if approval.revoked_at is not None and approval.revoked_at <= call_at:
                blockers.add("rights.approval_revoked")
            decisions = approval.permission_map()
            denied = [
                permission.value
                for permission in intent.required_permissions
                if not decisions.get(permission) or not decisions[permission].permitted
            ]
            if denied:
                blockers.add(f"rights.required_permissions_denied:{','.join(sorted(denied))}")
        return tuple(sorted(blockers))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def blockers(self) -> tuple[str, ...]:
        return self._blockers()

    @computed_field  # type: ignore[prop-decorator]
    @property
    def allowed(self) -> bool:
        return not self._blockers()

    @computed_field  # type: ignore[prop-decorator]
    @property
    def preflight_report_id(self) -> str:
        payload = self.model_dump(
            mode="json",
            exclude={"blockers", "allowed", "preflight_report_id"},
        )
        return f"source-call-preflight:{canonical_sha256(payload)}"


class BudgetHorizon(StrEnum):
    MONTHLY = "monthly"
    ANNUAL = "annual"


class BudgetUsageObservation(_StrictFrozenModel):
    source_coverage_entry_id: str = Field(pattern=r"^source-coverage-entry:[0-9a-f]{64}$")
    dimension: BudgetDimension
    unit: str = Field(min_length=1)
    metered_use: Decimal = Field(ge=0)
    independently_reconciled_use: Decimal = Field(ge=0)
    window_started_at: datetime
    window_completed_at: datetime
    observed_at: datetime
    telemetry_evidence_id: str = Field(min_length=1)
    telemetry_evidence_sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("window_started_at", "window_completed_at", "observed_at")
    @classmethod
    def validate_datetimes(cls, value: datetime, info: Any) -> datetime:
        return _require_aware(value, info.field_name)

    @model_validator(mode="after")
    def validate_window(self) -> BudgetUsageObservation:
        if self.window_completed_at <= self.window_started_at:
            raise ValueError("budget observation window must be positive")
        if self.observed_at < self.window_completed_at:
            raise ValueError("budget usage cannot be observed before the window completes")
        return self

    @property
    def key(self) -> tuple[str, BudgetDimension]:
        return self.source_coverage_entry_id, self.dimension


class BudgetUndercountExplanation(_StrictFrozenModel):
    source_coverage_entry_id: str = Field(pattern=r"^source-coverage-entry:[0-9a-f]{64}$")
    dimension: BudgetDimension
    planned_use: Decimal = Field(ge=0)
    observed_use: Decimal = Field(ge=0)
    rationale: str = Field(min_length=1)
    approved_by: str = Field(min_length=1)
    approved_at: datetime
    evidence_id: str = Field(min_length=1)
    evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    approval_signature_id: str = Field(pattern=_SIGNATURE_ID_PATTERN)
    approval_signature_sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("approved_at")
    @classmethod
    def validate_approved_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "approved_at")

    @property
    def key(self) -> tuple[str, BudgetDimension]:
        return self.source_coverage_entry_id, self.dimension


class FullCatalogBudgetReport(_StrictFrozenModel):
    """Row-complete planned/actual reconciliation for the source catalog."""

    catalog: SourceCoverageCatalog
    horizon: BudgetHorizon
    window_started_at: datetime
    window_completed_at: datetime
    evaluated_at: datetime
    observations: tuple[BudgetUsageObservation, ...]
    undercount_explanations: tuple[BudgetUndercountExplanation, ...] = ()

    @field_validator("window_started_at", "window_completed_at", "evaluated_at")
    @classmethod
    def validate_datetimes(cls, value: datetime, info: Any) -> datetime:
        return _require_aware(value, info.field_name)

    @model_validator(mode="after")
    def normalize_evidence(self) -> FullCatalogBudgetReport:
        if self.window_completed_at <= self.window_started_at:
            raise ValueError("budget reconciliation window must be positive")
        observations = tuple(sorted(self.observations, key=lambda item: (item.key[0], item.key[1].value)))
        explanations = tuple(sorted(self.undercount_explanations, key=lambda item: (item.key[0], item.key[1].value)))
        if len({item.key for item in observations}) != len(observations):
            raise ValueError("budget observations must be unique per planned line")
        if len({item.key for item in explanations}) != len(explanations):
            raise ValueError("budget undercount explanations must be unique per planned line")
        object.__setattr__(self, "observations", observations)
        object.__setattr__(self, "undercount_explanations", explanations)
        return self

    def _planned_lines(self) -> dict[tuple[str, BudgetDimension], tuple[SourceCoverageEntry, Any]]:
        return {
            (entry.source_coverage_entry_id, line.dimension): (entry, line)
            for entry in self.catalog.entries
            for line in entry.budget_lines
        }

    def _blockers(self) -> tuple[str, ...]:
        blockers: set[str] = set()
        if self.evaluated_at < self.window_completed_at:
            blockers.add("budget.evaluation_predates_window_completion")
        planned = self._planned_lines()
        observations = {item.key: item for item in self.observations}
        explanations = {item.key: item for item in self.undercount_explanations}
        dimensions = {key[1] for key in planned}
        for dimension in BudgetDimension:
            if dimension not in dimensions:
                blockers.add(f"budget.full_catalog_dimension_missing:{dimension.value}")
        for key in sorted(set(planned) - set(observations), key=lambda item: (item[0], item[1].value)):
            blockers.add(f"budget.observation_missing:{key[0]}:{key[1].value}")
        for key in sorted(set(observations) - set(planned), key=lambda item: (item[0], item[1].value)):
            blockers.add(f"budget.observation_outside_catalog:{key[0]}:{key[1].value}")

        for key in set(planned) & set(observations):
            _, line = planned[key]
            observation = observations[key]
            prefix = f"{key[0]}:{key[1].value}"
            if observation.unit != line.unit:
                blockers.add(f"budget.unit_mismatch:{prefix}")
            if (
                observation.window_started_at != self.window_started_at
                or observation.window_completed_at != self.window_completed_at
            ):
                blockers.add(f"budget.window_mismatch:{prefix}")
            if observation.observed_at > self.evaluated_at:
                blockers.add(f"budget.observation_postdated:{prefix}")
            if observation.metered_use != observation.independently_reconciled_use:
                blockers.add(f"budget.telemetry_reconciliation_failed:{prefix}")
            planned_use = (
                line.projected_monthly_use if self.horizon is BudgetHorizon.MONTHLY else line.projected_annual_use
            )
            approved_limit = (
                line.approved_monthly_limit if self.horizon is BudgetHorizon.MONTHLY else line.approved_annual_limit
            )
            observed_use = observation.independently_reconciled_use
            if observed_use > approved_limit:
                blockers.add(f"budget.approved_limit_exceeded:{prefix}")
            if observed_use < planned_use:
                explanation = explanations.get(key)
                if explanation is None:
                    blockers.add(f"budget.unexplained_undercount:{prefix}")
                elif (
                    explanation.planned_use != planned_use
                    or explanation.observed_use != observed_use
                    or explanation.approved_at > self.evaluated_at
                    or explanation.approved_at < self.window_completed_at
                ):
                    blockers.add(f"budget.invalid_undercount_explanation:{prefix}")
            elif key in explanations:
                blockers.add(f"budget.spurious_undercount_explanation:{prefix}")
        for key in set(explanations) - set(planned):
            blockers.add(f"budget.explanation_outside_catalog:{key[0]}:{key[1].value}")
        return tuple(sorted(blockers))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def blockers(self) -> tuple[str, ...]:
        return self._blockers()

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ready(self) -> bool:
        return not self._blockers()

    @computed_field  # type: ignore[prop-decorator]
    @property
    def budget_report_id(self) -> str:
        payload = self.model_dump(mode="json", exclude={"blockers", "ready", "budget_report_id"})
        return f"full-catalog-budget:{canonical_sha256(payload)}"


class ProductionRecheckSchedule(_StrictFrozenModel):
    production_recheck_schedule_id: str = Field(
        default="",
        pattern=r"^(?:|production-recheck-schedule:[0-9a-f]{64})$",
    )
    content_sha256: str = Field(default="", pattern=_CONTENT_SHA256_PATTERN)
    research_catalog_id: str = Field(pattern=r"^research-catalog:[0-9a-f]{64}$")
    research_catalog_sha256: str = Field(pattern=_SHA256_PATTERN)
    universe: UniverseRef
    source_coverage_catalog_id: str = Field(pattern=r"^source-coverage:[0-9a-f]{64}$")
    source_coverage_catalog_sha256: str = Field(pattern=_SHA256_PATTERN)
    registry_snapshot_id: str = Field(pattern=r"^registry-snapshot:[0-9a-f]{64}$")
    registry_snapshot_sha256: str = Field(pattern=_SHA256_PATTERN)
    release_manifest_id: str = Field(pattern=r"^release-manifest:[0-9a-f]{64}$")
    release_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    operational_state_ids: tuple[str, ...] = Field(min_length=1)
    cadence: timedelta
    maximum_lag: timedelta
    effective_at: datetime
    approved_at: datetime
    approved_by: str = Field(min_length=1)
    owner: str = Field(min_length=1)
    alert_id: str = Field(min_length=1)
    remediation_runbook: str = Field(min_length=1)
    approval_signature_id: str = Field(pattern=_SIGNATURE_ID_PATTERN)
    approval_signature_sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("effective_at", "approved_at")
    @classmethod
    def validate_datetimes(cls, value: datetime, info: Any) -> datetime:
        return _require_aware(value, info.field_name)

    @field_validator("operational_state_ids")
    @classmethod
    def validate_state_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not re.fullmatch(r"^source-operational-state:[0-9a-f]{64}$", value) for value in values):
            raise ValueError("operational_state_ids must be content-addressed")
        return _normalize_unique(values, "operational_state_ids")

    @model_validator(mode="after")
    def identify(self) -> ProductionRecheckSchedule:
        if self.approved_at > self.effective_at:
            raise ValueError("Production recheck approval must not postdate its effective time")
        if self.cadence <= timedelta(0) or self.maximum_lag <= timedelta(0):
            raise ValueError("Production recheck cadence and maximum lag must be positive")
        for reference_id, sha256 in (
            (self.research_catalog_id, self.research_catalog_sha256),
            (self.source_coverage_catalog_id, self.source_coverage_catalog_sha256),
            (self.registry_snapshot_id, self.registry_snapshot_sha256),
            (self.release_manifest_id, self.release_manifest_sha256),
        ):
            if not _reference_matches(reference_id, sha256):
                raise ValueError("Production recheck reference ID and hash do not match")
        _content_address(self, id_field="production_recheck_schedule_id", prefix="production-recheck-schedule")
        return self


class ScheduledOperationalRecheck(_StrictFrozenModel):
    schedule: ProductionRecheckSchedule
    registry_snapshot: RegistrySnapshot
    source_readiness: SourceReadinessReport
    budget_reconciliation: FullCatalogBudgetReport
    operational_states: tuple[SourceRegistryOperationalState, ...]
    scheduled_for: datetime
    evaluated_at: datetime

    @field_validator("scheduled_for", "evaluated_at")
    @classmethod
    def validate_datetimes(cls, value: datetime, info: Any) -> datetime:
        return _require_aware(value, info.field_name)

    @model_validator(mode="after")
    def normalize_states(self) -> ScheduledOperationalRecheck:
        states = tuple(sorted(self.operational_states, key=lambda item: item.operational_state_id))
        if len({item.operational_state_id for item in states}) != len(states):
            raise ValueError("scheduled recheck operational states must be unique")
        object.__setattr__(self, "operational_states", states)
        return self

    def _blockers(self) -> tuple[str, ...]:
        blockers: set[str] = set()
        schedule = self.schedule
        if self.scheduled_for < schedule.effective_at:
            blockers.add("recheck.scheduled_before_effective_time")
        if self.evaluated_at < self.scheduled_for:
            blockers.add("recheck.evaluation_predates_schedule")
        elif self.evaluated_at - self.scheduled_for > schedule.maximum_lag:
            blockers.add("recheck.maximum_lag_exceeded")
        if (
            self.registry_snapshot.registry_snapshot_id != schedule.registry_snapshot_id
            or self.registry_snapshot.content_sha256 != schedule.registry_snapshot_sha256
        ):
            blockers.add("recheck.registry_binding_mismatch")
        if self.source_readiness.evaluated_at != self.evaluated_at:
            blockers.add("recheck.readiness_not_current")
        if not self.source_readiness.ready:
            blockers.add("recheck.source_readiness_failed")
        if not self.budget_reconciliation.ready:
            blockers.add("recheck.budget_reconciliation_failed")
        if (
            self.source_readiness.catalog.source_coverage_catalog_id != schedule.source_coverage_catalog_id
            or self.source_readiness.catalog.content_sha256 != schedule.source_coverage_catalog_sha256
            or self.budget_reconciliation.catalog.source_coverage_catalog_id != schedule.source_coverage_catalog_id
            or self.budget_reconciliation.catalog.content_sha256 != schedule.source_coverage_catalog_sha256
        ):
            blockers.add("recheck.source_catalog_binding_mismatch")
        if (
            self.source_readiness.catalog.research_catalog_id != schedule.research_catalog_id
            or self.source_readiness.catalog.research_catalog_sha256 != schedule.research_catalog_sha256
            or self.source_readiness.catalog.universe != schedule.universe
        ):
            blockers.add("recheck.product_scope_binding_mismatch")
        if self.budget_reconciliation.evaluated_at != self.evaluated_at:
            blockers.add("recheck.budget_evidence_not_current")

        expected_state_ids = set(schedule.operational_state_ids)
        state_ids = {item.operational_state_id for item in self.operational_states}
        if state_ids != expected_state_ids:
            blockers.add("recheck.operational_state_set_mismatch")
        entry_by_id = {entry.source_registry_entry_id: entry for entry in self.registry_snapshot.sources}
        state_by_entry = {item.source_registry_entry_id: item for item in self.operational_states}
        for state in self.operational_states:
            registry_entry = entry_by_id.get(state.source_registry_entry_id)
            if (
                state.registry_snapshot_id != schedule.registry_snapshot_id
                or state.registry_snapshot_sha256 != schedule.registry_snapshot_sha256
                or registry_entry is None
                or registry_entry.content_sha256 != state.source_registry_entry_sha256
            ):
                blockers.add(f"recheck.operational_state_binding_mismatch:{state.source_registry_entry_id}")
            if state.effective_at > self.evaluated_at:
                blockers.add(f"recheck.operational_state_postdated:{state.source_registry_entry_id}")

        entries_by_cell: dict[tuple[Any, ...], list[SourceCoverageEntry]] = defaultdict(list)
        for coverage_entry in self.source_readiness.catalog.entries:
            entries_by_cell[coverage_entry.cell_key].append(coverage_entry)
        for requirement in self.source_readiness.catalog.requirements:
            enabled = [
                entry
                for entry in entries_by_cell.get(requirement.key, [])
                if entry.source_registry_entry_id in state_by_entry
                and state_by_entry[entry.source_registry_entry_id].callable
            ]
            if not enabled:
                blockers.add(f"recheck.no_enabled_source:{requirement.key}")

        next_due = self.scheduled_for + schedule.cadence
        for coverage_entry in self.source_readiness.catalog.entries:
            if coverage_entry.review_expires_at <= next_due:
                blockers.add(f"recheck.review_expires_before_next_run:{coverage_entry.source_coverage_entry_id}")
        for approval in self.source_readiness.rights_approvals:
            if approval.expires_at <= next_due:
                blockers.add(f"recheck.rights_expire_before_next_run:{approval.rights_approval_id}")
            if approval.revoked_at is not None and approval.revoked_at <= self.evaluated_at:
                blockers.add(f"recheck.rights_revoked:{approval.rights_approval_id}")
        return tuple(sorted(blockers))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def blockers(self) -> tuple[str, ...]:
        return self._blockers()

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ready(self) -> bool:
        return not self._blockers()

    @computed_field  # type: ignore[prop-decorator]
    @property
    def operational_recheck_id(self) -> str:
        payload = self.model_dump(mode="json", exclude={"blockers", "ready", "operational_recheck_id"})
        return f"production-operational-recheck:{canonical_sha256(payload)}"


class StagedBatchRequirement(_StrictFrozenModel):
    stage: int = Field(ge=1)
    issuer_count: int = Field(ge=1)
    instrument_count: int = Field(ge=1)
    required_capture_runs: int = Field(ge=1)


class StagedBatchEvidence(_StrictFrozenModel):
    stage: int = Field(ge=1)
    issuer_ids: tuple[str, ...] = Field(min_length=1)
    instrument_ids: tuple[str, ...] = Field(min_length=1)
    capture_evaluation_report_ids: tuple[str, ...] = Field(min_length=1)
    run_ids: tuple[str, ...] = Field(min_length=1)
    error_count: int = Field(ge=0)
    started_at: datetime
    completed_at: datetime
    evidence_id: str = Field(min_length=1)
    evidence_sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("started_at", "completed_at")
    @classmethod
    def validate_datetimes(cls, value: datetime, info: Any) -> datetime:
        return _require_aware(value, info.field_name)

    @field_validator("issuer_ids", "instrument_ids", "capture_evaluation_report_ids", "run_ids")
    @classmethod
    def validate_unique_values(cls, values: tuple[str, ...], info: Any) -> tuple[str, ...]:
        return _normalize_unique(values, info.field_name)

    @model_validator(mode="after")
    def validate_window(self) -> StagedBatchEvidence:
        if self.completed_at <= self.started_at:
            raise ValueError("staged batch window must be positive")
        return self


class ComparisonCriterion(_StrictFrozenModel):
    comparison_criterion_id: str = Field(
        default="",
        pattern=r"^(?:|comparison-criterion:[0-9a-f]{64})$",
    )
    content_sha256: str = Field(default="", pattern=_CONTENT_SHA256_PATTERN)
    metric_id: str = Field(pattern=_STABLE_ID_PATTERN)
    unit: str = Field(min_length=1)
    maximum_absolute_delta: Decimal = Field(ge=0)
    rationale: str = Field(min_length=1)
    approved_by: str = Field(min_length=1)
    approved_at: datetime
    evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    approval_signature_id: str = Field(pattern=_SIGNATURE_ID_PATTERN)
    approval_signature_sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("approved_at")
    @classmethod
    def validate_approved_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "approved_at")

    @model_validator(mode="after")
    def identify(self) -> ComparisonCriterion:
        _content_address(self, id_field="comparison_criterion_id", prefix="comparison-criterion")
        return self


class ComparisonObservation(_StrictFrozenModel):
    comparison_criterion_id: str = Field(pattern=r"^comparison-criterion:[0-9a-f]{64}$")
    metric_id: str = Field(pattern=_STABLE_ID_PATTERN)
    unit: str = Field(min_length=1)
    baseline_value: Decimal
    candidate_value: Decimal
    measured_at: datetime
    evidence_id: str = Field(min_length=1)
    evidence_sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("measured_at")
    @classmethod
    def validate_measured_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "measured_at")


class RollbackPlan(_StrictFrozenModel):
    rollback_plan_id: str = Field(default="", pattern=r"^(?:|rollback-plan:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=_CONTENT_SHA256_PATTERN)
    target_release_manifest_id: str = Field(pattern=r"^release-manifest:[0-9a-f]{64}$")
    target_release_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    runbook: str = Field(min_length=1)
    owner: str = Field(min_length=1)
    maximum_execution_time: timedelta
    maximum_test_age: timedelta
    tested_at: datetime
    test_evidence_id: str = Field(min_length=1)
    test_evidence_sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("tested_at")
    @classmethod
    def validate_tested_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "tested_at")

    @model_validator(mode="after")
    def identify(self) -> RollbackPlan:
        if self.maximum_execution_time <= timedelta(0) or self.maximum_test_age <= timedelta(0):
            raise ValueError("rollback execution and test-age limits must be positive")
        if not _reference_matches(
            self.target_release_manifest_id,
            self.target_release_manifest_sha256,
        ):
            raise ValueError("rollback release ID and hash do not match")
        _content_address(self, id_field="rollback_plan_id", prefix="rollback-plan")
        return self


class GraduationApprovalRole(StrEnum):
    INDEPENDENT_REVIEWER = "independent_reviewer"
    PRODUCT_OWNER = "product_owner"


class GraduationApproval(_StrictFrozenModel):
    role: GraduationApprovalRole
    approver: str = Field(min_length=1)
    graduation_plan_id: str = Field(pattern=r"^production-graduation-plan:[0-9a-f]{64}$")
    graduation_plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    evidence_bundle_id: str = Field(pattern=r"^production-graduation-evidence:[0-9a-f]{64}$")
    evidence_bundle_sha256: str = Field(pattern=_SHA256_PATTERN)
    approved_at: datetime
    approval_record_id: str = Field(min_length=1)
    approval_record_sha256: str = Field(pattern=_SHA256_PATTERN)
    approval_signature_id: str = Field(pattern=_SIGNATURE_ID_PATTERN)
    approval_signature_sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("approved_at")
    @classmethod
    def validate_approved_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "approved_at")


class ProductionGraduationPlan(_StrictFrozenModel):
    """Pre-run, product-owner-approved denominator and graduation criteria."""

    production_graduation_plan_id: str = Field(
        default="",
        pattern=r"^(?:|production-graduation-plan:[0-9a-f]{64})$",
    )
    content_sha256: str = Field(default="", pattern=_CONTENT_SHA256_PATTERN)
    research_catalog_id: str = Field(pattern=r"^research-catalog:[0-9a-f]{64}$")
    research_catalog_sha256: str = Field(pattern=_SHA256_PATTERN)
    universe: UniverseRef
    applicability_catalog_id: str = Field(pattern=r"^applicability:[0-9a-f]{64}$")
    applicability_catalog_sha256: str = Field(pattern=_SHA256_PATTERN)
    module_slo_catalog_id: str = Field(pattern=r"^module-slo:[0-9a-f]{64}$")
    module_slo_catalog_sha256: str = Field(pattern=_SHA256_PATTERN)
    consumer_slo_catalog_id: str = Field(pattern=r"^consumer-slo:[0-9a-f]{64}$")
    consumer_slo_catalog_sha256: str = Field(pattern=_SHA256_PATTERN)
    usage_telemetry_slo_catalog_id: str = Field(pattern=r"^usage-telemetry-slo:[0-9a-f]{64}$")
    usage_telemetry_slo_catalog_sha256: str = Field(pattern=_SHA256_PATTERN)
    capture_scope_id: str = Field(pattern=r"^capture-scope:[0-9a-f]{64}$")
    capture_scope_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_coverage_catalog_id: str = Field(pattern=r"^source-coverage:[0-9a-f]{64}$")
    source_coverage_catalog_sha256: str = Field(pattern=_SHA256_PATTERN)
    registry_snapshot_id: str = Field(pattern=r"^registry-snapshot:[0-9a-f]{64}$")
    registry_snapshot_sha256: str = Field(pattern=_SHA256_PATTERN)
    release_manifest_id: str = Field(pattern=r"^release-manifest:[0-9a-f]{64}$")
    release_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    operational_recheck_schedule_id: str = Field(pattern=r"^production-recheck-schedule:[0-9a-f]{64}$")
    operational_recheck_schedule_sha256: str = Field(pattern=_SHA256_PATTERN)
    expected_issuer_count: int = Field(ge=1)
    expected_instrument_count: int = Field(ge=1)
    required_domains: tuple[DataDomain, ...] = Field(min_length=1)
    staged_batches: tuple[StagedBatchRequirement, ...] = Field(min_length=1)
    natural_refresh_requirement_ids: tuple[str, ...] = Field(min_length=1)
    minimum_soak_duration: timedelta
    maximum_operational_evidence_age: timedelta
    comparison_criteria: tuple[ComparisonCriterion, ...] = Field(min_length=1)
    rollback: RollbackPlan
    effective_at: datetime
    approved_at: datetime
    approved_by: str = Field(min_length=1)
    approval_record_id: str = Field(min_length=1)
    approval_record_sha256: str = Field(pattern=_SHA256_PATTERN)
    approval_signature_id: str = Field(pattern=_SIGNATURE_ID_PATTERN)
    approval_signature_sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("effective_at", "approved_at")
    @classmethod
    def validate_datetimes(cls, value: datetime, info: Any) -> datetime:
        return _require_aware(value, info.field_name)

    @field_validator("required_domains")
    @classmethod
    def validate_domains(cls, values: tuple[DataDomain, ...]) -> tuple[DataDomain, ...]:
        if len(values) != len(set(values)):
            raise ValueError("required_domains must not contain duplicates")
        return tuple(sorted(values, key=lambda item: item.value))

    @field_validator("natural_refresh_requirement_ids")
    @classmethod
    def validate_refresh_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not re.fullmatch(r"^natural-refresh:[0-9a-f]{64}$", value) for value in values):
            raise ValueError("natural refresh requirement IDs must be content-addressed")
        return _normalize_unique(values, "natural_refresh_requirement_ids")

    @model_validator(mode="after")
    def identify(self) -> ProductionGraduationPlan:
        if self.approved_at > self.effective_at:
            raise ValueError("graduation plan approval must not postdate its effective time")
        if self.minimum_soak_duration <= timedelta(0) or self.maximum_operational_evidence_age <= timedelta(0):
            raise ValueError("graduation soak and evidence-age limits must be positive")
        for reference_id, sha256 in (
            (self.research_catalog_id, self.research_catalog_sha256),
            (self.applicability_catalog_id, self.applicability_catalog_sha256),
            (self.module_slo_catalog_id, self.module_slo_catalog_sha256),
            (self.consumer_slo_catalog_id, self.consumer_slo_catalog_sha256),
            (self.usage_telemetry_slo_catalog_id, self.usage_telemetry_slo_catalog_sha256),
            (self.capture_scope_id, self.capture_scope_sha256),
            (self.source_coverage_catalog_id, self.source_coverage_catalog_sha256),
            (self.registry_snapshot_id, self.registry_snapshot_sha256),
            (self.release_manifest_id, self.release_manifest_sha256),
            (self.operational_recheck_schedule_id, self.operational_recheck_schedule_sha256),
        ):
            if not _reference_matches(reference_id, sha256):
                raise ValueError("graduation plan reference ID and hash do not match")
        batches = tuple(sorted(self.staged_batches, key=lambda item: item.stage))
        if tuple(item.stage for item in batches) != tuple(range(1, len(batches) + 1)):
            raise ValueError("graduation batch stages must be contiguous from one")
        for previous, current in zip(batches, batches[1:], strict=False):
            if current.issuer_count < previous.issuer_count or current.instrument_count < previous.instrument_count:
                raise ValueError("graduation batch sizes cannot shrink")
        final = batches[-1]
        if final.issuer_count != self.expected_issuer_count or final.instrument_count != self.expected_instrument_count:
            raise ValueError("final graduation batch must equal the full expected scope")
        criteria = tuple(sorted(self.comparison_criteria, key=lambda item: item.comparison_criterion_id))
        if len({item.comparison_criterion_id for item in criteria}) != len(criteria):
            raise ValueError("comparison criteria must be unique")
        if any(item.approved_at > self.effective_at for item in criteria):
            raise ValueError("comparison criteria must be approved before the plan becomes effective")
        object.__setattr__(self, "staged_batches", batches)
        object.__setattr__(self, "comparison_criteria", criteria)
        _content_address(self, id_field="production_graduation_plan_id", prefix="production-graduation-plan")
        return self


class ProductionGraduationEvidence(_StrictFrozenModel):
    """Immutable evidence bundle signed by the two final approvers."""

    evidence_bundle_id: str = Field(
        default="",
        pattern=r"^(?:|production-graduation-evidence:[0-9a-f]{64})$",
    )
    content_sha256: str = Field(default="", pattern=_CONTENT_SHA256_PATTERN)
    research_catalog: ResearchCatalogManifest
    universe_manifest: UniverseManifest
    universe_memberships: tuple[UniverseMembership, ...]
    applicability: ApplicabilityCatalog
    module_slo_report: ModuleSloReport
    consumer_slo_report: ConsumerSloReport
    usage_telemetry_report: UsageTelemetryReport
    capture_scope: CaptureScope
    capture_reports: tuple[CaptureEvaluationReport, ...]
    release_manifest: ReleaseManifest
    registry_snapshot: RegistrySnapshot
    source_readiness: SourceReadinessReport
    budget_reconciliation: FullCatalogBudgetReport
    operational_recheck: ScheduledOperationalRecheck
    natural_refresh_reports: tuple[NaturalRefreshReport, ...]
    staged_batch_evidence: tuple[StagedBatchEvidence, ...]
    comparison_observations: tuple[ComparisonObservation, ...]
    soak_started_at: datetime
    soak_completed_at: datetime
    created_at: datetime

    @field_validator("soak_started_at", "soak_completed_at", "created_at")
    @classmethod
    def validate_datetimes(cls, value: datetime, info: Any) -> datetime:
        return _require_aware(value, info.field_name)

    @model_validator(mode="after")
    def normalize_and_identify(self) -> ProductionGraduationEvidence:
        if self.soak_completed_at <= self.soak_started_at:
            raise ValueError("graduation soak window must be positive")
        if self.created_at < self.soak_completed_at:
            raise ValueError("graduation evidence cannot be created before the soak completes")
        memberships = tuple(sorted(self.universe_memberships, key=lambda item: item.membership_id))
        capture_reports = tuple(sorted(self.capture_reports, key=lambda item: item.capture_evaluation_report_id))
        refresh_reports = tuple(
            sorted(
                self.natural_refresh_reports,
                key=lambda item: item.requirement.natural_refresh_requirement_id,
            )
        )
        batches = tuple(sorted(self.staged_batch_evidence, key=lambda item: item.stage))
        comparisons = tuple(sorted(self.comparison_observations, key=lambda item: item.comparison_criterion_id))
        for field_name, values, key in (
            ("universe_memberships", memberships, lambda item: item.membership_id),
            (
                "capture_reports",
                capture_reports,
                lambda item: item.capture_evaluation_report_id,
            ),
            (
                "natural_refresh_reports",
                refresh_reports,
                lambda item: item.requirement.natural_refresh_requirement_id,
            ),
            ("staged_batch_evidence", batches, lambda item: item.stage),
            (
                "comparison_observations",
                comparisons,
                lambda item: item.comparison_criterion_id,
            ),
        ):
            keys = [key(item) for item in values]
            if len(keys) != len(set(keys)):
                raise ValueError(f"{field_name} must not contain duplicates")
        object.__setattr__(self, "universe_memberships", memberships)
        object.__setattr__(self, "capture_reports", capture_reports)
        object.__setattr__(self, "natural_refresh_reports", refresh_reports)
        object.__setattr__(self, "staged_batch_evidence", batches)
        object.__setattr__(self, "comparison_observations", comparisons)
        _content_address(self, id_field="evidence_bundle_id", prefix="production-graduation-evidence")
        return self


class ProductionGraduationReport(_StrictFrozenModel):
    """Final derived decision; automated green checks alone cannot graduate."""

    plan: ProductionGraduationPlan
    evidence: ProductionGraduationEvidence
    approvals: tuple[GraduationApproval, ...]
    evaluated_at: datetime

    @field_validator("evaluated_at")
    @classmethod
    def validate_evaluated_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "evaluated_at")

    @model_validator(mode="after")
    def normalize_approvals(self) -> ProductionGraduationReport:
        approvals = tuple(sorted(self.approvals, key=lambda item: item.role.value))
        if len({item.role for item in approvals}) != len(approvals):
            raise ValueError("graduation approvals must be unique by role")
        object.__setattr__(self, "approvals", approvals)
        return self

    def _binding_blockers(self) -> set[str]:
        blockers: set[str] = set()
        plan = self.plan
        evidence = self.evidence
        catalog = evidence.research_catalog
        if (
            catalog.research_catalog_id != plan.research_catalog_id
            or catalog.content_sha256 != plan.research_catalog_sha256
        ):
            blockers.add("graduation.research_catalog_binding_mismatch")
        if catalog.scope_floor.universe != plan.universe:
            blockers.add("graduation.catalog_universe_binding_mismatch")
        if evidence.universe_manifest.ref != plan.universe:
            blockers.add("graduation.universe_manifest_binding_mismatch")
        applicability = evidence.applicability
        if (
            applicability.applicability_catalog_id != plan.applicability_catalog_id
            or applicability.content_sha256 != plan.applicability_catalog_sha256
            or applicability.research_catalog_id != plan.research_catalog_id
            or applicability.research_catalog_sha256 != plan.research_catalog_sha256
            or applicability.universe != plan.universe
        ):
            blockers.add("graduation.applicability_binding_mismatch")
        capture_scope = evidence.capture_scope
        if (
            capture_scope.capture_scope_id != plan.capture_scope_id
            or capture_scope.content_sha256 != plan.capture_scope_sha256
            or capture_scope.research_catalog_id != plan.research_catalog_id
            or capture_scope.research_catalog_sha256 != plan.research_catalog_sha256
            or capture_scope.universe != plan.universe
            or capture_scope.applicability_catalog_id != plan.applicability_catalog_id
            or capture_scope.applicability_catalog_sha256 != plan.applicability_catalog_sha256
            or capture_scope.slo_catalog_id != plan.module_slo_catalog_id
            or capture_scope.slo_catalog_sha256 != plan.module_slo_catalog_sha256
            or capture_scope.source_coverage_catalog_id != plan.source_coverage_catalog_id
            or capture_scope.source_coverage_catalog_sha256 != plan.source_coverage_catalog_sha256
        ):
            blockers.add("graduation.capture_scope_binding_mismatch")
        registry = evidence.registry_snapshot
        if (
            registry.registry_snapshot_id != plan.registry_snapshot_id
            or registry.content_sha256 != plan.registry_snapshot_sha256
            or capture_scope.source_registry_id != registry.source_registry_snapshot_id
            or capture_scope.source_registry_sha256 != registry.source_registry_sha256
            or capture_scope.semantic_type_registry_id != registry.semantic_type_registry_snapshot_id
            or capture_scope.semantic_type_registry_sha256 != registry.semantic_type_registry_sha256
        ):
            blockers.add("graduation.registry_binding_mismatch")
        release = evidence.release_manifest
        if (
            release.release_manifest_id != plan.release_manifest_id
            or release.manifest_sha256 != plan.release_manifest_sha256
        ):
            blockers.add("graduation.release_binding_mismatch")
        release_bindings = (
            (release.research_catalog_id, plan.research_catalog_id),
            (release.research_catalog_sha256, plan.research_catalog_sha256),
            (release.capture_scope_id, plan.capture_scope_id),
            (release.capture_scope_sha256, plan.capture_scope_sha256),
            (release.applicability_catalog_id, plan.applicability_catalog_id),
            (release.applicability_catalog_sha256, plan.applicability_catalog_sha256),
            (release.slo_catalog_id, plan.module_slo_catalog_id),
            (release.slo_catalog_sha256, plan.module_slo_catalog_sha256),
            (release.source_coverage_catalog_id, plan.source_coverage_catalog_id),
            (release.source_coverage_catalog_sha256, plan.source_coverage_catalog_sha256),
            (release.registry_snapshot_id, plan.registry_snapshot_id),
            (release.registry_snapshot_sha256, plan.registry_snapshot_sha256),
            (release.consumer_slo_catalog_id, plan.consumer_slo_catalog_id),
            (release.consumer_slo_catalog_sha256, plan.consumer_slo_catalog_sha256),
            (release.usage_telemetry_slo_catalog_id, plan.usage_telemetry_slo_catalog_id),
            (
                release.usage_telemetry_slo_catalog_sha256,
                plan.usage_telemetry_slo_catalog_sha256,
            ),
        )
        if release.universe != plan.universe or any(actual != expected for actual, expected in release_bindings):
            blockers.add("graduation.release_product_scope_mismatch")
        if (
            release.source_registry_id != registry.source_registry_snapshot_id
            or release.source_registry_sha256 != registry.source_registry_sha256
            or release.semantic_type_registry_id != registry.semantic_type_registry_snapshot_id
            or release.semantic_type_registry_sha256 != registry.semantic_type_registry_sha256
            or release.identifier_type_registry_id != registry.identifier_type_registry_snapshot_id
            or release.identifier_type_registry_sha256 != registry.identifier_type_registry_sha256
        ):
            blockers.add("graduation.release_registry_binding_mismatch")
        return blockers

    def _scope_and_slo_blockers(self) -> set[str]:
        blockers: set[str] = set()
        plan = self.plan
        evidence = self.evidence
        manifest = evidence.universe_manifest
        if manifest.definition_kind is not UniverseDefinitionKind.FIXED_COHORT:
            blockers.add("graduation.universe_not_fixed_cohort")
        membership_ids = {item.membership_id for item in evidence.universe_memberships}
        if membership_ids != set(manifest.membership_ids):
            blockers.add("graduation.universe_membership_set_mismatch")
        issuer_ids = {
            item.subject.id for item in evidence.universe_memberships if item.subject.kind is SubjectKind.ISSUER
        }
        instrument_ids = {
            item.subject.id for item in evidence.universe_memberships if item.subject.kind is SubjectKind.SECURITY
        }
        if len(issuer_ids) != plan.expected_issuer_count:
            blockers.add("graduation.issuer_count_mismatch")
        if len(instrument_ids) != plan.expected_instrument_count:
            blockers.add("graduation.instrument_count_mismatch")
        if evidence.research_catalog.scope_floor.minimums.issuers < plan.expected_issuer_count:
            blockers.add("graduation.catalog_issuer_floor_below_expected_count")
        for membership in evidence.universe_memberships:
            if membership.universe_id != plan.universe.universe_id:
                blockers.add(f"graduation.membership_universe_mismatch:{membership.membership_id}")
            if membership.knowable_at > evidence.soak_started_at or membership.recorded_at > evidence.soak_started_at:
                blockers.add(f"graduation.membership_postdates_soak:{membership.membership_id}")
            if not (
                membership.valid_from <= evidence.soak_started_at.date()
                and (membership.valid_to is None or evidence.soak_started_at.date() <= membership.valid_to)
            ):
                blockers.add(f"graduation.membership_not_valid_at_soak:{membership.membership_id}")

        captured_domains = {item.domain for item in evidence.capture_scope.requirements}
        missing_domains = set(plan.required_domains) - captured_domains
        for domain in sorted(missing_domains, key=lambda item: item.value):
            blockers.add(f"graduation.required_domain_missing:{domain.value}")

        module_report = evidence.module_slo_report
        if (
            module_report.slo_catalog.module_slo_catalog_id != plan.module_slo_catalog_id
            or module_report.slo_catalog.content_sha256 != plan.module_slo_catalog_sha256
            or module_report.applicability.applicability_catalog_id != plan.applicability_catalog_id
            or module_report.applicability.content_sha256 != plan.applicability_catalog_sha256
            or module_report.status is not EvaluationStatus.PASS
        ):
            blockers.add("graduation.module_slo_failed_or_mismatched")
        consumer_report = evidence.consumer_slo_report
        if (
            consumer_report.catalog.consumer_slo_catalog_id != plan.consumer_slo_catalog_id
            or consumer_report.catalog.content_sha256 != plan.consumer_slo_catalog_sha256
            or consumer_report.catalog.applicability_catalog_id != plan.applicability_catalog_id
            or consumer_report.catalog.applicability_catalog_sha256 != plan.applicability_catalog_sha256
            or consumer_report.status is not EvaluationStatus.PASS
        ):
            blockers.add("graduation.consumer_slo_failed_or_mismatched")
        telemetry = evidence.usage_telemetry_report
        if (
            telemetry.catalog.usage_telemetry_slo_catalog_id != plan.usage_telemetry_slo_catalog_id
            or telemetry.catalog.content_sha256 != plan.usage_telemetry_slo_catalog_sha256
            or telemetry.catalog.research_catalog_id != plan.research_catalog_id
            or telemetry.catalog.research_catalog_sha256 != plan.research_catalog_sha256
            or telemetry.catalog.universe != plan.universe
            or telemetry.catalog.applicability_catalog_id != plan.applicability_catalog_id
            or telemetry.catalog.applicability_catalog_sha256 != plan.applicability_catalog_sha256
            or telemetry.catalog.registry_snapshot_id != plan.registry_snapshot_id
            or telemetry.catalog.registry_snapshot_sha256 != plan.registry_snapshot_sha256
            or telemetry.status is not EvaluationStatus.PASS
        ):
            blockers.add("graduation.usage_telemetry_failed_or_mismatched")
        return blockers

    def _operational_blockers(self) -> set[str]:
        blockers: set[str] = set()
        plan = self.plan
        evidence = self.evidence
        readiness = evidence.source_readiness
        if not readiness.ready:
            blockers.add("graduation.source_readiness_failed")
        if (
            readiness.catalog.source_coverage_catalog_id != plan.source_coverage_catalog_id
            or readiness.catalog.content_sha256 != plan.source_coverage_catalog_sha256
            or readiness.catalog.applicability_catalog_id != plan.applicability_catalog_id
            or readiness.catalog.applicability_catalog_sha256 != plan.applicability_catalog_sha256
            or readiness.registry_snapshot.registry_snapshot_id != plan.registry_snapshot_id
            or readiness.registry_snapshot.content_sha256 != plan.registry_snapshot_sha256
        ):
            blockers.add("graduation.source_readiness_binding_mismatch")
        if (
            evidence.release_manifest.source_readiness_report_id != readiness.source_readiness_report_id
            or evidence.release_manifest.source_readiness_report_sha256
            != readiness.source_readiness_report_id.rsplit(":", 1)[-1]
        ):
            blockers.add("graduation.release_readiness_binding_mismatch")
        if not evidence.budget_reconciliation.ready:
            blockers.add("graduation.budget_reconciliation_failed")
        if (
            evidence.budget_reconciliation.catalog.source_coverage_catalog_id != plan.source_coverage_catalog_id
            or evidence.budget_reconciliation.catalog.content_sha256 != plan.source_coverage_catalog_sha256
        ):
            blockers.add("graduation.budget_catalog_binding_mismatch")
        recheck = evidence.operational_recheck
        if not recheck.ready:
            blockers.add("graduation.operational_recheck_failed")
        if (
            recheck.schedule.production_recheck_schedule_id != plan.operational_recheck_schedule_id
            or recheck.schedule.content_sha256 != plan.operational_recheck_schedule_sha256
            or recheck.schedule.release_manifest_id != plan.release_manifest_id
            or recheck.schedule.release_manifest_sha256 != plan.release_manifest_sha256
            or recheck.source_readiness.source_readiness_report_id != readiness.source_readiness_report_id
            or recheck.budget_reconciliation.budget_report_id != evidence.budget_reconciliation.budget_report_id
        ):
            blockers.add("graduation.operational_recheck_binding_mismatch")
        if recheck.evaluated_at > evidence.created_at:
            blockers.add("graduation.operational_evidence_postdated")
        elif evidence.created_at - recheck.evaluated_at > plan.maximum_operational_evidence_age:
            blockers.add("graduation.operational_evidence_expired")
        for approval in readiness.rights_approvals:
            if approval.expires_at <= self.evaluated_at:
                blockers.add(f"graduation.rights_expired:{approval.rights_approval_id}")
            if approval.revoked_at is not None and approval.revoked_at <= self.evaluated_at:
                blockers.add(f"graduation.rights_revoked:{approval.rights_approval_id}")
        for entry in readiness.catalog.entries:
            if entry.review_expires_at <= self.evaluated_at:
                blockers.add(f"graduation.source_review_expired:{entry.source_coverage_entry_id}")
        return blockers

    def _run_evidence_blockers(self) -> set[str]:
        blockers: set[str] = set()
        plan = self.plan
        evidence = self.evidence
        if plan.approved_at > evidence.soak_started_at or plan.effective_at > evidence.soak_started_at:
            blockers.add("graduation.plan_postdates_soak")
        if evidence.research_catalog.created_at > plan.approved_at:
            blockers.add("graduation.catalog_was_not_frozen_before_plan_approval")
        if evidence.soak_completed_at - evidence.soak_started_at < plan.minimum_soak_duration:
            blockers.add("graduation.soak_window_too_short")
        if evidence.created_at > self.evaluated_at:
            blockers.add("graduation.evidence_postdates_evaluation")

        capture_reports = {report.capture_evaluation_report_id: report for report in evidence.capture_reports}
        if not capture_reports:
            blockers.add("graduation.capture_evidence_missing")
        for report in capture_reports.values():
            if (
                not report.ready
                or report.environment is not CaptureEnvironment.PRODUCTION
                or report.capture_scope_id != plan.capture_scope_id
                or report.capture_scope_sha256 != plan.capture_scope_sha256
                or report.applicability_catalog_id != plan.applicability_catalog_id
                or report.applicability_catalog_sha256 != plan.applicability_catalog_sha256
                or report.source_coverage_projection_sha256 != evidence.capture_scope.source_coverage_projection_sha256
            ):
                blockers.add(f"graduation.capture_report_failed_or_mismatched:{report.capture_evaluation_report_id}")
            if not (evidence.soak_started_at <= report.evaluated_at <= evidence.soak_completed_at):
                blockers.add(f"graduation.capture_report_outside_soak:{report.capture_evaluation_report_id}")
        refresh_by_id = {
            report.requirement.natural_refresh_requirement_id: report for report in evidence.natural_refresh_reports
        }
        expected_refresh = set(plan.natural_refresh_requirement_ids)
        if set(refresh_by_id) != expected_refresh:
            blockers.add("graduation.natural_refresh_set_mismatch")
        if set(evidence.release_manifest.natural_refresh_requirement_ids) != expected_refresh:
            blockers.add("graduation.release_natural_refresh_binding_mismatch")
        for requirement_id, refresh_report in refresh_by_id.items():
            if refresh_report.status is not EvaluationStatus.PASS:
                blockers.add(f"graduation.natural_refresh_failed:{requirement_id}")
            if (
                refresh_report.observation_started_at < evidence.soak_started_at
                or refresh_report.evaluated_at > evidence.soak_completed_at
            ):
                blockers.add(f"graduation.natural_refresh_outside_soak:{requirement_id}")

        full_issuer_ids = {
            item.subject.id for item in evidence.universe_memberships if item.subject.kind is SubjectKind.ISSUER
        }
        full_instrument_ids = {
            item.subject.id for item in evidence.universe_memberships if item.subject.kind is SubjectKind.SECURITY
        }
        batch_requirements = {item.stage: item for item in plan.staged_batches}
        batch_evidence = {item.stage: item for item in evidence.staged_batch_evidence}
        if set(batch_requirements) != set(batch_evidence):
            blockers.add("graduation.staged_batch_set_mismatch")
        previous_completed_at: datetime | None = None
        for stage, requirement in batch_requirements.items():
            observed = batch_evidence.get(stage)
            if observed is None:
                continue
            if (
                len(observed.issuer_ids) != requirement.issuer_count
                or len(observed.instrument_ids) != requirement.instrument_count
            ):
                blockers.add(f"graduation.staged_batch_size_mismatch:{stage}")
            if not set(observed.issuer_ids).issubset(full_issuer_ids) or not set(observed.instrument_ids).issubset(
                full_instrument_ids
            ):
                blockers.add(f"graduation.staged_batch_scope_mismatch:{stage}")
            if len(observed.run_ids) < requirement.required_capture_runs or observed.error_count:
                blockers.add(f"graduation.staged_batch_run_failure:{stage}")
            if any(report_id not in capture_reports for report_id in observed.capture_evaluation_report_ids):
                blockers.add(f"graduation.staged_batch_capture_evidence_missing:{stage}")
            if len(observed.capture_evaluation_report_ids) < requirement.required_capture_runs:
                blockers.add(f"graduation.staged_batch_capture_run_count_low:{stage}")
            if not (
                evidence.soak_started_at <= observed.started_at < observed.completed_at <= evidence.soak_completed_at
            ):
                blockers.add(f"graduation.staged_batch_outside_soak:{stage}")
            if previous_completed_at is not None and observed.started_at < previous_completed_at:
                blockers.add(f"graduation.staged_batches_overlap_or_reorder:{stage}")
            previous_completed_at = observed.completed_at

        criteria = {item.comparison_criterion_id: item for item in plan.comparison_criteria}
        observations = {item.comparison_criterion_id: item for item in evidence.comparison_observations}
        if set(criteria) != set(observations):
            blockers.add("graduation.comparison_observation_set_mismatch")
        for criterion_id, criterion in criteria.items():
            observation = observations.get(criterion_id)
            if observation is None:
                continue
            if observation.metric_id != criterion.metric_id or observation.unit != criterion.unit:
                blockers.add(f"graduation.comparison_binding_mismatch:{criterion_id}")
            if abs(observation.candidate_value - observation.baseline_value) > criterion.maximum_absolute_delta:
                blockers.add(f"graduation.comparison_threshold_exceeded:{criterion_id}")
            if not (evidence.soak_started_at <= observation.measured_at <= evidence.soak_completed_at):
                blockers.add(f"graduation.comparison_outside_soak:{criterion_id}")
        return blockers

    def _approval_and_rollback_blockers(self) -> set[str]:
        blockers: set[str] = set()
        plan = self.plan
        evidence = self.evidence
        approvals = {item.role: item for item in self.approvals}
        if set(approvals) != set(GraduationApprovalRole):
            blockers.add("graduation.required_approvals_missing")
        if len({item.approver for item in self.approvals}) != len(self.approvals):
            blockers.add("graduation.approvers_not_independent")
        for role, approval in approvals.items():
            if (
                approval.graduation_plan_id != plan.production_graduation_plan_id
                or approval.graduation_plan_sha256 != plan.content_sha256
                or approval.evidence_bundle_id != evidence.evidence_bundle_id
                or approval.evidence_bundle_sha256 != evidence.content_sha256
            ):
                blockers.add(f"graduation.approval_binding_mismatch:{role.value}")
            if approval.approved_at < evidence.created_at or approval.approved_at > self.evaluated_at:
                blockers.add(f"graduation.approval_time_invalid:{role.value}")
        rollback = plan.rollback
        if rollback.target_release_manifest_id == plan.release_manifest_id:
            blockers.add("graduation.rollback_targets_candidate_release")
        if rollback.tested_at > evidence.soak_started_at:
            blockers.add("graduation.rollback_test_postdates_soak")
        elif evidence.soak_started_at - rollback.tested_at > rollback.maximum_test_age:
            blockers.add("graduation.rollback_test_expired")
        return blockers

    def _blockers(self) -> tuple[str, ...]:
        blockers = self._binding_blockers()
        blockers.update(self._scope_and_slo_blockers())
        blockers.update(self._operational_blockers())
        blockers.update(self._run_evidence_blockers())
        blockers.update(self._approval_and_rollback_blockers())
        return tuple(sorted(blockers))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def blockers(self) -> tuple[str, ...]:
        return self._blockers()

    @computed_field  # type: ignore[prop-decorator]
    @property
    def graduated(self) -> bool:
        return not self._blockers()

    @computed_field  # type: ignore[prop-decorator]
    @property
    def graduation_report_id(self) -> str:
        payload = self.model_dump(
            mode="json",
            exclude={"blockers", "graduated", "graduation_report_id"},
        )
        return f"production-graduation-report:{canonical_sha256(payload)}"


class GraduationAttestation(_StrictFrozenModel):
    """Independent signature over post-run evidence for one unchanged release."""

    graduation_attestation_id: str = Field(
        default="",
        pattern=r"^(?:|graduation-attestation:[0-9a-f]{64})$",
    )
    content_sha256: str = Field(default="", pattern=_CONTENT_SHA256_PATTERN)
    release_manifest_id: str = Field(pattern=r"^release-manifest:[0-9a-f]{64}$")
    release_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    candidate_commit_sha: str = Field(pattern=r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
    graduation_report: ProductionGraduationReport
    attestor_role: Literal["independent_reviewer"] = "independent_reviewer"
    attested_by: str = Field(min_length=1)
    attested_at: datetime
    independence_evidence_id: str = Field(min_length=1)
    independence_evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    signed_payload_sha256: str = Field(pattern=_SHA256_PATTERN)
    signature_ref: str = Field(min_length=1)
    signature_sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("attested_at")
    @classmethod
    def validate_attested_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "attested_at")

    @staticmethod
    def compute_signed_payload_sha256(
        *,
        release_manifest_id: str,
        release_manifest_sha256: str,
        candidate_commit_sha: str,
        graduation_report: ProductionGraduationReport,
        attested_by: str,
        attested_at: datetime,
        independence_evidence_id: str,
        independence_evidence_sha256: str,
    ) -> str:
        """Canonical payload that the independent signature must cover."""

        return canonical_sha256(
            {
                "schema": "truealpha.graduation-attestation.v1",
                "release_manifest_id": release_manifest_id,
                "release_manifest_sha256": release_manifest_sha256,
                "candidate_commit_sha": candidate_commit_sha,
                "graduation_report_id": graduation_report.graduation_report_id,
                "graduation_report_sha256": graduation_report.graduation_report_id.rsplit(":", 1)[-1],
                "attestor_role": "independent_reviewer",
                "attested_by": attested_by,
                "attested_at": _require_aware(attested_at, "attested_at").isoformat(),
                "independence_evidence_id": independence_evidence_id,
                "independence_evidence_sha256": independence_evidence_sha256,
            }
        )

    @model_validator(mode="after")
    def validate_and_identify(self) -> GraduationAttestation:
        report = self.graduation_report
        if not report.graduated:
            raise ValueError("graduation attestation requires a derived graduated report")
        release = report.evidence.release_manifest
        if (
            self.release_manifest_id != report.plan.release_manifest_id
            or self.release_manifest_sha256 != report.plan.release_manifest_sha256
            or self.release_manifest_id != release.release_manifest_id
            or self.release_manifest_sha256 != release.manifest_sha256
        ):
            raise ValueError("graduation attestation does not bind the unchanged release")
        if any(artifact.git_sha != self.candidate_commit_sha for artifact in release.artifacts):
            raise ValueError("candidate commit does not match every promoted release artifact")
        independent = next(
            (approval for approval in report.approvals if approval.role is GraduationApprovalRole.INDEPENDENT_REVIEWER),
            None,
        )
        product_owner = next(
            (approval for approval in report.approvals if approval.role is GraduationApprovalRole.PRODUCT_OWNER),
            None,
        )
        if independent is None or independent.approver != self.attested_by:
            raise ValueError("attestation signer must be the approved independent reviewer")
        if product_owner is not None and product_owner.approver == self.attested_by:
            raise ValueError("independent attestor cannot also be the product owner")
        if self.attested_at < report.evaluated_at:
            raise ValueError("graduation attestation cannot predate the derived report")
        expected_payload = self.compute_signed_payload_sha256(
            release_manifest_id=self.release_manifest_id,
            release_manifest_sha256=self.release_manifest_sha256,
            candidate_commit_sha=self.candidate_commit_sha,
            graduation_report=report,
            attested_by=self.attested_by,
            attested_at=self.attested_at,
            independence_evidence_id=self.independence_evidence_id,
            independence_evidence_sha256=self.independence_evidence_sha256,
        )
        if self.signed_payload_sha256 != expected_payload:
            raise ValueError("signed_payload_sha256 does not cover the exact graduation payload")
        _content_address(self, id_field="graduation_attestation_id", prefix="graduation-attestation")
        return self


class GraduationAttestationRepository(Protocol):
    def get(self, graduation_attestation_id: str) -> GraduationAttestation | None: ...


class GraduationAttestationSignatureVerifier(Protocol):
    def verify(self, attestation: GraduationAttestation) -> bool: ...


def resolve_graduation_attestation(
    repository: GraduationAttestationRepository,
    verifier: GraduationAttestationSignatureVerifier,
    *,
    graduation_attestation_id: str,
    release_manifest_id: str,
    release_manifest_sha256: str,
    candidate_commit_sha: str,
) -> GraduationAttestation:
    """Resolve a persisted attestation and verify its independent signature."""

    attestation = repository.get(graduation_attestation_id)
    if attestation is None:
        raise LookupError(f"graduation attestation {graduation_attestation_id} does not exist")
    if not verifier.verify(attestation):
        raise ValueError("graduation attestation signature verification failed")
    if (
        attestation.release_manifest_id != release_manifest_id
        or attestation.release_manifest_sha256 != release_manifest_sha256
    ):
        raise ValueError("graduation attestation release binding does not match")
    if attestation.candidate_commit_sha != candidate_commit_sha:
        raise ValueError("graduation attestation candidate commit does not match")
    return attestation


__all__ = [
    "BudgetHorizon",
    "BudgetUndercountExplanation",
    "BudgetUsageObservation",
    "ComparisonCriterion",
    "ComparisonObservation",
    "FullCatalogBudgetReport",
    "GraduationAttestation",
    "GraduationAttestationRepository",
    "GraduationAttestationSignatureVerifier",
    "GraduationApproval",
    "GraduationApprovalRole",
    "ProductionGraduationEvidence",
    "ProductionGraduationPlan",
    "ProductionGraduationReport",
    "ProductionRecheckSchedule",
    "RegistryCallState",
    "RollbackPlan",
    "ScheduledOperationalRecheck",
    "SourceCallIntent",
    "SourceCallPreflightReport",
    "SourceRegistryOperationalState",
    "StagedBatchEvidence",
    "StagedBatchRequirement",
    "resolve_graduation_attestation",
]
