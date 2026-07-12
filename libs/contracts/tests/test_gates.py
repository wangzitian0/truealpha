from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError
from truealpha_contracts.capture_contracts import CaptureEvaluationReport, CaptureRequirement, CaptureScope
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
from truealpha_contracts.gates import (
    BudgetHorizon,
    BudgetUndercountExplanation,
    BudgetUsageObservation,
    ComparisonCriterion,
    ComparisonObservation,
    FullCatalogBudgetReport,
    GraduationApproval,
    GraduationApprovalRole,
    GraduationAttestation,
    ProductionGraduationEvidence,
    ProductionGraduationPlan,
    ProductionGraduationReport,
    ProductionRecheckSchedule,
    RegistryCallState,
    RollbackPlan,
    ScheduledOperationalRecheck,
    SourceCallIntent,
    SourceCallPreflightReport,
    SourceRegistryOperationalState,
    StagedBatchEvidence,
    StagedBatchRequirement,
    resolve_graduation_attestation,
)
from truealpha_contracts.readiness import (
    ApplicabilityCatalog,
    ApplicabilityCell,
    ApplicabilityClassification,
    BudgetDimension,
    BudgetLine,
    ConsumerSloCatalog,
    ConsumerSloObservation,
    ConsumerSloReport,
    ConsumerSloRequirement,
    CoverageEvidence,
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
)
from truealpha_contracts.registries import RegistrySnapshot, SemanticTypeRegistryEntry, SourceRegistryEntry
from truealpha_contracts.release import ArtifactRole, ReleaseArtifact, ReleaseManifest
from truealpha_contracts.universe import (
    SubjectKind,
    SubjectRef,
    UniverseDefinitionKind,
    UniverseManifest,
    UniverseMembership,
    UniverseRef,
)
from truealpha_contracts.usage import DataUsageEvent, UsageEmitterKind, UsageStage

NOW = datetime(2026, 7, 12, 12, tzinfo=UTC)
SUBJECT = SubjectRef(kind=SubjectKind.ISSUER, id="issuer:alpha")
UNIVERSE = UniverseRef(
    universe_id="universe:topt-baseline",
    universe_version="2026.07.12",
    content_sha256="a" * 64,
)
RESEARCH_SHA = "b" * 64
RESEARCH_ID = "research-catalog:" + RESEARCH_SHA
DATA_REQUIREMENT_ID = "data-requirement:" + "d" * 64
APPLICABILITY_SHA = "e" * 64
APPLICABILITY_ID = "applicability:" + APPLICABILITY_SHA
SOURCE_ENVIRONMENTS = (
    CaptureEnvironment.LOCAL_DEV,
    CaptureEnvironment.LOCAL_TEST,
    CaptureEnvironment.GITHUB_CI,
    CaptureEnvironment.STAGING,
    CaptureEnvironment.PRODUCTION,
)


def _sha(seed: str) -> str:
    return canonical_sha256(seed)


def _registry() -> RegistrySnapshot:
    semantic = SemanticTypeRegistryEntry(
        semantic_type_id="semantic.filing",
        version="1.0.0",
        domain=DataDomain.FILINGS,
        schema_version="1.0.0",
        schema_fingerprint_sha256=_sha("schema"),
        normalized_model_key="FilingRecord",
        input_model_key="FilingInput",
        repository_key="FilingRepository",
        projector_key="FilingProjector",
        compatibility_sha256=_sha("compatibility"),
        model_implementation_sha256=_sha("model"),
        repository_implementation_sha256=_sha("repository"),
        projector_implementation_sha256=_sha("projector"),
    )
    sources = tuple(
        SourceRegistryEntry(
            source_id=source_id,
            version="1.0.0",
            adapter_id=f"adapter.{suffix}",
            adapter_version="1.0.0",
            normalizer_id=f"normalizer.{suffix}",
            normalizer_version="1.0.0",
            supported_domains=(DataDomain.FILINGS,),
            supported_type_ids=("semantic.filing",),
            configuration_schema_sha256=_sha(f"config-{suffix}"),
            mapping_schema_sha256=_sha(f"mapping-{suffix}"),
            adapter_implementation_sha256=_sha(f"adapter-{suffix}"),
            normalizer_implementation_sha256=_sha(f"normalizer-{suffix}"),
        )
        for source_id, suffix in (("source.sec", "sec"), ("source.sec-fallback", "fallback"))
    )
    return RegistrySnapshot(
        sources=sources,
        semantic_types=(semantic,),
        required_type_ids=("semantic.filing",),
    )


def _rights(entry: SourceRegistryEntry, *, expires_at: datetime = NOW + timedelta(days=30)) -> SourceRightsApproval:
    return SourceRightsApproval(
        source_id=entry.source_id,
        source_version=entry.version,
        source_registry_entry_id=entry.source_registry_entry_id,
        source_registry_entry_sha256=entry.content_sha256,
        authorized_owner="legal-owner",
        approved_by="legal-counsel",
        decision_basis=RightsDecisionBasis.LEGAL_COUNSEL,
        permission_decisions=tuple(
            PermissionDecision(
                permission=permission,
                permitted=True,
                rationale="Approved for the frozen product scope.",
            )
            for permission in SourceUsagePermission
        ),
        terms_evidence_id=f"terms:{entry.source_id}",
        terms_evidence_sha256=_sha(f"terms-{entry.source_id}"),
        approval_signature_id="signature:rights",
        approval_signature_sha256=_sha(f"signature-{entry.source_id}"),
        approved_at=NOW - timedelta(days=2),
        expires_at=expires_at,
    )


def _budget_lines(*, projected: Decimal = Decimal("10")) -> tuple[BudgetLine, ...]:
    return tuple(
        BudgetLine(
            dimension=dimension,
            unit=f"{dimension.value}-unit",
            approved_monthly_limit=Decimal("20"),
            approved_annual_limit=Decimal("200"),
            projected_monthly_use=projected,
            projected_annual_use=projected * Decimal("10"),
            bounded_probe_use=Decimal("1"),
            budget_approval_id=f"budget:{dimension.value}",
            budget_evidence_sha256=_sha(f"budget-{dimension.value}"),
            owner="finance-owner",
        )
        for dimension in BudgetDimension
    )


def _coverage_entry(
    registry_entry: SourceRegistryEntry,
    approval: SourceRightsApproval,
    *,
    role: SourceRole,
    priority: int,
    environment: CaptureEnvironment = CaptureEnvironment.PRODUCTION,
    review_expires_at: datetime = NOW + timedelta(days=30),
) -> SourceCoverageEntry:
    return SourceCoverageEntry(
        environment=environment,
        data_requirement_id=DATA_REQUIREMENT_ID,
        semantic_type_id="semantic.filing",
        semantic_type_version="1.0.0",
        subject=SUBJECT,
        domain=DataDomain.FILINGS,
        partition_key="2026Q2",
        role=role,
        priority=priority,
        source_id=registry_entry.source_id,
        source_version=registry_entry.version,
        source_registry_entry_id=registry_entry.source_registry_entry_id,
        source_registry_entry_sha256=registry_entry.content_sha256,
        rights_approval_id=approval.rights_approval_id,
        rights_approval_sha256=approval.content_sha256,
        identifier_level="issuer",
        capture_method="https",
        credential_owner="secret-owner",
        cadence=timedelta(days=1),
        review_expires_at=review_expires_at,
        knowability=KnowabilityEvidence(
            rule_id="publication-time",
            rule_version="1.0.0",
            basis=KnowabilityBasis.PUBLICATION_EVENT,
            evidence_id="knowability:sec",
            evidence_sha256=_sha(f"knowability-{registry_entry.source_id}"),
            observed_at=NOW - timedelta(days=1),
        ),
        coverage=CoverageEvidence(
            evidence_id="coverage:bounded-probe",
            artifact_sha256=(_sha(f"artifact-{registry_entry.source_id}"),),
            observed_at=NOW - timedelta(days=1),
            observed_count=1,
            earliest_knowable_at=NOW - timedelta(days=10),
            latest_knowable_at=NOW - timedelta(days=1),
            natural_update_ids=(f"publication:{registry_entry.source_id}",),
        ),
        budget_lines=_budget_lines(),
    )


