from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from typing import NamedTuple

import pytest
from pydantic import ValidationError
from truealpha_contracts.capture_contracts import CaptureRequirement, CaptureScope
from truealpha_contracts.catalog import (
    CanonicalQuestion,
    CatalogRequirementLevel,
    CatalogTargetKind,
    ExpectedOutputStatus,
    FactorCatalogTarget,
    InvocationTemplateSelector,
    ProductOwnerApproval,
    ResearchCatalogEntry,
    ResearchCatalogManifest,
    ResearchScopeFloor,
    ResearchScopeMinimums,
)
from truealpha_contracts.common import CaptureEnvironment, canonical_sha256
from truealpha_contracts.data_quality import DataDomain
from truealpha_contracts.execution import FactorInvocationTemplate, FactorKind
from truealpha_contracts.policy_bundle import (
    CatalogRootBinding,
    DemandSchedule,
    NaturalRefreshSourceBinding,
    NaturalRefreshSourceRef,
    PlannedUsageEvidence,
    RequirementGraphManifest,
    RequirementGraphNode,
    ScheduledCatalogInvocation,
    ScheduledRequirementPartitions,
    SourceCapability,
    SourceCapabilityCatalog,
    compile_expected_demand,
    evaluate_exact_natural_refresh,
    evaluate_source_capability_coverage,
    reconcile_expected_usage,
)
from truealpha_contracts.readiness import (
    ApplicabilityCatalog,
    ApplicabilityCell,
    ApplicabilityClassification,
    BudgetDimension,
    BudgetLine,
    CoverageEvidence,
    FallbackPolicy,
    KnowabilityBasis,
    KnowabilityEvidence,
    NaturalRefreshReport,
    NaturalRefreshRequirement,
    PermissionDecision,
    RefreshEvidenceKind,
    RefreshTransition,
    RightsDecisionBasis,
    SourceCoverageCatalog,
    SourceCoverageEntry,
    SourceCoverageRequirement,
    SourceRightsApproval,
    SourceRole,
    SourceUsagePermission,
)
from truealpha_contracts.registries import RegistrySnapshot, SemanticTypeRegistryEntry, SourceRegistryEntry
from truealpha_contracts.universe import (
    SubjectKind,
    SubjectRef,
    UniverseDefinitionKind,
    UniverseManifest,
    UniverseMembership,
)
from truealpha_contracts.usage import DataRequirement, DataUsageEvent, RequirementLevel, UsageEmitterKind, UsageStage

BASE = datetime(2026, 7, 1, tzinfo=UTC)
SUBJECT = SubjectRef(kind=SubjectKind.ISSUER, id="issuer:alphabet")
PARTITION = "2025-fy"


def _approval(seed: str, *, approved_at: datetime = BASE) -> ProductOwnerApproval:
    digest = seed * 64
    return ProductOwnerApproval(
        approved_by="product-owner:truealpha",
        approval_record_id=f"approval-record:{digest}",
        approval_record_sha256=digest,
        approved_at=approved_at,
    )


def _capture_requirement() -> CaptureRequirement:
    return CaptureRequirement(
        semantic_type_id="semantic.financial_fact",
        semantic_type_version="1.0.0",
        domain=DataDomain.FINANCIAL_FACTS,
        required_fields=("revenue",),
        subject_kinds=(SubjectKind.ISSUER,),
        cadence=timedelta(days=1),
        partition_rule_id="partition.fiscal:v1",
        freshness_policy_id="freshness.daily:v1",
        maximum_age=timedelta(days=1),
        quality_policy_ids=("quality.non-null:v1",),
    )


def _data_requirement(capture: CaptureRequirement) -> DataRequirement:
    return DataRequirement(
        capture_requirement_id=capture.capture_requirement_id,
        semantic_type_id=capture.semantic_type_id,
        domain=capture.domain,
        metric="revenue",
        subject_kinds=frozenset(capture.subject_kinds),
        level=RequirementLevel.REQUIRED,
        lookback=timedelta(days=400),
        valid_period_rule_id=capture.partition_rule_id,
        maximum_age=capture.maximum_age,
        cadence=capture.cadence,
    )


def _budget_lines() -> tuple[BudgetLine, ...]:
    return tuple(
        BudgetLine(
            dimension=dimension,
            unit=unit,
            approved_monthly_limit=Decimal("100"),
            approved_annual_limit=Decimal("1200"),
            projected_monthly_use=Decimal("20"),
            projected_annual_use=Decimal("240"),
            bounded_probe_use=Decimal("1"),
            budget_approval_id=f"budget-approval:{seed * 64}",
            budget_evidence_sha256=seed * 64,
            owner="data-platform",
        )
        for dimension, unit, seed in (
            (BudgetDimension.OBJECT_STORAGE, "gb-month", "2"),
            (BudgetDimension.API_CALLS, "request", "1"),
        )
    )


