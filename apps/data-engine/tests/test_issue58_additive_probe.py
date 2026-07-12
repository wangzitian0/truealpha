from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from factors.base.registered_semantic_probe import registered_semantic_probe
from pydantic import BaseModel, ConfigDict
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
    compile_capture_requirement_bindings,
    evaluate_capture_manifest,
)
from truealpha_contracts.common import CaptureEnvironment, canonical_sha256
from truealpha_contracts.data_quality import DataDomain, QualityStatus
from truealpha_contracts.execution import (
    FactorExecution,
    FactorInvocationTemplate,
    FactorKind,
    InputReadEvent,
    MaterializedFactorOutput,
    NormalizedRecordRef,
    PolicyBinding,
    PolicyRole,
    RunnerInputSelection,
    SemanticDraft,
    SemanticProducerKind,
    SnapshotCellSelection,
    SnapshotDemandCell,
    SnapshotManifest,
    SnapshotRequest,
    build_runner_input_selection,
    materialize_factor_output,
)
from truealpha_contracts.registries import (
    RegistrySnapshot,
    SemanticTypeRegistryEntry,
    SourceRegistryEntry,
)
from truealpha_contracts.universe import SubjectKind, SubjectRef, UniverseRef
from truealpha_contracts.usage import (
    DataRequirement,
    DataUsageEvent,
    PlannedCellQuality,
    PlannedDemandCell,
    QualityState,
    RequirementLevel,
    ReverseLineageEdge,
    StrategyDataQualityReview,
    StrategyUsageAudit,
    UsageEmitterKind,
    UsageFrequencySlice,
    UsageStage,
    build_strategy_usage_audit,
    build_usage_frequency_slice,
)

CUTOFF = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)
SUBJECT = SubjectRef(kind=SubjectKind.ISSUER, id="issuer.additive-probe")
PARTITION = "2026-fy"
RUN_ID = "strategy-run:additive-probe"
EXPECTED_STAGES = frozenset(
    {
        UsageStage.CAPTURE,
        UsageStage.NORMALIZATION,
        UsageStage.SNAPSHOT_SELECTION,
        UsageStage.FACTOR_CONSUMPTION,
    }
)


def _hash(character: str) -> str:
    return character * 64


class _ProbeSignalPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    subject: SubjectRef
    metric: str
    value: Decimal
    valid_on: date


def _baseline_registry() -> RegistrySnapshot:
    financial_type = SemanticTypeRegistryEntry(
        semantic_type_id="semantic.financial-fact",
        version="1.0.0",
        domain=DataDomain.FINANCIAL_FACTS,
        schema_version="1.0.0",
        schema_fingerprint_sha256=_hash("a"),
        normalized_model_key="contracts:FinancialFact",
        input_model_key="factors:FinancialFactInput",
        repository_key="repositories:FinancialFact",
        projector_key="projectors:FinancialFact",
        compatibility_sha256=_hash("b"),
        model_implementation_sha256=_hash("c"),
        repository_implementation_sha256=_hash("d"),
        projector_implementation_sha256=_hash("e"),
    )
    sec_source = SourceRegistryEntry(
        source_id="source.sec",
        version="1.0.0",
        adapter_id="adapter.sec",
        adapter_version="1.0.0",
        normalizer_id="normalizer.sec",
        normalizer_version="1.0.0",
        supported_domains=(DataDomain.FINANCIAL_FACTS,),
        supported_type_ids=(financial_type.semantic_type_id,),
        configuration_schema_sha256=_hash("1"),
        mapping_schema_sha256=_hash("2"),
        adapter_implementation_sha256=_hash("3"),
        normalizer_implementation_sha256=_hash("4"),
    )
    return RegistrySnapshot(
        sources=(sec_source,),
        semantic_types=(financial_type,),
        required_type_ids=(financial_type.semantic_type_id,),
    )


