from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from truealpha_contracts.data_quality import DataDomain
from truealpha_contracts.registries import RegistrySnapshot, SemanticTypeRegistryEntry, SourceRegistryEntry
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
    UsageStage,
    build_strategy_usage_audit,
    build_usage_frequency_slice,
)

START = datetime(2026, 7, 1, tzinfo=UTC)
END = datetime(2026, 8, 1, tzinfo=UTC)
SUBJECT = SubjectRef(kind=SubjectKind.ISSUER, id="issuer:cik-1")
UNIVERSE = UniverseRef(universe_id="universe:test", universe_version="v1", content_sha256="a" * 64)
RELEASE_ID = "release-manifest:" + "b" * 64
RUN_ID = "strategy-run:1"


def _hash(character: str) -> str:
    return character * 64


def _registry() -> RegistrySnapshot:
    semantic_type = SemanticTypeRegistryEntry(
        semantic_type_id="semantic.financial-fact",
        version="1.0.0",
        domain=DataDomain.FINANCIAL_FACTS,
        schema_version="1",
        schema_fingerprint_sha256=_hash("1"),
        normalized_model_key="contracts:FinancialFact",
        input_model_key="factors:FinancialFactInput",
        repository_key="repositories:FinancialFact",
        projector_key="projectors:FinancialFact",
        compatibility_sha256=_hash("2"),
        model_implementation_sha256=_hash("3"),
        repository_implementation_sha256=_hash("4"),
        projector_implementation_sha256=_hash("5"),
    )
    source = SourceRegistryEntry(
        source_id="source.sec",
        version="1.0.0",
        adapter_id="adapter.sec",
        adapter_version="1.0.0",
        normalizer_id="normalizer.sec",
        normalizer_version="1.0.0",
        supported_domains=(DataDomain.FINANCIAL_FACTS,),
        supported_type_ids=(semantic_type.semantic_type_id,),
        configuration_schema_sha256=_hash("6"),
        mapping_schema_sha256=_hash("7"),
        adapter_implementation_sha256=_hash("8"),
        normalizer_implementation_sha256=_hash("9"),
    )
    return RegistrySnapshot(
        sources=(source,),
        semantic_types=(semantic_type,),
        required_type_ids=(semantic_type.semantic_type_id,),
    )


def _requirement() -> DataRequirement:
    return DataRequirement(
        capture_requirement_id="capture-requirement:" + _hash("0"),
        semantic_type_id="semantic.financial-fact",
        domain=DataDomain.FINANCIAL_FACTS,
        metric="revenue",
        subject_kinds=frozenset({SubjectKind.ISSUER}),
        level=RequirementLevel.REQUIRED,
        lookback=timedelta(days=400),
        valid_period_rule_id="fiscal-period:annual",
        maximum_age=timedelta(days=550),
        cadence=timedelta(days=90),
    )


def _cell(requirement: DataRequirement) -> PlannedDemandCell:
    return PlannedDemandCell(
        requirement_id=requirement.requirement_id,
        capture_requirement_id=requirement.capture_requirement_id,
        semantic_type_id=requirement.semantic_type_id,
        domain=requirement.domain,
        subject=SUBJECT,
        partition_key="2025FY",
        level=RequirementLevel.REQUIRED,
        expected_stages=frozenset(UsageStage),
    )


def _event(
    requirement: DataRequirement,
    stage: UsageStage,
    *,
    operation: str | None = None,
    run_id: str = RUN_ID,
) -> DataUsageEvent:
    manifest_stage = stage in {UsageStage.CAPTURE, UsageStage.NORMALIZATION}
    return DataUsageEvent(
        operation_id=operation or f"operation:{stage.value}",
        emitter_kind=(
            UsageEmitterKind.CAPTURE_MANIFEST_EVALUATOR if manifest_stage else UsageEmitterKind.INSTRUMENTED_RUNNER
        ),
        emitter_id="capture-evaluator" if manifest_stage else "factor-runner",
        stage=stage,
        requirement_id=requirement.requirement_id,
        capture_requirement_id=requirement.capture_requirement_id,
        semantic_type_id=requirement.semantic_type_id,
        domain=requirement.domain,
        subject=SUBJECT,
        partition_key="2025FY",
        run_id=run_id,
        trace_id=f"trace:{run_id}",
        normalized_record_ids=(() if stage is UsageStage.CAPTURE else ("normalized-record:" + _hash("a"),)),
        evidence_ids=("capture-manifest:" + _hash("b") if manifest_stage else "runner-selection:" + _hash("c"),),
        occurred_at=START + timedelta(days=1),
        recorded_at=START + timedelta(days=1, seconds=1),
        retained_until=END + timedelta(days=365),
    )


