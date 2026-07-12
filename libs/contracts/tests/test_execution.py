from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError
from truealpha_contracts.data_quality import DataDomain
from truealpha_contracts.execution import (
    AvailabilityStatus,
    DecisionSnapshot,
    DependencyTemplate,
    ExtractionInvocation,
    ExtractionTemplate,
    FactorExecution,
    FactorInvocationTemplate,
    FactorKind,
    FactorOutputDraft,
    FactorValidationStatus,
    InputReadEvent,
    MaterializedFactorBatch,
    ModelRevisionRef,
    NormalizedRecordRef,
    PolicyBinding,
    PolicyRole,
    ReplayEventStream,
    RequirementHandle,
    RunnerInputSelection,
    SemanticDraft,
    SemanticProducerKind,
    SimulationEvent,
    SimulationEventKind,
    SnapshotCellSelection,
    SnapshotDemandCell,
    SnapshotManifest,
    SnapshotRequest,
    TraceBundle,
    TraceEdge,
    TraceNode,
    TraceNodeKind,
    build_runner_input_selection,
    build_runner_upstream_input_selection,
    materialize_factor_output,
    validate_extraction_replay,
)
from truealpha_contracts.registries import RegistrySnapshot, SemanticTypeRegistryEntry, SourceRegistryEntry
from truealpha_contracts.universe import (
    SubjectKind,
    SubjectRef,
    UniverseDefinitionKind,
    UniverseManifest,
    UniverseMembership,
)
from truealpha_contracts.usage import RequirementLevel

NOW = datetime(2026, 1, 10, tzinfo=UTC)
SUBJECT = SubjectRef(kind=SubjectKind.ISSUER, id="issuer.example")


def _hash(character: str) -> str:
    return character * 64


def _model_revision(*, revision: str = "2026-01-09") -> ModelRevisionRef:
    return ModelRevisionRef(
        provider="provider.example",
        model_id="extractor.financial-fact",
        immutable_revision=revision,
        endpoint_or_artifact_sha256=_hash("1"),
        decoding_parameters_sha256=_hash("2"),
    )


def _extraction_template(
    model_revision: ModelRevisionRef | None = None,
    *,
    version: str = "1.0.0",
) -> ExtractionTemplate:
    model_revision = model_revision or _model_revision()
    return ExtractionTemplate(
        template_name="financial-fact",
        template_version=version,
        semantic_type_id="semantic.financial-fact",
        semantic_type_version="1.0.0",
        payload_model_key="contracts:FinancialFact",
        output_schema_sha256=_hash("a"),
        instructions_sha256=_hash("3"),
        extractor_implementation_sha256=_hash("4"),
        model_revision_id=model_revision.model_revision_id,
        model_revision_sha256=model_revision.content_sha256,
    )


def _extraction_invocation(
    template: ExtractionTemplate | None = None,
    model_revision: ModelRevisionRef | None = None,
    *,
    attempt_number: int = 1,
    previous: ExtractionInvocation | None = None,
    response_sha256: str | None = None,
) -> ExtractionInvocation:
    model_revision = model_revision or _model_revision()
    template = template or _extraction_template(model_revision)
    return ExtractionInvocation(
        model_revision_id=model_revision.model_revision_id,
        model_revision_sha256=model_revision.content_sha256,
        extraction_template_id=template.extraction_template_id,
        extraction_template_sha256=template.content_sha256,
        input_sha256=_hash("5"),
        response_sha256=response_sha256 or _hash("6"),
        semantic_payload_sha256=_hash("7"),
        attempt_number=attempt_number,
        previous_invocation_id=None if previous is None else previous.extraction_invocation_id,
        previous_invocation_sha256=None if previous is None else previous.content_sha256,
        started_at=NOW - timedelta(days=4, minutes=1),
        completed_at=NOW - timedelta(days=4),
        invoker_id="extractor.runner",
        invoker_version="1.0.0",
        invoker_implementation_sha256=template.extractor_implementation_sha256,
    )