def _additive_registry() -> RegistrySnapshot:
    baseline = _baseline_registry()
    probe_type = SemanticTypeRegistryEntry(
        semantic_type_id="semantic.probe-signal",
        version="1.0.0",
        domain=DataDomain.FINANCIAL_FACTS,
        schema_version="1.0.0",
        schema_fingerprint_sha256=_hash("5"),
        normalized_model_key="contracts:ProbeSignal",
        input_model_key="factors:ProbeSignalInput",
        repository_key="repositories:ProbeSignal",
        projector_key="projectors:ProbeSignal",
        compatibility_sha256=_hash("6"),
        model_implementation_sha256=_hash("7"),
        repository_implementation_sha256=_hash("8"),
        projector_implementation_sha256=_hash("9"),
    )
    fixture_source = SourceRegistryEntry(
        source_id="source.probe-fixture",
        version="1.0.0",
        adapter_id="adapter.probe_fixture",
        adapter_version="1.0.0",
        normalizer_id="normalizer.probe_fixture",
        normalizer_version="1.0.0",
        supported_domains=(DataDomain.FINANCIAL_FACTS,),
        supported_type_ids=(baseline.semantic_types[0].semantic_type_id, probe_type.semantic_type_id),
        configuration_schema_sha256=_hash("0"),
        mapping_schema_sha256=_hash("1"),
        adapter_implementation_sha256=_hash("2"),
        normalizer_implementation_sha256=_hash("3"),
    )
    return RegistrySnapshot(
        sources=(*baseline.sources, fixture_source),
        semantic_types=(*baseline.semantic_types, probe_type),
        required_type_ids=(*baseline.required_type_ids, probe_type.semantic_type_id),
    )


def _capture_requirement(probe_type: SemanticTypeRegistryEntry) -> CaptureRequirement:
    return CaptureRequirement(
        semantic_type_id=probe_type.semantic_type_id,
        semantic_type_version=probe_type.version,
        domain=probe_type.domain,
        required_fields=("probe_signal",),
        subject_kinds=(SubjectKind.ISSUER,),
        cadence=timedelta(days=1),
        partition_rule_id="partition.fiscal:v1",
        freshness_policy_id="freshness.daily:v1",
        maximum_age=timedelta(days=2),
        quality_policy_ids=("quality.non_null:v1",),
    )


def _data_requirement(capture_requirement: CaptureRequirement, *, metric: str = "probe_signal") -> DataRequirement:
    return DataRequirement(
        capture_requirement_id=capture_requirement.capture_requirement_id,
        semantic_type_id=capture_requirement.semantic_type_id,
        domain=capture_requirement.domain,
        metric=metric,
        subject_kinds=frozenset(capture_requirement.subject_kinds),
        level=RequirementLevel.REQUIRED,
        lookback=timedelta(days=30),
        valid_period_rule_id=capture_requirement.partition_rule_id,
        maximum_age=capture_requirement.maximum_age,
        cadence=capture_requirement.cadence,
    )


def _projections(
    capture_requirement: CaptureRequirement,
) -> tuple[ApplicabilityMapping, SourceCoverageMapping, str, str]:
    applicability_key = (
        SUBJECT.kind,
        SUBJECT.id,
        capture_requirement.domain,
        PARTITION,
        capture_requirement.capture_requirement_id,
    )
    applicability: ApplicabilityMapping = {
        applicability_key: ("required", CUTOFF - timedelta(days=30)),
    }
    staging_entry = "source-coverage-entry:" + _hash("4")
    production_entry = "source-coverage-entry:" + _hash("5")
    source_coverage: SourceCoverageMapping = {
        (CaptureEnvironment.STAGING, *applicability_key): (staging_entry,),
        (CaptureEnvironment.PRODUCTION, *applicability_key): (production_entry,),
    }
    return applicability, source_coverage, staging_entry, production_entry