def _source_bundle(
    *,
    evaluated_at: datetime = NOW,
    rights_expiry: datetime = NOW + timedelta(days=30),
    review_expiry: datetime = NOW + timedelta(days=30),
    universe: UniverseRef = UNIVERSE,
    research_catalog_id: str = RESEARCH_ID,
    research_catalog_sha256: str = RESEARCH_SHA,
    applicability_catalog_id: str = APPLICABILITY_ID,
    applicability_catalog_sha256: str = APPLICABILITY_SHA,
) -> tuple[
    RegistrySnapshot,
    SourceReadinessReport,
    tuple[SourceRegistryOperationalState, ...],
]:
    registry = _registry()
    approvals = tuple(_rights(entry, expires_at=rights_expiry) for entry in registry.sources)
    entries = tuple(
        entry
        for environment in SOURCE_ENVIRONMENTS
        for entry in (
            _coverage_entry(
                registry.sources[0],
                approvals[0],
                role=SourceRole.PRIMARY,
                priority=0,
                environment=environment,
                review_expires_at=review_expiry,
            ),
            _coverage_entry(
                registry.sources[1],
                approvals[1],
                role=SourceRole.FALLBACK,
                priority=1,
                environment=environment,
                review_expires_at=review_expiry,
            ),
        )
    )
    requirements = tuple(
        SourceCoverageRequirement(
            environment=environment,
            data_requirement_id=DATA_REQUIREMENT_ID,
            semantic_type_id="semantic.filing",
            semantic_type_version="1.0.0",
            subject=SUBJECT,
            domain=DataDomain.FILINGS,
            partition_key="2026Q2",
            required_permissions=frozenset(
                {
                    SourceUsagePermission.RAW_RETENTION,
                    SourceUsagePermission.PUBLIC_REPORTS,
                }
            ),
            minimum_observed_count=1,
            minimum_natural_updates=1,
            fallback_policy=FallbackPolicy.REQUIRED,
        )
        for environment in SOURCE_ENVIRONMENTS
    )
    catalog = SourceCoverageCatalog(
        catalog_version="1.0.0",
        research_catalog_id=research_catalog_id,
        research_catalog_sha256=research_catalog_sha256,
        universe=universe,
        applicability_catalog_id=applicability_catalog_id,
        applicability_catalog_sha256=applicability_catalog_sha256,
        source_registry_id=registry.source_registry_snapshot_id,
        source_registry_sha256=registry.source_registry_sha256,
        effective_at=NOW - timedelta(days=1),
        approved_at=NOW - timedelta(days=2),
        approved_by="product-owner",
        approval_signature_id="signature:source-catalog",
        approval_signature_sha256=_sha("source-catalog-signature"),
        requirements=requirements,
        entries=entries,
    )
    readiness = SourceReadinessReport(
        catalog=catalog,
        registry_snapshot=registry,
        rights_approvals=approvals,
        evaluated_at=evaluated_at,
    )
    states = tuple(
        SourceRegistryOperationalState(
            registry_snapshot_id=registry.registry_snapshot_id,
            registry_snapshot_sha256=registry.content_sha256,
            source_registry_entry_id=entry.source_registry_entry_id,
            source_registry_entry_sha256=entry.content_sha256,
            call_state=RegistryCallState.ENABLED,
            effective_at=NOW - timedelta(days=1),
            recorded_by="runtime-owner",
            decision_evidence_id=f"activation:{entry.source_id}",
            decision_evidence_sha256=_sha(f"activation-{entry.source_id}"),
        )
        for entry in registry.sources
    )
    return registry, readiness, states


def _intent(
    registry: RegistrySnapshot,
    readiness: SourceReadinessReport,
    *,
    intended_at: datetime = NOW + timedelta(minutes=1),
) -> SourceCallIntent:
    primary = next(
        entry
        for entry in readiness.catalog.entries
        if entry.role is SourceRole.PRIMARY and entry.environment is CaptureEnvironment.PRODUCTION
    )
    registry_entry = next(
        entry for entry in registry.sources if entry.source_registry_entry_id == primary.source_registry_entry_id
    )
    return SourceCallIntent(
        operation_id="capture:production:2026q2",
        registry_snapshot_id=registry.registry_snapshot_id,
        registry_snapshot_sha256=registry.content_sha256,
        source_id=registry_entry.source_id,
        source_version=registry_entry.version,
        source_registry_entry_id=registry_entry.source_registry_entry_id,
        source_registry_entry_sha256=registry_entry.content_sha256,
        source_coverage_entry_id=primary.source_coverage_entry_id,
        environment=primary.environment,
        subject=primary.subject,
        domain=primary.domain,
        partition_key=primary.partition_key,
        required_permissions=frozenset(
            {
                SourceUsagePermission.RAW_RETENTION,
                SourceUsagePermission.PUBLIC_REPORTS,
            }
        ),
        intended_call_at=intended_at,
        maximum_preflight_age=timedelta(minutes=5),
    )


def _budget_report(
    catalog: SourceCoverageCatalog,
    *,
    evaluated_at: datetime = NOW,
    actual_overrides: dict[tuple[str, BudgetDimension], tuple[Decimal, Decimal]] | None = None,
    explanations: tuple[BudgetUndercountExplanation, ...] = (),
) -> FullCatalogBudgetReport:
    window_start = evaluated_at - timedelta(days=30)
    observations = []
    for entry in catalog.entries:
        for line in entry.budget_lines:
            metered, reconciled = (actual_overrides or {}).get(
                (entry.source_coverage_entry_id, line.dimension),
                (line.projected_monthly_use, line.projected_monthly_use),
            )
            observations.append(
                BudgetUsageObservation(
                    source_coverage_entry_id=entry.source_coverage_entry_id,
                    dimension=line.dimension,
                    unit=line.unit,
                    metered_use=metered,
                    independently_reconciled_use=reconciled,
                    window_started_at=window_start,
                    window_completed_at=evaluated_at,
                    observed_at=evaluated_at,
                    telemetry_evidence_id=f"ledger:{entry.source_id}:{line.dimension.value}",
                    telemetry_evidence_sha256=_sha(
                        f"ledger-{entry.source_id}-{line.dimension.value}-{evaluated_at.isoformat()}"
                    ),
                )
            )
    return FullCatalogBudgetReport(
        catalog=catalog,
        horizon=BudgetHorizon.MONTHLY,
        window_started_at=window_start,
        window_completed_at=evaluated_at,
        evaluated_at=evaluated_at,
        observations=tuple(observations),
        undercount_explanations=explanations,
    )


