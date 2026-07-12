import json
import os
import re
import runpy
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from functools import cache
from pathlib import Path
from typing import Any

import psycopg
import pytest
from data_engine.config import settings
from data_engine.contract_repository import (
    ContractConflictError,
    ContractIntegrityError,
    ContractKind,
    ContractKindMismatchError,
    PostgresCaptureEvaluationRepository,
    PostgresCaptureManifestRepository,
    PostgresCaptureScopeRepository,
    PostgresGraduationAttestationRepository,
    PostgresRegistrySnapshotRepository,
    PostgresReleaseManifestRepository,
    PostgresResearchCatalogRepository,
    PostgresSnapshotRepository,
    PostgresStrategyUsageAuditRepository,
    PostgresTraceBundleRepository,
)
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ValidationError
from truealpha_contracts.capture_contracts import (
    ApplicabilityMapping,
    CaptureEvaluationReport,
    CaptureManifest,
    CaptureRequirement,
    CaptureScope,
    SourceCoverageMapping,
    canonical_applicability_projection_sha256,
    canonical_source_coverage_projection_sha256,
    evaluate_capture_manifest,
)
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
from truealpha_contracts.execution import (
    FactorInvocationTemplate,
    FactorKind,
    NormalizedRecordRef,
    PolicyBinding,
    PolicyRole,
    SemanticDraft,
    SemanticProducerKind,
    SnapshotCellSelection,
    SnapshotDemandCell,
    SnapshotManifest,
    SnapshotRequest,
    TraceBundle,
    TraceEdge,
    TraceNode,
    TraceNodeKind,
)
from truealpha_contracts.gates import GraduationAttestation
from truealpha_contracts.registries import (
    RegistrySnapshot,
    SemanticTypeRegistryEntry,
    SourceRegistryEntry,
)
from truealpha_contracts.release import ArtifactRole, ReleaseArtifact, ReleaseManifest
from truealpha_contracts.universe import (
    SubjectKind,
    SubjectRef,
    UniverseDefinitionKind,
    UniverseManifest,
    UniverseMembership,
    UniverseRef,
)
from truealpha_contracts.usage import (
    PlannedDemandCell,
    RequirementLevel,
    ReverseLineageEdge,
    StrategyUsageAudit,
    UsageStage,
    build_strategy_usage_audit,
)

NOW = datetime(2026, 7, 12, tzinfo=UTC)
SUBJECT = SubjectRef(kind=SubjectKind.ISSUER, id="issuer.example")
TEMP_TABLE = "gate0_contract_objects"


def _sha(character: str) -> str:
    return character * 64


def _registry() -> RegistrySnapshot:
    semantic_type = SemanticTypeRegistryEntry(
        semantic_type_id="semantic.financial-fact",
        version="1.0.0",
        domain=DataDomain.FINANCIAL_FACTS,
        schema_version="1",
        schema_fingerprint_sha256=_sha("a"),
        normalized_model_key="contracts:FinancialFact",
        input_model_key="factors:FinancialFactInput",
        repository_key="repositories:FinancialFact",
        projector_key="projectors:FinancialFact",
        compatibility_sha256=_sha("b"),
        model_implementation_sha256=_sha("c"),
        repository_implementation_sha256=_sha("d"),
        projector_implementation_sha256=_sha("e"),
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
        configuration_schema_sha256=_sha("1"),
        mapping_schema_sha256=_sha("2"),
        adapter_implementation_sha256=_sha("3"),
        normalizer_implementation_sha256=_sha("4"),
    )
    return RegistrySnapshot(
        sources=(source,),
        semantic_types=(semantic_type,),
        required_type_ids=(semantic_type.semantic_type_id,),
    )


def _universe_ref() -> UniverseRef:
    return UniverseRef(
        universe_id="universe.topt",
        universe_version="2026.07.12",
        content_sha256=_sha("5"),
    )


def _approval(seed: str, approved_at: datetime) -> ProductOwnerApproval:
    approval_hash = _sha(seed)
    return ProductOwnerApproval(
        approved_by="test-product-owner",
        approval_record_id=f"approval-record:{approval_hash}",
        approval_record_sha256=approval_hash,
        approved_at=approved_at,
    )