def _lineage(
    events: tuple[DataUsageEvent, ...],
    *,
    run_id: str = RUN_ID,
    include_quality: bool = True,
) -> tuple[ReverseLineageEdge, ...]:
    decision = f"decision:{run_id}"
    state = f"state-transition:{run_id}"
    trade = f"trade:{run_id}"
    valuation = f"valuation:{run_id}"
    metric = f"metric:{run_id}"
    pairs = {
        (run_id, decision, "produced"),
        (decision, state, "transitioned"),
        (state, trade, "executed"),
        (trade, valuation, "valued"),
        (valuation, metric, "measured"),
    }
    for event in events:
        pairs.add((metric, event.trace_id, "traced"))
        for input_id in (
            *event.normalized_record_ids,
            *event.consumed_market_event_ids,
            *event.evidence_ids,
        ):
            pairs.add((event.trace_id, input_id, "consumed"))
    if include_quality:
        pairs.add((metric, "source-readiness:" + _hash("2"), "reviewed_source"))
        pairs.add((metric, "quality:evidence-1", "reviewed_type"))
    return tuple(
        ReverseLineageEdge(downstream_id=downstream, upstream_id=upstream, relation=relation)
        for downstream, upstream, relation in sorted(pairs)
    )


def _audit(
    requirement: DataRequirement,
    events: tuple[DataUsageEvent, ...],
    *,
    run_id: str = RUN_ID,
    include_quality: bool = True,
    planned_cell: PlannedDemandCell | None = None,
) -> StrategyUsageAudit:
    return build_strategy_usage_audit(
        strategy_run_id=run_id,
        planned_cells=(planned_cell or _cell(requirement),),
        events=events,
        trace_bundle_ids=("trace-bundle:" + _hash("f"),),
        reverse_lineage=_lineage(events, run_id=run_id, include_quality=include_quality),
        affected_decision_ids=(f"decision:{run_id}",),
        affected_state_transition_ids=(f"state-transition:{run_id}",),
        affected_trade_ids=(f"trade:{run_id}",),
        affected_valuation_ids=(f"valuation:{run_id}",),
        affected_metric_ids=(f"metric:{run_id}",),
        research_catalog_id="research-catalog:" + _hash("c"),
        research_catalog_sha256=_hash("c"),
        universe=UNIVERSE,
        applicability_catalog_id="applicability:" + _hash("d"),
        applicability_catalog_sha256=_hash("d"),
        slo_catalog_id="module-slo:" + _hash("e"),
        slo_catalog_sha256=_hash("e"),
        release_manifest_id=RELEASE_ID,
        registry_snapshot=_registry(),
        run_started_at=START + timedelta(hours=1),
        run_completed_at=START + timedelta(days=2),
        audited_at=START + timedelta(days=3),
        auditor_id="dagster.strategy-usage-audit",
        auditor_version="1.0.0",
        auditor_implementation_sha256=_hash("d"),
    )


def _slice(requirement: DataRequirement, events: tuple[DataUsageEvent, ...]):
    return build_usage_frequency_slice(
        audits=(_audit(requirement, events),),
        window_start=START,
        window_end=END,
    )


def test_data_requirement_is_content_addressed_typed_and_source_neutral():
    requirement = _requirement()
    assert requirement.requirement_id == "data-requirement:" + requirement.content_sha256
    assert requirement.capture_requirement_id.startswith("capture-requirement:")
    assert requirement.subject_kinds == frozenset({SubjectKind.ISSUER})
    assert requirement.model_dump(mode="json")["subject_kinds"] == ["issuer"]
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        DataRequirement(**requirement.model_dump(exclude={"requirement_id", "content_sha256"}), source_id="source.sec")
    with pytest.raises(ValidationError, match="capture_requirement_id"):
        DataRequirement(
            **requirement.model_dump(exclude={"requirement_id", "content_sha256", "capture_requirement_id"})
        )


def test_usage_emitter_ownership_is_stage_specific_and_consumer_analytics_are_absent():
    requirement = _requirement()
    event = _event(requirement, UsageStage.FACTOR_CONSUMPTION)
    with pytest.raises(ValidationError, match="instrumented_runner"):
        DataUsageEvent.model_validate(
            event.model_copy(
                update={
                    "emitter_kind": UsageEmitterKind.CAPTURE_MANIFEST_EVALUATOR,
                    "usage_event_id": "",
                    "content_sha256": "",
                }
            ).model_dump()
        )
    assert "consumer_read" not in {stage.value for stage in UsageStage}


