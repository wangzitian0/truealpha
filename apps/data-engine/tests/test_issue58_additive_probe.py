from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from factors.base.registered_semantic_probe import registered_semantic_probe
from pydantic import BaseModel, ConfigDict
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.data_quality import DataDomain
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


# #1238 deleted the capture contracts. The catalog, universe and capture identifiers below stand in
# for the objects that the snapshot, usage and audit contracts still name by identifier and hash.
RESEARCH_CATALOG_SHA256 = _hash("a")
APPLICABILITY_CATALOG_SHA256 = _hash("c")
SLO_CATALOG_SHA256 = _hash("e")
UNIVERSE = UniverseRef(universe_id="universe.topt-probe", universe_version="2026-07-12", content_sha256=_hash("b"))
CAPTURE_REQUIREMENT_ID = "capture-requirement:" + _hash("2")
CAPTURE_MANIFEST_ID = "capture-manifest:" + _hash("3")
CAPTURE_EVIDENCE_ID = "capture-evidence:" + _hash("5")


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


def _data_requirement(probe_type: SemanticTypeRegistryEntry, *, metric: str = "probe_signal") -> DataRequirement:
    return DataRequirement(
        capture_requirement_id=CAPTURE_REQUIREMENT_ID,
        semantic_type_id=probe_type.semantic_type_id,
        domain=probe_type.domain,
        metric=metric,
        subject_kinds=frozenset({SubjectKind.ISSUER}),
        level=RequirementLevel.REQUIRED,
        lookback=timedelta(days=30),
        valid_period_rule_id="partition.fiscal:v1",
        maximum_age=timedelta(days=2),
        cadence=timedelta(days=1),
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
    snapshot: SnapshotManifest,
    selection: RunnerInputSelection,
    output: MaterializedFactorOutput,
) -> tuple[DataUsageEvent, ...]:
    evidence_by_stage = {
        UsageStage.CAPTURE: (CAPTURE_MANIFEST_ID,),
        UsageStage.NORMALIZATION: (CAPTURE_EVIDENCE_ID,),
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
        research_catalog_id="research-catalog:" + RESEARCH_CATALOG_SHA256,
        research_catalog_sha256=RESEARCH_CATALOG_SHA256,
        universe=UNIVERSE,
        applicability_catalog_id="applicability:" + APPLICABILITY_CATALOG_SHA256,
        applicability_catalog_sha256=APPLICABILITY_CATALOG_SHA256,
        slo_catalog_id="module-slo:" + SLO_CATALOG_SHA256,
        slo_catalog_sha256=SLO_CATALOG_SHA256,
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
    probe_type: SemanticTypeRegistryEntry
    data_requirement: DataRequirement
    payload: _ProbeSignalPayload
    record: NormalizedRecordRef
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
    data_requirement = _data_requirement(probe_type)
    payload, record = _normalized_probe_record(registry)
    demand, snapshot = _snapshot(registry, data_requirement, record)
    execution, selection, read_event, output = _run_probe(data_requirement, snapshot)
    planned_cell = _planned_cell(data_requirement)
    events = _usage_events(data_requirement, record, snapshot, selection, output)
    audit = _usage_audit(registry, planned_cell, events)
    quality, review = _quality_review(audit, planned_cell)
    frequency = build_usage_frequency_slice(
        audits=(audit,),
        window_start=audit.run_started_at - timedelta(seconds=1),
        window_end=audit.audited_at + timedelta(seconds=1),
    )
    return _ProbeClosure(
        registry=registry,
        probe_type=probe_type,
        data_requirement=data_requirement,
        payload=payload,
        record=record,
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
    assert closure.demand.planned_cell_id == closure.planned_cell.planned_cell_id
    assert closure.selection.factor_inputs[0].observation.payload_sha256 == closure.record.draft.payload_sha256
    assert closure.output.consumed_input_ids == (closure.record.normalized_record_id,)
    assert closure.output.input_read_event_ids == (closure.read_event.input_read_event_id,)
    assert closure.output.minimum_consumed_confidence == closure.record.confidence
    assert closure.audit.telemetry_complete
    assert closure.frequency.telemetry_complete
    assert closure.frequency.strategy_usage_audit_ids == (closure.audit.strategy_usage_audit_id,)
    assert closure.review.ready


def test_additive_probe_fails_wrong_type_and_missing_runner_reads() -> None:
    closure = _build_probe_closure()
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
    undeclared_requirement = _data_requirement(closure.probe_type, metric="undeclared_probe_signal")
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
            closure.planned_cell,
            closure.events,
            reverse_lineage=broken_lineage,
        )

    partial_events = tuple(event for event in closure.events if event.stage is not UsageStage.FACTOR_CONSUMPTION)
    incomplete_audit = _usage_audit(
        closure.registry,
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
