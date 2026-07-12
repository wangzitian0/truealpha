"""Registry-driven Dagster composition for the frozen Issue #58 contracts."""

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol, cast

import dagster as dg
from dagster import AssetExecutionContext
from psycopg import Connection
from pydantic import BaseModel, ConfigDict, field_validator
from truealpha_contracts.execution import FactorInvocationTemplate
from truealpha_contracts.models import _require_aware
from truealpha_contracts.registries import RegistrySnapshot, SemanticTypeRegistryEntry, SourceRegistryEntry
from truealpha_contracts.usage import (
    DataRequirement,
    StrategyDataQualityReview,
    StrategyUsageAudit,
    UsageFrequencySlice,
    build_usage_frequency_slice,
)

from data_engine.contract_repository import (
    ContractIntegrityError,
    PostgresStrategyDataQualityReviewRepository,
    PostgresStrategyUsageAuditRepository,
    PostgresUsageFrequencySliceRepository,
)

GROUP_NAME = "contract_evidence"


class UsageWindow(BaseModel):
    """Explicit bounded input for a derived usage-frequency materialization."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    window_start: datetime
    window_end: datetime

    @field_validator("window_start", "window_end")
    @classmethod
    def validate_datetimes(cls, value: datetime, info) -> datetime:
        return _require_aware(value, info.field_name)

    def model_post_init(self, _context: object) -> None:
        if self.window_end <= self.window_start:
            raise ValueError("usage window must be positive")


class ContractEvidenceStore(Protocol):
    """Append-only store owned by the data-engine orchestration boundary."""

    def put_strategy_usage_audit(self, audit: StrategyUsageAudit) -> bool: ...

    def put_usage_frequency_slice(self, frequency: UsageFrequencySlice) -> bool: ...

    def put_strategy_data_quality_review(self, review: StrategyDataQualityReview) -> bool: ...


class PostgresContractEvidenceStore:
    """Concrete resource used by the Dagster evidence assets."""

    def __init__(
        self,
        connection: Connection[Any],
        *,
        schema: str | None = "staging",
        table: str = "contract_objects",
    ) -> None:
        self._usage_audits = PostgresStrategyUsageAuditRepository(connection, schema=schema, table=table)
        self._usage_frequency = PostgresUsageFrequencySliceRepository(connection, schema=schema, table=table)
        self._quality_reviews = PostgresStrategyDataQualityReviewRepository(connection, schema=schema, table=table)

    def put_strategy_usage_audit(self, audit: StrategyUsageAudit) -> bool:
        return self._usage_audits.put(audit)

    def put_usage_frequency_slice(self, frequency: UsageFrequencySlice) -> bool:
        return self._usage_frequency.put(frequency)

    def put_strategy_data_quality_review(self, review: StrategyDataQualityReview) -> bool:
        return self._quality_reviews.put(review)

    def list_strategy_data_quality_reviews(self, strategy_run_id: str) -> tuple[StrategyDataQualityReview, ...]:
        """Resolve a run to durable reviews without trusting their embedded audit alone."""

        audits = {audit.strategy_usage_audit_id: audit for audit in self._usage_audits.list_for_run(strategy_run_id)}
        reviews = self._quality_reviews.list_for_run(strategy_run_id)
        for review in reviews:
            persisted_audit = audits.get(review.strategy_usage_audit_id)
            if persisted_audit is None:
                raise ContractIntegrityError(f"quality review {review.review_id} has no persisted strategy usage audit")
            if review.usage_audit != persisted_audit:
                raise ContractIntegrityError(
                    f"quality review {review.review_id} embeds drifted strategy usage evidence"
                )
        return reviews


def _evidence_store(context: AssetExecutionContext) -> ContractEvidenceStore:
    return cast(ContractEvidenceStore, context.resources.contract_evidence_store)


@dg.asset(
    name="strategy_usage_audit",
    group_name=GROUP_NAME,
    required_resource_keys={"contract_evidence_store"},
    description="Persist the complete unpaginated runner-owned usage audit before any view or review.",
)
def materialize_strategy_usage_audit(
    context: AssetExecutionContext,
    prepared_strategy_usage_audit: StrategyUsageAudit,
) -> dg.Output[StrategyUsageAudit]:
    inserted = _evidence_store(context).put_strategy_usage_audit(prepared_strategy_usage_audit)
    return dg.Output(
        prepared_strategy_usage_audit,
        metadata={
            "strategy_usage_audit_id": prepared_strategy_usage_audit.strategy_usage_audit_id,
            "strategy_run_id": prepared_strategy_usage_audit.strategy_run_id,
            "planned_cell_count": len(prepared_strategy_usage_audit.planned_cells),
            "telemetry_complete": prepared_strategy_usage_audit.telemetry_complete,
            "inserted": inserted,
        },
        data_version=dg.DataVersion(prepared_strategy_usage_audit.content_sha256),
    )


@dg.asset(
    name="usage_frequency_slice",
    group_name=GROUP_NAME,
    required_resource_keys={"contract_evidence_store"},
    description="Derive a bounded read view only from the persisted complete usage audit asset.",
)
def materialize_usage_frequency_slice(
    context: AssetExecutionContext,
    strategy_usage_audit: StrategyUsageAudit,
    usage_window: UsageWindow,
) -> dg.Output[UsageFrequencySlice]:
    frequency = build_usage_frequency_slice(
        audits=(strategy_usage_audit,),
        window_start=usage_window.window_start,
        window_end=usage_window.window_end,
    )
    inserted = _evidence_store(context).put_usage_frequency_slice(frequency)
    return dg.Output(
        frequency,
        metadata={
            "usage_frequency_slice_id": frequency.usage_frequency_slice_id,
            "strategy_usage_audit_id": strategy_usage_audit.strategy_usage_audit_id,
            "inserted": inserted,
        },
        data_version=dg.DataVersion(frequency.content_sha256),
    )


@dg.asset(
    name="strategy_data_quality_review",
    group_name=GROUP_NAME,
    required_resource_keys={"contract_evidence_store"},
    description="Persist a reverse quality review only after its exact usage audit materializes.",
)
def materialize_strategy_data_quality_review(
    context: AssetExecutionContext,
    strategy_usage_audit: StrategyUsageAudit,
    prepared_strategy_data_quality_review: StrategyDataQualityReview,
) -> dg.Output[StrategyDataQualityReview]:
    review = prepared_strategy_data_quality_review
    if review.strategy_usage_audit_id != strategy_usage_audit.strategy_usage_audit_id:
        raise dg.Failure("strategy quality review references another usage audit")
    if review.usage_audit != strategy_usage_audit:
        raise dg.Failure("strategy quality review embeds drifted usage evidence")
    inserted = _evidence_store(context).put_strategy_data_quality_review(review)
    return dg.Output(
        review,
        metadata={
            "strategy_data_quality_review_id": review.review_id,
            "strategy_usage_audit_id": strategy_usage_audit.strategy_usage_audit_id,
            "ready": review.ready,
            "inserted": inserted,
        },
        data_version=dg.DataVersion(review.content_sha256),
    )


CONTRACT_EVIDENCE_ASSETS = (
    materialize_strategy_usage_audit,
    materialize_usage_frequency_slice,
    materialize_strategy_data_quality_review,
)


def _capture_key(source: SourceRegistryEntry) -> dg.AssetKey:
    return dg.AssetKey(("capture", source.source_id, source.version))


def _normalization_key(source: SourceRegistryEntry, semantic_type: SemanticTypeRegistryEntry) -> dg.AssetKey:
    return dg.AssetKey(
        (
            "normalize",
            semantic_type.semantic_type_id,
            semantic_type.version,
            source.source_id,
            source.version,
        )
    )


def _snapshot_key(semantic_type: SemanticTypeRegistryEntry) -> dg.AssetKey:
    return dg.AssetKey(("snapshot", semantic_type.semantic_type_id, semantic_type.version))


def _factor_key(template_id: str) -> dg.AssetKey:
    return dg.AssetKey(("factor", template_id))


def _asset_key_string(key: dg.AssetKey) -> str:
    return key.to_user_string()


def _sorted_specs(specs: Sequence[dg.AssetSpec]) -> tuple[dg.AssetSpec, ...]:
    return tuple(sorted(specs, key=lambda item: item.key.to_user_string()))


def compile_registry_asset_specs(
    *,
    registry: RegistrySnapshot,
    requirements: Sequence[DataRequirement],
    factor_templates: Sequence[FactorInvocationTemplate],
) -> tuple[dg.AssetSpec, ...]:
    """Compile additive source/type/factor specs without dispatching on their names."""

    semantic_types = {entry.semantic_type_id: entry for entry in registry.semantic_types}
    requirements_by_id = {requirement.requirement_id: requirement for requirement in requirements}
    templates_by_id = {template.factor_template_id: template for template in factor_templates}
    if len(requirements_by_id) != len(requirements):
        raise ValueError("Dagster composition received duplicate DataRequirement IDs")
    if len(templates_by_id) != len(factor_templates):
        raise ValueError("Dagster composition received duplicate factor template IDs")

    specs: list[dg.AssetSpec] = []
    normalizers_by_type: dict[str, list[dg.AssetKey]] = {type_id: [] for type_id in semantic_types}
    for source in registry.sources:
        capture_key = _capture_key(source)
        specs.append(
            dg.AssetSpec(
                key=capture_key,
                group_name="registered_capture",
                metadata={
                    "source_registry_entry_id": source.source_registry_entry_id,
                    "source_registry_entry_sha256": source.content_sha256,
                },
            )
        )
        for type_id in source.supported_type_ids:
            semantic_type = semantic_types[type_id]
            normalization_key = _normalization_key(source, semantic_type)
            normalizers_by_type[type_id].append(normalization_key)
            specs.append(
                dg.AssetSpec(
                    key=normalization_key,
                    deps=(capture_key,),
                    group_name="registered_normalization",
                    metadata={
                        "semantic_type_registry_entry_id": semantic_type.semantic_type_registry_entry_id,
                        "semantic_type_registry_entry_sha256": semantic_type.content_sha256,
                    },
                )
            )

    capture_manifest_key = dg.AssetKey("capture_manifest")
    specs.append(
        dg.AssetSpec(
            key=capture_manifest_key,
            deps=tuple(_capture_key(source) for source in registry.sources),
            group_name="registered_capture",
            metadata={"registry_snapshot_id": registry.registry_snapshot_id},
        )
    )
    for type_id, semantic_type in semantic_types.items():
        producers = tuple(sorted(normalizers_by_type[type_id], key=lambda item: item.to_user_string()))
        if not producers:
            raise ValueError(f"semantic type {type_id} has no registered normalizer")
        specs.append(
            dg.AssetSpec(
                key=_snapshot_key(semantic_type),
                deps=(capture_manifest_key, *producers),
                group_name="registered_snapshot",
                metadata={"semantic_type_registry_entry_id": semantic_type.semantic_type_registry_entry_id},
            )
        )

    for template in factor_templates:
        template_requirements: list[dg.AssetKey] = []
        for requirement_id in template.data_requirement_ids:
            requirement = requirements_by_id.get(requirement_id)
            if requirement is None:
                raise ValueError(f"factor template references unknown DataRequirement {requirement_id}")
            resolved_type = semantic_types.get(requirement.semantic_type_id)
            if resolved_type is None or resolved_type.domain is not requirement.domain:
                raise ValueError(f"factor requirement {requirement_id} does not resolve in the semantic registry")
            template_requirements.append(_snapshot_key(resolved_type))
        upstream: list[dg.AssetKey] = []
        for dependency in template.dependencies:
            if dependency.template_id not in templates_by_id:
                raise ValueError(f"factor template references unknown dependency {dependency.template_id}")
            upstream.append(_factor_key(dependency.template_id))
        dependency_keys: set[dg.AssetKey] = {*template_requirements, *upstream}
        sorted_dependency_keys: tuple[dg.AssetKey, ...] = tuple(sorted(dependency_keys, key=_asset_key_string))
        specs.append(
            dg.AssetSpec(
                key=_factor_key(template.factor_template_id),
                deps=sorted_dependency_keys,
                group_name="registered_factor",
                metadata={
                    "factor_template_id": template.factor_template_id,
                    "factor_template_sha256": template.content_sha256,
                },
            )
        )
    return _sorted_specs(specs)


__all__ = [
    "CONTRACT_EVIDENCE_ASSETS",
    "ContractEvidenceStore",
    "PostgresContractEvidenceStore",
    "UsageWindow",
    "compile_registry_asset_specs",
    "materialize_strategy_data_quality_review",
    "materialize_strategy_usage_audit",
    "materialize_usage_frequency_slice",
]
