from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError
from truealpha_contracts import CaptureEnvironment
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
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.data_quality import DataDomain
from truealpha_contracts.execution import FactorInvocationTemplate, FactorKind
from truealpha_contracts.readiness import (
    REQUIRED_SOURCE_ENVIRONMENTS,
    ApplicabilityCatalog,
    ApplicabilityCell,
    ApplicabilityClassification,
    ApplicabilityPolicy,
    BudgetDimension,
    BudgetLine,
    CatalogPolicyClosureReport,
    ConsumerSloCatalog,
    ConsumerSloReport,
    ConsumerSloRequirement,
    CoverageEvidence,
    CoverageGap,
    EvaluationStatus,
    FallbackPolicy,
    KnowabilityBasis,
    KnowabilityEvidence,
    ModuleOutcome,
    ModuleSloCatalog,
    ModuleSloObservation,
    ModuleSloReport,
    ModuleSloThreshold,
    NaturalRefreshReport,
    NaturalRefreshRequirement,
    PermissionDecision,
    RefreshEvidenceKind,
    RefreshTransition,
    RightsDecisionBasis,
    SourceCoverageCatalog,
    SourceCoverageEntry,
    SourceCoverageRequirement,
    SourceReadinessReport,
    SourceRightsApproval,
    SourceRole,
    SourceUsagePermission,
    UsageTelemetryReconciliation,
    UsageTelemetryReport,
    UsageTelemetryRequirement,
    UsageTelemetrySloCatalog,
    evaluate_catalog_policy_closure,
    evaluate_source_coverage_closure,
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
    RequirementLevel,
    UsageEmitterKind,
    UsageStage,
)