def _schedule(
    registry: RegistrySnapshot,
    readiness: SourceReadinessReport,
    states: tuple[SourceRegistryOperationalState, ...],
    *,
    cadence: timedelta = timedelta(days=1),
    release_sha: str = "c" * 64,
) -> ProductionRecheckSchedule:
    return ProductionRecheckSchedule(
        research_catalog_id=readiness.catalog.research_catalog_id,
        research_catalog_sha256=readiness.catalog.research_catalog_sha256,
        universe=readiness.catalog.universe,
        source_coverage_catalog_id=readiness.catalog.source_coverage_catalog_id,
        source_coverage_catalog_sha256=readiness.catalog.content_sha256,
        registry_snapshot_id=registry.registry_snapshot_id,
        registry_snapshot_sha256=registry.content_sha256,
        release_manifest_id="release-manifest:" + release_sha,
        release_manifest_sha256=release_sha,
        operational_state_ids=tuple(item.operational_state_id for item in states),
        cadence=cadence,
        maximum_lag=timedelta(minutes=10),
        effective_at=NOW - timedelta(days=1),
        approved_at=NOW - timedelta(days=2),
        approved_by="product-owner",
        owner="runtime-owner",
        alert_id="alert:source-recheck",
        remediation_runbook="runbook://source-recheck",
        approval_signature_id="signature:recheck",
        approval_signature_sha256=_sha("recheck-signature"),
    )


def test_preflight_requires_exact_ready_enabled_registry_entry_before_call() -> None:
    registry, readiness, states = _source_bundle()
    intent = _intent(registry, readiness)
    report = SourceCallPreflightReport(
        intent=intent,
        registry_snapshot=registry,
        source_readiness=readiness,
        operational_states=states,
        evaluated_at=NOW,
    )
    assert report.allowed
    assert report.blockers == ()
    assert report.preflight_report_id.startswith("source-call-preflight:")

    selected_state = next(
        state for state in states if state.source_registry_entry_id == intent.source_registry_entry_id
    )
    disabled = SourceRegistryOperationalState(
        **selected_state.model_dump(
            exclude={
                "operational_state_id",
                "content_sha256",
                "callable",
                "call_state",
                "disabled_reason",
            }
        ),
        call_state=RegistryCallState.DISABLED,
        disabled_reason="Provider disabled pending review.",
    )
    disabled_report = SourceCallPreflightReport(
        intent=intent,
        registry_snapshot=registry,
        source_readiness=readiness,
        operational_states=tuple(disabled if item is selected_state else item for item in states),
        evaluated_at=NOW,
    )
    assert not disabled_report.allowed
    assert "operational_state.selected_entry_disabled" in disabled_report.blockers
    assert any(entry.source_registry_entry_id == disabled.source_registry_entry_id for entry in registry.sources), (
        "disabling a source must not remove historical registry content"
    )

    missing_report = SourceCallPreflightReport(
        intent=intent,
        registry_snapshot=registry,
        source_readiness=readiness,
        operational_states=(states[1],),
        evaluated_at=NOW,
    )
    assert not missing_report.allowed
    assert "operational_state.selected_entry_missing" in missing_report.blockers

    primary = next(
        entry
        for entry in readiness.catalog.entries
        if entry.environment is CaptureEnvironment.PRODUCTION and entry.role is SourceRole.PRIMARY
    )
    missing_cost = SourceCoverageEntry.model_validate(
        {
            **primary.model_dump(exclude={"source_coverage_entry_id", "budget_lines"}),
            "budget_lines": tuple(
                line for line in primary.budget_lines if line.dimension is not BudgetDimension.VENDOR_FEES
            ),
        }
    )
    incomplete_catalog = SourceCoverageCatalog.model_validate(
        {
            **readiness.catalog.model_dump(exclude={"source_coverage_catalog_id", "entries"}),
            "entries": tuple(missing_cost if entry is primary else entry for entry in readiness.catalog.entries),
        }
    )
    incomplete_readiness = SourceReadinessReport(
        catalog=incomplete_catalog,
        registry_snapshot=registry,
        rights_approvals=readiness.rights_approvals,
        evaluated_at=NOW,
    )
    incomplete_intent = _intent(registry, incomplete_readiness)
    incomplete = SourceCallPreflightReport(
        intent=incomplete_intent,
        registry_snapshot=registry,
        source_readiness=incomplete_readiness,
        operational_states=states,
        evaluated_at=NOW,
    )
    assert not incomplete.allowed
    assert "coverage.vendor_fees_budget_missing" in incomplete.blockers


def test_preflight_fails_expired_rights_and_rejects_manual_override() -> None:
    evaluated_at = NOW + timedelta(days=31)
    registry, readiness, states = _source_bundle(
        evaluated_at=evaluated_at,
        rights_expiry=NOW + timedelta(days=30),
        review_expiry=NOW + timedelta(days=60),
    )
    intent = _intent(registry, readiness, intended_at=evaluated_at + timedelta(minutes=1))
    report = SourceCallPreflightReport(
        intent=intent,
        registry_snapshot=registry,
        source_readiness=readiness,
        operational_states=states,
        evaluated_at=evaluated_at,
    )
    assert not report.allowed
    assert "readiness.failed" in report.blockers
    assert "rights.approval_expired" in report.blockers
    with pytest.raises(ValidationError, match="Extra inputs"):
        SourceCallPreflightReport.model_validate(
            {
                "intent": intent,
                "registry_snapshot": registry,
                "source_readiness": readiness,
                "operational_states": states,
                "evaluated_at": evaluated_at,
                "allowed": True,
            }
        )


def test_full_catalog_budget_reconciliation_is_row_complete_and_fail_closed() -> None:
    _, readiness, _ = _source_bundle()
    report = _budget_report(readiness.catalog)
    assert report.ready
    assert report.blockers == ()
    assert report.budget_report_id.startswith("full-catalog-budget:")

    missing_observation = FullCatalogBudgetReport(
        catalog=report.catalog,
        horizon=report.horizon,
        window_started_at=report.window_started_at,
        window_completed_at=report.window_completed_at,
        evaluated_at=report.evaluated_at,
        observations=report.observations[:-1],
    )
    assert not missing_observation.ready
    assert any("budget.observation_missing" in blocker for blocker in missing_observation.blockers)

    primary = next(entry for entry in readiness.catalog.entries if entry.role is SourceRole.PRIMARY)
    key = (primary.source_coverage_entry_id, BudgetDimension.API_CALLS)
    undercount = _budget_report(
        readiness.catalog,
        actual_overrides={key: (Decimal("1"), Decimal("1"))},
    )
    assert not undercount.ready
    assert any("budget.unexplained_undercount" in blocker for blocker in undercount.blockers)

    explanation = BudgetUndercountExplanation(
        source_coverage_entry_id=key[0],
        dimension=key[1],
        planned_use=Decimal("10"),
        observed_use=Decimal("1"),
        rationale="The scheduled filing was not published in this closed window.",
        approved_by="finance-owner",
        approved_at=NOW,
        evidence_id="variance:publication-not-due",
        evidence_sha256=_sha("variance-publication-not-due"),
        approval_signature_id="signature:variance",
        approval_signature_sha256=_sha("variance-signature"),
    )
    explained = _budget_report(
        readiness.catalog,
        actual_overrides={key: (Decimal("1"), Decimal("1"))},
        explanations=(explanation,),
    )
    assert explained.ready

    telemetry_gap = _budget_report(
        readiness.catalog,
        actual_overrides={key: (Decimal("1"), Decimal("10"))},
    )
    assert not telemetry_gap.ready
    assert any("budget.telemetry_reconciliation_failed" in blocker for blocker in telemetry_gap.blockers)

    over_budget = _budget_report(
        readiness.catalog,
        actual_overrides={key: (Decimal("21"), Decimal("21"))},
    )
    assert not over_budget.ready
    assert any("budget.approved_limit_exceeded" in blocker for blocker in over_budget.blockers)
    with pytest.raises(ValidationError, match="Extra inputs"):
        FullCatalogBudgetReport.model_validate(
            {
                **report.model_dump(exclude={"blockers", "ready", "budget_report_id"}),
                "ready": True,
            }
        )