def _catalog() -> ResearchCatalogManifest:
    universe = _universe_ref()
    parameter_hash = canonical_sha256({})
    template = FactorInvocationTemplate(
        factor_id="factor.gppe",
        factor_version="1.0.0",
        factor_implementation_sha256=_sha("6"),
        factor_kind=FactorKind.BASE,
        parameter_model_key="catalog:InvocationParameters",
        parameter_schema_sha256=_sha("7"),
        canonical_parameters_sha256=parameter_hash,
        data_requirement_ids=(f"data-requirement:{_sha('8')}",),
    )
    question = CanonicalQuestion(
        question_key="question.gppe",
        tool_kind=CatalogTargetKind.FACTOR,
        catalog_aliases=("gppe",),
        subject_scope=(SUBJECT,),
        requirement_level=CatalogRequirementLevel.REQUIRED,
        expected_output_type_ids=("output.factor.v1",),
        expected_statuses=(ExpectedOutputStatus.AVAILABLE,),
        prompt_examples=("Run GPPE for the approved issuer.",),
        approved_at=NOW,
    )
    entry = ResearchCatalogEntry(
        catalog_alias="gppe",
        requirement_level=CatalogRequirementLevel.REQUIRED,
        target=FactorCatalogTarget(
            factor_id=template.factor_id,
            factor_version=template.factor_version,
            definition_sha256=template.factor_implementation_sha256,
        ),
        universe=universe,
        subject_scope=(SUBJECT,),
        invocation_template=InvocationTemplateSelector(
            target_kind=CatalogTargetKind.FACTOR,
            factor_template=template,
            frozen_at=NOW,
        ),
        applicability_policy_id=f"applicability-policy:{_sha('9')}",
        applicability_policy_sha256=_sha("9"),
        slo_policy_id=f"slo-policy:{_sha('a')}",
        slo_policy_sha256=_sha("a"),
        canonical_question_ids=(question.canonical_question_id,),
        expected_output_type_ids=("output.factor.v1",),
        approved_at=NOW,
    )
    floor = ResearchScopeFloor(
        universe=universe,
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
        approval=_approval("b", NOW),
    )
    return ResearchCatalogManifest(
        catalog_version="1.0.0",
        vision_sha256=_sha("c"),
        scope_floor=floor,
        entries=(entry,),
        canonical_questions=(question,),
        catalog_approval=_approval("d", NOW + timedelta(minutes=30)),
        created_at=NOW + timedelta(hours=1),
        effective_at=NOW + timedelta(hours=2),
    )


def _universe_manifest() -> UniverseManifest:
    return UniverseManifest.create(
        universe_id="universe.topt",
        universe_version="2026.07.12",
        definition_kind=UniverseDefinitionKind.FIXED_COHORT,
        effective_at=NOW - timedelta(days=30),
        owner="research-owner",
        membership_ids=("membership:issuer.example",),
    )


def _capture_requirement() -> CaptureRequirement:
    return CaptureRequirement(
        semantic_type_id="semantic.financial-fact",
        semantic_type_version="1.0.0",
        domain=DataDomain.FINANCIAL_FACTS,
        required_fields=("value",),
        subject_kinds=(SubjectKind.ISSUER,),
        cadence=timedelta(days=1),
        partition_rule_id="partition.fiscal-year",
        freshness_policy_id="freshness.daily",
        maximum_age=timedelta(days=7),
        quality_policy_ids=("quality.financial-fact",),
    )