def _registry() -> RegistrySnapshot:
    semantic_type = SemanticTypeRegistryEntry(
        semantic_type_id="semantic.financial-fact",
        version="1.0.0",
        domain=DataDomain.FINANCIAL_FACTS,
        schema_version="1",
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
    source = SourceRegistryEntry(
        source_id="source.sec",
        version="1.0.0",
        adapter_id="adapter.sec",
        adapter_version="1.0.0",
        normalizer_id="normalizer.sec",
        normalizer_version="1.0.0",
        supported_domains=(DataDomain.FINANCIAL_FACTS,),
        supported_type_ids=(semantic_type.semantic_type_id,),
        configuration_schema_sha256=_hash("1"),
        mapping_schema_sha256=_hash("2"),
        adapter_implementation_sha256=_hash("3"),
        normalizer_implementation_sha256=_hash("4"),
    )
    return RegistrySnapshot(
        sources=(source,),
        semantic_types=(semantic_type,),
        required_type_ids=(semantic_type.semantic_type_id,),
    )


def _universe() -> UniverseManifest:
    return UniverseManifest.create(
        universe_id="universe.topt",
        universe_version="2026-01",
        definition_kind=UniverseDefinitionKind.FIXED_COHORT,
        effective_at=NOW - timedelta(days=30),
        owner="research-owner",
        membership_ids=("membership:issuer.example",),
    )


def _membership() -> UniverseMembership:
    return UniverseMembership(
        membership_id="membership:issuer.example",
        universe_id="universe.topt",
        subject=SUBJECT,
        valid_from=date(2025, 1, 1),
        knowable_at=NOW - timedelta(days=20),
        recorded_at=NOW - timedelta(days=19),
        confidence=Decimal("1"),
        raw_ref="raw:universe",
    )


def _policies() -> tuple[PolicyBinding, ...]:
    return tuple(
        PolicyBinding(
            role=role,
            policy_id=f"policy.{role.value}",
            policy_version="1.0.0",
            implementation_sha256=_hash("5"),
        )
        for role in PolicyRole
    )


def _demand() -> SnapshotDemandCell:
    return SnapshotDemandCell(
        requirement_id=f"data-requirement:{_hash('6')}",
        capture_requirement_id=f"capture-requirement:{_hash('6')}",
        semantic_type_id="semantic.financial-fact",
        semantic_type_version="1.0.0",
        domain=DataDomain.FINANCIAL_FACTS,
        subject=SUBJECT,
        partition_key="FY2025",
        level=RequirementLevel.REQUIRED,
    )


def _request(registry: RegistrySnapshot | None = None) -> SnapshotRequest:
    registry = registry or _registry()
    return SnapshotRequest(
        universe=_universe().ref,
        as_of=NOW,
        valid_on=date(2025, 12, 31),
        registry_snapshot_id=registry.registry_snapshot_id,
        registry_snapshot_sha256=registry.content_sha256,
        source_registry_id=registry.source_registry_snapshot_id,
        source_registry_sha256=registry.source_registry_sha256,
        semantic_type_registry_id=registry.semantic_type_registry_snapshot_id,
        semantic_type_registry_sha256=registry.semantic_type_registry_sha256,
        policy_bindings=_policies(),
        demand_cells=(_demand(),),
    )


def _draft(**overrides) -> SemanticDraft:
    values = {
        "semantic_type_id": "semantic.financial-fact",
        "semantic_type_version": "1.0.0",
        "payload_model_key": "contracts:FinancialFact",
        "payload_schema_sha256": _hash("a"),
        "payload_sha256": _hash("7"),
        "subject": SUBJECT,
        "valid_from": date(2025, 1, 1),
        "valid_to": date(2025, 12, 31),
        "knowable_at": NOW - timedelta(days=5),
        "produced_at": NOW - timedelta(days=4),
        "producer_kind": SemanticProducerKind.DETERMINISTIC_NORMALIZER,
        "producer_id": "normalizer.sec",
        "producer_version": "1.0.0",
        "producer_implementation_sha256": _hash("4"),
    }
    values.update(overrides)
    return SemanticDraft.model_validate(values)


def _record(registry: RegistrySnapshot | None = None, **overrides) -> NormalizedRecordRef:
    registry = registry or _registry()
    source = registry.sources[0]
    values = {
        "draft": _draft(),
        "document_id": "document:sec-10k",
        "raw_object_id": "raw-object:sec-10k",
        "raw_object_sha256": _hash("8"),
        "source_registry_entry_id": source.source_registry_entry_id,
        "source_registry_entry_sha256": source.content_sha256,
        "mapping_version": "1.0.0",
        "mapping_implementation_sha256": _hash("9"),
        "recorded_at": NOW - timedelta(days=3),
        "confidence": Decimal("0.8"),
    }
    values.update(overrides)
    return NormalizedRecordRef.model_validate(values)


def _snapshot() -> SnapshotManifest:
    registry = _registry()
    request = _request(registry)
    record = _record(registry)
    return SnapshotManifest(
        request=request,
        registry_snapshot=registry,
        resolved_subjects=(SUBJECT,),
        universe_manifest=_universe(),
        universe_memberships=(_membership(),),
        normalized_records=(record,),
        selections=(SnapshotCellSelection(demand=_demand(), normalized_record_ids=(record.normalized_record_id,)),),
        resolved_at=NOW + timedelta(seconds=1),
        resolver_id="snapshot-resolver",
        resolver_version="1.0.0",
        resolver_implementation_sha256=_hash("0"),
    )


def _template(kind: FactorKind = FactorKind.BASE) -> FactorInvocationTemplate:
    dependencies: tuple[DependencyTemplate, ...] = ()
    if kind is not FactorKind.BASE:
        base = _template()
        dependencies = (DependencyTemplate(alias="base.gppe", template_id=base.factor_template_id),)
    return FactorInvocationTemplate(
        factor_id=f"factor.{kind.value}",
        factor_version="1.0.0",
        factor_implementation_sha256=_hash("9"),
        factor_kind=kind,
        parameter_model_key="factors:Parameters",
        parameter_schema_sha256=_hash("a"),
        canonical_parameters_sha256=_hash("b"),
        data_requirement_ids=(f"data-requirement:{_hash('6')}",),
        dependencies=dependencies,
    )


def _execution(kind: FactorKind = FactorKind.BASE, *, started_at: datetime = NOW) -> FactorExecution:
    snapshot = _snapshot()
    return FactorExecution(
        template=_template(kind),
        snapshot_id=snapshot.snapshot_id,
        snapshot_sha256=snapshot.content_sha256,
        ordered_subjects=(SUBJECT,),
        upstream_batch_ids=(() if kind is FactorKind.BASE else (f"factor-batch:{_hash('c')}",)),
        started_at=started_at,
    )


def _selection(execution: FactorExecution) -> RunnerInputSelection:
    return build_runner_input_selection(
        execution=execution,
        snapshot=_snapshot(),
        selected_at=NOW,
        runner_id="factor-runner",
        runner_version="1.0.0",
        runner_implementation_sha256=_hash("d"),
    )


def _draft_output() -> FactorOutputDraft:
    return FactorOutputDraft(
        output_key="gppe",
        subject=SUBJECT,
        output_model_key="factors:GppeOutput",
        output_schema_sha256=_hash("e"),
        output_payload_sha256=_hash("f"),
        availability_status=AvailabilityStatus.AVAILABLE,
        factor_validation_status=FactorValidationStatus.ACCEPTED,
    )


def test_extraction_requires_exact_chain_and_record_attaches_lineage():
    with pytest.raises(ValidationError, match="exact model, template, and invocation"):
        _draft(producer_kind=SemanticProducerKind.VERSIONED_EXTRACTION)

    model_revision = _model_revision()
    template = _extraction_template(model_revision)
    invocation = _extraction_invocation(template, model_revision)
    extracted = _draft(
        producer_kind=SemanticProducerKind.VERSIONED_EXTRACTION,
        producer_id=invocation.invoker_id,
        producer_version=invocation.invoker_version,
        producer_implementation_sha256=invocation.invoker_implementation_sha256,
        model_revision_id=model_revision.model_revision_id,
        model_revision_sha256=model_revision.content_sha256,
        extraction_template_id=template.extraction_template_id,
        extraction_template_sha256=template.content_sha256,
        extraction_invocation_id=invocation.extraction_invocation_id,
        extraction_invocation_sha256=invocation.content_sha256,
        payload_sha256=invocation.semantic_payload_sha256,
    )
    assert (
        validate_extraction_replay(
            draft=extracted,
            invocation=invocation,
            template=template,
            model_revision=model_revision,
        )
        is extracted
    )
    record = _record(draft=extracted)

    assert extracted.semantic_draft_id.startswith("semantic-draft:")
    assert record.normalized_record_id.startswith("normalized-record:")
    assert record.raw_object_sha256 == _hash("8")
    with pytest.raises(ValidationError, match="cannot claim extraction identities"):
        _draft(model_revision_id=model_revision.model_revision_id)


def test_model_template_and_invocation_are_content_addressed_and_version_frozen():
    model_revision = _model_revision()
    template = _extraction_template(model_revision)
    invocation = _extraction_invocation(template, model_revision)

    assert model_revision.model_revision_id == "model-revision:" + model_revision.content_sha256
    assert template.extraction_template_id == "extraction-template:" + template.content_sha256
    assert invocation.extraction_invocation_id == "extraction-invocation:" + invocation.content_sha256
    with pytest.raises(ValidationError, match="model revisions must be immutable"):
        _model_revision(revision="2026-latest")
    with pytest.raises(ValidationError, match="template versions must be immutable"):
        _extraction_template(model_revision, version="v2-current")
    with pytest.raises(ValidationError, match="invoker version must be immutable"):
        ExtractionInvocation(
            **invocation.model_dump(exclude={"extraction_invocation_id", "content_sha256", "invoker_version"}),
            invoker_version="main",
        )
    with pytest.raises(ValidationError, match="model-revision ID and hash"):
        ExtractionTemplate(
            **template.model_dump(exclude={"extraction_template_id", "content_sha256", "model_revision_sha256"}),
            model_revision_sha256=_hash("f"),
        )
    with pytest.raises(ValidationError, match="content_sha256"):
        ModelRevisionRef(
            **model_revision.model_dump(exclude={"model_revision_id", "content_sha256"}),
            content_sha256=_hash("f"),
        )
    with pytest.raises(ValidationError, match="content_sha256"):
        ExtractionInvocation(
            **invocation.model_dump(exclude={"extraction_invocation_id", "content_sha256"}),
            content_sha256=_hash("f"),
        )


def test_extraction_attempt_chain_and_replay_fail_closed():
    model_revision = _model_revision()
    template = _extraction_template(model_revision)
    first = _extraction_invocation(template, model_revision)
    with pytest.raises(ValidationError, match="first extraction attempt"):
        ExtractionInvocation(
            **first.model_dump(
                exclude={
                    "extraction_invocation_id",
                    "content_sha256",
                    "previous_invocation_id",
                    "previous_invocation_sha256",
                }
            ),
            previous_invocation_id=first.extraction_invocation_id,
            previous_invocation_sha256=first.content_sha256,
        )
    with pytest.raises(ValidationError, match="retry extraction attempt"):
        _extraction_invocation(template, model_revision, attempt_number=2)
    retry = _extraction_invocation(template, model_revision, attempt_number=2, previous=first)
    assert retry.extraction_invocation_id != first.extraction_invocation_id

    draft = _draft(
        producer_kind=SemanticProducerKind.VERSIONED_EXTRACTION,
        producer_id=first.invoker_id,
        producer_version=first.invoker_version,
        producer_implementation_sha256=first.invoker_implementation_sha256,
        model_revision_id=model_revision.model_revision_id,
        model_revision_sha256=model_revision.content_sha256,
        extraction_template_id=template.extraction_template_id,
        extraction_template_sha256=template.content_sha256,
        extraction_invocation_id=first.extraction_invocation_id,
        extraction_invocation_sha256=first.content_sha256,
        payload_sha256=first.semantic_payload_sha256,
    )
    substituted_attempt = _extraction_invocation(
        template,
        model_revision,
        response_sha256=_hash("8"),
    )
    with pytest.raises(ValueError, match="frozen invocation chain"):
        validate_extraction_replay(
            draft=draft,
            invocation=substituted_attempt,
            template=template,
            model_revision=model_revision,
        )
    changed_template = _extraction_template(model_revision, version="2.0.0")
    changed_invocation = _extraction_invocation(changed_template, model_revision)
    with pytest.raises(ValueError, match="frozen invocation chain"):
        validate_extraction_replay(
            draft=draft,
            invocation=changed_invocation,
            template=changed_template,
            model_revision=model_revision,
        )


def test_restatement_must_append_and_name_the_superseded_record():
    original = _record()
    with pytest.raises(ValidationError, match="restatements must append"):
        _record(is_restatement=True)

    restated = _record(
        draft=_draft(payload_sha256=_hash("c")),
        is_restatement=True,
        supersedes_record_id=original.normalized_record_id,
    )

    assert restated.normalized_record_id != original.normalized_record_id


def test_snapshot_scope_is_exclusive_and_policy_complete():
    request = _request()
    with pytest.raises(ValidationError, match="exactly one"):
        SnapshotRequest(
            **request.model_dump(exclude={"snapshot_request_id", "content_sha256", "subjects"}),
            subjects=(SUBJECT,),
        )
    with pytest.raises(ValidationError, match="policies are incomplete"):
        SnapshotRequest(
            **request.model_dump(exclude={"snapshot_request_id", "content_sha256", "policy_bindings"}),
            policy_bindings=tuple(item for item in request.policy_bindings if item.role is not PolicyRole.FUSION),
        )


def test_snapshot_is_row_complete_and_rejects_future_or_drifted_records():
    snapshot = _snapshot()
    assert snapshot.snapshot_id == f"snapshot:{snapshot.content_sha256}"

    future = _record(
        draft=_draft(knowable_at=NOW + timedelta(seconds=1), produced_at=NOW + timedelta(seconds=2)),
        recorded_at=NOW + timedelta(seconds=3),
    )
    with pytest.raises(ValidationError, match="future normalized record"):
        SnapshotManifest(
            **snapshot.model_dump(exclude={"snapshot_id", "content_sha256", "normalized_records", "selections"}),
            normalized_records=(future,),
            selections=(SnapshotCellSelection(demand=_demand(), normalized_record_ids=(future.normalized_record_id,)),),
        )

    wrong_source = _record(source_registry_entry_sha256=_hash("f"))
    with pytest.raises(ValidationError, match="unknown or drifted source"):
        SnapshotManifest(
            **snapshot.model_dump(exclude={"snapshot_id", "content_sha256", "normalized_records", "selections"}),
            normalized_records=(wrong_source,),
            selections=(
                SnapshotCellSelection(demand=_demand(), normalized_record_ids=(wrong_source.normalized_record_id,)),
            ),
        )


def test_factor_template_is_stable_while_execution_binds_snapshot_and_not_start_time():
    first = _execution(started_at=NOW)
    retry = _execution(started_at=NOW + timedelta(minutes=1))

    assert first.template.factor_template_id == retry.template.factor_template_id
    assert first.factor_execution_id == retry.factor_execution_id
    assert first.template.factor_template_id != first.factor_execution_id


def test_runner_derives_lineage_and_confidence_from_actual_reads():
    execution = _execution()
    selection = _selection(execution)
    selected = selection.bindings[0]
    read = InputReadEvent(
        factor_execution_id=execution.factor_execution_id,
        selection_id=selection.selection_id,
        requirement_handle_id=selected.handle.requirement_handle_id,
        output_key="gppe",
        read_index=0,
        trace_id="trace:gppe",
        occurred_at=NOW + timedelta(seconds=1),
    )

    output = materialize_factor_output(
        execution=execution,
        selection=selection,
        draft=_draft_output(),
        read_events=(read,),
        materialized_at=NOW + timedelta(seconds=2),
    )

    assert output.consumed_input_ids == (selected.input_id,)
    assert output.input_lineage[0].requirement_id == selected.demand.requirement_id
    assert output.input_lineage[0].capture_requirement_id == selected.demand.capture_requirement_id
    assert output.input_lineage[0].planned_cell_id == selected.demand.planned_cell_id
    assert output.input_lineage[0].evidence_status.value == "verified"
    assert output.input_lineage[0].input_read_event_ids == (read.input_read_event_id,)
    assert output.input_lineage[0].trace_ids == (read.trace_id,)
    assert output.minimum_consumed_confidence == Decimal("0.8")
    assert output.trace_complete is True
    with pytest.raises(ValidationError, match="Extra inputs"):
        FactorOutputDraft(**_draft_output().model_dump(), consumed_input_ids=(selected.input_id,))


def test_factor_visible_capability_is_opaque_and_cannot_forge_runner_binding():
    selection = _selection(_execution())
    factor_input = selection.factor_inputs[0]
    visible = factor_input.model_dump(mode="json")

    assert set(visible) == {"handle", "observation"}
    assert set(visible["handle"]) == {"requirement_handle_id"}
    assert "input_id" not in visible["observation"]
    assert "semantic_type_id" not in visible["observation"]

    forged_binding = selection.bindings[0].model_copy(
        update={"handle": RequirementHandle(requirement_handle_id=f"requirement-handle:{_hash('1')}")}
    )
    forged = selection.model_copy(update={"selection_id": "", "content_sha256": "", "bindings": (forged_binding,)})
    with pytest.raises(ValidationError, match="not minted for the exact runner demand binding"):
        RunnerInputSelection.model_validate(forged.model_dump())


def test_missing_or_forged_runner_reads_cannot_create_available_output():
    execution = _execution()
    selection = _selection(execution)
    with pytest.raises(ValidationError, match="available output requires"):
        materialize_factor_output(
            execution=execution,
            selection=selection,
            draft=_draft_output(),
            read_events=(),
            materialized_at=NOW + timedelta(seconds=2),
        )

    forged = InputReadEvent(
        factor_execution_id=execution.factor_execution_id,
        selection_id=selection.selection_id,
        requirement_handle_id=f"requirement-handle:{_hash('1')}",
        output_key="gppe",
        read_index=0,
        trace_id="trace:forged",
        occurred_at=NOW,
    )
    with pytest.raises(ValueError, match="not attributable"):
        materialize_factor_output(
            execution=execution,
            selection=selection,
            draft=_draft_output(),
            read_events=(forged,),
            materialized_at=NOW + timedelta(seconds=2),
        )


def test_composites_require_a_persisted_upstream_boundary():
    template = _template(FactorKind.COMPOSITE)
    snapshot = _snapshot()
    with pytest.raises(ValidationError, match="persisted upstream batches"):
        FactorExecution(
            template=template,
            snapshot_id=snapshot.snapshot_id,
            snapshot_sha256=snapshot.content_sha256,
            ordered_subjects=(SUBJECT,),
            started_at=NOW,
        )

    base_execution = _execution()
    batch = MaterializedFactorBatch(
        factor_execution_id=base_execution.factor_execution_id,
        snapshot_id=base_execution.snapshot_id,
        output_ids=(f"factor-output:{_hash('2')}",),
        repository_commit_id="commit:base-batch",
        persisted_at=NOW,
    )
    composite = FactorExecution(
        template=template,
        snapshot_id=snapshot.snapshot_id,
        snapshot_sha256=snapshot.content_sha256,
        ordered_subjects=(SUBJECT,),
        upstream_batch_ids=(batch.materialized_batch_id,),
        started_at=NOW,
    )
    assert composite.upstream_batch_ids == (batch.materialized_batch_id,)


def test_composite_capability_is_minted_only_from_persisted_sanitized_upstream_output():
    base_execution = _execution()
    base_selection = _selection(base_execution)
    base_read = InputReadEvent(
        factor_execution_id=base_execution.factor_execution_id,
        selection_id=base_selection.selection_id,
        requirement_handle_id=base_selection.bindings[0].handle.requirement_handle_id,
        output_key="gppe",
        read_index=0,
        trace_id="trace:base",
        occurred_at=NOW + timedelta(seconds=1),
    )
    base_output = materialize_factor_output(
        execution=base_execution,
        selection=base_selection,
        draft=_draft_output(),
        read_events=(base_read,),
        materialized_at=NOW + timedelta(seconds=2),
    )
    batch = MaterializedFactorBatch(
        factor_execution_id=base_execution.factor_execution_id,
        snapshot_id=base_execution.snapshot_id,
        output_ids=(base_output.materialized_output_id,),
        repository_commit_id="commit:base-batch",
        persisted_at=NOW + timedelta(seconds=3),
    )
    snapshot = _snapshot()
    composite = FactorExecution(
        template=_template(FactorKind.COMPOSITE),
        snapshot_id=snapshot.snapshot_id,
        snapshot_sha256=snapshot.content_sha256,
        ordered_subjects=(SUBJECT,),
        upstream_batch_ids=(batch.materialized_batch_id,),
        started_at=NOW + timedelta(seconds=4),
    )
    selection = build_runner_upstream_input_selection(
        execution=composite,
        demand=_demand(),
        upstream_batch=batch,
        upstream_output=base_output,
        selected_at=NOW + timedelta(seconds=4),
        runner_id="factor-runner",
        runner_version="1.0.0",
        runner_implementation_sha256=_hash("d"),
    )
    visible = selection.factor_inputs[0].model_dump(mode="json")
    assert "factor_execution_id" not in visible["observation"]
    assert "factor_validation_status" not in visible["observation"]
    assert "input_lineage" not in visible["observation"]

    read = InputReadEvent(
        factor_execution_id=composite.factor_execution_id,
        selection_id=selection.selection_id,
        requirement_handle_id=selection.bindings[0].handle.requirement_handle_id,
        output_key="gppe",
        read_index=0,
        trace_id="trace:composite",
        occurred_at=NOW + timedelta(seconds=5),
    )
    output = materialize_factor_output(
        execution=composite,
        selection=selection,
        draft=_draft_output(),
        read_events=(read,),
        materialized_at=NOW + timedelta(seconds=6),
    )
    assert output.upstream_output_ids == (base_output.materialized_output_id,)
    assert output.input_lineage[0].input_id == base_output.materialized_output_id


def test_future_events_are_gated_by_knowability_not_economic_date():
    snapshot = _snapshot()
    decision = DecisionSnapshot(
        universe=_universe().ref,
        snapshot_id=snapshot.snapshot_id,
        snapshot_sha256=snapshot.content_sha256,
        strategy_template_id=_template(FactorKind.STRATEGY).factor_template_id,
        strategy_execution_id=_execution(FactorKind.STRATEGY).factor_execution_id,
        materialized_batch_ids=(f"factor-batch:{_hash('3')}",),
        decision_output_ids=(f"factor-output:{_hash('4')}",),
        cutoff=NOW,
        valid_on=date(2025, 12, 31),
        created_at=NOW + timedelta(seconds=1),
    )
    announced_action = SimulationEvent(
        kind=SimulationEventKind.CORPORATE_ACTION,
        subject=SubjectRef(kind=SubjectKind.SECURITY, id="security.example"),
        source_record_id=f"normalized-record:{_hash('5')}",
        payload_schema_sha256=_hash("6"),
        payload_sha256=_hash("7"),
        event_at=NOW + timedelta(days=10),
        knowable_at=NOW + timedelta(days=1),
        recorded_at=NOW + timedelta(days=1, seconds=1),
    )
    assert ReplayEventStream(decision=decision, events=(announced_action,)).events == (announced_action,)

    leaked = announced_action.model_copy(
        update={"knowable_at": NOW, "recorded_at": NOW, "simulation_event_id": "", "content_sha256": ""}
    )
    leaked = SimulationEvent.model_validate(leaked.model_dump())
    with pytest.raises(ValidationError, match="belongs in the PIT snapshot"):
        ReplayEventStream(decision=decision, events=(leaked,))


def test_reverse_trace_requires_a_connected_path_to_raw_and_quality_evidence():
    nodes = (
        TraceNode(node_id="strategy:run", kind=TraceNodeKind.STRATEGY_RUN, content_sha256=_hash("1")),
        TraceNode(node_id="execution:1", kind=TraceNodeKind.FACTOR_EXECUTION, content_sha256=_hash("2")),
        TraceNode(node_id="snapshot:1", kind=TraceNodeKind.SNAPSHOT, content_sha256=_hash("3")),
        TraceNode(node_id="record:1", kind=TraceNodeKind.NORMALIZED_RECORD, content_sha256=_hash("4")),
        TraceNode(node_id="raw:1", kind=TraceNodeKind.RAW_OBJECT, content_sha256=_hash("5")),
        TraceNode(node_id="quality:1", kind=TraceNodeKind.QUALITY_EVIDENCE, content_sha256=_hash("6")),
    )
    edges = (
        TraceEdge(downstream_id="strategy:run", upstream_id="execution:1", relation="used"),
        TraceEdge(downstream_id="execution:1", upstream_id="snapshot:1", relation="read"),
        TraceEdge(downstream_id="snapshot:1", upstream_id="record:1", relation="selected"),
        TraceEdge(downstream_id="record:1", upstream_id="raw:1", relation="normalized_from"),
        TraceEdge(downstream_id="record:1", upstream_id="quality:1", relation="evaluated_by"),
    )
    trace = TraceBundle(
        root_node_id="strategy:run",
        nodes=nodes,
        edges=edges,
        built_by="trace-indexer",
        builder_version="1.0.0",
        builder_implementation_sha256=_hash("7"),
        built_at=NOW,
    )
    assert trace.trace_bundle_id.startswith("trace-bundle:")

    with pytest.raises(ValidationError, match="without a reverse path"):
        TraceBundle(
            root_node_id="strategy:run",
            nodes=nodes,
            edges=edges[:-1],
            built_by="trace-indexer",
            builder_version="1.0.0",
            builder_implementation_sha256=_hash("7"),
            built_at=NOW,
        )