def _capture_scope(
    registry: RegistrySnapshot,
    capture_requirement: CaptureRequirement,
    applicability: ApplicabilityMapping,
    source_coverage: SourceCoverageMapping,
) -> CaptureScope:
    return CaptureScope(
        research_catalog_id="research-catalog:" + _hash("a"),
        research_catalog_sha256=_hash("a"),
        universe=UniverseRef(
            universe_id="universe.topt-probe",
            universe_version="2026-07-12",
            content_sha256=_hash("b"),
        ),
        applicability_catalog_id="applicability:" + _hash("c"),
        applicability_catalog_sha256=_hash("c"),
        applicability_projection_sha256=canonical_applicability_projection_sha256(applicability),
        source_coverage_catalog_id="source-coverage:" + _hash("d"),
        source_coverage_catalog_sha256=_hash("d"),
        source_coverage_projection_sha256=canonical_source_coverage_projection_sha256(source_coverage),
        slo_catalog_id="module-slo:" + _hash("e"),
        slo_catalog_sha256=_hash("e"),
        source_registry_id=registry.source_registry_snapshot_id,
        source_registry_sha256=registry.source_registry_sha256,
        semantic_type_registry_id=registry.semantic_type_registry_snapshot_id,
        semantic_type_registry_sha256=registry.semantic_type_registry_sha256,
        requirements=(capture_requirement,),
        effective_at=CUTOFF - timedelta(days=30),
        owner="data-platform",
    )


def _normalized_probe_record(
    registry: RegistrySnapshot,
) -> tuple[_ProbeSignalPayload, NormalizedRecordRef]:
    probe_type = next(item for item in registry.semantic_types if item.semantic_type_id == "semantic.probe-signal")
    fixture_source = next(item for item in registry.sources if item.source_id == "source.probe-fixture")
    payload = _ProbeSignalPayload(
        subject=SUBJECT,
        metric="probe_signal",
        value=Decimal("0.73"),
        valid_on=CUTOFF.date(),
    )
    draft = SemanticDraft(
        semantic_type_id=probe_type.semantic_type_id,
        semantic_type_version=probe_type.version,
        payload_model_key=probe_type.normalized_model_key,
        payload_schema_sha256=probe_type.schema_fingerprint_sha256,
        payload_sha256=canonical_sha256(payload.model_dump(mode="json")),
        subject=SUBJECT,
        valid_from=CUTOFF.date() - timedelta(days=30),
        valid_to=CUTOFF.date() + timedelta(days=30),
        knowable_at=CUTOFF - timedelta(hours=1),
        produced_at=CUTOFF - timedelta(minutes=40),
        producer_kind=SemanticProducerKind.DETERMINISTIC_NORMALIZER,
        producer_id=fixture_source.normalizer_id,
        producer_version=fixture_source.normalizer_version,
        producer_implementation_sha256=fixture_source.normalizer_implementation_sha256,
    )
    record = NormalizedRecordRef(
        draft=draft,
        document_id="document:probe-fixture-1",
        raw_object_id="raw-object:probe-fixture-1",
        raw_object_sha256=_hash("6"),
        source_registry_entry_id=fixture_source.source_registry_entry_id,
        source_registry_entry_sha256=fixture_source.content_sha256,
        mapping_version="probe-fixture:v1",
        mapping_implementation_sha256=fixture_source.normalizer_implementation_sha256,
        recorded_at=CUTOFF - timedelta(minutes=30),
        confidence=Decimal("0.97"),
    )
    return payload, record


def _capture_evidence(
    capture_requirement: CaptureRequirement,
    record: NormalizedRecordRef,
    source_coverage_entry_id: str,
    *,
    knowable_at: datetime | None = None,
) -> CaptureRecordEvidence:
    return CaptureRecordEvidence(
        source_coverage_entry_id=source_coverage_entry_id,
        raw_id="raw.fetches:probe-signal-1",
        raw_sha256=record.raw_object_sha256,
        normalized_id=record.normalized_record_id,
        semantic_type_id=capture_requirement.semantic_type_id,
        semantic_type_version=capture_requirement.semantic_type_version,
        populated_fields=capture_requirement.required_fields,
        knowable_at=knowable_at or record.draft.knowable_at,
        recorded_at=record.recorded_at,
        valid_from=CUTOFF - timedelta(days=30),
        valid_to=CUTOFF + timedelta(days=30),
        confidence=record.confidence,
        mapping_version=record.mapping_version,
        policy_versions={
            capture_requirement.freshness_policy_id: "v1",
            capture_requirement.partition_rule_id: "v1",
        },
        quality_check_ids=capture_requirement.quality_policy_ids,
        quality_status=QualityStatus.PASS,
        lineage_sha256=record.content_sha256,
    )