def _registry_source() -> SourceRegistryEntry:
    return SourceRegistryEntry(
        source_id="source.sec.companyfacts",
        version="1.0.0",
        adapter_id="adapter.sec.companyfacts",
        adapter_version="1.0.0",
        normalizer_id="normalizer.sec.companyfacts",
        normalizer_version="1.0.0",
        supported_domains=(DataDomain.FINANCIAL_FACTS,),
        supported_type_ids=("semantic.financial_fact",),
        configuration_schema_sha256="2" * 64,
        mapping_schema_sha256="3" * 64,
        adapter_implementation_sha256="4" * 64,
        normalizer_implementation_sha256="5" * 64,
    )


def _registry_snapshot(source: SourceRegistryEntry) -> RegistrySnapshot:
    semantic_type = SemanticTypeRegistryEntry(
        semantic_type_id="semantic.financial_fact",
        version="1.0.0",
        domain=DataDomain.FINANCIAL_FACTS,
        schema_version="1",
        schema_fingerprint_sha256="6" * 64,
        normalized_model_key="truealpha_contracts.models:FinancialFact",
        input_model_key="factors.types:Fact",
        repository_key="data_engine.repositories:FinancialFactRepository",
        projector_key="data_engine.projectors:FinancialFactProjector",
        compatibility_sha256="7" * 64,
        model_implementation_sha256="8" * 64,
        repository_implementation_sha256="9" * 64,
        projector_implementation_sha256="a" * 64,
    )
    return RegistrySnapshot(
        sources=(source,),
        semantic_types=(semantic_type,),
        required_type_ids=(semantic_type.semantic_type_id,),
    )


def _rights(source: SourceRegistryEntry) -> SourceRightsApproval:
    return SourceRightsApproval(
        source_id=source.source_id,
        source_version=source.version,
        source_registry_entry_id=source.source_registry_entry_id,
        source_registry_entry_sha256=source.content_sha256,
        authorized_owner="data-platform",
        approved_by="legal-owner",
        decision_basis=RightsDecisionBasis.AUTHORIZED_HUMAN,
        permission_decisions=tuple(
            PermissionDecision(permission=permission, permitted=True, rationale="Approved for the bounded product.")
            for permission in SourceUsagePermission
        ),
        terms_evidence_id="terms:sec-companyfacts-v1",
        terms_evidence_sha256="b" * 64,
        approval_signature_id="signature:source-rights-v1",
        approval_signature_sha256="c" * 64,
        approved_at=BASE,
        expires_at=BASE + timedelta(days=365),
    )


def _factor_template(alias: str, requirement_id: str, seed: str) -> FactorInvocationTemplate:
    return FactorInvocationTemplate(
        factor_id=f"factor.{alias}",
        factor_version="1.0.0",
        factor_implementation_sha256=seed * 64,
        factor_kind=FactorKind.BASE,
        parameter_model_key="catalog:InvocationParameters",
        parameter_schema_sha256="e" * 64,
        canonical_parameters_sha256=canonical_sha256({}),
        data_requirement_ids=(requirement_id,),
    )


def _catalog(
    universe_manifest: UniverseManifest,
    requirement: DataRequirement,
) -> ResearchCatalogManifest:
    aliases = ("module.a", "module.b")
    question = CanonicalQuestion(
        question_key="question.shared.input",
        tool_kind=CatalogTargetKind.FACTOR,
        catalog_aliases=aliases,
        subject_scope=(SUBJECT,),
        requirement_level=CatalogRequirementLevel.REQUIRED,
        expected_output_type_ids=("output.factor.v1",),
        expected_statuses=(ExpectedOutputStatus.AVAILABLE,),
        prompt_examples=("Run both approved modules for the issuer.",),
        approved_at=BASE,
    )
    entries: list[ResearchCatalogEntry] = []
    for index, alias in enumerate(aliases, start=1):
        seed = str(index)
        template = _factor_template(alias, requirement.requirement_id, seed)
        entries.append(
            ResearchCatalogEntry(
                catalog_alias=alias,
                requirement_level=CatalogRequirementLevel.REQUIRED,
                target=FactorCatalogTarget(
                    factor_id=template.factor_id,
                    factor_version=template.factor_version,
                    definition_sha256=template.factor_implementation_sha256,
                ),
                universe=universe_manifest.ref,
                subject_scope=(SUBJECT,),
                invocation_template=InvocationTemplateSelector(
                    target_kind=CatalogTargetKind.FACTOR,
                    factor_template=template,
                    frozen_at=BASE,
                ),
                applicability_policy_id="applicability-policy:" + seed * 64,
                applicability_policy_sha256=seed * 64,
                slo_policy_id="slo-policy:" + seed * 64,
                slo_policy_sha256=seed * 64,
                canonical_question_ids=(question.canonical_question_id,),
                expected_output_type_ids=("output.factor.v1",),
                approved_at=BASE,
            )
        )
    floor = ResearchScopeFloor(
        universe=universe_manifest.ref,
        minimums=ResearchScopeMinimums(
            issuers=1,
            funds=1,
            themes=1,
            analysts=1,
            scenarios=1,
            screens=1,
            rankings=1,
            strategies=1,
            canonical_questions=1,
        ),
        required_entry_ids=tuple(item.catalog_entry_id for item in entries),
        required_question_ids=(question.canonical_question_id,),
        approval=_approval("3"),
    )
    return ResearchCatalogManifest(
        catalog_version="1.0.0",
        vision_sha256="4" * 64,
        scope_floor=floor,
        entries=tuple(entries),
        canonical_questions=(question,),
        catalog_approval=_approval("5", approved_at=BASE + timedelta(hours=1)),
        created_at=BASE + timedelta(hours=2),
        effective_at=BASE + timedelta(days=1),
    )