def test_scheduled_recheck_revalidates_rights_budget_and_next_window() -> None:
    registry, readiness, states = _source_bundle()
    budget = _budget_report(readiness.catalog)
    schedule = _schedule(registry, readiness, states)
    report = ScheduledOperationalRecheck(
        schedule=schedule,
        registry_snapshot=registry,
        source_readiness=readiness,
        budget_reconciliation=budget,
        operational_states=states,
        scheduled_for=NOW,
        evaluated_at=NOW,
    )
    assert report.ready
    assert report.blockers == ()

    unsafe_schedule = _schedule(
        registry,
        readiness,
        states,
        cadence=timedelta(days=31),
    )
    unsafe = ScheduledOperationalRecheck(
        schedule=unsafe_schedule,
        registry_snapshot=registry,
        source_readiness=readiness,
        budget_reconciliation=budget,
        operational_states=states,
        scheduled_for=NOW,
        evaluated_at=NOW,
    )
    assert not unsafe.ready
    assert any("rights_expire_before_next_run" in blocker for blocker in unsafe.blockers)


SOAK_START = NOW + timedelta(days=1)
SOAK_END = SOAK_START + timedelta(days=2)


def _graduation_universe() -> tuple[UniverseManifest, tuple[UniverseMembership, ...]]:
    membership_ids = ("membership:issuer-alpha", "membership:security-alpha")
    manifest = UniverseManifest.create(
        universe_id="universe:topt-graduation",
        universe_version="2026.07.13",
        definition_kind=UniverseDefinitionKind.FIXED_COHORT,
        effective_at=SOAK_START - timedelta(days=3),
        owner="research-owner",
        membership_ids=membership_ids,
    )
    memberships = (
        UniverseMembership(
            membership_id=membership_ids[0],
            universe_id=manifest.ref.universe_id,
            subject=SubjectRef(kind=SubjectKind.ISSUER, id="issuer:alpha"),
            valid_from=date(2026, 1, 1),
            knowable_at=SOAK_START - timedelta(days=3),
            recorded_at=SOAK_START - timedelta(days=3),
            confidence=Decimal("1"),
            raw_ref="raw.universe:topt-issuer",
        ),
        UniverseMembership(
            membership_id=membership_ids[1],
            universe_id=manifest.ref.universe_id,
            subject=SubjectRef(kind=SubjectKind.SECURITY, id="security:alpha-common"),
            valid_from=date(2026, 1, 1),
            knowable_at=SOAK_START - timedelta(days=3),
            recorded_at=SOAK_START - timedelta(days=3),
            confidence=Decimal("1"),
            raw_ref="raw.universe:topt-security",
        ),
    )
    return manifest, memberships


def _research_catalog(universe: UniverseRef) -> ResearchCatalogManifest:
    approval_sha = "2" * 64
    approval = ProductOwnerApproval(
        approved_by="product-owner",
        approval_record_id="approval-record:" + approval_sha,
        approval_record_sha256=approval_sha,
        approved_at=SOAK_START - timedelta(days=4),
    )
    question = CanonicalQuestion(
        question_key="question.filing.alpha",
        tool_kind=CatalogTargetKind.FACTOR,
        catalog_aliases=("filings",),
        subject_scope=(SUBJECT,),
        requirement_level=CatalogRequirementLevel.REQUIRED,
        expected_output_type_ids=("output.filing.v1",),
        expected_statuses=(ExpectedOutputStatus.AVAILABLE,),
        prompt_examples=("Load the approved filing for Alpha.",),
        approved_at=SOAK_START - timedelta(days=4),
    )
    implementation_sha = "3" * 64
    factor_template = FactorInvocationTemplate(
        factor_id="factor.filings.alpha",
        factor_version="1.0.0",
        factor_implementation_sha256=implementation_sha,
        factor_kind=FactorKind.BASE,
        parameter_model_key="catalog:FilingParameters",
        parameter_schema_sha256="4" * 64,
        canonical_parameters_sha256=canonical_sha256({}),
        data_requirement_ids=(DATA_REQUIREMENT_ID,),
        dependencies=(),
    )
    selector = InvocationTemplateSelector(
        target_kind=CatalogTargetKind.FACTOR,
        factor_template=factor_template,
        parameters=(),
        frozen_at=SOAK_START - timedelta(days=4),
    )
    entry = ResearchCatalogEntry(
        catalog_alias="filings",
        requirement_level=CatalogRequirementLevel.REQUIRED,
        target=FactorCatalogTarget(
            factor_id=factor_template.factor_id,
            factor_version=factor_template.factor_version,
            definition_sha256=implementation_sha,
        ),
        universe=universe,
        subject_scope=(SUBJECT,),
        invocation_template=selector,
        applicability_policy_id="applicability-policy:" + "5" * 64,
        applicability_policy_sha256="5" * 64,
        slo_policy_id="slo-policy:" + "6" * 64,
        slo_policy_sha256="6" * 64,
        canonical_question_ids=(question.canonical_question_id,),
        expected_output_type_ids=("output.filing.v1",),
        approved_at=SOAK_START - timedelta(days=4),
    )
    minimums = ResearchScopeMinimums(
        issuers=1,
        funds=1,
        themes=1,
        analysts=1,
        scenarios=1,
        screens=1,
        rankings=1,
        strategies=1,
        canonical_questions=1,
    )
    scope_floor = ResearchScopeFloor(
        universe=universe,
        minimums=minimums,
        required_entry_ids=(entry.catalog_entry_id,),
        required_question_ids=(question.canonical_question_id,),
        approval=approval,
    )
    return ResearchCatalogManifest(
        catalog_version="1.0.0",
        vision_sha256="7" * 64,
        predecessor_catalog_id=None,
        scope_floor=scope_floor,
        entries=(entry,),
        canonical_questions=(question,),
        narrowed_claim=None,
        catalog_approval=approval,
        created_at=SOAK_START - timedelta(days=3),
        effective_at=SOAK_START - timedelta(days=2),
    )


def _applicability(
    catalog: ResearchCatalogManifest,
    universe: UniverseRef,
) -> ApplicabilityCatalog:
    return ApplicabilityCatalog(
        catalog_version="1.0.0",
        research_catalog_id=catalog.research_catalog_id,
        research_catalog_sha256=catalog.content_sha256,
        universe=universe,
        effective_at=SOAK_START - timedelta(days=2),
        approved_at=SOAK_START - timedelta(days=3),
        approved_by="independent-reviewer",
        approval_signature_id="signature:applicability",
        approval_signature_sha256=_sha("applicability-signature"),
        cells=(
            ApplicabilityCell(
                module_id="module.filings",
                catalog_alias="filings",
                data_requirement_id=DATA_REQUIREMENT_ID,
                subject=SUBJECT,
                domain=DataDomain.FILINGS,
                partition_key="2026Q2",
                classification=ApplicabilityClassification.REQUIRED,
                reason="Required by the frozen research catalog.",
                effective_at=SOAK_START - timedelta(days=2),
            ),
        ),
    )