def _snapshot() -> SnapshotManifest:
    registry = _registry()
    universe = _universe_manifest()
    membership = UniverseMembership(
        membership_id="membership:issuer.example",
        universe_id=universe.ref.universe_id,
        subject=SUBJECT,
        valid_from=date(2025, 1, 1),
        knowable_at=NOW - timedelta(days=20),
        recorded_at=NOW - timedelta(days=19),
        confidence=Decimal("1"),
        raw_ref="raw:universe",
    )
    demand = SnapshotDemandCell(
        requirement_id=f"data-requirement:{_sha('8')}",
        capture_requirement_id=_capture_requirement().capture_requirement_id,
        semantic_type_id="semantic.financial-fact",
        semantic_type_version="1.0.0",
        domain=DataDomain.FINANCIAL_FACTS,
        subject=SUBJECT,
        partition_key="FY2025",
        level=RequirementLevel.REQUIRED,
    )
    request = SnapshotRequest(
        universe=universe.ref,
        as_of=NOW,
        valid_on=date(2025, 12, 31),
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
                implementation_sha256=_sha("e"),
            )
            for role in PolicyRole
        ),
        demand_cells=(demand,),
    )
    draft = SemanticDraft(
        semantic_type_id="semantic.financial-fact",
        semantic_type_version="1.0.0",
        payload_model_key="contracts:FinancialFact",
        payload_schema_sha256=_sha("a"),
        payload_sha256=_sha("f"),
        subject=SUBJECT,
        valid_from=date(2025, 1, 1),
        valid_to=date(2025, 12, 31),
        knowable_at=NOW - timedelta(days=5),
        produced_at=NOW - timedelta(days=4),
        producer_kind=SemanticProducerKind.DETERMINISTIC_NORMALIZER,
        producer_id="normalizer.sec",
        producer_version="1.0.0",
        producer_implementation_sha256=_sha("4"),
    )
    source = registry.sources[0]
    record = NormalizedRecordRef(
        draft=draft,
        document_id="document:sec-10k",
        raw_object_id="raw-object:sec-10k",
        raw_object_sha256=_sha("1"),
        source_registry_entry_id=source.source_registry_entry_id,
        source_registry_entry_sha256=source.content_sha256,
        mapping_version="1.0.0",
        mapping_implementation_sha256=_sha("2"),
        recorded_at=NOW - timedelta(days=3),
        confidence=Decimal("0.8"),
    )
    return SnapshotManifest(
        request=request,
        registry_snapshot=registry,
        resolved_subjects=(SUBJECT,),
        universe_manifest=universe,
        universe_memberships=(membership,),
        normalized_records=(record,),
        selections=(
            SnapshotCellSelection(
                demand=demand,
                normalized_record_ids=(record.normalized_record_id,),
            ),
        ),
        resolved_at=NOW + timedelta(seconds=1),
        resolver_id="snapshot-resolver",
        resolver_version="1.0.0",
        resolver_implementation_sha256=_sha("3"),
    )


def _release() -> ReleaseManifest:
    catalog = _catalog()
    migration_ids = ("0001.sql", "0002.sql")
    artifacts = tuple(
        ReleaseArtifact(
            role=role,
            image_or_bundle=f"ghcr.io/example/{role.value}@sha256:{_sha('4')}",
            digest=f"sha256:{_sha('4')}",
            git_sha=_sha("5")[:40],
            sbom_sha256=_sha("6"),
            signature_ref=f"sigstore:{role.value}",
        )
        for role in ArtifactRole
    )
    return ReleaseManifest(
        contract_version="contracts:v1",
        mart_schema_version="mart:v1",
        research_catalog_id=catalog.research_catalog_id,
        research_catalog_sha256=catalog.content_sha256,
        universe=catalog.scope_floor.universe,
        capture_scope_id=f"capture-scope:{_sha('7')}",
        capture_scope_sha256=_sha("7"),
        applicability_catalog_id=f"applicability:{_sha('8')}",
        applicability_catalog_sha256=_sha("8"),
        source_coverage_catalog_id=f"source-coverage:{_sha('9')}",
        source_coverage_catalog_sha256=_sha("9"),
        source_readiness_report_id=f"source-readiness:{_sha('a')}",
        source_readiness_report_sha256=_sha("a"),
        slo_catalog_id=f"module-slo:{_sha('b')}",
        slo_catalog_sha256=_sha("b"),
        consumer_slo_catalog_id=f"consumer-slo:{_sha('3')}",
        consumer_slo_catalog_sha256=_sha("3"),
        usage_telemetry_slo_catalog_id=f"usage-telemetry-slo:{_sha('4')}",
        usage_telemetry_slo_catalog_sha256=_sha("4"),
        registry_snapshot_id=f"registry-snapshot:{_sha('5')}",
        registry_snapshot_sha256=_sha("5"),
        source_registry_id=f"source-registry:{_sha('c')}",
        source_registry_sha256=_sha("c"),
        semantic_type_registry_id=f"semantic-type-registry:{_sha('d')}",
        semantic_type_registry_sha256=_sha("d"),
        identifier_type_registry_id=f"identifier-type-registry:{_sha('f')}",
        identifier_type_registry_sha256=_sha("f"),
        configuration_sha256={"dagster": _sha("e")},
        migration_ids=migration_ids,
        migration_set_sha256=canonical_sha256(migration_ids),
        artifacts=artifacts,
        natural_refresh_requirement_ids=(f"natural-refresh:{_sha('7')}",),
        created_at=NOW,
        manifest_signature_ref="sigstore:release",
    )