class BundleInputs(NamedTuple):
    catalog: ResearchCatalogManifest
    graph: RequirementGraphManifest
    schedule: DemandSchedule
    universe: UniverseManifest
    memberships: tuple[UniverseMembership, ...]
    applicability: ApplicabilityCatalog
    capture_scope: CaptureScope
    requirements: tuple[DataRequirement, ...]


def _bundle_inputs() -> BundleInputs:
    membership = UniverseMembership(
        membership_id="membership:alphabet-2026",
        universe_id="universe:vision-fixed",
        subject=SUBJECT,
        valid_from=date(2020, 1, 1),
        knowable_at=BASE - timedelta(days=2),
        recorded_at=BASE - timedelta(days=1),
        confidence=Decimal("1"),
        raw_ref="raw.universe:alphabet-2026",
    )
    universe = UniverseManifest.create(
        universe_id=membership.universe_id,
        universe_version="2026.07.01",
        definition_kind=UniverseDefinitionKind.FIXED_COHORT,
        membership_ids=(membership.membership_id,),
        effective_at=BASE - timedelta(days=1),
        owner="research-platform",
    )
    capture = _capture_requirement()
    requirement = _data_requirement(capture)
    catalog = _catalog(universe, requirement)
    nodes = tuple(
        RequirementGraphNode(
            node_id=f"node:{entry.catalog_alias}",
            factor_template=entry.invocation_template.factor_template,
            module_id=entry.catalog_alias,
            emitter_id=f"runner:{entry.catalog_alias}",
            data_requirement_ids=(requirement.requirement_id,),
            usage_stages=frozenset({UsageStage.FACTOR_CONSUMPTION}),
        )
        for entry in catalog.entries
    )
    roots = tuple(
        CatalogRootBinding(catalog_entry_id=entry.catalog_entry_id, node_id=f"node:{entry.catalog_alias}")
        for entry in catalog.entries
    )
    graph = RequirementGraphManifest(
        graph_version="1.0.0",
        research_catalog_id=catalog.research_catalog_id,
        research_catalog_sha256=catalog.content_sha256,
        roots=roots,
        nodes=nodes,
    )

    applicability = ApplicabilityCatalog(
        catalog_version="1.0.0",
        research_catalog_id=catalog.research_catalog_id,
        research_catalog_sha256=catalog.content_sha256,
        universe=universe.ref,
        effective_at=BASE + timedelta(days=2),
        approved_at=BASE + timedelta(days=1),
        approved_by="product-owner:truealpha",
        approval_signature_id="signature:applicability-v1",
        approval_signature_sha256="6" * 64,
        cells=tuple(
            ApplicabilityCell(
                module_id=entry.catalog_alias,
                catalog_alias=entry.catalog_alias,
                data_requirement_id=requirement.requirement_id,
                subject=SUBJECT,
                domain=requirement.domain,
                partition_key=PARTITION,
                classification=ApplicabilityClassification.REQUIRED,
                reason="The approved module requires the shared financial input.",
                effective_at=BASE + timedelta(days=2),
            )
            for entry in catalog.entries
        ),
    )
    invocations = tuple(
        ScheduledCatalogInvocation(
            run_id=run_id,
            catalog_entry_id=entry.catalog_entry_id,
            scheduled_for=BASE + timedelta(days=4, hours=index),
            as_of=BASE + timedelta(days=4),
            valid_on=date(2026, 7, 5),
            requirement_partitions=(
                ScheduledRequirementPartitions(
                    data_requirement_id=requirement.requirement_id,
                    valid_period_rule_id=requirement.valid_period_rule_id,
                    window_start=BASE + timedelta(days=4) - requirement.lookback,
                    window_end=BASE + timedelta(days=4),
                    partition_keys=(PARTITION,),
                    resolver_id="partition-resolver:fiscal",
                    resolver_version="1.0.0",
                    resolver_implementation_sha256="d" * 64,
                ),
            ),
        )
        for index, (run_id, entry) in enumerate(
            (run_id, entry) for run_id in ("run.1", "run.2") for entry in catalog.entries
        )
    )
    schedule = DemandSchedule(
        schedule_version="1.0.0",
        research_catalog_id=catalog.research_catalog_id,
        research_catalog_sha256=catalog.content_sha256,
        universe=universe.ref,
        applicability_catalog_id=applicability.applicability_catalog_id,
        applicability_catalog_sha256=applicability.content_sha256,
        invocations=invocations,
        effective_at=BASE + timedelta(days=3),
    )
    capture_scope = CaptureScope(
        research_catalog_id=catalog.research_catalog_id,
        research_catalog_sha256=catalog.content_sha256,
        universe=universe.ref,
        applicability_catalog_id=applicability.applicability_catalog_id,
        applicability_catalog_sha256=applicability.content_sha256,
        applicability_projection_sha256="7" * 64,
        source_coverage_catalog_id="source-coverage:" + "8" * 64,
        source_coverage_catalog_sha256="8" * 64,
        source_coverage_projection_sha256="9" * 64,
        slo_catalog_id="module-slo:" + "a" * 64,
        slo_catalog_sha256="a" * 64,
        source_registry_id="source-registry:" + "b" * 64,
        source_registry_sha256="b" * 64,
        semantic_type_registry_id="semantic-type-registry:" + "c" * 64,
        semantic_type_registry_sha256="c" * 64,
        requirements=(capture,),
        effective_at=BASE + timedelta(days=2),
        owner="data-platform",
    )
    return BundleInputs(
        catalog,
        graph,
        schedule,
        universe,
        (membership,),
        applicability,
        capture_scope,
        (requirement,),
    )