def _slo_reports(
    applicability: ApplicabilityCatalog,
) -> tuple[ModuleSloReport, ConsumerSloReport]:
    threshold = ModuleSloThreshold(
        module_id="module.filings",
        minimum_subject_count=1,
        minimum_usable_coverage=Decimal("1"),
        maximum_unavailable_ratio=Decimal("0"),
        maximum_stale_ratio=Decimal("0"),
        maximum_unresolved_ratio=Decimal("0"),
        maximum_unclassified_ratio=Decimal("0"),
        maximum_low_confidence_ratio=Decimal("0"),
        rationale="The required cell must be usable.",
        evidence_sha256=_sha("module-slo-evidence"),
        approved_by="independent-reviewer",
        approved_at=SOAK_START - timedelta(days=3),
        approval_signature_id="signature:module-slo",
        approval_signature_sha256=_sha("module-slo-signature"),
    )
    module_catalog = ModuleSloCatalog(
        catalog_version="1.0.0",
        applicability_catalog_id=applicability.applicability_catalog_id,
        applicability_catalog_sha256=applicability.content_sha256,
        effective_at=SOAK_START - timedelta(days=2),
        approved_at=SOAK_START - timedelta(days=3),
        approved_by="independent-reviewer",
        approval_signature_id="signature:module-slo-catalog",
        approval_signature_sha256=_sha("module-slo-catalog-signature"),
        thresholds=(threshold,),
    )
    module_report = ModuleSloReport(
        applicability=applicability,
        slo_catalog=module_catalog,
        run_started_at=SOAK_START,
        evaluated_at=SOAK_END,
        observations=(
            ModuleSloObservation(
                module_id="module.filings",
                catalog_alias="filings",
                data_requirement_id=DATA_REQUIREMENT_ID,
                subject=SUBJECT,
                domain=DataDomain.FILINGS,
                partition_key="2026Q2",
                outcome=ModuleOutcome.USABLE,
                observed_at=SOAK_START + timedelta(hours=3),
                output_id="mart-output:filings-alpha",
                trace_complete=True,
            ),
        ),
    )
    consumer_requirement = ConsumerSloRequirement(
        consumer_id="app-web",
        endpoint_id="research-card",
        minimum_availability=Decimal("1"),
        maximum_latency_ms=500,
        maximum_row_count=100,
        require_authenticated=True,
        require_trace_complete=True,
        maximum_permission_failure_ratio=Decimal("0"),
        error_budget_ratio=Decimal("0"),
        owner="app-owner",
        remediation_runbook="runbook://consumer-slo",
    )
    consumer_catalog = ConsumerSloCatalog(
        catalog_version="1.0.0",
        applicability_catalog_id=applicability.applicability_catalog_id,
        applicability_catalog_sha256=applicability.content_sha256,
        effective_at=SOAK_START - timedelta(days=2),
        approved_at=SOAK_START - timedelta(days=3),
        approved_by="product-owner",
        approval_signature_id="signature:consumer-slo",
        approval_signature_sha256=_sha("consumer-slo-signature"),
        requirements=(consumer_requirement,),
    )
    consumer_report = ConsumerSloReport(
        catalog=consumer_catalog,
        evaluated_at=SOAK_END,
        observations=(
            ConsumerSloObservation(
                consumer_id="app-web",
                endpoint_id="research-card",
                window_started_at=SOAK_START,
                window_completed_at=SOAK_END,
                request_count=10,
                successful_request_count=10,
                authenticated_request_count=10,
                trace_complete_count=10,
                permission_failure_count=0,
                error_count=0,
                latency_p95_ms=100,
                largest_row_count=20,
            ),
        ),
    )
    return module_report, consumer_report


def _capture_scope(
    catalog: ResearchCatalogManifest,
    applicability: ApplicabilityCatalog,
    readiness: SourceReadinessReport,
    registry: RegistrySnapshot,
    module_slo_report: ModuleSloReport,
) -> CaptureScope:
    requirement = CaptureRequirement(
        semantic_type_id="semantic.filing",
        semantic_type_version="1.0.0",
        domain=DataDomain.FILINGS,
        required_fields=("published_at",),
        subject_kinds=(SubjectKind.ISSUER,),
        cadence=timedelta(days=1),
        partition_rule_id="partition.quarter",
        freshness_policy_id="freshness.daily",
        maximum_age=timedelta(days=2),
        quality_policy_ids=("quality.filing",),
    )
    return CaptureScope(
        research_catalog_id=catalog.research_catalog_id,
        research_catalog_sha256=catalog.content_sha256,
        universe=catalog.scope_floor.universe,
        applicability_catalog_id=applicability.applicability_catalog_id,
        applicability_catalog_sha256=applicability.content_sha256,
        applicability_projection_sha256=_sha("applicability-projection"),
        source_coverage_catalog_id=readiness.catalog.source_coverage_catalog_id,
        source_coverage_catalog_sha256=readiness.catalog.content_sha256,
        source_coverage_projection_sha256=_sha("source-coverage-projection"),
        slo_catalog_id=module_slo_report.slo_catalog.module_slo_catalog_id,
        slo_catalog_sha256=module_slo_report.slo_catalog.content_sha256,
        source_registry_id=registry.source_registry_snapshot_id,
        source_registry_sha256=registry.source_registry_sha256,
        semantic_type_registry_id=registry.semantic_type_registry_snapshot_id,
        semantic_type_registry_sha256=registry.semantic_type_registry_sha256,
        requirements=(requirement,),
        effective_at=SOAK_START - timedelta(days=1),
        owner="capture-owner",
    )


def _capture_report(scope: CaptureScope, applicability: ApplicabilityCatalog) -> CaptureEvaluationReport:
    manifest_sha = _sha("production-capture-manifest")
    return CaptureEvaluationReport(
        capture_scope_id=scope.capture_scope_id,
        capture_scope_sha256=scope.content_sha256,
        capture_manifest_id="capture-manifest:" + manifest_sha,
        capture_manifest_sha256=manifest_sha,
        applicability_catalog_id=applicability.applicability_catalog_id,
        applicability_catalog_sha256=applicability.content_sha256,
        applicability_projection_sha256=scope.applicability_projection_sha256,
        source_coverage_projection_sha256=scope.source_coverage_projection_sha256,
        environment=CaptureEnvironment.PRODUCTION,
        evaluated_at=SOAK_START + timedelta(hours=2),
        blocking_reason_codes=(),
    )