def _capture_applicability(requirement: CaptureRequirement) -> ApplicabilityMapping:
    return {
        (
            SUBJECT.kind,
            SUBJECT.id,
            requirement.domain,
            "fy2025",
            requirement.capture_requirement_id,
        ): ("required", NOW - timedelta(hours=1))
    }


def _capture_source_coverage(requirement: CaptureRequirement) -> SourceCoverageMapping:
    return {
        (
            CaptureEnvironment.STAGING,
            SUBJECT.kind,
            SUBJECT.id,
            requirement.domain,
            "fy2025",
            requirement.capture_requirement_id,
        ): (f"source-coverage-entry:{_sha('0')}",)
    }


def _capture_scope() -> CaptureScope:
    catalog = _catalog()
    registry = _registry()
    requirement = _capture_requirement()
    applicability = _capture_applicability(requirement)
    source_coverage = _capture_source_coverage(requirement)
    return CaptureScope(
        research_catalog_id=catalog.research_catalog_id,
        research_catalog_sha256=catalog.content_sha256,
        universe=catalog.scope_floor.universe,
        applicability_catalog_id=f"applicability:{_sha('8')}",
        applicability_catalog_sha256=_sha("8"),
        applicability_projection_sha256=canonical_applicability_projection_sha256(applicability),
        source_coverage_catalog_id=f"source-coverage:{_sha('9')}",
        source_coverage_catalog_sha256=_sha("9"),
        source_coverage_projection_sha256=canonical_source_coverage_projection_sha256(source_coverage),
        slo_catalog_id=f"module-slo:{_sha('b')}",
        slo_catalog_sha256=_sha("b"),
        source_registry_id=registry.source_registry_snapshot_id,
        source_registry_sha256=registry.source_registry_sha256,
        semantic_type_registry_id=registry.semantic_type_registry_snapshot_id,
        semantic_type_registry_sha256=registry.semantic_type_registry_sha256,
        requirements=(requirement,),
        effective_at=NOW - timedelta(days=1),
        owner="capture-owner",
    )


def _capture_manifest(scope: CaptureScope | None = None) -> CaptureManifest:
    scope = scope or _capture_scope()
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
        partition_key="fy2025",
        as_of=NOW,
        started_at=NOW + timedelta(minutes=1),
        cells=(),
        created_at=NOW + timedelta(minutes=2),
    )


def _capture_evaluation() -> CaptureEvaluationReport:
    scope = _capture_scope()
    manifest = _capture_manifest(scope)
    requirement = scope.requirements[0]
    return evaluate_capture_manifest(
        scope,
        manifest,
        applicability_catalog_id=scope.applicability_catalog_id,
        applicability_catalog_sha256=scope.applicability_catalog_sha256,
        applicability=_capture_applicability(requirement),
        source_coverage=_capture_source_coverage(requirement),
        evaluated_at=manifest.created_at + timedelta(minutes=1),
    )


