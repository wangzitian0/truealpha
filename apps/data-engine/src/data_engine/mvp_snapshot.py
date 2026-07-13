"""Row-complete capture evaluation and PIT snapshot for D1 filing documents."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta

from truealpha_contracts.capture_contracts import (
    ApplicabilityMapping,
    CaptureCell,
    CaptureEvaluationReport,
    CaptureManifest,
    CaptureRecordEvidence,
    CaptureRequirement,
    CaptureScope,
    SourceCoverageMapping,
    canonical_applicability_projection_sha256,
    canonical_source_coverage_projection_sha256,
    evaluate_capture_manifest,
)
from truealpha_contracts.common import CaptureEnvironment, canonical_sha256
from truealpha_contracts.data_quality import DataDomain, QualityStatus
from truealpha_contracts.execution import (
    FactorExecution,
    FactorInvocationTemplate,
    FactorKind,
    NormalizedRecordRef,
    PolicyBinding,
    PolicyRole,
    RunnerInputSelection,
    SnapshotCellSelection,
    SnapshotDemandCell,
    SnapshotManifest,
    SnapshotRequest,
    build_runner_input_selection,
)
from truealpha_contracts.registries import RegistrySnapshot
from truealpha_contracts.universe import SubjectKind, UniverseRef
from truealpha_contracts.usage import DataRequirement, RequirementLevel


def _hash(label: str) -> str:
    return canonical_sha256({"d1_mvp_filing": label})


@dataclass(frozen=True)
class FilingSnapshotBundle:
    scope: CaptureScope
    manifest: CaptureManifest
    evaluation: CaptureEvaluationReport
    snapshot: SnapshotManifest
    runner_selection: RunnerInputSelection


def build_filing_snapshot(
    *,
    records: tuple[NormalizedRecordRef, ...],
    selected_record: NormalizedRecordRef,
    registry: RegistrySnapshot,
    as_of: datetime,
) -> FilingSnapshotBundle:
    if not records or selected_record not in records:
        raise ValueError("filing snapshot requires its selected record in the capture evidence")
    subject = selected_record.draft.subject
    if any(record.draft.subject != subject for record in records):
        raise ValueError("filing snapshot records must belong to one issuer")
    requirement = CaptureRequirement(
        semantic_type_id=selected_record.draft.semantic_type_id,
        semantic_type_version=selected_record.draft.semantic_type_version,
        domain=DataDomain.FILINGS,
        required_fields=("accession", "content_sha256", "content_type", "filing_date", "form", "report_period"),
        subject_kinds=(SubjectKind.ISSUER,),
        cadence=timedelta(days=365),
        partition_rule_id="partition.filing-report-period:v1",
        freshness_policy_id="freshness.filing:v1",
        maximum_age=timedelta(days=3650),
        quality_policy_ids=("quality.filing-schema:v1", "quality.raw-lineage:v1"),
    )
    data_requirement = DataRequirement(
        capture_requirement_id=requirement.capture_requirement_id,
        semantic_type_id=requirement.semantic_type_id,
        domain=requirement.domain,
        metric="content_sha256",
        subject_kinds=frozenset(requirement.subject_kinds),
        level=RequirementLevel.REQUIRED,
        lookback=timedelta(days=3650),
        valid_period_rule_id=requirement.partition_rule_id,
        maximum_age=requirement.maximum_age,
        cadence=requirement.cadence,
    )
    partition = "plug-fy2020"
    key = (subject.kind, subject.id, requirement.domain, partition, requirement.capture_requirement_id)
    applicability: ApplicabilityMapping = {key: ("required", as_of - timedelta(days=1))}
    coverage_entry = "source-coverage-entry:" + _hash("fixture-sec-filing-coverage")
    source_coverage: SourceCoverageMapping = {(CaptureEnvironment.GITHUB_CI, *key): (coverage_entry,)}
    scope = CaptureScope(
        research_catalog_id="research-catalog:" + _hash("catalog"),
        research_catalog_sha256=_hash("catalog"),
        universe=UniverseRef(
            universe_id="universe.d1-filing",
            universe_version="corpus-v1",
            content_sha256=_hash("universe"),
        ),
        applicability_catalog_id="applicability:" + _hash("applicability"),
        applicability_catalog_sha256=_hash("applicability"),
        applicability_projection_sha256=canonical_applicability_projection_sha256(applicability),
        source_coverage_catalog_id="source-coverage:" + _hash("source-coverage"),
        source_coverage_catalog_sha256=_hash("source-coverage"),
        source_coverage_projection_sha256=canonical_source_coverage_projection_sha256(source_coverage),
        slo_catalog_id="module-slo:" + _hash("slo"),
        slo_catalog_sha256=_hash("slo"),
        source_registry_id=registry.source_registry_snapshot_id,
        source_registry_sha256=registry.source_registry_sha256,
        semantic_type_registry_id=registry.semantic_type_registry_snapshot_id,
        semantic_type_registry_sha256=registry.semantic_type_registry_sha256,
        requirements=(requirement,),
        effective_at=as_of - timedelta(days=1),
        owner="batch-d1-mvp-filing",
    )
    evidence = tuple(
        CaptureRecordEvidence(
            source_coverage_entry_id=coverage_entry,
            raw_id=f"raw.object:{record.raw_object_sha256}",
            raw_sha256=record.raw_object_sha256,
            normalized_id=record.normalized_record_id,
            semantic_type_id=record.draft.semantic_type_id,
            semantic_type_version=record.draft.semantic_type_version,
            populated_fields=requirement.required_fields,
            knowable_at=record.draft.knowable_at,
            recorded_at=record.recorded_at,
            valid_from=datetime.combine(record.draft.valid_from, time.min, UTC),
            valid_to=datetime.combine(record.draft.valid_to, time.max, UTC),
            confidence=record.confidence,
            mapping_version=record.mapping_version,
            policy_versions={
                requirement.freshness_policy_id: "v1",
                requirement.partition_rule_id: "v1",
            },
            quality_check_ids=requirement.quality_policy_ids,
            quality_status=QualityStatus.PASS,
            lineage_sha256=record.content_sha256,
        )
        for record in records
    )
    cell = CaptureCell(
        subject=subject,
        domain=requirement.domain,
        partition_key=partition,
        capture_requirement_id=requirement.capture_requirement_id,
        applicability="required",
        status="complete",
        evidence=evidence,
    )
    created_at = max(record.recorded_at for record in records) + timedelta(minutes=1)
    manifest = CaptureManifest(
        capture_scope_id=scope.capture_scope_id,
        capture_scope_sha256=scope.content_sha256,
        environment=CaptureEnvironment.GITHUB_CI,
        research_catalog_id=scope.research_catalog_id,
        research_catalog_sha256=scope.research_catalog_sha256,
        applicability_catalog_id=scope.applicability_catalog_id,
        applicability_catalog_sha256=scope.applicability_catalog_sha256,
        source_coverage_catalog_id=scope.source_coverage_catalog_id,
        source_coverage_catalog_sha256=scope.source_coverage_catalog_sha256,
        slo_catalog_id=scope.slo_catalog_id,
        slo_catalog_sha256=scope.slo_catalog_sha256,
        source_registry_id=scope.source_registry_id,
        source_registry_sha256=scope.source_registry_sha256,
        semantic_type_registry_id=scope.semantic_type_registry_id,
        semantic_type_registry_sha256=scope.semantic_type_registry_sha256,
        partition_key=partition,
        as_of=as_of,
        started_at=as_of,
        cells=(cell,),
        created_at=created_at,
    )
    evaluation = evaluate_capture_manifest(
        scope,
        manifest,
        applicability_catalog_id=scope.applicability_catalog_id,
        applicability_catalog_sha256=scope.applicability_catalog_sha256,
        applicability=applicability,
        source_coverage=source_coverage,
        evaluated_at=created_at + timedelta(seconds=1),
    )
    if not evaluation.ready:
        raise ValueError(f"filing capture evidence is not ready: {evaluation.blocking_reason_codes}")
    demand = SnapshotDemandCell(
        requirement_id=data_requirement.requirement_id,
        capture_requirement_id=requirement.capture_requirement_id,
        semantic_type_id=requirement.semantic_type_id,
        semantic_type_version=requirement.semantic_type_version,
        domain=requirement.domain,
        subject=subject,
        partition_key=partition,
        level=data_requirement.level,
    )
    request = SnapshotRequest(
        subjects=(subject,),
        as_of=as_of,
        valid_on=selected_record.draft.valid_to,
        registry_snapshot_id=registry.registry_snapshot_id,
        registry_snapshot_sha256=registry.content_sha256,
        source_registry_id=registry.source_registry_snapshot_id,
        source_registry_sha256=registry.source_registry_sha256,
        semantic_type_registry_id=registry.semantic_type_registry_snapshot_id,
        semantic_type_registry_sha256=registry.semantic_type_registry_sha256,
        policy_bindings=tuple(
            PolicyBinding(
                role=role,
                policy_id=f"policy.{role.value}",
                policy_version="1.0.0",
                implementation_sha256=_hash(f"policy-{role.value}"),
            )
            for role in PolicyRole
            if role is not PolicyRole.MEMBERSHIP
        ),
        demand_cells=(demand,),
    )
    snapshot = SnapshotManifest(
        request=request,
        registry_snapshot=registry,
        resolved_subjects=(subject,),
        normalized_records=(selected_record,),
        selections=(
            SnapshotCellSelection(
                demand=demand,
                normalized_record_ids=(selected_record.normalized_record_id,),
            ),
        ),
        resolved_at=created_at + timedelta(minutes=1),
        resolver_id="batch:D1FilingSnapshotResolver",
        resolver_version="1.0.0",
        resolver_implementation_sha256=_hash("snapshot-resolver"),
    )
    template = FactorInvocationTemplate(
        factor_id="d1_filing_document_probe",
        factor_version="1.0.0",
        factor_implementation_sha256=_hash("unregistered-filing-probe"),
        factor_kind=FactorKind.BASE,
        parameter_model_key="batch:NoParameters",
        parameter_schema_sha256=_hash("no-parameters-schema"),
        canonical_parameters_sha256=_hash("no-parameters"),
        data_requirement_ids=(data_requirement.requirement_id,),
    )
    execution = FactorExecution(
        template=template,
        snapshot_id=snapshot.snapshot_id,
        snapshot_sha256=snapshot.content_sha256,
        ordered_subjects=(subject,),
        started_at=snapshot.resolved_at + timedelta(seconds=1),
    )
    selection = build_runner_input_selection(
        execution=execution,
        snapshot=snapshot,
        selected_at=execution.started_at + timedelta(seconds=1),
        runner_id="batch:D1FilingRunner",
        runner_version="1.0.0",
        runner_implementation_sha256=_hash("filing-runner"),
    )
    return FilingSnapshotBundle(
        scope=scope,
        manifest=manifest,
        evaluation=evaluation,
        snapshot=snapshot,
        runner_selection=selection,
    )


__all__ = ["FilingSnapshotBundle", "build_filing_snapshot"]