def _natural_refresh() -> NaturalRefreshReport:
    requirement = NaturalRefreshRequirement(
        source_class="source.sec",
        source_ids=("source.sec",),
        environment=CaptureEnvironment.PRODUCTION,
        subject=SUBJECT,
        domain=DataDomain.FILINGS,
        partition_pattern="2026Q*",
        cadence=timedelta(days=3),
        maximum_age=timedelta(days=3),
        required_naturally_changed_partitions=1,
        required_publication_transitions=1,
        maximum_observation_window=timedelta(days=3),
        effective_at=SOAK_START - timedelta(days=2),
        approved_at=SOAK_START - timedelta(days=3),
        approved_by="product-owner",
        owner="source-owner",
        alert_id="alert:natural-refresh",
        remediation_runbook="runbook://natural-refresh",
        approval_signature_id="signature:natural-refresh",
        approval_signature_sha256=_sha("natural-refresh-signature"),
    )
    transition = RefreshTransition(
        requirement_id=requirement.natural_refresh_requirement_id,
        source_id="source.sec",
        source_version="1.0.0",
        subject=SUBJECT,
        domain=DataDomain.FILINGS,
        partition_key="2026Q2",
        evidence_kind=RefreshEvidenceKind.NATURAL_PUBLICATION,
        previous_publication_id="publication:previous",
        current_publication_id="publication:current",
        previous_content_sha256=_sha("previous-publication"),
        current_content_sha256=_sha("current-publication"),
        previous_published_at=SOAK_START - timedelta(hours=1),
        current_published_at=SOAK_START + timedelta(days=1),
        observed_at=SOAK_START + timedelta(days=1, hours=1),
    )
    return NaturalRefreshReport(
        requirement=requirement,
        observation_started_at=SOAK_START,
        evaluated_at=SOAK_END,
        transitions=(transition,),
    )


def _artifacts() -> tuple[ReleaseArtifact, ...]:
    return tuple(
        ReleaseArtifact(
            role=role,
            image_or_bundle=f"ghcr.io/truealpha/{role.value}@sha256:{_sha(role.value)}",
            digest="sha256:" + _sha(f"digest-{role.value}"),
            git_sha="8" * 40,
            sbom_sha256=_sha(f"sbom-{role.value}"),
            signature_ref=f"sigstore:{role.value}",
        )
        for role in ArtifactRole
    )


def _release(
    catalog: ResearchCatalogManifest,
    applicability: ApplicabilityCatalog,
    module_report: ModuleSloReport,
    consumer_report: ConsumerSloReport,
    scope: CaptureScope,
    registry: RegistrySnapshot,
    readiness: SourceReadinessReport,
    refresh: NaturalRefreshReport,
    usage_catalog: UsageTelemetrySloCatalog,
) -> ReleaseManifest:
    migration_ids = ("0001.sql",)
    return ReleaseManifest(
        contract_version="contracts:v1",
        mart_schema_version="mart:v1",
        research_catalog_id=catalog.research_catalog_id,
        research_catalog_sha256=catalog.content_sha256,
        universe=catalog.scope_floor.universe,
        capture_scope_id=scope.capture_scope_id,
        capture_scope_sha256=scope.content_sha256,
        applicability_catalog_id=applicability.applicability_catalog_id,
        applicability_catalog_sha256=applicability.content_sha256,
        source_coverage_catalog_id=readiness.catalog.source_coverage_catalog_id,
        source_coverage_catalog_sha256=readiness.catalog.content_sha256,
        source_readiness_report_id=readiness.source_readiness_report_id,
        source_readiness_report_sha256=readiness.source_readiness_report_id.rsplit(":", 1)[-1],
        slo_catalog_id=module_report.slo_catalog.module_slo_catalog_id,
        slo_catalog_sha256=module_report.slo_catalog.content_sha256,
        consumer_slo_catalog_id=consumer_report.catalog.consumer_slo_catalog_id,
        consumer_slo_catalog_sha256=consumer_report.catalog.content_sha256,
        usage_telemetry_slo_catalog_id=usage_catalog.usage_telemetry_slo_catalog_id,
        usage_telemetry_slo_catalog_sha256=usage_catalog.content_sha256,
        registry_snapshot_id=registry.registry_snapshot_id,
        registry_snapshot_sha256=registry.content_sha256,
        source_registry_id=registry.source_registry_snapshot_id,
        source_registry_sha256=registry.source_registry_sha256,
        semantic_type_registry_id=registry.semantic_type_registry_snapshot_id,
        semantic_type_registry_sha256=registry.semantic_type_registry_sha256,
        identifier_type_registry_id=registry.identifier_type_registry_snapshot_id,
        identifier_type_registry_sha256=registry.identifier_type_registry_sha256,
        configuration_sha256={"data-engine": _sha("data-engine-config")},
        migration_ids=migration_ids,
        migration_set_sha256=canonical_sha256(migration_ids),
        artifacts=_artifacts(),
        natural_refresh_requirement_ids=(refresh.requirement.natural_refresh_requirement_id,),
        created_at=SOAK_START - timedelta(days=2),
        manifest_signature_ref="sigstore:release",
    )


def _usage_catalog(
    catalog: ResearchCatalogManifest,
    universe: UniverseRef,
    applicability: ApplicabilityCatalog,
    registry: RegistrySnapshot,
    capture_requirement_id: str,
) -> tuple[UsageTelemetrySloCatalog, UsageTelemetryRequirement]:
    requirement = UsageTelemetryRequirement(
        data_requirement_id=DATA_REQUIREMENT_ID,
        capture_requirement_id=capture_requirement_id,
        semantic_type_id="semantic.filing",
        emitter_kind=UsageEmitterKind.CAPTURE_MANIFEST_EVALUATOR,
        emitter_id="capture-evaluator",
        stage=UsageStage.CAPTURE,
        subject=SUBJECT,
        domain=DataDomain.FILINGS,
        partition_key="2026Q2",
        expected_window_start=SOAK_START,
        expected_window_end=SOAK_END - timedelta(hours=1),
        expected_minimum_events=1,
        expected_maximum_events=1,
        maximum_lag=timedelta(minutes=1),
        minimum_retention=timedelta(days=365),
        demand_evidence_id="demand:usage",
        demand_evidence_sha256=_sha("usage-demand"),
    )
    usage_catalog = UsageTelemetrySloCatalog(
        catalog_version="1.0.0",
        research_catalog_id=catalog.research_catalog_id,
        research_catalog_sha256=catalog.content_sha256,
        universe=universe,
        applicability_catalog_id=applicability.applicability_catalog_id,
        applicability_catalog_sha256=applicability.content_sha256,
        registry_snapshot_id=registry.registry_snapshot_id,
        registry_snapshot_sha256=registry.content_sha256,
        effective_at=SOAK_START - timedelta(days=1),
        approved_at=SOAK_START - timedelta(days=2),
        approved_by="independent-reviewer",
        approval_signature_id="signature:usage-slo",
        approval_signature_sha256=_sha("usage-slo-signature"),
        completeness_target=Decimal("1"),
        maximum_catalog_lag=timedelta(minutes=1),
        requirements=(requirement,),
    )
    return usage_catalog, requirement


def _usage_report(
    usage_catalog: UsageTelemetrySloCatalog,
    requirement: UsageTelemetryRequirement,
    *,
    capture_requirement_id: str,
    telemetry_missing: bool,
) -> UsageTelemetryReport:
    event = DataUsageEvent(
        operation_id="capture:production:2026q2",
        emitter_kind=UsageEmitterKind.CAPTURE_MANIFEST_EVALUATOR,
        emitter_id="capture-evaluator",
        stage=UsageStage.CAPTURE,
        requirement_id=DATA_REQUIREMENT_ID,
        capture_requirement_id=capture_requirement_id,
        semantic_type_id="semantic.filing",
        domain=DataDomain.FILINGS,
        subject=SUBJECT,
        partition_key="2026Q2",
        run_id="run:production",
        trace_id="trace:production",
        evidence_ids=("capture-manifest:production",),
        occurred_at=SOAK_START + timedelta(hours=1),
        recorded_at=SOAK_START + timedelta(hours=1, seconds=1),
        retained_until=SOAK_END + timedelta(days=365),
    )
    events = () if telemetry_missing else (event,)
    return UsageTelemetryReport(
        catalog=usage_catalog,
        evaluated_at=SOAK_END,
        events=events,
        reconciliations=(
            UsageTelemetryReconciliation(
                telemetry_requirement_id=requirement.telemetry_requirement_id,
                source_event_count=len(events),
                reconciled_at=SOAK_END,
                evidence_sha256=_sha("usage-reconciliation"),
            ),
        ),
    )