def _trace_bundle() -> TraceBundle:
    nodes = (
        TraceNode(node_id="strategy-run:repository", kind=TraceNodeKind.STRATEGY_RUN, content_sha256=_sha("1")),
        TraceNode(
            node_id="factor-execution:repository",
            kind=TraceNodeKind.FACTOR_EXECUTION,
            content_sha256=_sha("2"),
        ),
        TraceNode(node_id="snapshot:repository", kind=TraceNodeKind.SNAPSHOT, content_sha256=_sha("3")),
        TraceNode(
            node_id="normalized-record:repository",
            kind=TraceNodeKind.NORMALIZED_RECORD,
            content_sha256=_sha("4"),
        ),
        TraceNode(node_id="raw-object:repository", kind=TraceNodeKind.RAW_OBJECT, content_sha256=_sha("5")),
        TraceNode(
            node_id="quality-evidence:repository",
            kind=TraceNodeKind.QUALITY_EVIDENCE,
            content_sha256=_sha("6"),
        ),
    )
    edges = tuple(
        TraceEdge(downstream_id=downstream, upstream_id=upstream, relation="depends_on")
        for downstream, upstream in (
            ("strategy-run:repository", "factor-execution:repository"),
            ("factor-execution:repository", "snapshot:repository"),
            ("snapshot:repository", "normalized-record:repository"),
            ("normalized-record:repository", "raw-object:repository"),
            ("raw-object:repository", "quality-evidence:repository"),
        )
    )
    return TraceBundle(
        root_node_id="strategy-run:repository",
        nodes=nodes,
        edges=edges,
        built_by="trace.builder",
        builder_version="1.0.0",
        builder_implementation_sha256=_sha("7"),
        built_at=NOW,
    )


def _strategy_usage_audit() -> StrategyUsageAudit:
    catalog = _catalog()
    release = _release()
    registry = _registry()
    capture_requirement = _capture_requirement()
    planned_cell = PlannedDemandCell(
        requirement_id=f"data-requirement:{_sha('8')}",
        capture_requirement_id=capture_requirement.capture_requirement_id,
        semantic_type_id=capture_requirement.semantic_type_id,
        domain=capture_requirement.domain,
        subject=SUBJECT,
        partition_key="fy2025",
        level=RequirementLevel.REQUIRED,
        expected_stages=frozenset({UsageStage.FACTOR_CONSUMPTION}),
    )
    return build_strategy_usage_audit(
        strategy_run_id="strategy-run:repository",
        planned_cells=(planned_cell,),
        events=(),
        trace_bundle_ids=(_trace_bundle().trace_bundle_id,),
        reverse_lineage=(
            ReverseLineageEdge(
                downstream_id="strategy-run:repository",
                upstream_id="decision:repository",
                relation="produced",
            ),
        ),
        affected_decision_ids=("decision:repository",),
        research_catalog_id=catalog.research_catalog_id,
        research_catalog_sha256=catalog.content_sha256,
        universe=catalog.scope_floor.universe,
        applicability_catalog_id=f"applicability:{_sha('8')}",
        applicability_catalog_sha256=_sha("8"),
        slo_catalog_id=f"module-slo:{_sha('b')}",
        slo_catalog_sha256=_sha("b"),
        release_manifest_id=release.release_manifest_id,
        registry_snapshot=registry,
        run_started_at=NOW,
        run_completed_at=NOW + timedelta(minutes=1),
        audited_at=NOW + timedelta(minutes=2),
        auditor_id="usage.auditor",
        auditor_version="1.0.0",
        auditor_implementation_sha256=_sha("c"),
    )


@cache
def _graduation_attestation() -> GraduationAttestation:
    # Reuse the canonical contract fixture so this remains a fully derived,
    # independently approved graduation rather than a hand-built green flag.
    fixture_module = runpy.run_path(str(Path(__file__).parents[3] / "libs/contracts/tests/test_gates.py"))
    attestation = fixture_module["_attestation"](fixture_module["_graduation_report"]())
    assert isinstance(attestation, GraduationAttestation)
    return attestation


@dataclass(frozen=True)
class _RepositoryCase:
    name: str
    kind: ContractKind
    model_type: type[BaseModel]
    id_field: str
    hash_field: str
    id_prefix: str
    contract_factory: Callable[[], BaseModel]
    postgres_repository_type: type