def _compile(inputs: BundleInputs):
    return compile_expected_demand(
        research_catalog=inputs.catalog,
        requirement_graph=inputs.graph,
        schedule=inputs.schedule,
        universe_manifest=inputs.universe,
        universe_memberships=inputs.memberships,
        applicability=inputs.applicability,
        capture_scope=inputs.capture_scope,
        data_requirements=inputs.requirements,
    )


def test_source_capability_catalog_is_applicability_independent_and_source_ref_is_exact():
    inputs = _bundle_inputs()
    entry_hash = "d" * 64
    source_ref = NaturalRefreshSourceRef(
        source_id="source.sec.companyfacts",
        source_version="1.0.0",
        source_registry_entry_id=f"source-registry-entry:{entry_hash}",
        source_registry_entry_sha256=entry_hash,
    )
    budget_lines = _budget_lines()
    capability = SourceCapability(
        source=source_ref,
        semantic_type_id="semantic.financial_fact",
        semantic_type_version="1.0.0",
        domain=DataDomain.FINANCIAL_FACTS,
        subject_kinds=frozenset({SubjectKind.ISSUER}),
        partition_pattern="fiscal-year:*",
        permissions=frozenset({SourceUsagePermission.RAW_RETENTION}),
        rights_approval_id="source-rights:" + "e" * 64,
        rights_approval_sha256="e" * 64,
        budget_lines=budget_lines,
    )
    catalog = SourceCapabilityCatalog(
        catalog_version="1.0.0",
        research_catalog_id=inputs.catalog.research_catalog_id,
        research_catalog_sha256=inputs.catalog.content_sha256,
        universe=inputs.universe.ref,
        source_registry_id="source-registry:" + "f" * 64,
        source_registry_sha256="f" * 64,
        capabilities=(capability,),
        effective_at=BASE + timedelta(days=2),
        owner="data-platform",
    )

    assert not any("applicability" in field for field in SourceCapabilityCatalog.model_fields)
    assert catalog.capabilities == (capability,)
    assert [line.dimension for line in capability.budget_lines] == [
        BudgetDimension.API_CALLS,
        BudgetDimension.OBJECT_STORAGE,
    ]
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        SourceCapabilityCatalog.model_validate(
            {
                **catalog.model_dump(mode="json", exclude={"source_capability_catalog_id", "content_sha256"}),
                "applicability_catalog_id": inputs.applicability.applicability_catalog_id,
            }
        )
    with pytest.raises(ValidationError, match="source registry entry ID and hash do not match"):
        NaturalRefreshSourceRef(
            source_id="source.sec.companyfacts",
            source_version="1.0.0",
            source_registry_entry_id="source-registry-entry:" + "1" * 64,
            source_registry_entry_sha256="2" * 64,
        )
    with pytest.raises(ValidationError, match="Field required"):
        SourceCapability.model_validate(capability.model_dump(exclude={"budget_lines"}))
    with pytest.raises(ValidationError, match="budget dimensions must be unique"):
        SourceCapability.model_validate(
            {
                **capability.model_dump(exclude={"source_capability_id", "content_sha256", "budget_lines"}),
                "budget_lines": (budget_lines[0], budget_lines[0]),
            }
        )
    with pytest.raises(ValidationError, match="content_sha256 does not match canonical content"):
        SourceCapability.model_validate(
            {
                **capability.model_dump(),
                "budget_lines": (
                    capability.budget_lines[0].model_copy(update={"projected_monthly_use": Decimal("21")}),
                    capability.budget_lines[1],
                ),
            }
        )


