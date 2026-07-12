"""Generate and verify Issue #58 Python/TypeScript conformance artifacts."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from truealpha_contracts.capture_contracts import (
    CaptureCell,
    CaptureManifest,
    CaptureRecordEvidence,
    CaptureRequirement,
    CaptureScope,
)
from truealpha_contracts.common import CaptureEnvironment, canonical_sha256
from truealpha_contracts.data_quality import DataDomain, QualityStatus
from truealpha_contracts.execution import (
    ExtractionInvocation,
    ExtractionTemplate,
    ModelRevisionRef,
    NormalizedRecordRef,
    PolicyBinding,
    PolicyRole,
    SemanticDraft,
    SemanticProducerKind,
    SnapshotCellSelection,
    SnapshotDemandCell,
    SnapshotManifest,
    SnapshotRequest,
    validate_extraction_replay,
)
from truealpha_contracts.registries import (
    IdentifierNamespaceKind,
    IdentifierTypeRegistryEntry,
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
)
from truealpha_contracts.usage import RequirementLevel

CONTRACT_SET = "truealpha.issue-58.exact-contracts.v1"
OUTPUT_DIR = Path(__file__).resolve().parent
SCHEMA_PATH = OUTPUT_DIR / "issue58.schemas.json"
FIXTURE_PATH = OUTPUT_DIR / "issue58.fixtures.json"
NOW = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)
SUBJECT = SubjectRef(kind=SubjectKind.ISSUER, id="issuer.example")


def _hash(character: str) -> str:
    return character * 64


def _registry() -> RegistrySnapshot:
    semantic_type = SemanticTypeRegistryEntry(
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
    identifier_type = IdentifierTypeRegistryEntry(
        identifier_type_id="identifier.entity.legal-entity-id",
        version="1.0.0",
        namespace_kind=IdentifierNamespaceKind.ENTITY,
        semantic_definition_sha256=_hash("5"),
        schema_version="1.0.0",
        schema_fingerprint_sha256=_hash("6"),
        compatibility_sha256=_hash("7"),
        validator_implementation_sha256=_hash("8"),
        canonicalizer_implementation_sha256=_hash("9"),
    )
    return RegistrySnapshot(
        sources=(source,),
        semantic_types=(semantic_type,),
        identifier_types=(identifier_type,),
        required_type_ids=(semantic_type.semantic_type_id,),
        required_identifier_type_ids=(identifier_type.identifier_type_id,),
    )


def _universe() -> UniverseManifest:
    return UniverseManifest.create(
        universe_id="universe.topt-core",
        universe_version="2026-07-12",
        definition_kind=UniverseDefinitionKind.FIXED_COHORT,
        effective_at=NOW - timedelta(days=30),
        owner="research-owner",
        membership_ids=("membership:issuer.example",),
    )


def _extraction_chain() -> tuple[ModelRevisionRef, ExtractionTemplate, ExtractionInvocation]:
    model_revision = ModelRevisionRef(
        provider="provider.example",
        model_id="extractor.financial-fact",
        immutable_revision="2026-07-01",
        endpoint_or_artifact_sha256=_hash("1"),
        decoding_parameters_sha256=_hash("2"),
    )
    template = ExtractionTemplate(
        template_name="financial-fact",
        template_version="1.0.0",
        semantic_type_id="semantic.financial-fact",
        semantic_type_version="1.0.0",
        payload_model_key="contracts:FinancialFact",
        output_schema_sha256=_hash("a"),
        instructions_sha256=_hash("3"),
        extractor_implementation_sha256=_hash("4"),
        model_revision_id=model_revision.model_revision_id,
        model_revision_sha256=model_revision.content_sha256,
    )
    invocation = ExtractionInvocation(
        model_revision_id=model_revision.model_revision_id,
        model_revision_sha256=model_revision.content_sha256,
        extraction_template_id=template.extraction_template_id,
        extraction_template_sha256=template.content_sha256,
        input_sha256=_hash("5"),
        response_sha256=_hash("6"),
        semantic_payload_sha256=_hash("7"),
        attempt_number=1,
        started_at=NOW - timedelta(days=4, minutes=1),
        completed_at=NOW - timedelta(days=4),
        invoker_id="extractor.runner",
        invoker_version="1.0.0",
        invoker_implementation_sha256=template.extractor_implementation_sha256,
    )
    return model_revision, template, invocation


def _membership(universe: UniverseManifest) -> UniverseMembership:
    return UniverseMembership(
        membership_id=universe.membership_ids[0],
        universe_id=universe.ref.universe_id,
        subject=SUBJECT,
        valid_from=date(2025, 1, 1),
        knowable_at=NOW - timedelta(days=20),
        recorded_at=NOW - timedelta(days=19),
        confidence=Decimal("1"),
        raw_ref="raw:universe-membership",
    )


def _capture_scope(registry: RegistrySnapshot, universe: UniverseManifest) -> CaptureScope:
    requirement = CaptureRequirement(
        semantic_type_id=registry.semantic_types[0].semantic_type_id,
        semantic_type_version=registry.semantic_types[0].version,
        domain=DataDomain.FINANCIAL_FACTS,
        required_fields=("gross_profit", "revenue"),
        subject_kinds=(SubjectKind.ISSUER,),
        cadence=timedelta(days=1),
        partition_rule_id="partition.fiscal:v1",
        freshness_policy_id="freshness.daily:v1",
        maximum_age=timedelta(days=2),
        quality_policy_ids=("quality.non-null:v1", "quality.numeric:v1"),
    )
    return CaptureScope(
        research_catalog_id="research-catalog:" + _hash("a"),
        research_catalog_sha256=_hash("a"),
        universe=universe.ref,
        applicability_catalog_id="applicability:" + _hash("b"),
        applicability_catalog_sha256=_hash("b"),
        applicability_projection_sha256=_hash("c"),
        source_coverage_catalog_id="source-coverage:" + _hash("d"),
        source_coverage_catalog_sha256=_hash("d"),
        source_coverage_projection_sha256=_hash("e"),
        slo_catalog_id="module-slo:" + _hash("f"),
        slo_catalog_sha256=_hash("f"),
        source_registry_id=registry.source_registry_snapshot_id,
        source_registry_sha256=registry.source_registry_sha256,
        semantic_type_registry_id=registry.semantic_type_registry_snapshot_id,
        semantic_type_registry_sha256=registry.semantic_type_registry_sha256,
        requirements=(requirement,),
        effective_at=NOW - timedelta(days=20),
        owner="data-platform",
    )


def _capture_manifest(scope: CaptureScope, registry: RegistrySnapshot) -> CaptureManifest:
    requirement = scope.requirements[0]
    evidence = CaptureRecordEvidence(
        source_coverage_entry_id="source-coverage-entry:" + _hash("1"),
        raw_id="raw.fetches:financial-fact-2025",
        raw_sha256=_hash("2"),
        normalized_id="staging.financial-facts:issuer-example-2025",
        semantic_type_id=requirement.semantic_type_id,
        semantic_type_version=requirement.semantic_type_version,
        populated_fields=requirement.required_fields,
        knowable_at=NOW - timedelta(hours=2),
        recorded_at=NOW + timedelta(minutes=1),
        valid_from=NOW - timedelta(days=365),
        valid_to=NOW,
        confidence=Decimal("0.98"),
        mapping_version="sec-companyfacts:v1",
        policy_versions={"freshness.daily:v1": "v1", "partition.fiscal:v1": "v1"},
        quality_check_ids=("quality.non-null:v1", "quality.numeric:v1"),
        quality_status=QualityStatus.PASS,
        lineage_sha256=_hash("3"),
    )
    cell = CaptureCell(
        subject=SUBJECT,
        domain=requirement.domain,
        partition_key="fy2025",
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
        source_registry_id=registry.source_registry_snapshot_id,
        source_registry_sha256=registry.source_registry_sha256,
        semantic_type_registry_id=registry.semantic_type_registry_snapshot_id,
        semantic_type_registry_sha256=registry.semantic_type_registry_sha256,
        partition_key="fy2025",
        as_of=NOW,
        started_at=NOW + timedelta(minutes=1),
        cells=(cell,),
        created_at=NOW + timedelta(minutes=2),
    )


def _snapshot(
    registry: RegistrySnapshot,
    universe: UniverseManifest,
    scope: CaptureScope,
    model_revision: ModelRevisionRef,
    template: ExtractionTemplate,
    invocation: ExtractionInvocation,
) -> SnapshotManifest:
    policies = tuple(
        PolicyBinding(
            role=role,
            policy_id=f"policy.{role.value}",
            policy_version="1.0.0",
            implementation_sha256=_hash("4"),
        )
        for role in PolicyRole
    )
    demand = SnapshotDemandCell(
        requirement_id="data-requirement:" + _hash("6"),
        capture_requirement_id=scope.requirements[0].capture_requirement_id,
        semantic_type_id=registry.semantic_types[0].semantic_type_id,
        semantic_type_version=registry.semantic_types[0].version,
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
        policy_bindings=policies,
        demand_cells=(demand,),
    )
    draft = SemanticDraft(
        semantic_type_id=registry.semantic_types[0].semantic_type_id,
        semantic_type_version=registry.semantic_types[0].version,
        payload_model_key=template.payload_model_key,
        payload_schema_sha256=template.output_schema_sha256,
        payload_sha256=invocation.semantic_payload_sha256,
        subject=SUBJECT,
        valid_from=date(2025, 1, 1),
        valid_to=date(2025, 12, 31),
        knowable_at=NOW - timedelta(days=5),
        produced_at=invocation.completed_at,
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
    )
    validate_extraction_replay(
        draft=draft,
        invocation=invocation,
        template=template,
        model_revision=model_revision,
    )
    record = NormalizedRecordRef(
        draft=draft,
        document_id="document:sec-10k-2025",
        raw_object_id="raw-object:sec-10k-2025",
        raw_object_sha256=_hash("7"),
        source_registry_entry_id=registry.sources[0].source_registry_entry_id,
        source_registry_entry_sha256=registry.sources[0].content_sha256,
        mapping_version="1.0.0",
        mapping_implementation_sha256=_hash("8"),
        recorded_at=NOW - timedelta(days=3),
        confidence=Decimal("0.9"),
    )
    return SnapshotManifest(
        request=request,
        registry_snapshot=registry,
        resolved_subjects=(SUBJECT,),
        universe_manifest=universe,
        universe_memberships=(_membership(universe),),
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
        resolver_implementation_sha256=_hash("9"),
    )


def _release(
    scope: CaptureScope,
    registry: RegistrySnapshot,
    universe: UniverseManifest,
    model_revision: ModelRevisionRef,
    template: ExtractionTemplate,
) -> ReleaseManifest:
    migration_ids = ("0001_core.sql", "0002_capture.sql")
    artifacts = tuple(
        ReleaseArtifact(
            role=role,
            image_or_bundle=f"ghcr.io/truealpha/{role.value}@sha256:{_hash('1')}",
            digest="sha256:" + _hash("1"),
            git_sha="2" * 40,
            sbom_sha256=_hash("3"),
            signature_ref=f"sigstore:{role.value}:sha256:{_hash('4')}",
        )
        for role in ArtifactRole
    )
    return ReleaseManifest(
        contract_version="contracts:v1",
        mart_schema_version="mart:v1",
        research_catalog_id=scope.research_catalog_id,
        research_catalog_sha256=scope.research_catalog_sha256,
        universe=universe.ref,
        capture_scope_id=scope.capture_scope_id,
        capture_scope_sha256=scope.content_sha256,
        applicability_catalog_id=scope.applicability_catalog_id,
        applicability_catalog_sha256=scope.applicability_catalog_sha256,
        source_coverage_catalog_id=scope.source_coverage_catalog_id,
        source_coverage_catalog_sha256=scope.source_coverage_catalog_sha256,
        source_readiness_report_id="source-readiness:" + _hash("5"),
        source_readiness_report_sha256=_hash("5"),
        slo_catalog_id=scope.slo_catalog_id,
        slo_catalog_sha256=scope.slo_catalog_sha256,
        consumer_slo_catalog_id="consumer-slo:" + _hash("6"),
        consumer_slo_catalog_sha256=_hash("6"),
        usage_telemetry_slo_catalog_id="usage-telemetry-slo:" + _hash("7"),
        usage_telemetry_slo_catalog_sha256=_hash("7"),
        registry_snapshot_id=registry.registry_snapshot_id,
        registry_snapshot_sha256=registry.content_sha256,
        source_registry_id=registry.source_registry_snapshot_id,
        source_registry_sha256=registry.source_registry_sha256,
        semantic_type_registry_id=registry.semantic_type_registry_snapshot_id,
        semantic_type_registry_sha256=registry.semantic_type_registry_sha256,
        identifier_type_registry_id=registry.identifier_type_registry_snapshot_id,
        identifier_type_registry_sha256=registry.identifier_type_registry_sha256,
        configuration_sha256={"app-web": _hash("8"), "data-engine": _hash("9")},
        migration_ids=migration_ids,
        migration_set_sha256=canonical_sha256(migration_ids),
        artifacts=artifacts,
        natural_refresh_requirement_ids=("natural-refresh:" + _hash("a"),),
        approved_model_revisions=(model_revision,),
        approved_extraction_templates=(template,),
        created_at=NOW - timedelta(days=10),
        manifest_signature_ref="sigstore:release:sha256:" + _hash("b"),
    )


def _contracts() -> dict[str, BaseModel]:
    registry = _registry()
    universe = _universe()
    scope = _capture_scope(registry, universe)
    model_revision, template, invocation = _extraction_chain()
    return {
        "CaptureScope": scope,
        "CaptureManifest": _capture_manifest(scope, registry),
        "SnapshotManifest": _snapshot(registry, universe, scope, model_revision, template, invocation),
        "ReleaseManifest": _release(scope, registry, universe, model_revision, template),
    }


def _render(value: Any) -> str:
    return json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True) + "\n"


def _portable_json(value: Any) -> Any:
    """Normalize JSON numbers after parsing, matching JavaScript's number model."""

    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, list):
        return [_portable_json(item) for item in value]
    if isinstance(value, dict):
        return {key: _portable_json(item) for key, item in value.items()}
    return value