CASES = (
    _RepositoryCase(
        name="registry",
        kind=ContractKind.REGISTRY_SNAPSHOT,
        model_type=RegistrySnapshot,
        id_field="registry_snapshot_id",
        hash_field="content_sha256",
        id_prefix="registry-snapshot",
        contract_factory=_registry,
        postgres_repository_type=PostgresRegistrySnapshotRepository,
    ),
    _RepositoryCase(
        name="catalog",
        kind=ContractKind.RESEARCH_CATALOG,
        model_type=ResearchCatalogManifest,
        id_field="research_catalog_id",
        hash_field="content_sha256",
        id_prefix="research-catalog",
        contract_factory=_catalog,
        postgres_repository_type=PostgresResearchCatalogRepository,
    ),
    _RepositoryCase(
        name="snapshot",
        kind=ContractKind.SNAPSHOT_MANIFEST,
        model_type=SnapshotManifest,
        id_field="snapshot_id",
        hash_field="content_sha256",
        id_prefix="snapshot",
        contract_factory=_snapshot,
        postgres_repository_type=PostgresSnapshotRepository,
    ),
    _RepositoryCase(
        name="release",
        kind=ContractKind.RELEASE_MANIFEST,
        model_type=ReleaseManifest,
        id_field="release_manifest_id",
        hash_field="manifest_sha256",
        id_prefix="release-manifest",
        contract_factory=_release,
        postgres_repository_type=PostgresReleaseManifestRepository,
    ),
    _RepositoryCase(
        name="capture-scope",
        kind=ContractKind.CAPTURE_SCOPE,
        model_type=CaptureScope,
        id_field="capture_scope_id",
        hash_field="content_sha256",
        id_prefix="capture-scope",
        contract_factory=_capture_scope,
        postgres_repository_type=PostgresCaptureScopeRepository,
    ),
    _RepositoryCase(
        name="capture-manifest",
        kind=ContractKind.CAPTURE_MANIFEST,
        model_type=CaptureManifest,
        id_field="capture_manifest_id",
        hash_field="content_sha256",
        id_prefix="capture-manifest",
        contract_factory=_capture_manifest,
        postgres_repository_type=PostgresCaptureManifestRepository,
    ),
    _RepositoryCase(
        name="capture-evaluation",
        kind=ContractKind.CAPTURE_EVALUATION_REPORT,
        model_type=CaptureEvaluationReport,
        id_field="capture_evaluation_report_id",
        hash_field="content_sha256",
        id_prefix="capture-evaluation",
        contract_factory=_capture_evaluation,
        postgres_repository_type=PostgresCaptureEvaluationRepository,
    ),
    _RepositoryCase(
        name="trace-bundle",
        kind=ContractKind.TRACE_BUNDLE,
        model_type=TraceBundle,
        id_field="trace_bundle_id",
        hash_field="content_sha256",
        id_prefix="trace-bundle",
        contract_factory=_trace_bundle,
        postgres_repository_type=PostgresTraceBundleRepository,
    ),
    _RepositoryCase(
        name="strategy-usage-audit",
        kind=ContractKind.STRATEGY_USAGE_AUDIT,
        model_type=StrategyUsageAudit,
        id_field="strategy_usage_audit_id",
        hash_field="content_sha256",
        id_prefix="strategy-usage-audit",
        contract_factory=_strategy_usage_audit,
        postgres_repository_type=PostgresStrategyUsageAuditRepository,
    ),
    _RepositoryCase(
        name="graduation-attestation",
        kind=ContractKind.GRADUATION_ATTESTATION,
        model_type=GraduationAttestation,
        id_field="graduation_attestation_id",
        hash_field="content_sha256",
        id_prefix="graduation-attestation",
        contract_factory=_graduation_attestation,
        postgres_repository_type=PostgresGraduationAttestationRepository,
    ),
)


@dataclass(frozen=True)
class _MemoryRow:
    kind: str
    content_sha256: str
    payload: dict[str, Any]