def _capture_manifest(scope: CaptureScope, evidence: CaptureRecordEvidence) -> CaptureManifest:
    requirement = scope.requirements[0]
    cell = CaptureCell(
        subject=SUBJECT,
        domain=requirement.domain,
        partition_key=PARTITION,
        capture_requirement_id=requirement.capture_requirement_id,
        applicability="required",
        status="complete",
        evidence=(evidence,),
    )
    return CaptureManifest(
        capture_scope_id=scope.capture_scope_id,
        capture_scope_sha256=scope.content_sha256,
        environment=CaptureEnvironment.STAGING,
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
        partition_key=PARTITION,
        as_of=CUTOFF,
        started_at=CUTOFF,
        cells=(cell,),
        created_at=CUTOFF + timedelta(minutes=1),
    )


def _evaluate_capture(
    scope: CaptureScope,
    manifest: CaptureManifest,
    applicability: ApplicabilityMapping,
    source_coverage: SourceCoverageMapping,
) -> CaptureEvaluationReport:
    return evaluate_capture_manifest(
        scope,
        manifest,
        applicability_catalog_id=scope.applicability_catalog_id,
        applicability_catalog_sha256=scope.applicability_catalog_sha256,
        applicability=applicability,
        source_coverage=source_coverage,
        evaluated_at=manifest.created_at + timedelta(minutes=1),
    )


def _snapshot(
    registry: RegistrySnapshot,
    data_requirement: DataRequirement,
    record: NormalizedRecordRef,
) -> tuple[SnapshotDemandCell, SnapshotManifest]:
    probe_type = next(
        item for item in registry.semantic_types if item.semantic_type_id == data_requirement.semantic_type_id
    )
    demand = SnapshotDemandCell(
        requirement_id=data_requirement.requirement_id,
        capture_requirement_id=data_requirement.capture_requirement_id,
        semantic_type_id=probe_type.semantic_type_id,
        semantic_type_version=probe_type.version,
        domain=data_requirement.domain,
        subject=SUBJECT,
        partition_key=PARTITION,
        level=data_requirement.level,
    )
    policies = tuple(
        PolicyBinding(
            role=role,
            policy_id=f"policy.{role.value}",
            policy_version="1.0.0",
            implementation_sha256=_hash("7"),
        )
        for role in PolicyRole
        if role is not PolicyRole.MEMBERSHIP
    )
    request = SnapshotRequest(
        subjects=(SUBJECT,),
        as_of=CUTOFF + timedelta(minutes=2),
        valid_on=CUTOFF.date(),
        registry_snapshot_id=registry.registry_snapshot_id,
        registry_snapshot_sha256=registry.content_sha256,
        source_registry_id=registry.source_registry_snapshot_id,
        source_registry_sha256=registry.source_registry_sha256,
        semantic_type_registry_id=registry.semantic_type_registry_snapshot_id,
        semantic_type_registry_sha256=registry.semantic_type_registry_sha256,
        policy_bindings=policies,
        demand_cells=(demand,),
    )
    snapshot = SnapshotManifest(
        request=request,
        registry_snapshot=registry,
        resolved_subjects=(SUBJECT,),
        normalized_records=(record,),
        selections=(SnapshotCellSelection(demand=demand, normalized_record_ids=(record.normalized_record_id,)),),
        resolved_at=request.as_of + timedelta(seconds=1),
        resolver_id="snapshot-resolver",
        resolver_version="1.0.0",
        resolver_implementation_sha256=_hash("8"),
    )
    return demand, snapshot