def _graduation_report(
    *,
    missing_approval: bool = False,
    telemetry_missing: bool = False,
    shrunken_scope: bool = False,
    over_budget: bool = False,
    evaluated_at: datetime = SOAK_END + timedelta(minutes=4),
) -> ProductionGraduationReport:
    universe_manifest, memberships = _graduation_universe()
    catalog = _research_catalog(universe_manifest.ref)
    applicability = _applicability(catalog, universe_manifest.ref)
    registry, readiness, states = _source_bundle(
        evaluated_at=SOAK_END,
        rights_expiry=SOAK_END + timedelta(days=30),
        review_expiry=SOAK_END + timedelta(days=30),
        universe=universe_manifest.ref,
        research_catalog_id=catalog.research_catalog_id,
        research_catalog_sha256=catalog.content_sha256,
        applicability_catalog_id=applicability.applicability_catalog_id,
        applicability_catalog_sha256=applicability.content_sha256,
    )
    module_report, consumer_report = _slo_reports(applicability)
    capture_scope = _capture_scope(catalog, applicability, readiness, registry, module_report)
    capture_report = _capture_report(capture_scope, applicability)
    refresh = _natural_refresh()
    usage_catalog, usage_requirement = _usage_catalog(
        catalog,
        universe_manifest.ref,
        applicability,
        registry,
        capture_scope.requirements[0].capture_requirement_id,
    )
    release = _release(
        catalog,
        applicability,
        module_report,
        consumer_report,
        capture_scope,
        registry,
        readiness,
        refresh,
        usage_catalog,
    )
    usage_report = _usage_report(
        usage_catalog,
        usage_requirement,
        capture_requirement_id=capture_scope.requirements[0].capture_requirement_id,
        telemetry_missing=telemetry_missing,
    )
    overrides: dict[tuple[str, BudgetDimension], tuple[Decimal, Decimal]] = {}
    if over_budget:
        primary = next(
            entry
            for entry in readiness.catalog.entries
            if entry.environment is CaptureEnvironment.PRODUCTION and entry.role is SourceRole.PRIMARY
        )
        overrides[(primary.source_coverage_entry_id, BudgetDimension.API_CALLS)] = (
            Decimal("21"),
            Decimal("21"),
        )
    budget = _budget_report(
        readiness.catalog,
        evaluated_at=SOAK_END,
        actual_overrides=overrides,
    )
    schedule = _schedule(
        registry,
        readiness,
        states,
        release_sha=release.manifest_sha256,
    )
    recheck = ScheduledOperationalRecheck(
        schedule=schedule,
        registry_snapshot=registry,
        source_readiness=readiness,
        budget_reconciliation=budget,
        operational_states=states,
        scheduled_for=SOAK_END,
        evaluated_at=SOAK_END,
    )
    comparison = ComparisonCriterion(
        metric_id="capture.row-count-delta",
        unit="rows",
        maximum_absolute_delta=Decimal("0"),
        rationale="Candidate and baseline must be row-complete.",
        approved_by="independent-reviewer",
        approved_at=SOAK_START - timedelta(days=2),
        evidence_sha256=_sha("comparison-criterion"),
        approval_signature_id="signature:comparison",
        approval_signature_sha256=_sha("comparison-signature"),
    )
    rollback_sha = "9" * 64
    rollback = RollbackPlan(
        target_release_manifest_id="release-manifest:" + rollback_sha,
        target_release_manifest_sha256=rollback_sha,
        runbook="runbook://rollback",
        owner="runtime-owner",
        maximum_execution_time=timedelta(minutes=30),
        maximum_test_age=timedelta(days=30),
        tested_at=SOAK_START - timedelta(days=1),
        test_evidence_id="restore-test:production",
        test_evidence_sha256=_sha("restore-test"),
    )
    plan = ProductionGraduationPlan(
        research_catalog_id=catalog.research_catalog_id,
        research_catalog_sha256=catalog.content_sha256,
        universe=universe_manifest.ref,
        applicability_catalog_id=applicability.applicability_catalog_id,
        applicability_catalog_sha256=applicability.content_sha256,
        module_slo_catalog_id=module_report.slo_catalog.module_slo_catalog_id,
        module_slo_catalog_sha256=module_report.slo_catalog.content_sha256,
        consumer_slo_catalog_id=consumer_report.catalog.consumer_slo_catalog_id,
        consumer_slo_catalog_sha256=consumer_report.catalog.content_sha256,
        usage_telemetry_slo_catalog_id=usage_catalog.usage_telemetry_slo_catalog_id,
        usage_telemetry_slo_catalog_sha256=usage_catalog.content_sha256,
        capture_scope_id=capture_scope.capture_scope_id,
        capture_scope_sha256=capture_scope.content_sha256,
        source_coverage_catalog_id=readiness.catalog.source_coverage_catalog_id,
        source_coverage_catalog_sha256=readiness.catalog.content_sha256,
        registry_snapshot_id=registry.registry_snapshot_id,
        registry_snapshot_sha256=registry.content_sha256,
        release_manifest_id=release.release_manifest_id,
        release_manifest_sha256=release.manifest_sha256,
        operational_recheck_schedule_id=schedule.production_recheck_schedule_id,
        operational_recheck_schedule_sha256=schedule.content_sha256,
        expected_issuer_count=1,
        expected_instrument_count=1,
        required_domains=(DataDomain.FILINGS,),
        staged_batches=(
            StagedBatchRequirement(
                stage=1,
                issuer_count=1,
                instrument_count=1,
                required_capture_runs=1,
            ),
        ),
        natural_refresh_requirement_ids=(refresh.requirement.natural_refresh_requirement_id,),
        minimum_soak_duration=timedelta(days=2),
        maximum_operational_evidence_age=timedelta(hours=1),
        comparison_criteria=(comparison,),
        rollback=rollback,
        effective_at=SOAK_START - timedelta(days=1),
        approved_at=SOAK_START - timedelta(days=2),
        approved_by="product-owner",
        approval_record_id="approval:graduation-plan",
        approval_record_sha256=_sha("graduation-plan-approval"),
        approval_signature_id="signature:graduation-plan",
        approval_signature_sha256=_sha("graduation-plan-signature"),
    )
    selected_memberships = memberships[:1] if shrunken_scope else memberships
    evidence = ProductionGraduationEvidence(
        research_catalog=catalog,
        universe_manifest=universe_manifest,
        universe_memberships=selected_memberships,
        applicability=applicability,
        module_slo_report=module_report,
        consumer_slo_report=consumer_report,
        usage_telemetry_report=usage_report,
        capture_scope=capture_scope,
        capture_reports=(capture_report,),
        release_manifest=release,
        registry_snapshot=registry,
        source_readiness=readiness,
        budget_reconciliation=budget,
        operational_recheck=recheck,
        natural_refresh_reports=(refresh,),
        staged_batch_evidence=(
            StagedBatchEvidence(
                stage=1,
                issuer_ids=("issuer:alpha",),
                instrument_ids=("security:alpha-common",),
                capture_evaluation_report_ids=(capture_report.capture_evaluation_report_id,),
                run_ids=("run:production",),
                error_count=0,
                started_at=SOAK_START + timedelta(hours=1),
                completed_at=SOAK_START + timedelta(hours=2),
                evidence_id="batch-evidence:1",
                evidence_sha256=_sha("batch-evidence"),
            ),
        ),
        comparison_observations=(
            ComparisonObservation(
                comparison_criterion_id=comparison.comparison_criterion_id,
                metric_id=comparison.metric_id,
                unit=comparison.unit,
                baseline_value=Decimal("1"),
                candidate_value=Decimal("1"),
                measured_at=SOAK_START + timedelta(hours=3),
                evidence_id="comparison:row-count",
                evidence_sha256=_sha("comparison-observation"),
            ),
        ),
        soak_started_at=SOAK_START,
        soak_completed_at=SOAK_END,
        created_at=SOAK_END + timedelta(minutes=1),
    )
    approvals = tuple(
        GraduationApproval(
            role=role,
            approver=approver,
            graduation_plan_id=plan.production_graduation_plan_id,
            graduation_plan_sha256=plan.content_sha256,
            evidence_bundle_id=evidence.evidence_bundle_id,
            evidence_bundle_sha256=evidence.content_sha256,
            approved_at=SOAK_END + timedelta(minutes=offset),
            approval_record_id=f"approval:{role.value}",
            approval_record_sha256=_sha(f"approval-{role.value}"),
            approval_signature_id=f"signature:{role.value}",
            approval_signature_sha256=_sha(f"signature-{role.value}"),
        )
        for role, approver, offset in (
            (GraduationApprovalRole.INDEPENDENT_REVIEWER, "independent-reviewer", 2),
            (GraduationApprovalRole.PRODUCT_OWNER, "product-owner", 3),
        )
    )
    if missing_approval:
        approvals = approvals[:1]
    return ProductionGraduationReport(
        plan=plan,
        evidence=evidence,
        approvals=approvals,
        evaluated_at=evaluated_at,
    )