class _MemoryContractRepository:
    """Fixture adapter kept behaviorally aligned with the Postgres port."""

    def __init__(self, case: _RepositoryCase) -> None:
        self.case = case
        self.rows: dict[str, _MemoryRow] = {}
        self.id_pattern = re.compile(rf"^{re.escape(case.id_prefix)}:[0-9a-f]{{64}}$")

    def put(self, contract: BaseModel) -> bool:
        if not isinstance(contract, self.case.model_type):
            raise ContractKindMismatchError
        validated, payload = self._validate(contract.model_dump(mode="json", exclude_computed_fields=True))
        contract_id, content_sha256 = self._identity(validated)
        existing = self.rows.get(contract_id)
        if existing is None:
            self.rows[contract_id] = _MemoryRow(
                kind=self.case.kind.value,
                content_sha256=content_sha256,
                payload=json.loads(json.dumps(payload)),
            )
            return True
        self._require_kind(existing.kind)
        if existing.content_sha256 != content_sha256 or existing.payload != payload:
            raise ContractConflictError
        self._validate_stored(contract_id, existing)
        return False

    def get(self, contract_id: str) -> BaseModel | None:
        self._require_id(contract_id)
        row = self.rows.get(contract_id)
        if row is None:
            return None
        self._require_kind(row.kind)
        return self._validate_stored(contract_id, row)

    def force_row(
        self,
        *,
        contract_id: str,
        kind: str,
        content_sha256: str,
        payload: dict[str, Any],
    ) -> None:
        self.rows[contract_id] = _MemoryRow(kind, content_sha256, json.loads(json.dumps(payload)))

    def _validate_stored(self, contract_id: str, row: _MemoryRow) -> BaseModel:
        validated, canonical_payload = self._validate(row.payload)
        validated_id, validated_hash = self._identity(validated)
        if canonical_payload != row.payload or validated_id != contract_id or validated_hash != row.content_sha256:
            raise ContractIntegrityError
        return validated

    def _validate(self, payload: dict[str, Any]) -> tuple[BaseModel, dict[str, Any]]:
        try:
            encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
            validated = self.case.model_type.model_validate_json(encoded)
        except (TypeError, ValueError, ValidationError) as error:
            raise ContractIntegrityError from error
        return validated, validated.model_dump(mode="json", exclude_computed_fields=True)

    def _identity(self, contract: BaseModel) -> tuple[str, str]:
        contract_id = getattr(contract, self.case.id_field)
        content_sha256 = getattr(contract, self.case.hash_field)
        self._require_id(contract_id)
        if contract_id != f"{self.case.id_prefix}:{content_sha256}":
            raise ContractIntegrityError
        return contract_id, content_sha256

    def _require_id(self, contract_id: str) -> None:
        if not self.id_pattern.fullmatch(contract_id):
            raise ContractKindMismatchError

    def _require_kind(self, kind: str) -> None:
        if kind != self.case.kind.value:
            raise ContractKindMismatchError


@dataclass(frozen=True)
class _Backend:
    name: str
    connection: Any | None


@pytest.fixture
def postgres_conn():
    try:
        connection = psycopg.connect(settings.database_url, connect_timeout=3)
    except psycopg.OperationalError as error:
        runtime_required = os.environ.get("TRUEALPHA_REQUIRE_RUNTIME", "").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if os.environ.get("DATABASE_URL") or runtime_required:
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; in-memory repository parity tests still run")

    connection.execute(
        f"""
        create temporary table {TEMP_TABLE} (
            contract_id text primary key,
            contract_kind text not null,
            content_sha256 text not null,
            payload jsonb not null
        ) on commit drop
        """
    )
    yield connection
    connection.rollback()
    connection.close()


@pytest.fixture(params=("memory", "postgres"))
def backend(request) -> _Backend:
    if request.param == "memory":
        return _Backend(name="memory", connection=None)
    return _Backend(name="postgres", connection=request.getfixturevalue("postgres_conn"))


def _repository(case: _RepositoryCase, backend: _Backend):
    if backend.connection is None:
        return _MemoryContractRepository(case)
    return case.postgres_repository_type(
        backend.connection,
        schema=None,
        table=TEMP_TABLE,
    )


def _force_row(
    repository,
    backend: _Backend,
    *,
    contract_id: str,
    kind: str,
    content_sha256: str,
    payload: dict[str, Any],
) -> None:
    if isinstance(repository, _MemoryContractRepository):
        repository.force_row(
            contract_id=contract_id,
            kind=kind,
            content_sha256=content_sha256,
            payload=payload,
        )
        return
    assert backend.connection is not None
    backend.connection.execute(
        f"""
        insert into {TEMP_TABLE} (contract_id, contract_kind, content_sha256, payload)
        values (%s, %s, %s, %s)
        """,
        (contract_id, kind, content_sha256, Jsonb(payload)),
    )