def _run_probe(
    data_requirement: DataRequirement,
    snapshot: SnapshotManifest,
) -> tuple[FactorExecution, RunnerInputSelection, InputReadEvent, MaterializedFactorOutput]:
    template = FactorInvocationTemplate(
        factor_id="registered_semantic_probe",
        factor_version="1.0.0",
        factor_implementation_sha256=_hash("9"),
        factor_kind=FactorKind.BASE,
        parameter_model_key="contracts:NoParameters",
        parameter_schema_sha256=_hash("a"),
        canonical_parameters_sha256=_hash("b"),
        data_requirement_ids=(data_requirement.requirement_id,),
    )
    execution = FactorExecution(
        template=template,
        snapshot_id=snapshot.snapshot_id,
        snapshot_sha256=snapshot.content_sha256,
        ordered_subjects=(SUBJECT,),
        started_at=snapshot.resolved_at + timedelta(seconds=1),
    )
    selection = build_runner_input_selection(
        execution=execution,
        snapshot=snapshot,
        selected_at=execution.started_at + timedelta(seconds=1),
        runner_id="factor-runner",
        runner_version="1.0.0",
        runner_implementation_sha256=_hash("c"),
    )
    draft = registered_semantic_probe(subject=SUBJECT, inputs=selection.factor_inputs)
    read_event = InputReadEvent(
        factor_execution_id=execution.factor_execution_id,
        selection_id=selection.selection_id,
        requirement_handle_id=selection.bindings[0].handle.requirement_handle_id,
        output_key=draft.output_key,
        read_index=0,
        trace_id="trace:additive-probe",
        occurred_at=selection.selected_at + timedelta(seconds=1),
    )
    output = materialize_factor_output(
        execution=execution,
        snapshot=snapshot,
        selection=selection,
        draft=draft,
        read_events=(read_event,),
        materialized_at=read_event.occurred_at + timedelta(seconds=1),
    )
    return execution, selection, read_event, output


def _planned_cell(data_requirement: DataRequirement) -> PlannedDemandCell:
    return PlannedDemandCell(
        requirement_id=data_requirement.requirement_id,
        capture_requirement_id=data_requirement.capture_requirement_id,
        semantic_type_id=data_requirement.semantic_type_id,
        domain=data_requirement.domain,
        subject=SUBJECT,
        partition_key=PARTITION,
        level=data_requirement.level,
        expected_stages=EXPECTED_STAGES,
    )


def _usage_events(
    data_requirement: DataRequirement,
    record: NormalizedRecordRef,
    capture_manifest: CaptureManifest,
    snapshot: SnapshotManifest,
    selection: RunnerInputSelection,
    output: MaterializedFactorOutput,
) -> tuple[DataUsageEvent, ...]:
    evidence_by_stage = {
        UsageStage.CAPTURE: (capture_manifest.capture_manifest_id,),
        UsageStage.NORMALIZATION: (capture_manifest.cells[0].evidence[0].evidence_id,),
        UsageStage.SNAPSHOT_SELECTION: (snapshot.snapshot_id,),
        UsageStage.FACTOR_CONSUMPTION: (selection.selection_id, output.materialized_output_id),
    }
    events = []
    for offset, stage in enumerate(sorted(EXPECTED_STAGES, key=lambda item: item.value)):
        manifest_owned = stage in {UsageStage.CAPTURE, UsageStage.NORMALIZATION}
        occurred_at = output.materialized_at + timedelta(seconds=offset + 1)
        events.append(
            DataUsageEvent(
                operation_id=f"operation:{stage.value}:additive-probe",
                emitter_kind=(
                    UsageEmitterKind.CAPTURE_MANIFEST_EVALUATOR
                    if manifest_owned
                    else UsageEmitterKind.INSTRUMENTED_RUNNER
                ),
                emitter_id="capture-evaluator" if manifest_owned else "factor-runner",
                stage=stage,
                requirement_id=data_requirement.requirement_id,
                capture_requirement_id=data_requirement.capture_requirement_id,
                semantic_type_id=data_requirement.semantic_type_id,
                domain=data_requirement.domain,
                subject=SUBJECT,
                partition_key=PARTITION,
                run_id=RUN_ID,
                trace_id="trace:additive-probe",
                normalized_record_ids=(() if stage is UsageStage.CAPTURE else (record.normalized_record_id,)),
                evidence_ids=evidence_by_stage[stage],
                occurred_at=occurred_at,
                recorded_at=occurred_at + timedelta(seconds=1),
                retained_until=CUTOFF + timedelta(days=365),
            )
        )
    return tuple(events)