def test_compile_expected_demand_keeps_shared_input_separate_by_run_module_and_emitter():
    plan = _compile(_bundle_inputs())

    assert [run.run_id for run in plan.runs] == ["run.1", "run.2"]
    assert all(len(run.input_cells) == 1 for run in plan.runs)
    assert plan.runs[0].input_cells[0].planned_cell_id == plan.runs[1].input_cells[0].planned_cell_id
    for run in plan.runs:
        assert {usage.run_id for usage in run.usage_requirements} == {run.run_id}
        assert {usage.module_id for usage in run.usage_requirements} == {"module.a", "module.b"}
        assert {usage.emitter_id for usage in run.usage_requirements} == {"runner:module.a", "runner:module.b"}
        assert {usage.emitter_kind for usage in run.usage_requirements} == {
            UsageEmitterKind.CAPTURE_MANIFEST_EVALUATOR,
            UsageEmitterKind.INSTRUMENTED_RUNNER,
        }
        assert run.input_cells[0].expected_stages == frozenset(
            {
                UsageStage.CAPTURE,
                UsageStage.NORMALIZATION,
                UsageStage.SNAPSHOT_SELECTION,
                UsageStage.FACTOR_CONSUMPTION,
            }
        )
    assert len({usage.planned_usage_requirement_id for run in plan.runs for usage in run.usage_requirements}) == 16


def test_compile_expected_demand_never_uses_recorded_at_as_a_pit_axis():
    inputs = _bundle_inputs()
    recorded_after_run = inputs.memberships[0].model_copy(update={"recorded_at": BASE + timedelta(days=30)})

    plan = _compile(inputs._replace(memberships=(recorded_after_run,)))

    assert len(plan.runs) == 2


def test_compile_expected_demand_rejects_requirement_graph_cycles():
    inputs = _bundle_inputs()
    left, right = inputs.graph.nodes
    cyclic = RequirementGraphManifest(
        graph_version=inputs.graph.graph_version,
        research_catalog_id=inputs.catalog.research_catalog_id,
        research_catalog_sha256=inputs.catalog.content_sha256,
        roots=inputs.graph.roots,
        nodes=(
            left.model_copy(update={"upstream_node_ids": (right.node_id,)}),
            right.model_copy(update={"upstream_node_ids": (left.node_id,)}),
        ),
    )

    with pytest.raises(ValueError, match="contains a cycle"):
        _compile(inputs._replace(graph=cyclic))


def test_compile_expected_demand_rejects_graph_edges_not_declared_by_the_factor_template():
    inputs = _bundle_inputs()
    left, right = inputs.graph.nodes
    drifted = RequirementGraphManifest(
        graph_version=inputs.graph.graph_version,
        research_catalog_id=inputs.catalog.research_catalog_id,
        research_catalog_sha256=inputs.catalog.content_sha256,
        roots=inputs.graph.roots,
        nodes=(left.model_copy(update={"upstream_node_ids": (right.node_id,)}), right),
    )

    with pytest.raises(ValueError, match="dependencies drift"):
        _compile(inputs._replace(graph=drifted))


def test_compile_expected_demand_rejects_orphan_graph_nodes():
    inputs = _bundle_inputs()
    template = inputs.catalog.entries[0].invocation_template.factor_template
    orphan = RequirementGraphNode(
        node_id="node:orphan",
        factor_template=template,
        module_id="module.orphan",
        emitter_id="runner:module.orphan",
        data_requirement_ids=template.data_requirement_ids,
        usage_stages=frozenset({UsageStage.FACTOR_CONSUMPTION}),
    )
    graph = RequirementGraphManifest(
        graph_version=inputs.graph.graph_version,
        research_catalog_id=inputs.catalog.research_catalog_id,
        research_catalog_sha256=inputs.catalog.content_sha256,
        roots=inputs.graph.roots,
        nodes=(*inputs.graph.nodes, orphan),
    )

    with pytest.raises(ValueError, match="orphan nodes"):
        _compile(inputs._replace(graph=graph))


@pytest.mark.parametrize("mode", ["missing", "extra"])
def test_compile_expected_demand_rejects_missing_or_extra_applicability(mode: str):
    inputs = _bundle_inputs()
    cells = inputs.applicability.cells
    if mode == "missing":
        replacement_cells = cells[:1]
    else:
        replacement_cells = (
            *cells,
            ApplicabilityCell(
                module_id="module.extra",
                catalog_alias="module.a",
                data_requirement_id=inputs.requirements[0].requirement_id,
                subject=SUBJECT,
                domain=inputs.requirements[0].domain,
                partition_key=PARTITION,
                classification=ApplicabilityClassification.REQUIRED,
                reason="This undeclared coordinate must fail closed.",
                effective_at=BASE + timedelta(days=2),
            ),
        )
    applicability = ApplicabilityCatalog(
        catalog_version=inputs.applicability.catalog_version,
        research_catalog_id=inputs.catalog.research_catalog_id,
        research_catalog_sha256=inputs.catalog.content_sha256,
        universe=inputs.universe.ref,
        effective_at=inputs.applicability.effective_at,
        approved_at=inputs.applicability.approved_at,
        approved_by=inputs.applicability.approved_by,
        approval_signature_id=inputs.applicability.approval_signature_id,
        approval_signature_sha256=inputs.applicability.approval_signature_sha256,
        cells=replacement_cells,
    )
    schedule = DemandSchedule(
        schedule_version=inputs.schedule.schedule_version,
        research_catalog_id=inputs.catalog.research_catalog_id,
        research_catalog_sha256=inputs.catalog.content_sha256,
        universe=inputs.universe.ref,
        applicability_catalog_id=applicability.applicability_catalog_id,
        applicability_catalog_sha256=applicability.content_sha256,
        invocations=inputs.schedule.invocations,
        effective_at=inputs.schedule.effective_at,
    )
    capture_scope = CaptureScope(
        **{
            **inputs.capture_scope.model_dump(
                exclude={
                    "capture_scope_id",
                    "content_sha256",
                    "applicability_catalog_id",
                    "applicability_catalog_sha256",
                }
            ),
            "applicability_catalog_id": applicability.applicability_catalog_id,
            "applicability_catalog_sha256": applicability.content_sha256,
        }
    )

    with pytest.raises(ValueError, match="applicability does not exactly cover"):
        _compile(inputs._replace(schedule=schedule, applicability=applicability, capture_scope=capture_scope))