def test_usage_event_identity_is_retry_idempotent_but_operation_specific():
    requirement = _requirement()
    event = _event(requirement, UsageStage.FACTOR_CONSUMPTION)
    replay = event.model_copy(
        update={
            "usage_event_id": "",
            "content_sha256": "",
            "occurred_at": event.occurred_at + timedelta(minutes=1),
            "recorded_at": event.recorded_at + timedelta(minutes=1),
        }
    )
    replay = DataUsageEvent.model_validate(replay.model_dump())
    other_operation = _event(requirement, UsageStage.FACTOR_CONSUMPTION, operation="operation:other")
    assert replay.usage_event_id == event.usage_event_id
    assert other_operation.usage_event_id != event.usage_event_id


def test_lifecycle_usage_can_bind_exact_market_event_without_normalized_record():
    requirement = _requirement()
    event = _event(requirement, UsageStage.TRADE_EXECUTION).model_copy(
        update={
            "usage_event_id": "",
            "content_sha256": "",
            "normalized_record_ids": (),
            "consumed_market_event_ids": ("simulation-event:" + _hash("e"),),
        }
    )
    event = DataUsageEvent.model_validate(event.model_dump())

    assert event.normalized_record_ids == ()
    assert event.consumed_market_event_ids == ("simulation-event:" + _hash("e"),)


def test_zero_or_partial_usage_remains_an_explicit_failure():
    requirement = _requirement()
    empty = _slice(requirement, ())
    assert not empty.telemetry_complete
    assert empty.strategy_usage_audits[0].strategy_usage_audit_id.startswith("strategy-usage-audit:")
    assert empty.missing_required[0].missing_stages == tuple(
        sorted(_cell(requirement).expected_stages, key=lambda item: item.value)
    )

    partial = _slice(requirement, (_event(requirement, UsageStage.CAPTURE),))
    assert not partial.telemetry_complete
    assert UsageStage.CAPTURE not in partial.missing_required[0].missing_stages
    assert partial.counts_by_stage[UsageStage.CAPTURE] == 1


def test_complete_usage_is_derived_and_duplicate_events_do_not_inflate_frequency():
    requirement = _requirement()
    events = tuple(_event(requirement, stage) for stage in UsageStage)
    result = _slice(requirement, events)
    assert result.telemetry_complete
    assert not result.missing_required
    assert result.usage_frequency_slice_id == "usage-frequency:" + result.content_sha256
    assert result.registry_snapshot_id == _registry().registry_snapshot_id
    assert result.strategy_usage_audit_ids == (result.strategy_usage_audits[0].strategy_usage_audit_id,)
    assert result.trace_bundle_ids == ("trace-bundle:" + _hash("f"),)
    audit = result.strategy_usage_audits[0]
    assert audit.planned_cells[0].model_dump(mode="json")["expected_stages"] == sorted(
        stage.value for stage in UsageStage
    )
    assert requirement.requirement_id in audit.derivation_input_ids
    assert requirement.capture_requirement_id in audit.derivation_input_ids
    assert audit.registry_snapshot.registry_snapshot_id in audit.derivation_input_ids
    assert StrategyUsageAudit.model_validate_json(audit.model_dump_json()) == audit
    with pytest.raises(ValueError, match="cannot be counted twice"):
        _slice(requirement, (events[0], events[0]))


def test_undeclared_subject_type_or_stage_fails_reconciliation():
    requirement = _requirement()
    event = _event(requirement, UsageStage.CAPTURE)
    undeclared = DataUsageEvent.model_validate(
        event.model_copy(
            update={
                "subject": SubjectRef(kind=SubjectKind.ISSUER, id="issuer:cik-2"),
                "planned_cell_id": "",
                "usage_event_id": "",
                "content_sha256": "",
            }
        ).model_dump()
    )
    with pytest.raises(ValueError, match="not declared"):
        _slice(requirement, (undeclared,))

    wrong_type = DataUsageEvent.model_validate(
        event.model_copy(
            update={
                "semantic_type_id": "semantic.price-bar",
                "planned_cell_id": "",
                "usage_event_id": "",
                "content_sha256": "",
            }
        ).model_dump()
    )
    with pytest.raises(ValueError, match="not declared"):
        _slice(requirement, (wrong_type,))

    cell = _cell(requirement).model_copy(update={"expected_stages": frozenset({UsageStage.CAPTURE})})
    with pytest.raises(ValueError, match="stage is undeclared"):
        _audit(
            requirement,
            (_event(requirement, UsageStage.NORMALIZATION),),
            planned_cell=cell,
        )