def test_production_graduation_binds_exact_scope_and_requires_two_human_approvals() -> None:
    report = _graduation_report()
    assert report.graduated
    assert report.blockers == ()
    assert report.graduation_report_id.startswith("production-graduation-report:")

    missing = _graduation_report(missing_approval=True)
    assert not missing.graduated
    assert "graduation.required_approvals_missing" in missing.blockers


def test_graduation_fails_telemetry_budget_scope_expiry_and_manual_override() -> None:
    telemetry = _graduation_report(telemetry_missing=True)
    assert not telemetry.graduated
    assert "graduation.usage_telemetry_failed_or_mismatched" in telemetry.blockers

    budget = _graduation_report(over_budget=True)
    assert not budget.graduated
    assert "graduation.budget_reconciliation_failed" in budget.blockers

    shrunken = _graduation_report(shrunken_scope=True)
    assert not shrunken.graduated
    assert "graduation.instrument_count_mismatch" in shrunken.blockers
    assert "graduation.universe_membership_set_mismatch" in shrunken.blockers

    expired = _graduation_report(evaluated_at=SOAK_END + timedelta(days=31))
    assert not expired.graduated
    assert any(blocker.startswith("graduation.rights_expired:") for blocker in expired.blockers)

    valid = _graduation_report()
    with pytest.raises(ValidationError, match="Extra inputs"):
        ProductionGraduationReport.model_validate(
            {
                **valid.model_dump(exclude={"blockers", "graduated", "graduation_report_id"}),
                "graduated": True,
            }
        )


def _attestation(report: ProductionGraduationReport) -> GraduationAttestation:
    attested_at = report.evaluated_at + timedelta(minutes=1)
    release = report.evidence.release_manifest
    candidate_commit_sha = release.artifacts[0].git_sha
    independence_evidence_id = "independence-review:graduation"
    independence_evidence_sha256 = _sha("independence-review")
    signed_payload_sha256 = GraduationAttestation.compute_signed_payload_sha256(
        release_manifest_id=release.release_manifest_id,
        release_manifest_sha256=release.manifest_sha256,
        candidate_commit_sha=candidate_commit_sha,
        graduation_report=report,
        attested_by="independent-reviewer",
        attested_at=attested_at,
        independence_evidence_id=independence_evidence_id,
        independence_evidence_sha256=independence_evidence_sha256,
    )
    return GraduationAttestation(
        release_manifest_id=release.release_manifest_id,
        release_manifest_sha256=release.manifest_sha256,
        candidate_commit_sha=candidate_commit_sha,
        graduation_report=report,
        attested_by="independent-reviewer",
        attested_at=attested_at,
        independence_evidence_id=independence_evidence_id,
        independence_evidence_sha256=independence_evidence_sha256,
        signed_payload_sha256=signed_payload_sha256,
        signature_ref="sigstore:graduation-attestation",
        signature_sha256=_sha("graduation-attestation-signature"),
    )


class _AttestationRepository:
    def __init__(self, attestation: GraduationAttestation | None):
        self.attestation = attestation

    def get(self, graduation_attestation_id: str) -> GraduationAttestation | None:
        if self.attestation is not None and self.attestation.graduation_attestation_id == graduation_attestation_id:
            return self.attestation
        return None


class _AttestationVerifier:
    def __init__(self, accepted: bool):
        self.accepted = accepted

    def verify(self, attestation: GraduationAttestation) -> bool:
        return self.accepted and bool(attestation.signature_ref)


def test_graduation_attestation_wraps_only_a_real_derived_graduation() -> None:
    graduated = _graduation_report()
    attestation = _attestation(graduated)
    assert attestation.graduation_attestation_id == ("graduation-attestation:" + attestation.content_sha256)
    assert attestation.release_manifest_id == graduated.plan.release_manifest_id
    assert (
        resolve_graduation_attestation(
            _AttestationRepository(attestation),
            _AttestationVerifier(True),
            graduation_attestation_id=attestation.graduation_attestation_id,
            release_manifest_id=attestation.release_manifest_id,
            release_manifest_sha256=attestation.release_manifest_sha256,
            candidate_commit_sha=attestation.candidate_commit_sha,
        )
        is attestation
    )
    with pytest.raises(ValueError, match="signature verification failed"):
        resolve_graduation_attestation(
            _AttestationRepository(attestation),
            _AttestationVerifier(False),
            graduation_attestation_id=attestation.graduation_attestation_id,
            release_manifest_id=attestation.release_manifest_id,
            release_manifest_sha256=attestation.release_manifest_sha256,
            candidate_commit_sha=attestation.candidate_commit_sha,
        )

    with pytest.raises(ValidationError, match="derived graduated report"):
        _attestation(_graduation_report(missing_approval=True))

    values = {
        field_name: getattr(attestation, field_name)
        for field_name in GraduationAttestation.model_fields
        if field_name not in {"graduation_attestation_id", "content_sha256"}
    }
    with pytest.raises(ValidationError, match="candidate commit"):
        GraduationAttestation.model_validate(
            {
                **values,
                "candidate_commit_sha": "0" * 40,
            }
        )
    with pytest.raises(ValidationError, match="signed_payload_sha256"):
        GraduationAttestation.model_validate(
            {
                **values,
                "signed_payload_sha256": "0" * 64,
            }
        )
    with pytest.raises(ValidationError, match="Extra inputs"):
        GraduationAttestation.model_validate(
            {
                **values,
                "ready": True,
            }
        )