def test_schedule_requires_exact_requirement_partition_window():
    inputs = _bundle_inputs()
    invocation = inputs.schedule.invocations[0]
    selection = invocation.requirement_partitions[0]
    shortened = ScheduledRequirementPartitions(
        **{
            **selection.model_dump(exclude={"partition_selection_id", "content_sha256", "window_start"}),
            "window_start": selection.window_start + timedelta(days=1),
        }
    )
    changed_invocation = ScheduledCatalogInvocation(
        **{
            **invocation.model_dump(exclude={"scheduled_invocation_id", "content_sha256", "requirement_partitions"}),
            "requirement_partitions": (shortened,),
        }
    )
    schedule = DemandSchedule(
        schedule_version=inputs.schedule.schedule_version,
        research_catalog_id=inputs.schedule.research_catalog_id,
        research_catalog_sha256=inputs.schedule.research_catalog_sha256,
        universe=inputs.schedule.universe,
        applicability_catalog_id=inputs.schedule.applicability_catalog_id,
        applicability_catalog_sha256=inputs.schedule.applicability_catalog_sha256,
        invocations=(changed_invocation, *inputs.schedule.invocations[1:]),
        effective_at=inputs.schedule.effective_at,
    )

    with pytest.raises(ValueError, match="exact requirement lookback window"):
        _compile(inputs._replace(schedule=schedule))


def test_content_addresses_equal_datetime_instants_identically():
    inputs = _bundle_inputs()
    invocation = inputs.schedule.invocations[0]
    plus_eight = timezone(timedelta(hours=8))
    shifted = ScheduledCatalogInvocation(
        run_id=invocation.run_id,
        catalog_entry_id=invocation.catalog_entry_id,
        scheduled_for=invocation.scheduled_for.astimezone(plus_eight),
        as_of=invocation.as_of.astimezone(plus_eight),
        valid_on=invocation.valid_on,
        requirement_partitions=tuple(
            ScheduledRequirementPartitions(
                **{
                    **selection.model_dump(
                        exclude={"partition_selection_id", "content_sha256", "window_start", "window_end"}
                    ),
                    "window_start": selection.window_start.astimezone(plus_eight),
                    "window_end": selection.window_end.astimezone(plus_eight),
                }
            )
            for selection in invocation.requirement_partitions
        ),
    )

    assert shifted.content_sha256 == invocation.content_sha256


def test_exact_usage_reconciliation_rejects_reusing_one_event_for_two_requirements():
    inputs = _bundle_inputs()
    plan = _compile(inputs)
    requirement = inputs.requirements[0]
    usages = tuple(item for run in plan.runs for item in run.usage_requirements)
    evidence: list[PlannedUsageEvidence] = []
    for index, usage in enumerate(usages):
        event = DataUsageEvent(
            operation_id=f"operation:{index}",
            emitter_kind=usage.emitter_kind,
            emitter_id=usage.emitter_id,
            stage=usage.stage,
            planned_cell_id=usage.planned_cell_id,
            requirement_id=requirement.requirement_id,
            capture_requirement_id=requirement.capture_requirement_id,
            semantic_type_id=requirement.semantic_type_id,
            domain=requirement.domain,
            subject=SUBJECT,
            partition_key=PARTITION,
            run_id=usage.run_id,
            trace_id=f"trace:{index}",
            normalized_record_ids=() if usage.stage is UsageStage.CAPTURE else (f"normalized:{index}",),
            evidence_ids=(f"evidence:{index}",),
            occurred_at=BASE + timedelta(days=5),
            recorded_at=BASE + timedelta(days=5, minutes=1),
            retained_until=BASE + timedelta(days=400),
        )
        evidence.append(
            PlannedUsageEvidence(
                planned_usage_requirement_id=usage.planned_usage_requirement_id,
                run_id=usage.run_id,
                scheduled_invocation_id=usage.scheduled_invocation_id,
                module_id=usage.module_id,
                emitter_id=usage.emitter_id,
                stage=usage.stage,
                planned_cell_id=usage.planned_cell_id,
                usage_event=event,
            )
        )

    assert reconcile_expected_usage(expected_demand=plan, evidence=tuple(evidence)).ready
    source_usage = usages[0]
    reused = PlannedUsageEvidence(
        planned_usage_requirement_id=evidence[1].planned_usage_requirement_id,
        run_id=source_usage.run_id,
        scheduled_invocation_id=source_usage.scheduled_invocation_id,
        module_id=source_usage.module_id,
        emitter_id=source_usage.emitter_id,
        stage=source_usage.stage,
        planned_cell_id=source_usage.planned_cell_id,
        usage_event=evidence[0].usage_event,
    )
    report = reconcile_expected_usage(expected_demand=plan, evidence=(evidence[0], reused, *evidence[2:]))
    assert not report.ready
    assert "usage.event_reused_across_requirements" in report.blocking_reason_codes

    wrong_module = PlannedUsageEvidence(
        planned_usage_requirement_id=evidence[0].planned_usage_requirement_id,
        run_id=source_usage.run_id,
        scheduled_invocation_id=source_usage.scheduled_invocation_id,
        module_id="module:wrong",
        emitter_id=source_usage.emitter_id,
        stage=source_usage.stage,
        planned_cell_id=source_usage.planned_cell_id,
        usage_event=evidence[0].usage_event,
    )
    wrong_module_report = reconcile_expected_usage(
        expected_demand=plan,
        evidence=(wrong_module, *evidence[1:]),
    )
    assert (
        "usage.binding_mismatch:" + evidence[0].planned_usage_requirement_id
        in wrong_module_report.blocking_reason_codes
    )