def build_artifacts() -> tuple[str, str]:
    model_types: dict[str, type[BaseModel]] = {
        "CaptureScope": CaptureScope,
        "CaptureManifest": CaptureManifest,
        "SnapshotManifest": SnapshotManifest,
        "ReleaseManifest": ReleaseManifest,
    }
    contracts = _contracts()
    schemas = {name: model_type.model_json_schema(mode="validation") for name, model_type in model_types.items()}
    schema_hashes = {name: canonical_sha256(_portable_json(schema)) for name, schema in schemas.items()}
    fixture_values = {name: model.model_dump(mode="json") for name, model in contracts.items()}

    for name, model_type in model_types.items():
        restored = model_type.model_validate(fixture_values[name])
        if restored.model_dump(mode="json") != fixture_values[name]:
            raise RuntimeError(f"{name} fixture does not round-trip through its Python DTO")

    schema_bundle = {
        "contract_set": CONTRACT_SET,
        "schema_sha256": schema_hashes,
        "schemas": schemas,
    }
    fixture_bundle = {
        "contract_set": CONTRACT_SET,
        "schema_sha256": schema_hashes,
        "contracts": fixture_values,
    }
    return _render(schema_bundle), _render(fixture_bundle)


def _check(path: Path, expected: str) -> bool:
    if not path.exists():
        print(f"missing generated conformance artifact: {path}")
        return False
    if path.read_text() != expected:
        print(f"stale generated conformance artifact: {path}")
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args()
    schema_text, fixture_text = build_artifacts()
    if args.write:
        SCHEMA_PATH.write_text(schema_text)
        FIXTURE_PATH.write_text(fixture_text)
        return 0
    return 0 if _check(SCHEMA_PATH, schema_text) and _check(FIXTURE_PATH, fixture_text) else 1


if __name__ == "__main__":
    raise SystemExit(main())