def test_frequency_aggregates_only_complete_matching_audits_and_preserves_run_identity():
    requirement = _requirement()
    first_events = tuple(_event(requirement, stage) for stage in UsageStage)
    second_run = "strategy-run:2"
    second_events = tuple(_event(requirement, stage, run_id=second_run) for stage in UsageStage)
    first = _audit(requirement, first_events)
    second = _audit(requirement, second_events, run_id=second_run)

    result = build_usage_frequency_slice(audits=(second, first), window_start=START, window_end=END)

    assert result.distinct_run_ids == (RUN_ID, second_run)
    assert len(result.strategy_usage_audit_ids) == 2
    assert result.counts_by_stage[UsageStage.METRIC] == 2
    assert result.telemetry_complete


def test_strategy_usage_audit_rejects_broken_reverse_lineage():
    requirement = _requirement()
    event = _event(requirement, UsageStage.FACTOR_CONSUMPTION)
    audit = _audit(requirement, (event,))
    broken_edges = tuple(edge for edge in audit.reverse_lineage if edge.upstream_id != event.normalized_record_ids[0])
    broken = audit.model_copy(
        update={
            "strategy_usage_audit_id": "",
            "content_sha256": "",
            "reverse_lineage": broken_edges,
            "derivation_input_ids": (),
            "missing_required": (),
            "counts_by_stage": {},
            "telemetry_complete": False,
        }
    )
    with pytest.raises(ValidationError, match="reverse lineage is incomplete"):
        StrategyUsageAudit.model_validate(broken.model_dump())


def _quality(requirement: DataRequirement, state: QualityState = QualityState.PASS) -> PlannedCellQuality:
    return PlannedCellQuality(
        planned_cell=_cell(requirement),
        source_coverage_entry_ids=("source-coverage-entry:" + _hash("1"),),
        source_readiness_report_id="source-readiness:" + _hash("2"),
        source_readiness_report_sha256=_hash("2"),
        semantic_quality_evidence_ids=("quality:evidence-1",),
        source_state=state,
        semantic_type_state=state,
        freshness_state=state,
        rights_state=state,
    )


def test_reverse_quality_review_derives_readiness_from_usage_quality_and_paths():
    requirement = _requirement()
    audit = _audit(requirement, tuple(_event(requirement, stage) for stage in UsageStage))
    review = StrategyDataQualityReview(
        strategy_run_id=RUN_ID,
        strategy_usage_audit_id=audit.strategy_usage_audit_id,
        usage_audit=audit,
        cell_quality=(_quality(requirement),),
        evaluator_id="strategy-data-quality",
        evaluator_version="1.0.0",
        evaluator_implementation_sha256=_hash("3"),
        evaluated_at=END,
    )
    assert review.ready
    assert review.blocking_reason_codes == ()
    assert review.review_id == "strategy-data-quality-review:" + review.content_sha256

    failing_audit = _audit(
        requirement,
        tuple(_event(requirement, stage) for stage in UsageStage),
        include_quality=False,
    )
    failing = StrategyDataQualityReview(
        strategy_run_id=RUN_ID,
        strategy_usage_audit_id=failing_audit.strategy_usage_audit_id,
        usage_audit=failing_audit,
        cell_quality=(_quality(requirement, QualityState.UNKNOWN),),
        evaluator_id="strategy-data-quality",
        evaluator_version="1.0.0",
        evaluator_implementation_sha256=_hash("3"),
        evaluated_at=END,
    )
    assert not failing.ready
    assert any(code.startswith("quality.source_unknown") for code in failing.blocking_reason_codes)
    assert "lineage.quality_evidence_unreachable" in failing.blocking_reason_codes
    with pytest.raises(ValidationError, match="Extra inputs"):
        StrategyDataQualityReview(
            **review.model_dump(exclude={"review_id", "content_sha256", "ready", "blocking_reason_codes"}),
            ready=False,
        )

    with pytest.raises(ValidationError, match="exact usage audit"):
        StrategyDataQualityReview(
            **review.model_dump(
                exclude={
                    "review_id",
                    "content_sha256",
                    "ready",
                    "blocking_reason_codes",
                    "strategy_usage_audit_id",
                }
            ),
            strategy_usage_audit_id="strategy-usage-audit:" + _hash("0"),
        )