def test_source_coverage_rows_must_match_one_exact_capability():
    inputs = _bundle_inputs()
    source = _registry_source()
    registry = _registry_snapshot(source)
    rights = _rights(source)
    budget_lines = tuple(sorted(_budget_lines(), key=lambda item: item.dimension.value))
    source_ref = NaturalRefreshSourceRef(
        source_id=source.source_id,
        source_version=source.version,
        source_registry_entry_id=source.source_registry_entry_id,
        source_registry_entry_sha256=source.content_sha256,
    )
    capability = SourceCapability(
        source=source_ref,
        semantic_type_id="semantic.financial_fact",
        semantic_type_version="1.0.0",
        domain=DataDomain.FINANCIAL_FACTS,
        subject_kinds=frozenset({SubjectKind.ISSUER}),
        partition_pattern="*",
        permissions=frozenset({SourceUsagePermission.RAW_RETENTION}),
        rights_approval_id=rights.rights_approval_id,
        rights_approval_sha256=rights.content_sha256,
        budget_lines=budget_lines,
    )
    capability_catalog = SourceCapabilityCatalog(
        catalog_version="1.0.0",
        research_catalog_id=inputs.catalog.research_catalog_id,
        research_catalog_sha256=inputs.catalog.content_sha256,
        universe=inputs.universe.ref,
        source_registry_id=registry.source_registry_snapshot_id,
        source_registry_sha256=registry.source_registry_sha256,
        capabilities=(capability,),
        effective_at=BASE + timedelta(days=2),
        owner="data-platform",
    )
    coverage_requirements = tuple(
        SourceCoverageRequirement(
            environment=environment,
            data_requirement_id=inputs.requirements[0].requirement_id,
            semantic_type_id=capability.semantic_type_id,
            semantic_type_version=capability.semantic_type_version,
            subject=SUBJECT,
            domain=capability.domain,
            partition_key=PARTITION,
            required_permissions=frozenset({SourceUsagePermission.RAW_RETENTION}),
            minimum_observed_count=1,
            fallback_policy=FallbackPolicy.REQUIRED,
        )
        for environment in (
            CaptureEnvironment.LOCAL_DEV,
            CaptureEnvironment.LOCAL_TEST,
            CaptureEnvironment.GITHUB_CI,
            CaptureEnvironment.STAGING,
            CaptureEnvironment.PRODUCTION,
        )
    )
    coverage_evidence = CoverageEvidence(
        evidence_id="coverage:sec-companyfacts",
        artifact_sha256=("d" * 64,),
        observed_at=BASE + timedelta(days=2),
        observed_count=1,
        earliest_knowable_at=BASE - timedelta(days=365),
        latest_knowable_at=BASE,
    )
    knowability = KnowabilityEvidence(
        rule_id="knowability:publication",
        rule_version="1.0.0",
        basis=KnowabilityBasis.PUBLICATION_EVENT,
        evidence_id="evidence:publication-policy",
        evidence_sha256="e" * 64,
        observed_at=BASE,
    )
    coverage_entries = tuple(
        SourceCoverageEntry(
            environment=requirement.environment,
            data_requirement_id=requirement.data_requirement_id,
            semantic_type_id=requirement.semantic_type_id,
            semantic_type_version=requirement.semantic_type_version,
            subject=requirement.subject,
            domain=requirement.domain,
            partition_key=requirement.partition_key,
            role=SourceRole.PRIMARY,
            priority=0,
            source_id=source.source_id,
            source_version=source.version,
            source_registry_entry_id=source.source_registry_entry_id,
            source_registry_entry_sha256=source.content_sha256,
            rights_approval_id=rights.rights_approval_id,
            rights_approval_sha256=rights.content_sha256,
            identifier_level="issuer",
            capture_method="api",
            credential_owner="data-platform",
            cadence=timedelta(days=1),
            review_expires_at=BASE + timedelta(days=365),
            knowability=knowability,
            coverage=coverage_evidence,
            budget_lines=budget_lines,
        )
        for requirement in coverage_requirements
    )
    coverage_catalog = SourceCoverageCatalog(
        catalog_version="1.0.0",
        research_catalog_id=inputs.catalog.research_catalog_id,
        research_catalog_sha256=inputs.catalog.content_sha256,
        universe=inputs.universe.ref,
        applicability_catalog_id=inputs.applicability.applicability_catalog_id,
        applicability_catalog_sha256=inputs.applicability.content_sha256,
        source_registry_id=registry.source_registry_snapshot_id,
        source_registry_sha256=registry.source_registry_sha256,
        effective_at=BASE + timedelta(days=2),
        approved_at=BASE + timedelta(days=1),
        approved_by="product-owner:truealpha",
        approval_signature_id="signature:coverage-v1",
        approval_signature_sha256="f" * 64,
        requirements=coverage_requirements,
        entries=coverage_entries,
    )

    assert evaluate_source_capability_coverage(
        capability_catalog=capability_catalog,
        coverage_catalog=coverage_catalog,
        registry_snapshot=registry,
        rights_approvals=(rights,),
        evaluated_at=BASE + timedelta(days=5),
    ).ready
    drifted_entry = SourceCoverageEntry(
        **{
            **coverage_entries[0].model_dump(exclude={"source_coverage_entry_id", "budget_lines"}),
            "budget_lines": (
                budget_lines[0].model_copy(update={"projected_monthly_use": Decimal("21")}),
                budget_lines[1],
            ),
        }
    )
    drifted_catalog = SourceCoverageCatalog(
        **{
            **coverage_catalog.model_dump(exclude={"source_coverage_catalog_id", "entries"}),
            "entries": (drifted_entry, *coverage_entries[1:]),
        }
    )
    assert not evaluate_source_capability_coverage(
        capability_catalog=capability_catalog,
        coverage_catalog=drifted_catalog,
        registry_snapshot=registry,
        rights_approvals=(rights,),
        evaluated_at=BASE + timedelta(days=5),
    ).ready