def _reverse_lineage(
    events: tuple[DataUsageEvent, ...],
    *,
    include_quality: bool = True,
) -> tuple[ReverseLineageEdge, ...]:
    decision_id = "decision:additive-probe"
    trace_id = "trace:additive-probe"
    pairs = {
        (RUN_ID, decision_id, "produced"),
        (decision_id, trace_id, "traced"),
    }
    for event in events:
        for evidence_id in (
            *event.normalized_record_ids,
            *event.consumed_market_event_ids,
            *event.evidence_ids,
        ):
            pairs.add((trace_id, evidence_id, "consumed"))
    if include_quality:
        pairs.add((trace_id, "source-readiness:" + _hash("d"), "reviewed_source"))
        pairs.add((trace_id, "quality:probe-signal", "reviewed_type"))
    return tuple(
        ReverseLineageEdge(downstream_id=downstream, upstream_id=upstream, relation=relation)
        for downstream, upstream, relation in sorted(pairs)
    )


def _usage_audit(
    registry: RegistrySnapshot,
    scope: CaptureScope,
    planned_cell: PlannedDemandCell,
    events: tuple[DataUsageEvent, ...],
    *,
    reverse_lineage: tuple[ReverseLineageEdge, ...] | None = None,
) -> StrategyUsageAudit:
    run_started_at = min(event.occurred_at for event in events) - timedelta(seconds=1)
    run_completed_at = max(event.recorded_at for event in events) + timedelta(seconds=1)
    return build_strategy_usage_audit(
        strategy_run_id=RUN_ID,
        planned_cells=(planned_cell,),
        events=events,
        trace_bundle_ids=("trace-bundle:" + _hash("e"),),
        reverse_lineage=reverse_lineage or _reverse_lineage(events),
        affected_decision_ids=("decision:additive-probe",),
        research_catalog_id=scope.research_catalog_id,
        research_catalog_sha256=scope.research_catalog_sha256,
        universe=scope.universe,
        applicability_catalog_id=scope.applicability_catalog_id,
        applicability_catalog_sha256=scope.applicability_catalog_sha256,
        slo_catalog_id=scope.slo_catalog_id,
        slo_catalog_sha256=scope.slo_catalog_sha256,
        release_manifest_id="release-manifest:" + _hash("f"),
        registry_snapshot=registry,
        run_started_at=run_started_at,
        run_completed_at=run_completed_at,
        audited_at=run_completed_at + timedelta(seconds=1),
        auditor_id="dagster.strategy-usage-audit",
        auditor_version="1.0.0",
        auditor_implementation_sha256=_hash("0"),
    )


def _quality_review(
    audit: StrategyUsageAudit,
    planned_cell: PlannedDemandCell,
) -> tuple[PlannedCellQuality, StrategyDataQualityReview]:
    quality = PlannedCellQuality(
        planned_cell=planned_cell,
        source_coverage_entry_ids=("source-coverage-entry:" + _hash("4"),),
        source_readiness_report_id="source-readiness:" + _hash("d"),
        source_readiness_report_sha256=_hash("d"),
        semantic_quality_evidence_ids=("quality:probe-signal",),
        source_state=QualityState.PASS,
        semantic_type_state=QualityState.PASS,
        freshness_state=QualityState.PASS,
        rights_state=QualityState.PASS,
    )
    review = StrategyDataQualityReview(
        strategy_run_id=audit.strategy_run_id,
        strategy_usage_audit_id=audit.strategy_usage_audit_id,
        usage_audit=audit,
        cell_quality=(quality,),
        evaluator_id="strategy-data-quality",
        evaluator_version="1.0.0",
        evaluator_implementation_sha256=_hash("1"),
        evaluated_at=audit.audited_at + timedelta(seconds=1),
    )
    return quality, review


@dataclass(frozen=True)
class _ProbeClosure:
    registry: RegistrySnapshot
    capture_requirement: CaptureRequirement
    data_requirement: DataRequirement
    applicability: ApplicabilityMapping
    source_coverage: SourceCoverageMapping
    scope: CaptureScope
    payload: _ProbeSignalPayload
    record: NormalizedRecordRef
    evidence: CaptureRecordEvidence
    capture_manifest: CaptureManifest
    capture_report: CaptureEvaluationReport
    demand: SnapshotDemandCell
    snapshot: SnapshotManifest
    execution: FactorExecution
    selection: RunnerInputSelection
    read_event: InputReadEvent
    output: MaterializedFactorOutput
    planned_cell: PlannedDemandCell
    events: tuple[DataUsageEvent, ...]
    audit: StrategyUsageAudit
    quality: PlannedCellQuality
    review: StrategyDataQualityReview
    frequency: UsageFrequencySlice