BASE = datetime(2026, 7, 1, tzinfo=UTC)
RUN = BASE + timedelta(days=2)
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SUBJECT = SubjectRef(kind=SubjectKind.ISSUER, id="company:a")
UNIVERSE = UniverseRef(
    universe_id="universe:topt",
    universe_version="2026-03-31",
    content_sha256=SHA_A,
)
DATA_REQUIREMENT = DataRequirement(
    capture_requirement_id="capture-requirement:" + "7" * 64,
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
DATA_REQUIREMENT_ID = DATA_REQUIREMENT.requirement_id
RESEARCH_ID = "research-catalog:" + SHA_B


def _semantic_type() -> SemanticTypeRegistryEntry:
    return SemanticTypeRegistryEntry(
        semantic_type_id="semantic.financial-fact",
        version="1.0.0",
        domain=DataDomain.FINANCIAL_FACTS,
        schema_version="1",
        schema_fingerprint_sha256="d" * 64,
        normalized_model_key="truealpha_contracts.models:FinancialFact",
        input_model_key="factors.types:Fact",
        repository_key="data_engine.repositories:FinancialFactRepository",
        projector_key="data_engine.projectors:FinancialFactProjector",
        compatibility_sha256="e" * 64,
        model_implementation_sha256="f" * 64,
        repository_implementation_sha256="1" * 64,
        projector_implementation_sha256="2" * 64,
    )


def _registry_source(source_id: str, source_version: str = "2026-06") -> SourceRegistryEntry:
    component = source_id.removeprefix("source.")
    return SourceRegistryEntry(
        source_id=source_id,
        version=source_version,
        adapter_id=f"adapter.{component}",
        adapter_version="1.0.0",
        normalizer_id=f"normalizer.{component}",
        normalizer_version="1.0.0",
        supported_domains=(DataDomain.FINANCIAL_FACTS,),
        supported_type_ids=("semantic.financial-fact",),
        configuration_schema_sha256="3" * 64,
        mapping_schema_sha256="4" * 64,
        adapter_implementation_sha256="5" * 64,
        normalizer_implementation_sha256="6" * 64,
    )


def _registry(*approvals: SourceRightsApproval) -> RegistrySnapshot:
    return RegistrySnapshot(
        sources=tuple(_registry_source(item.source_id, item.source_version) for item in approvals),
        semantic_types=(_semantic_type(),),
        required_type_ids=("semantic.financial-fact",),
    )


def _rights(
    source_id: str,
    *,
    expires_at: datetime = BASE + timedelta(days=365),
    denied: SourceUsagePermission | None = None,
) -> SourceRightsApproval:
    registry_entry = _registry_source(source_id)
    return SourceRightsApproval(
        source_id=source_id,
        source_version="2026-06",
        source_registry_entry_id=registry_entry.source_registry_entry_id,
        source_registry_entry_sha256=registry_entry.content_sha256,
        authorized_owner="legal-owner@example.com",
        approved_by="terms-approver@example.com",
        decision_basis=RightsDecisionBasis.AUTHORIZED_HUMAN,
        permission_decisions=tuple(
            PermissionDecision(
                permission=permission,
                permitted=permission is not denied,
                rationale="Reviewed against the provider terms.",
            )
            for permission in SourceUsagePermission
        ),
        terms_evidence_id=f"terms:{source_id}:2026-06",
        terms_evidence_sha256=SHA_B,
        approval_signature_id=f"signature:{source_id}:2026-06",
        approval_signature_sha256=SHA_C,
        approved_at=BASE,
        expires_at=expires_at,
    )


def _coverage_entry(
    rights: SourceRightsApproval,
    *,
    role: SourceRole,
    environment: CaptureEnvironment,
    gaps: tuple[CoverageGap, ...] = (),
) -> SourceCoverageEntry:
    return SourceCoverageEntry(
        environment=environment,
        data_requirement_id=DATA_REQUIREMENT_ID,
        semantic_type_id="semantic.financial-fact",
        semantic_type_version="1.0.0",
        subject=SUBJECT,
        domain=DataDomain.FINANCIAL_FACTS,
        partition_key="2025FY",
        role=role,
        priority=0 if role is SourceRole.PRIMARY else 1,
        source_id=rights.source_id,
        source_version=rights.source_version,
        source_registry_entry_id=rights.source_registry_entry_id,
        source_registry_entry_sha256=rights.source_registry_entry_sha256,
        rights_approval_id=rights.rights_approval_id,
        rights_approval_sha256=rights.content_sha256,
        identifier_level="issuer",
        capture_method="https-api",
        credential_owner="runtime-owner@example.com",
        cadence=timedelta(days=1),
        review_expires_at=BASE + timedelta(days=300),
        knowability=KnowabilityEvidence(
            rule_id="provider-publication-time",
            rule_version="1",
            basis=KnowabilityBasis.PUBLICATION_EVENT,
            evidence_id="probe:publication-time",
            evidence_sha256=SHA_A,
            observed_at=BASE,
        ),
        coverage=CoverageEvidence(
            evidence_id="probe:company-a:2025fy",
            artifact_sha256=(SHA_B,),
            observed_at=BASE,
            observed_count=2,
            earliest_knowable_at=BASE - timedelta(days=30),
            latest_knowable_at=BASE,
            natural_update_ids=("publication:1", "publication:2"),
            gaps=gaps,
        ),
        budget_lines=(
            BudgetLine(
                dimension=BudgetDimension.API_CALLS,
                unit="calls",
                approved_monthly_limit=Decimal("1000"),
                approved_annual_limit=Decimal("12000"),
                projected_monthly_use=Decimal("100"),
                projected_annual_use=Decimal("1200"),
                bounded_probe_use=Decimal("5"),
                budget_approval_id="budget:2026",
                budget_evidence_sha256=SHA_C,
                owner="budget-owner@example.com",
            ),
        ),
    )


def _source_catalog(
    primary: SourceRightsApproval,
    fallback: SourceRightsApproval,
    *,
    gaps: tuple[CoverageGap, ...] = (),
    research_catalog: ResearchCatalogManifest | None = None,
    applicability: ApplicabilityCatalog | None = None,
) -> SourceCoverageCatalog:
    registry = _registry(primary, fallback)
    applicability = applicability or _applicability(
        research_catalog_id=(research_catalog.research_catalog_id if research_catalog else RESEARCH_ID),
        research_catalog_sha256=(research_catalog.content_sha256 if research_catalog else SHA_B),
    )
    return SourceCoverageCatalog(
        catalog_version="1",
        research_catalog_id=(research_catalog.research_catalog_id if research_catalog else RESEARCH_ID),
        research_catalog_sha256=(research_catalog.content_sha256 if research_catalog else SHA_B),
        universe=(research_catalog.scope_floor.universe if research_catalog else UNIVERSE),
        applicability_catalog_id=applicability.applicability_catalog_id,
        applicability_catalog_sha256=applicability.content_sha256,
        source_registry_id=registry.source_registry_snapshot_id,
        source_registry_sha256=registry.source_registry_sha256,
        effective_at=BASE + timedelta(hours=1),
        approved_at=BASE,
        approved_by="product-owner@example.com",
        approval_signature_id="signature:source-catalog:1",
        approval_signature_sha256=SHA_C,
        requirements=tuple(
            SourceCoverageRequirement(
                environment=environment,
                data_requirement_id=DATA_REQUIREMENT_ID,
                semantic_type_id="semantic.financial-fact",
                semantic_type_version="1.0.0",
                subject=SUBJECT,
                domain=DataDomain.FINANCIAL_FACTS,
                partition_key="2025FY",
                required_permissions=frozenset(
                    {
                        SourceUsagePermission.RAW_RETENTION,
                        SourceUsagePermission.DERIVED_METRICS,
                    }
                ),
                minimum_observed_count=2,
                history_start=BASE - timedelta(days=30),
                history_end=BASE,
                minimum_natural_updates=2,
                requires_historical_knowability=True,
                fallback_policy=FallbackPolicy.REQUIRED,
            )
            for environment in REQUIRED_SOURCE_ENVIRONMENTS
        ),
        entries=tuple(
            entry
            for environment in REQUIRED_SOURCE_ENVIRONMENTS
            for entry in (
                _coverage_entry(primary, role=SourceRole.PRIMARY, environment=environment, gaps=gaps),
                _coverage_entry(fallback, role=SourceRole.FALLBACK, environment=environment),
            )
        ),
    )


def _applicability(
    *,
    effective_at: datetime = BASE + timedelta(hours=1),
    universe_hash: str = SHA_A,
    research_catalog_id: str = RESEARCH_ID,
    research_catalog_sha256: str = SHA_B,
    universe: UniverseRef | None = None,
):
    return ApplicabilityCatalog(
        catalog_version="1",
        research_catalog_id=research_catalog_id,
        research_catalog_sha256=research_catalog_sha256,
        universe=universe or UNIVERSE.model_copy(update={"content_sha256": universe_hash}),
        effective_at=effective_at,
        approved_at=BASE,
        approved_by="product-owner@example.com",
        approval_signature_id="signature:applicability:1",
        approval_signature_sha256=SHA_C,
        cells=(
            ApplicabilityCell(
                module_id="peg",
                catalog_alias="peg.revenue-growth",
                data_requirement_id=DATA_REQUIREMENT_ID,
                subject=SUBJECT,
                domain=DataDomain.FINANCIAL_FACTS,
                partition_key="2025FY",
                classification=ApplicabilityClassification.REQUIRED,
                reason="Revenue and earnings are required for the approved PEG claim.",
                effective_at=effective_at,
            ),
        ),
    )


def _product_approval(seed: str) -> ProductOwnerApproval:
    digest = seed * 64
    return ProductOwnerApproval(
        approved_by="product-owner@example.com",
        approval_record_id=f"approval-record:{digest}",
        approval_record_sha256=digest,
        approved_at=BASE,
    )


def _applicability_policy(
    applicability: ApplicabilityCatalog | None = None,
    *,
    approved_at: datetime = BASE,
) -> ApplicabilityPolicy:
    applicability = applicability or _applicability()
    return ApplicabilityPolicy(
        policy_version="1",
        module_id="peg",
        catalog_alias="peg.revenue-growth",
        universe=applicability.universe,
        effective_at=applicability.effective_at,
        approved_at=approved_at,
        approved_by="product-owner@example.com",
        approval_signature_id="signature:applicability-policy:1",
        approval_signature_sha256=SHA_C,
        cells=applicability.cells,
    )


def _module_threshold(*, approved_at: datetime = BASE, module_id: str = "peg") -> ModuleSloThreshold:
    return ModuleSloThreshold(
        module_id=module_id,
        minimum_subject_count=1,
        minimum_usable_coverage=Decimal("1"),
        maximum_unavailable_ratio=Decimal("0"),
        maximum_stale_ratio=Decimal("0"),
        maximum_unresolved_ratio=Decimal("0"),
        maximum_unclassified_ratio=Decimal("0"),
        maximum_low_confidence_ratio=Decimal("0"),
        rationale="All frozen TOPT core subjects are blocking.",
        evidence_sha256=SHA_B,
        approved_by="independent-reviewer@example.com",
        approved_at=approved_at,
        approval_signature_id="signature:peg-slo:1",
        approval_signature_sha256=SHA_C,
    )


def _research_catalog(
    applicability_policy: ApplicabilityPolicy | None = None,
    slo_policy: ModuleSloThreshold | None = None,
) -> ResearchCatalogManifest:
    applicability_policy = applicability_policy or _applicability_policy()
    slo_policy = slo_policy or _module_threshold()
    template = FactorInvocationTemplate(
        factor_id="factor.peg",
        factor_version="1.0.0",
        factor_implementation_sha256="7" * 64,
        factor_kind=FactorKind.BASE,
        parameter_model_key="catalog:InvocationParameters",
        parameter_schema_sha256="8" * 64,
        canonical_parameters_sha256=canonical_sha256({}),
        data_requirement_ids=(DATA_REQUIREMENT_ID,),
    )
    question = CanonicalQuestion(
        question_key="question.peg.revenue-growth",
        tool_kind=CatalogTargetKind.FACTOR,
        catalog_aliases=("peg.revenue-growth",),
        subject_scope=(SUBJECT,),
        requirement_level=CatalogRequirementLevel.REQUIRED,
        expected_output_type_ids=("output.peg.v1",),
        expected_statuses=(ExpectedOutputStatus.AVAILABLE,),
        prompt_examples=("Evaluate the approved revenue-growth PEG convention.",),
        approved_at=BASE,
    )
    entry = ResearchCatalogEntry(
        catalog_alias="peg.revenue-growth",
        requirement_level=CatalogRequirementLevel.REQUIRED,
        target=FactorCatalogTarget(
            factor_id=template.factor_id,
            factor_version=template.factor_version,
            definition_sha256=template.factor_implementation_sha256,
        ),
        universe=UNIVERSE,
        subject_scope=(SUBJECT,),
        invocation_template=InvocationTemplateSelector(
            target_kind=CatalogTargetKind.FACTOR,
            factor_template=template,
            frozen_at=BASE,
        ),
        applicability_policy_id=applicability_policy.applicability_policy_id,
        applicability_policy_sha256=applicability_policy.content_sha256,
        slo_policy_id=slo_policy.slo_policy_id,
        slo_policy_sha256=slo_policy.content_sha256,
        canonical_question_ids=(question.canonical_question_id,),
        expected_output_type_ids=("output.peg.v1",),
        approved_at=BASE,
    )
    floor = ResearchScopeFloor(
        universe=UNIVERSE,
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
        required_entry_ids=(entry.catalog_entry_id,),
        required_question_ids=(question.canonical_question_id,),
        approval=_product_approval("2"),
    )
    return ResearchCatalogManifest(
        catalog_version="1.0.0",
        vision_sha256="3" * 64,
        scope_floor=floor,
        entries=(entry,),
        canonical_questions=(question,),
        catalog_approval=_product_approval("4"),
        created_at=BASE + timedelta(minutes=1),
        effective_at=BASE + timedelta(hours=1),
    )


def _module_slo(
    applicability: ApplicabilityCatalog,
    *,
    thresholds: tuple[ModuleSloThreshold, ...] | None = None,
) -> ModuleSloCatalog:
    return ModuleSloCatalog(
        catalog_version="1",
        applicability_catalog_id=applicability.applicability_catalog_id,
        applicability_catalog_sha256=applicability.content_sha256,
        effective_at=BASE + timedelta(hours=1),
        approved_at=BASE,
        approved_by="quality-owner@example.com",
        approval_signature_id="signature:module-slo:1",
        approval_signature_sha256=SHA_A,
        thresholds=thresholds or (_module_threshold(),),
    )


def _policy_closure_fixture() -> tuple[
    ResearchCatalogManifest,
    ApplicabilityPolicy,
    ApplicabilityCatalog,
    ModuleSloThreshold,
    ModuleSloCatalog,
]:
    seed_applicability = _applicability()
    applicability_policy = _applicability_policy(seed_applicability)
    slo_policy = _module_threshold()
    research_catalog = _research_catalog(applicability_policy, slo_policy)
    applicability = _applicability(
        research_catalog_id=research_catalog.research_catalog_id,
        research_catalog_sha256=research_catalog.content_sha256,
        universe=research_catalog.scope_floor.universe,
    )
    return (
        research_catalog,
        applicability_policy,
        applicability,
        slo_policy,
        _module_slo(applicability, thresholds=(slo_policy,)),
    )


def _module_observation() -> ModuleSloObservation:
    return ModuleSloObservation(
        module_id="peg",
        catalog_alias="peg.revenue-growth",
        data_requirement_id=DATA_REQUIREMENT_ID,
        subject=SUBJECT,
        domain=DataDomain.FINANCIAL_FACTS,
        partition_key="2025FY",
        outcome=ModuleOutcome.USABLE,
        observed_at=RUN + timedelta(minutes=5),
        output_id="factor-output:peg:a",
        trace_complete=True,
    )


def _telemetry_catalog(applicability: ApplicabilityCatalog) -> UsageTelemetrySloCatalog:
    return UsageTelemetrySloCatalog(
        catalog_version="1",
        research_catalog_id="research-catalog:" + SHA_A,
        research_catalog_sha256=SHA_A,
        universe=UNIVERSE,
        applicability_catalog_id=applicability.applicability_catalog_id,
        applicability_catalog_sha256=applicability.content_sha256,
        registry_snapshot_id="registry-snapshot:" + SHA_C,
        registry_snapshot_sha256=SHA_C,
        effective_at=BASE + timedelta(hours=1),
        approved_at=BASE,
        approved_by="runtime-owner@example.com",
        approval_signature_id="signature:telemetry-slo:1",
        approval_signature_sha256=SHA_A,
        completeness_target=Decimal("1"),
        maximum_catalog_lag=timedelta(hours=1),
        requirements=(
            UsageTelemetryRequirement(
                data_requirement_id=DATA_REQUIREMENT_ID,
                capture_requirement_id=DATA_REQUIREMENT.capture_requirement_id,
                semantic_type_id="semantic.financial-fact",
                emitter_kind=UsageEmitterKind.INSTRUMENTED_RUNNER,
                emitter_id="factor-runner:peg",
                stage=UsageStage.FACTOR_CONSUMPTION,
                subject=SUBJECT,
                domain=DataDomain.FINANCIAL_FACTS,
                partition_key="2025FY",
                expected_window_start=RUN,
                expected_window_end=RUN + timedelta(hours=1),
                expected_minimum_events=1,
                expected_maximum_events=1,
                maximum_lag=timedelta(minutes=5),
                minimum_retention=timedelta(days=365),
                demand_evidence_id="requirement-graph:peg:a",
                demand_evidence_sha256=SHA_B,
            ),
        ),
    )


def _telemetry_event(*, lag: timedelta) -> DataUsageEvent:
    occurred_at = RUN + timedelta(minutes=30)
    return DataUsageEvent(
        operation_id="factor-run:1/peg/company:a/2025FY",
        emitter_kind=UsageEmitterKind.INSTRUMENTED_RUNNER,
        emitter_id="factor-runner:peg",
        stage=UsageStage.FACTOR_CONSUMPTION,
        requirement_id=DATA_REQUIREMENT_ID,
        capture_requirement_id=DATA_REQUIREMENT.capture_requirement_id,
        semantic_type_id="semantic.financial-fact",
        subject=SUBJECT,
        domain=DataDomain.FINANCIAL_FACTS,
        partition_key="2025FY",
        run_id="factor-run:1",
        trace_id="trace:factor-run-1",
        normalized_record_ids=("normalized-record:1",),
        evidence_ids=("runner-selection:1",),
        occurred_at=occurred_at,
        recorded_at=occurred_at + lag,
        retained_until=occurred_at + timedelta(days=366),
    )


def _refresh_requirement() -> NaturalRefreshRequirement:
    return NaturalRefreshRequirement(
        source_class="regulatory-filings",
        source_ids=("sec.submissions",),
        environment=CaptureEnvironment.STAGING,
        subject=SUBJECT,
        domain=DataDomain.FILINGS,
        partition_pattern="accession:*",
        cadence=timedelta(days=120),
        maximum_age=timedelta(days=120),
        required_naturally_changed_partitions=1,
        required_publication_transitions=1,
        maximum_observation_window=timedelta(days=180),
        effective_at=BASE + timedelta(hours=1),
        approved_at=BASE,
        approved_by="product-owner@example.com",
        owner="capture-owner@example.com",
        alert_id="alert:sec-filing-refresh",
        remediation_runbook="runbook:sec-filing-refresh",
        approval_signature_id="signature:natural-refresh:1",
        approval_signature_sha256=SHA_A,
    )


def _transition(
    requirement: NaturalRefreshRequirement,
    *,
    kind: RefreshEvidenceKind = RefreshEvidenceKind.NATURAL_PUBLICATION,
    unchanged: bool = False,
) -> RefreshTransition:
    return RefreshTransition(
        requirement_id=requirement.natural_refresh_requirement_id,
        source_id="sec.submissions",
        source_version="2026-07",
        subject=SUBJECT,
        domain=DataDomain.FILINGS,
        partition_key="accession:2",
        evidence_kind=kind,
        previous_publication_id="accession:1",
        current_publication_id="accession:1" if unchanged else "accession:2",
        previous_content_sha256=SHA_A,
        current_content_sha256=SHA_A if unchanged else SHA_B,
        previous_published_at=BASE + timedelta(days=10),
        current_published_at=BASE + timedelta(days=30),
        observed_at=BASE + timedelta(days=31),
    )


def test_source_readiness_is_content_addressed_and_passes_complete_matrix():
    primary = _rights("source.vendor-primary")
    fallback = _rights("source.vendor-fallback")
    catalog = _source_catalog(primary, fallback)
    report = SourceReadinessReport(
        catalog=catalog,
        registry_snapshot=_registry(primary, fallback),
        rights_approvals=(fallback, primary),
        evaluated_at=RUN,
    )

    assert catalog.source_coverage_catalog_id.startswith("source-coverage:")
    assert report.source_readiness_report_id.startswith("source-readiness:")
    assert report.status is EvaluationStatus.PASS
    assert report.ready
    assert not report.blockers


def test_source_coverage_permissions_have_canonical_json_order():
    primary = _rights("source.vendor-primary")
    fallback = _rights("source.vendor-fallback")
    catalog = _source_catalog(primary, fallback)
    payload = catalog.model_dump(mode="json")

    for requirement in payload["requirements"]:
        assert requirement["required_permissions"] == sorted(requirement["required_permissions"])
    assert SourceCoverageCatalog.model_validate(payload) == catalog


def test_source_readiness_fails_missing_expired_or_denied_rights_and_has_no_override():
    primary = _rights("source.vendor-primary", denied=SourceUsagePermission.RAW_RETENTION)
    fallback = _rights("source.vendor-fallback", expires_at=BASE + timedelta(days=1))
    catalog = _source_catalog(primary, fallback)
    registry = _registry(primary, fallback)
    report = SourceReadinessReport(
        catalog=catalog,
        registry_snapshot=registry,
        rights_approvals=(primary, fallback),
        evaluated_at=RUN,
    )

    assert report.status is EvaluationStatus.FAIL
    assert any("required permissions denied" in blocker for blocker in report.blockers)
    assert any("rights approval expired" in blocker for blocker in report.blockers)
    missing = SourceReadinessReport(
        catalog=catalog,
        registry_snapshot=registry,
        rights_approvals=(),
        evaluated_at=RUN,
    )
    assert any("rights approval is missing" in blocker for blocker in missing.blockers)
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        SourceReadinessReport(
            catalog=catalog,
            registry_snapshot=registry,
            rights_approvals=(primary, fallback),
            evaluated_at=RUN,
            status=EvaluationStatus.PASS,
        )


def test_source_readiness_fails_longitudinal_gap():
    primary = _rights("source.vendor-primary")
    fallback = _rights("source.vendor-fallback")
    gap = CoverageGap(gap_id="2025Q3", detail="No public snapshot was found.", evidence_sha256=SHA_A)
    report = SourceReadinessReport(
        catalog=_source_catalog(primary, fallback, gaps=(gap,)),
        registry_snapshot=_registry(primary, fallback),
        rights_approvals=(primary, fallback),
        evaluated_at=RUN,
    )
    assert report.status is EvaluationStatus.FAIL
    assert any("coverage contains gaps" in blocker for blocker in report.blockers)


def test_source_coverage_closure_is_derived_from_catalog_applicability_and_all_environments():
    research_catalog = _research_catalog()
    applicability = _applicability(
        research_catalog_id=research_catalog.research_catalog_id,
        research_catalog_sha256=research_catalog.content_sha256,
        universe=research_catalog.scope_floor.universe,
    )
    primary = _rights("source.vendor-primary")
    fallback = _rights("source.vendor-fallback")
    source_catalog = _source_catalog(
        primary,
        fallback,
        research_catalog=research_catalog,
        applicability=applicability,
    )

    report = evaluate_source_coverage_closure(
        source_catalog,
        research_catalog,
        applicability,
        (DATA_REQUIREMENT,),
        evaluated_at=RUN,
    )

    assert report.ready
    assert not report.blocking_reason_codes
    assert {item.environment for item in source_catalog.requirements} == REQUIRED_SOURCE_ENVIRONMENTS

    expanded = ApplicabilityCatalog(
        **applicability.model_dump(exclude={"applicability_catalog_id", "cells"}),
        cells=(
            *applicability.cells,
            applicability.cells[0].model_copy(
                update={"subject": SubjectRef(kind=SubjectKind.ISSUER, id="company:outside-catalog")}
            ),
        ),
    )
    shrunken_matrix = _source_catalog(
        primary,
        fallback,
        research_catalog=research_catalog,
        applicability=expanded,
    )
    failed = evaluate_source_coverage_closure(
        shrunken_matrix,
        research_catalog,
        expanded,
        (DATA_REQUIREMENT,),
        evaluated_at=RUN,
    )
    assert not failed.ready
    assert any(code.startswith("matrix.missing:") for code in failed.blocking_reason_codes)
    assert any("subject_outside_catalog_entry" in code for code in failed.blocking_reason_codes)


def test_source_ids_are_open_but_validated_and_rights_resolve_every_permission():
    assert _rights("source.new-provider-feed-v3").source_id == "source.new-provider-feed-v3"
    with pytest.raises(ValidationError):
        _rights("Not Valid")
    with pytest.raises(ValidationError, match="every usage permission"):
        SourceRightsApproval(
            source_id="source.vendor-primary",
            source_version="1",
            source_registry_entry_id="source-registry-entry:" + SHA_A,
            source_registry_entry_sha256=SHA_A,
            authorized_owner="owner",
            approved_by="approver",
            decision_basis=RightsDecisionBasis.AUTHORIZED_HUMAN,
            permission_decisions=(
                PermissionDecision(
                    permission=SourceUsagePermission.RAW_RETENTION,
                    permitted=True,
                    rationale="Approved.",
                ),
            ),
            terms_evidence_id="terms:1",
            terms_evidence_sha256=SHA_B,
            approval_signature_id="signature:rights:1",
            approval_signature_sha256=SHA_C,
            approved_at=BASE,
            expires_at=BASE + timedelta(days=1),
        )


def test_catalog_policy_closure_resolves_exact_pre_catalog_policies():
    research, policy, applicability, threshold, slo_catalog = _policy_closure_fixture()

    report = evaluate_catalog_policy_closure(
        research,
        (policy,),
        applicability,
        slo_catalog,
        evaluated_at=RUN,
    )

    assert isinstance(report, CatalogPolicyClosureReport)
    assert report.ready
    assert report.blocking_reason_codes == ()
    assert policy.applicability_policy_id == f"applicability-policy:{policy.content_sha256}"
    assert threshold.slo_policy_id == f"slo-policy:{threshold.content_sha256}"
    assert report.catalog_policy_closure_report_id.startswith("catalog-policy-closure:")
    assert ApplicabilityPolicy.model_validate_json(policy.model_dump_json()) == policy
    assert ModuleSloThreshold.model_validate_json(threshold.model_dump_json()) == threshold
    assert CatalogPolicyClosureReport.model_validate_json(report.model_dump_json(exclude={"ready"})) == report
    assert "research_catalog_id" not in ApplicabilityPolicy.model_fields
    assert "research_catalog_id" not in ModuleSloThreshold.model_fields

    with pytest.raises(ValidationError, match="applicability_policy_id does not match"):
        ApplicabilityPolicy(
            **policy.model_dump(exclude={"applicability_policy_id"}),
            applicability_policy_id="applicability-policy:" + SHA_A,
        )
    with pytest.raises(ValidationError, match="slo_policy_id does not match"):
        ModuleSloThreshold(
            **threshold.model_dump(exclude={"slo_policy_id"}),
            slo_policy_id="slo-policy:" + SHA_A,
        )


def test_catalog_policy_closure_fails_missing_and_extra_policy_objects():
    research, policy, applicability, threshold, slo_catalog = _policy_closure_fixture()
    missing = evaluate_catalog_policy_closure(
        research,
        (),
        applicability,
        slo_catalog,
        evaluated_at=RUN,
    )
    assert not missing.ready
    assert "applicability.missing_policy:peg.revenue-growth" in missing.blocking_reason_codes
    assert any(code.startswith("applicability.missing_policy_cell:") for code in missing.blocking_reason_codes)

    extra_policy = ApplicabilityPolicy(
        **policy.model_dump(exclude={"applicability_policy_id", "policy_version"}),
        policy_version="2",
    )
    extra_threshold = _module_threshold(module_id="peg-secondary")
    expanded_slo = _module_slo(
        applicability,
        thresholds=(threshold, extra_threshold),
    )
    extra = evaluate_catalog_policy_closure(
        research,
        (policy, extra_policy),
        applicability,
        expanded_slo,
        evaluated_at=RUN,
    )
    assert not extra.ready
    assert f"applicability.extra_policy:{extra_policy.applicability_policy_id}" in extra.blocking_reason_codes
    assert f"slo.extra_policy:{extra_threshold.slo_policy_id}" in extra.blocking_reason_codes
    assert any(code.startswith("applicability.duplicate_policy_cell:") for code in extra.blocking_reason_codes)


def test_catalog_policy_closure_fails_content_and_concrete_cell_drift():
    research, policy, applicability, threshold, slo_catalog = _policy_closure_fixture()
    drifted_policy = policy.model_copy(update={"approved_by": "unapproved-editor@example.com"})
    drifted_threshold = threshold.model_copy(update={"rationale": "Changed after approval."})
    drifted_slo = slo_catalog.model_copy(update={"thresholds": (drifted_threshold,)})
    drift = evaluate_catalog_policy_closure(
        research,
        (drifted_policy,),
        applicability,
        drifted_slo,
        evaluated_at=RUN,
    )
    assert not drift.ready
    assert any(code.startswith("applicability.policy_content_drift:") for code in drift.blocking_reason_codes)
    assert "applicability.policy_ref_drift:peg.revenue-growth" in drift.blocking_reason_codes
    assert any(code.startswith("slo.policy_content_drift:") for code in drift.blocking_reason_codes)
    assert "slo.policy_ref_drift:peg.revenue-growth" in drift.blocking_reason_codes

    changed_cell = applicability.cells[0].model_copy(update={"reason": "Changed after policy freeze."})
    changed_applicability = ApplicabilityCatalog(
        **applicability.model_dump(exclude={"applicability_catalog_id", "cells"}),
        cells=(changed_cell,),
    )
    changed_slo = _module_slo(changed_applicability, thresholds=(threshold,))
    changed = evaluate_catalog_policy_closure(
        research,
        (policy,),
        changed_applicability,
        changed_slo,
        evaluated_at=RUN,
    )
    assert not changed.ready
    assert any(code.startswith("applicability.policy_cell_drift:") for code in changed.blocking_reason_codes)


def test_catalog_policy_closure_fails_missing_slo_and_postdated_policies():
    research, policy, applicability, threshold, _ = _policy_closure_fixture()
    unrelated_threshold = _module_threshold(module_id="unrelated")
    missing_slo_catalog = _module_slo(applicability, thresholds=(unrelated_threshold,))
    missing = evaluate_catalog_policy_closure(
        research,
        (policy,),
        applicability,
        missing_slo_catalog,
        evaluated_at=RUN,
    )
    assert "slo.missing_policy:peg.revenue-growth" in missing.blocking_reason_codes
    assert f"slo.extra_policy:{unrelated_threshold.slo_policy_id}" in missing.blocking_reason_codes

    late_policy = _applicability_policy(approved_at=BASE + timedelta(minutes=30))
    late_threshold = _module_threshold(approved_at=BASE + timedelta(minutes=30))
    late_research = _research_catalog(late_policy, late_threshold)
    late_applicability = _applicability(
        research_catalog_id=late_research.research_catalog_id,
        research_catalog_sha256=late_research.content_sha256,
        universe=late_research.scope_floor.universe,
    )
    late = evaluate_catalog_policy_closure(
        late_research,
        (late_policy,),
        late_applicability,
        _module_slo(late_applicability, thresholds=(late_threshold,)),
        evaluated_at=RUN,
    )
    assert "time.applicability_policy_postdated:peg.revenue-growth" in late.blocking_reason_codes
    assert "time.slo_policy_postdated:peg.revenue-growth" in late.blocking_reason_codes


def test_module_slo_joins_pre_run_applicability_and_rejects_missing_or_duplicate_cells():
    applicability = _applicability()
    slo = _module_slo(applicability)

    missing = ModuleSloReport(
        applicability=applicability,
        slo_catalog=slo,
        run_started_at=RUN,
        evaluated_at=RUN + timedelta(hours=1),
        observations=(),
    )
    assert missing.status is EvaluationStatus.FAIL
    assert any("observation is missing" in blocker for blocker in missing.blockers)

    observation = _module_observation()
    duplicate = ModuleSloReport(
        applicability=applicability,
        slo_catalog=slo,
        run_started_at=RUN,
        evaluated_at=RUN + timedelta(hours=1),
        observations=(observation, observation),
    )
    assert any("duplicate SLO observations" in blocker for blocker in duplicate.blockers)


def test_catalog_alias_is_part_of_the_applicability_denominator():
    first = _applicability()
    applicability = ApplicabilityCatalog(
        catalog_version=first.catalog_version,
        research_catalog_id=first.research_catalog_id,
        research_catalog_sha256=first.research_catalog_sha256,
        universe=first.universe,
        effective_at=first.effective_at,
        approved_at=first.approved_at,
        approved_by=first.approved_by,
        approval_signature_id=first.approval_signature_id,
        approval_signature_sha256=first.approval_signature_sha256,
        cells=(
            *first.cells,
            ApplicabilityCell(
                module_id="peg",
                catalog_alias="peg.forward-earnings",
                data_requirement_id="data-requirement:" + "e" * 64,
                subject=SUBJECT,
                domain=DataDomain.FORECASTS,
                partition_key="2026FY",
                classification=ApplicabilityClassification.REQUIRED,
                reason="Forward earnings is a separate canonical Catalog input.",
                effective_at=first.effective_at,
            ),
        ),
    )
    report = ModuleSloReport(
        applicability=applicability,
        slo_catalog=_module_slo(applicability),
        run_started_at=RUN,
        evaluated_at=RUN + timedelta(hours=1),
        observations=(_module_observation(),),
    )

    assert report.status is EvaluationStatus.FAIL
    assert any("peg.forward-earnings" in blocker and "missing" in blocker for blocker in report.blockers)


def test_module_slo_rejects_postdated_and_mismatched_applicability():
    approved = _applicability()
    slo = _module_slo(approved)
    postdated = _applicability(effective_at=RUN + timedelta(hours=1))
    report = ModuleSloReport(
        applicability=postdated,
        slo_catalog=slo,
        run_started_at=RUN,
        evaluated_at=RUN + timedelta(hours=2),
        observations=(_module_observation(),),
    )
    assert report.status is EvaluationStatus.FAIL
    assert any("different applicability" in blocker for blocker in report.blockers)
    assert any("postdated applicability" in blocker for blocker in report.blockers)


def test_usage_telemetry_treats_absent_and_late_events_as_failures():
    catalog = _telemetry_catalog(_applicability())
    evaluated_at = RUN + timedelta(hours=2)
    absent = UsageTelemetryReport(
        catalog=catalog,
        evaluated_at=evaluated_at,
        events=(),
        reconciliations=(
            UsageTelemetryReconciliation(
                telemetry_requirement_id=catalog.requirements[0].telemetry_requirement_id,
                source_event_count=0,
                reconciled_at=evaluated_at,
                evidence_sha256=SHA_C,
            ),
        ),
    )
    assert absent.status is EvaluationStatus.FAIL
    assert any("absent or incomplete" in blocker for blocker in absent.blockers)

    late = UsageTelemetryReport(
        catalog=catalog,
        evaluated_at=evaluated_at,
        events=(_telemetry_event(lag=timedelta(minutes=6)),),
        reconciliations=(
            UsageTelemetryReconciliation(
                telemetry_requirement_id=catalog.requirements[0].telemetry_requirement_id,
                source_event_count=1,
                reconciled_at=evaluated_at,
                evidence_sha256=SHA_C,
            ),
        ),
    )
    assert late.status is EvaluationStatus.FAIL
    assert any("arrived late" in blocker for blocker in late.blockers)


def test_usage_telemetry_accepts_attributed_timely_reconciled_event():
    catalog = _telemetry_catalog(_applicability())
    evaluated_at = RUN + timedelta(hours=2)
    report = UsageTelemetryReport(
        catalog=catalog,
        evaluated_at=evaluated_at,
        events=(_telemetry_event(lag=timedelta(minutes=1)),),
        reconciliations=(
            UsageTelemetryReconciliation(
                telemetry_requirement_id=catalog.requirements[0].telemetry_requirement_id,
                source_event_count=1,
                reconciled_at=evaluated_at,
                evidence_sha256=SHA_C,
            ),
        ),
    )
    assert report.status is EvaluationStatus.PASS
    assert not report.blockers


def test_usage_telemetry_rejects_a_different_capture_requirement() -> None:
    catalog = _telemetry_catalog(_applicability())
    event = _telemetry_event(lag=timedelta(minutes=1))
    wrong_capture_event = DataUsageEvent(
        **event.model_dump(
            exclude={
                "usage_event_id",
                "content_sha256",
                "planned_cell_id",
                "capture_requirement_id",
            }
        ),
        capture_requirement_id="capture-requirement:" + "f" * 64,
    )
    evaluated_at = RUN + timedelta(hours=2)
    report = UsageTelemetryReport(
        catalog=catalog,
        evaluated_at=evaluated_at,
        events=(wrong_capture_event,),
        reconciliations=(
            UsageTelemetryReconciliation(
                telemetry_requirement_id=catalog.requirements[0].telemetry_requirement_id,
                source_event_count=1,
                reconciled_at=evaluated_at,
                evidence_sha256=SHA_C,
            ),
        ),
    )

    assert report.status is EvaluationStatus.FAIL
    assert any("outside the catalog" in blocker for blocker in report.blockers)


@pytest.mark.parametrize(
    ("kind", "unchanged", "message"),
    [
        (RefreshEvidenceKind.SYNTHETIC_MUTATION, False, "cannot satisfy natural refresh"),
        (RefreshEvidenceKind.NATURAL_PUBLICATION, True, "unchanged publication"),
    ],
)
def test_natural_refresh_rejects_synthetic_and_unchanged_evidence(kind, unchanged, message):
    requirement = _refresh_requirement()
    report = NaturalRefreshReport(
        requirement=requirement,
        observation_started_at=BASE + timedelta(days=1),
        evaluated_at=BASE + timedelta(days=40),
        transitions=(_transition(requirement, kind=kind, unchanged=unchanged),),
    )
    assert report.status is EvaluationStatus.FAIL
    assert any(message in blocker for blocker in report.blockers)


def test_natural_refresh_accepts_a_changed_publication_transition():
    requirement = _refresh_requirement()
    report = NaturalRefreshReport(
        requirement=requirement,
        observation_started_at=BASE + timedelta(days=1),
        evaluated_at=BASE + timedelta(days=40),
        transitions=(_transition(requirement),),
    )
    assert report.status is EvaluationStatus.PASS
    assert not report.blockers


def test_consumer_slo_catalog_is_content_addressed_and_missing_observation_fails():
    applicability = _applicability()
    catalog = ConsumerSloCatalog(
        catalog_version="1",
        applicability_catalog_id=applicability.applicability_catalog_id,
        applicability_catalog_sha256=applicability.content_sha256,
        effective_at=BASE + timedelta(hours=1),
        approved_at=BASE,
        approved_by="runtime-owner@example.com",
        approval_signature_id="signature:consumer-slo:1",
        approval_signature_sha256=SHA_A,
        requirements=(
            ConsumerSloRequirement(
                consumer_id="mcp",
                endpoint_id="factor-card",
                minimum_availability=Decimal("0.99"),
                maximum_latency_ms=500,
                maximum_row_count=100,
                require_authenticated=True,
                require_trace_complete=True,
                maximum_permission_failure_ratio=Decimal("0"),
                error_budget_ratio=Decimal("0.01"),
                owner="mcp-owner@example.com",
                remediation_runbook="runbook:mcp",
            ),
        ),
    )
    report = ConsumerSloReport(catalog=catalog, evaluated_at=RUN, observations=())
    assert catalog.consumer_slo_catalog_id.startswith("consumer-slo:")
    assert report.status is EvaluationStatus.FAIL
    assert any("observation is missing" in blocker for blocker in report.blockers)