def test_natural_refresh_transitions_require_exact_registry_versions():
    source = _registry_source()
    registry = _registry_snapshot(source)
    requirement = NaturalRefreshRequirement(
        source_class="regulatory-filings",
        source_ids=(source.source_id,),
        environment=CaptureEnvironment.STAGING,
        subject=SUBJECT,
        domain=DataDomain.FINANCIAL_FACTS,
        partition_pattern="2025-*",
        cadence=timedelta(days=120),
        maximum_age=timedelta(days=120),
        required_naturally_changed_partitions=1,
        required_publication_transitions=1,
        maximum_observation_window=timedelta(days=180),
        effective_at=BASE + timedelta(hours=1),
        approved_at=BASE,
        approved_by="product-owner:truealpha",
        owner="data-platform",
        alert_id="alert:natural-refresh",
        remediation_runbook="runbook:natural-refresh",
        approval_signature_id="signature:natural-refresh-v1",
        approval_signature_sha256="1" * 64,
    )
    transition = RefreshTransition(
        requirement_id=requirement.natural_refresh_requirement_id,
        source_id=source.source_id,
        source_version=source.version,
        subject=SUBJECT,
        domain=requirement.domain,
        partition_key=PARTITION,
        evidence_kind=RefreshEvidenceKind.NATURAL_PUBLICATION,
        previous_publication_id="publication:1",
        current_publication_id="publication:2",
        previous_content_sha256="2" * 64,
        current_content_sha256="3" * 64,
        previous_published_at=BASE + timedelta(days=10),
        current_published_at=BASE + timedelta(days=30),
        observed_at=BASE + timedelta(days=31),
    )
    report = NaturalRefreshReport(
        requirement=requirement,
        observation_started_at=BASE + timedelta(days=1),
        evaluated_at=BASE + timedelta(days=40),
        transitions=(transition,),
    )
    binding = NaturalRefreshSourceBinding(
        natural_refresh_requirement_id=requirement.natural_refresh_requirement_id,
        natural_refresh_requirement_sha256=requirement.content_sha256,
        sources=(
            NaturalRefreshSourceRef(
                source_id=source.source_id,
                source_version=source.version,
                source_registry_entry_id=source.source_registry_entry_id,
                source_registry_entry_sha256=source.content_sha256,
            ),
        ),
    )

    assert evaluate_exact_natural_refresh(report=report, source_binding=binding, registry_snapshot=registry).ready
    drifted_report = NaturalRefreshReport(
        requirement=requirement,
        observation_started_at=report.observation_started_at,
        evaluated_at=report.evaluated_at,
        transitions=(transition.model_copy(update={"source_version": "2.0.0"}),),
    )
    assert not evaluate_exact_natural_refresh(
        report=drifted_report,
        source_binding=binding,
        registry_snapshot=registry,
    ).ready