def _build_probe_closure() -> _ProbeClosure:
    registry = _additive_registry()
    probe_type = next(item for item in registry.semantic_types if item.semantic_type_id == "semantic.probe-signal")
    capture_requirement = _capture_requirement(probe_type)
    data_requirement = _data_requirement(capture_requirement)
    assert compile_capture_requirement_bindings((data_requirement,), (capture_requirement,)) == {
        data_requirement.requirement_id: capture_requirement
    }
    applicability, source_coverage, staging_entry, _production_entry = _projections(capture_requirement)
    scope = _capture_scope(registry, capture_requirement, applicability, source_coverage)
    payload, record = _normalized_probe_record(registry)
    evidence = _capture_evidence(capture_requirement, record, staging_entry)
    capture_manifest = _capture_manifest(scope, evidence)
    capture_report = _evaluate_capture(scope, capture_manifest, applicability, source_coverage)
    demand, snapshot = _snapshot(registry, data_requirement, record)
    execution, selection, read_event, output = _run_probe(data_requirement, snapshot)
    planned_cell = _planned_cell(data_requirement)
    events = _usage_events(data_requirement, record, capture_manifest, snapshot, selection, output)
    audit = _usage_audit(registry, scope, planned_cell, events)
    quality, review = _quality_review(audit, planned_cell)
    frequency = build_usage_frequency_slice(
        audits=(audit,),
        window_start=audit.run_started_at - timedelta(seconds=1),
        window_end=audit.audited_at + timedelta(seconds=1),
    )
    return _ProbeClosure(
        registry=registry,
        capture_requirement=capture_requirement,
        data_requirement=data_requirement,
        applicability=applicability,
        source_coverage=source_coverage,
        scope=scope,
        payload=payload,
        record=record,
        evidence=evidence,
        capture_manifest=capture_manifest,
        capture_report=capture_report,
        demand=demand,
        snapshot=snapshot,
        execution=execution,
        selection=selection,
        read_event=read_event,
        output=output,
        planned_cell=planned_cell,
        events=events,
        audit=audit,
        quality=quality,
        review=review,
        frequency=frequency,
    )


def test_additive_source_type_and_probe_close_the_full_generic_path() -> None:
    closure = _build_probe_closure()
    fixture_source = next(item for item in closure.registry.sources if item.source_id == "source.probe-fixture")

    assert {"semantic.financial-fact", "semantic.probe-signal"}.issubset(fixture_source.supported_type_ids)
    assert closure.payload.metric == "probe_signal"
    assert closure.capture_report.ready
    assert closure.demand.planned_cell_id == closure.planned_cell.planned_cell_id
    assert closure.selection.factor_inputs[0].observation.payload_sha256 == closure.record.draft.payload_sha256
    assert closure.output.consumed_input_ids == (closure.record.normalized_record_id,)
    assert closure.output.input_read_event_ids == (closure.read_event.input_read_event_id,)
    assert closure.output.minimum_consumed_confidence == closure.record.confidence
    assert closure.audit.telemetry_complete
    assert closure.frequency.telemetry_complete
    assert closure.frequency.strategy_usage_audit_ids == (closure.audit.strategy_usage_audit_id,)
    assert closure.review.ready