def test_repository_case_matrix_covers_every_durable_contract_kind():
    assert {case.kind for case in CASES} == set(ContractKind)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_all_typed_repositories_roundtrip_and_duplicate_idempotency(case, backend):
    repository = _repository(case, backend)
    contract = case.contract_factory()
    contract_id = getattr(contract, case.id_field)

    assert repository.put(contract)
    assert not repository.put(contract)
    restored = repository.get(contract_id)

    assert type(restored) is case.model_type
    assert restored == contract


def test_unknown_get_and_cross_kind_calls_are_rejected_consistently(backend):
    registry_case, catalog_case = CASES[:2]
    repository = _repository(registry_case, backend)
    catalog = catalog_case.contract_factory()

    assert repository.get(f"registry-snapshot:{_sha('0')}") is None
    with pytest.raises(ContractKindMismatchError):
        repository.get(getattr(catalog, catalog_case.id_field))
    with pytest.raises(ContractKindMismatchError):
        repository.put(catalog)


def test_same_id_different_content_conflicts_and_tamper_is_not_readable(backend):
    case = CASES[0]
    repository = _repository(case, backend)
    contract = case.contract_factory()
    contract_id = getattr(contract, case.id_field)
    content_sha256 = getattr(contract, case.hash_field)
    tampered = contract.model_dump(mode="json", exclude_computed_fields=True)
    tampered["required_type_ids"] = []
    _force_row(
        repository,
        backend,
        contract_id=contract_id,
        kind=case.kind.value,
        content_sha256=content_sha256,
        payload=tampered,
    )

    with pytest.raises(ContractConflictError):
        repository.put(contract)
    with pytest.raises(ContractIntegrityError):
        repository.get(contract_id)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_stored_content_hash_tamper_is_rejected_for_every_kind(case, backend):
    repository = _repository(case, backend)
    contract = case.contract_factory()
    contract_id = getattr(contract, case.id_field)
    content_sha256 = getattr(contract, case.hash_field)
    tampered_hash = _sha("0") if content_sha256 != _sha("0") else _sha("1")
    _force_row(
        repository,
        backend,
        contract_id=contract_id,
        kind=case.kind.value,
        content_sha256=tampered_hash,
        payload=contract.model_dump(mode="json", exclude_computed_fields=True),
    )

    with pytest.raises(ContractIntegrityError):
        repository.get(contract_id)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_stored_cross_kind_row_is_rejected_for_every_kind(case, backend):
    repository = _repository(case, backend)
    contract = case.contract_factory()
    contract_id = getattr(contract, case.id_field)
    wrong_kind = next(kind for kind in ContractKind if kind is not case.kind)
    _force_row(
        repository,
        backend,
        contract_id=contract_id,
        kind=wrong_kind.value,
        content_sha256=getattr(contract, case.hash_field),
        payload=contract.model_dump(mode="json", exclude_computed_fields=True),
    )

    with pytest.raises(ContractKindMismatchError):
        repository.get(contract_id)


def test_release_and_graduation_evidence_are_separate_linked_objects(backend):
    release_case = next(case for case in CASES if case.kind is ContractKind.RELEASE_MANIFEST)
    graduation_case = next(case for case in CASES if case.kind is ContractKind.GRADUATION_ATTESTATION)
    release_repository = _repository(release_case, backend)
    graduation_repository = _repository(graduation_case, backend)
    attestation = _graduation_attestation()
    release = attestation.graduation_report.evidence.release_manifest

    assert release_repository.put(release)
    assert graduation_repository.put(attestation)
    assert attestation.release_manifest_id == release.release_manifest_id
    assert (
        graduation_repository.get(
            attestation.graduation_attestation_id
        ).graduation_report.evidence.release_manifest.release_manifest_id
        == release.release_manifest_id
    )

    with pytest.raises(ContractKindMismatchError):
        release_repository.put(attestation)
    with pytest.raises(ContractKindMismatchError):
        graduation_repository.put(release)