def test_additive_probe_fails_stale_wrong_type_and_missing_runner_reads() -> None:
    closure = _build_probe_closure()
    staging_entry = closure.capture_manifest.cells[0].evidence[0].source_coverage_entry_id
    assert staging_entry is not None
    stale_evidence = _capture_evidence(
        closure.capture_requirement,
        closure.record,
        staging_entry,
        knowable_at=CUTOFF - closure.capture_requirement.maximum_age - timedelta(seconds=1),
    )
    stale_manifest = _capture_manifest(closure.scope, stale_evidence)
    stale_report = _evaluate_capture(
        closure.scope,
        stale_manifest,
        closure.applicability,
        closure.source_coverage,
    )
    assert not stale_report.ready
    assert any(code.startswith("evidence.stale:") for code in stale_report.blocking_reason_codes)

    financial_type = next(
        item for item in closure.registry.semantic_types if item.semantic_type_id == "semantic.financial-fact"
    )
    wrong_draft = SemanticDraft(
        **closure.record.draft.model_dump(
            exclude={
                "semantic_draft_id",
                "content_sha256",
                "semantic_type_id",
                "semantic_type_version",
                "payload_model_key",
                "payload_schema_sha256",
            }
        ),
        semantic_type_id=financial_type.semantic_type_id,
        semantic_type_version=financial_type.version,
        payload_model_key=financial_type.normalized_model_key,
        payload_schema_sha256=financial_type.schema_fingerprint_sha256,
    )
    wrong_record = NormalizedRecordRef(
        **closure.record.model_dump(exclude={"normalized_record_id", "content_sha256", "draft"}),
        draft=wrong_draft,
    )
    with pytest.raises(ValueError, match="does not match frozen demand"):
        SnapshotManifest(
            **closure.snapshot.model_dump(
                exclude={"snapshot_id", "content_sha256", "normalized_records", "selections"}
            ),
            normalized_records=(wrong_record,),
            selections=(
                SnapshotCellSelection(
                    demand=closure.demand,
                    normalized_record_ids=(wrong_record.normalized_record_id,),
                ),
            ),
        )

    draft = registered_semantic_probe(subject=SUBJECT, inputs=closure.selection.factor_inputs)
    with pytest.raises(ValueError, match="available output requires runner-derived input"):
        materialize_factor_output(
            execution=closure.execution,
            snapshot=closure.snapshot,
            selection=closure.selection,
            draft=draft,
            read_events=(),
            materialized_at=closure.output.materialized_at,
        )


def test_additive_probe_fails_undeclared_broken_and_required_zero_use() -> None:
    closure = _build_probe_closure()
    undeclared_requirement = _data_requirement(closure.capture_requirement, metric="undeclared_probe_signal")
    undeclared_event = DataUsageEvent(
        **closure.events[0].model_dump(
            exclude={
                "usage_event_id",
                "content_sha256",
                "planned_cell_id",
                "requirement_id",
            }
        ),
        requirement_id=undeclared_requirement.requirement_id,
    )
    with pytest.raises(ValueError, match="not declared by planned demand"):
        _usage_audit(
            closure.registry,
            closure.scope,
            closure.planned_cell,
            (undeclared_event,),
            reverse_lineage=_reverse_lineage((undeclared_event,)),
        )

    broken_lineage = tuple(
        edge for edge in _reverse_lineage(closure.events) if edge.upstream_id != closure.record.normalized_record_id
    )
    with pytest.raises(ValueError, match="reverse lineage is incomplete"):
        _usage_audit(
            closure.registry,
            closure.scope,
            closure.planned_cell,
            closure.events,
            reverse_lineage=broken_lineage,
        )

    partial_events = tuple(event for event in closure.events if event.stage is not UsageStage.FACTOR_CONSUMPTION)
    incomplete_audit = _usage_audit(
        closure.registry,
        closure.scope,
        closure.planned_cell,
        partial_events,
    )
    incomplete_review = StrategyDataQualityReview(
        strategy_run_id=incomplete_audit.strategy_run_id,
        strategy_usage_audit_id=incomplete_audit.strategy_usage_audit_id,
        usage_audit=incomplete_audit,
        cell_quality=(closure.quality,),
        evaluator_id="strategy-data-quality",
        evaluator_version="1.0.0",
        evaluator_implementation_sha256=_hash("1"),
        evaluated_at=incomplete_audit.audited_at + timedelta(seconds=1),
    )
    assert not incomplete_audit.telemetry_complete
    assert not incomplete_review.ready
    assert "usage.telemetry_incomplete" in incomplete_review.blocking_reason_codes
    assert any(code.startswith("usage.required_zero_or_under_use:") for code in incomplete_review.blocking_reason_codes)
