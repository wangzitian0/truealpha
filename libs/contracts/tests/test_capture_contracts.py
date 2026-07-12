from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError
from truealpha_contracts.capture_contracts import (
    ApplicabilityMapping,
    CaptureCell,
    CaptureManifest,
    CaptureRecordEvidence,
    CaptureRequirement,
    CaptureScope,
    SourceCoverageMapping,
    applicability_mapping_from_catalog,
    canonical_applicability_projection_sha256,
    canonical_source_coverage_projection_sha256,
    compile_capture_requirement_bindings,
    evaluate_capture_manifest,
    project_capture_applicability,
)
from truealpha_contracts.common import CaptureEnvironment
from truealpha_contracts.data_quality import DataDomain
from truealpha_contracts.readiness import (
    ApplicabilityCatalog,
    ApplicabilityCell,
    ApplicabilityClassification,
)
from truealpha_contracts.universe import SubjectKind, SubjectRef, UniverseRef
from truealpha_contracts.usage import DataRequirement, RequirementLevel

SCOPE_EFFECTIVE_AT = datetime(2026, 5, 28, 0, 0, tzinfo=UTC)
AS_OF = datetime(2026, 5, 29, 12, 0, tzinfo=UTC)
STARTED_AT = AS_OF + timedelta(minutes=5)
CREATED_AT = STARTED_AT + timedelta(minutes=5)
EVALUATED_AT = CREATED_AT + timedelta(minutes=1)


def _binding(prefix: str, character: str) -> tuple[str, str]:
    content_sha256 = character * 64
    return f"{prefix}:{content_sha256}", content_sha256


RESEARCH_ID, RESEARCH_SHA = _binding("research-catalog", "1")
APPLICABILITY_ID, APPLICABILITY_SHA = _binding("applicability", "2")
SOURCE_COVERAGE_ID, SOURCE_COVERAGE_SHA = _binding("source-coverage", "3")
SLO_ID, SLO_SHA = _binding("module-slo", "4")
SOURCE_REGISTRY_ID, SOURCE_REGISTRY_SHA = _binding("source-registry", "5")
SEMANTIC_REGISTRY_ID, SEMANTIC_REGISTRY_SHA = _binding("semantic-type-registry", "6")


def _requirement(*, reverse: bool = False) -> CaptureRequirement:
    required_fields = ("gross_profit", "revenue")
    quality_policy_ids = ("quality.non_null:v1", "quality.numeric:v1")
    return CaptureRequirement(
        semantic_type_id="semantic.financial_fact",
        semantic_type_version="1.0.0",
        domain=DataDomain.FINANCIAL_FACTS,
        required_fields=tuple(reversed(required_fields)) if reverse else required_fields,
        subject_kinds=(SubjectKind.ISSUER,),
        cadence=timedelta(days=1),
        partition_rule_id="partition.fiscal:v1",
        freshness_policy_id="freshness.daily:v1",
        maximum_age=timedelta(days=1),
        quality_policy_ids=tuple(reversed(quality_policy_ids)) if reverse else quality_policy_ids,
    )


def _data_requirement(requirement: CaptureRequirement, **updates: object) -> DataRequirement:
    values: dict[str, object] = {
        "capture_requirement_id": requirement.capture_requirement_id,
        "semantic_type_id": requirement.semantic_type_id,
        "domain": requirement.domain,
        "metric": "revenue",
        "subject_kinds": frozenset(requirement.subject_kinds),
        "level": RequirementLevel.REQUIRED,
        "lookback": timedelta(days=400),
        "valid_period_rule_id": requirement.partition_rule_id,
        "maximum_age": requirement.maximum_age,
        "cadence": requirement.cadence,
    }
    values.update(updates)
    return DataRequirement(**values)


def _projected_applicability(
    requirement: CaptureRequirement,
    *,
    subject_ids: tuple[str, ...] = ("issuer:alphabet",),
    classification: str = "required",
    effective_at: datetime = SCOPE_EFFECTIVE_AT,
):
    return {
        (
            SubjectKind.ISSUER,
            subject_id,
            DataDomain.FINANCIAL_FACTS,
            "2025-fy",
            requirement.capture_requirement_id,
        ): (classification, effective_at)
        for subject_id in subject_ids
    }


def _source_coverage_for_applicability(applicability: ApplicabilityMapping) -> SourceCoverageMapping:
    environment_hash = {
        CaptureEnvironment.LOCAL_DEV: "1",
        CaptureEnvironment.LOCAL_TEST: "2",
        CaptureEnvironment.GITHUB_CI: "3",
        CaptureEnvironment.STAGING: "7",
        CaptureEnvironment.PRODUCTION: "5",
    }
    return {
        (environment, *key): ("source-coverage-entry:" + character * 64,)
        for key, (classification, _) in applicability.items()
        if classification != "not_applicable"
        for environment, character in environment_hash.items()
    }


def _scope(
    *,
    projected_subject_ids: tuple[str, ...] = ("issuer:alphabet",),
    classification: str = "required",
    applicability_effective_at: datetime = SCOPE_EFFECTIVE_AT,
) -> CaptureScope:
    requirement = _requirement()
    projection = _projected_applicability(
        requirement,
        subject_ids=projected_subject_ids,
        classification=classification,
        effective_at=applicability_effective_at,
    )
    source_projection = _source_coverage_for_applicability(projection)
    return CaptureScope(
        research_catalog_id=RESEARCH_ID,
        research_catalog_sha256=RESEARCH_SHA,
        universe=UniverseRef(
            universe_id="universe:topt-etf-us",
            universe_version="2026-05-28",
            content_sha256="0" * 64,
        ),
        applicability_catalog_id=APPLICABILITY_ID,
        applicability_catalog_sha256=APPLICABILITY_SHA,
        applicability_projection_sha256=canonical_applicability_projection_sha256(projection),
        source_coverage_catalog_id=SOURCE_COVERAGE_ID,
        source_coverage_catalog_sha256=SOURCE_COVERAGE_SHA,
        source_coverage_projection_sha256=canonical_source_coverage_projection_sha256(source_projection),
        slo_catalog_id=SLO_ID,
        slo_catalog_sha256=SLO_SHA,
        source_registry_id=SOURCE_REGISTRY_ID,
        source_registry_sha256=SOURCE_REGISTRY_SHA,
        semantic_type_registry_id=SEMANTIC_REGISTRY_ID,
        semantic_type_registry_sha256=SEMANTIC_REGISTRY_SHA,
        requirements=(requirement,),
        effective_at=SCOPE_EFFECTIVE_AT,
        owner="data-platform",
    )


def _applicability_catalog(
    requirement: CaptureRequirement,
    data_requirement: DataRequirement,
) -> ApplicabilityCatalog:
    universe = UniverseRef(
        universe_id="universe:topt-etf-us",
        universe_version="2026-05-28",
        content_sha256="0" * 64,
    )
    return ApplicabilityCatalog(
        catalog_version="1.0.0",
        research_catalog_id=RESEARCH_ID,
        research_catalog_sha256=RESEARCH_SHA,
        universe=universe,
        effective_at=SCOPE_EFFECTIVE_AT,
        approved_at=SCOPE_EFFECTIVE_AT - timedelta(days=1),
        approved_by="product-owner",
        approval_signature_id="signature:applicability-v1",
        approval_signature_sha256="9" * 64,
        cells=(
            ApplicabilityCell(
                module_id="gppe",
                catalog_alias="gppe.primary",
                data_requirement_id=data_requirement.requirement_id,
                subject=SubjectRef(kind=SubjectKind.ISSUER, id="issuer:alphabet"),
                domain=requirement.domain,
                partition_key="2025-fy",
                classification=ApplicabilityClassification.REQUIRED,
                reason="The approved GPPE module requires this cell.",
                effective_at=SCOPE_EFFECTIVE_AT,
            ),
            ApplicabilityCell(
                module_id="peg",
                catalog_alias="peg.optional",
                data_requirement_id=data_requirement.requirement_id,
                subject=SubjectRef(kind=SubjectKind.ISSUER, id="issuer:alphabet"),
                domain=requirement.domain,
                partition_key="2025-fy",
                classification=ApplicabilityClassification.OPTIONAL,
                reason="The PEG module uses the same cell optionally.",
                effective_at=SCOPE_EFFECTIVE_AT,
            ),
        ),
    )


def _evidence(**updates: object) -> CaptureRecordEvidence:
    values: dict[str, object] = {
        "source_coverage_entry_id": "source-coverage-entry:" + "7" * 64,
        "raw_id": "raw.fetches:financial-fact-1",
        "raw_sha256": "7" * 64,
        "normalized_id": "staging.financial_facts:financial-fact-1",
        "semantic_type_id": "semantic.financial_fact",
        "semantic_type_version": "1.0.0",
        "populated_fields": ("gross_profit", "revenue"),
        "knowable_at": AS_OF - timedelta(hours=1),
        "recorded_at": STARTED_AT + timedelta(minutes=1),
        "valid_from": AS_OF - timedelta(days=365),
        "valid_to": None,
        "confidence": Decimal("0.99"),
        "mapping_version": "sec-companyfacts:v1",
        "policy_versions": {
            "freshness.daily:v1": "v1",
            "partition.fiscal:v1": "v1",
        },
        "quality_check_ids": ("quality.non_null:v1", "quality.numeric:v1"),
        "quality_status": "pass",
        "lineage_sha256": "8" * 64,
    }
    values.update(updates)
    return CaptureRecordEvidence(**values)


def _cell(
    *,
    status: str = "complete",
    applicability: str = "required",
    evidence: tuple[CaptureRecordEvidence, ...] | None = None,
    reason_codes: tuple[str, ...] = (),
    capture_requirement_id: str | None = None,
    subject: SubjectRef | None = None,
) -> CaptureCell:
    requirement = _scope().requirements[0]
    return CaptureCell(
        subject=subject or SubjectRef(kind=SubjectKind.ISSUER, id="issuer:alphabet"),
        domain=DataDomain.FINANCIAL_FACTS,
        partition_key="2025-fy",
        capture_requirement_id=capture_requirement_id or requirement.capture_requirement_id,
        applicability=applicability,
        status=status,
        evidence=(_evidence(),) if evidence is None else evidence,
        reason_codes=reason_codes,
    )


def _manifest(
    *cells: CaptureCell,
    scope: CaptureScope | None = None,
    **updates: object,
) -> CaptureManifest:
    frozen_scope = scope or _scope()
    values: dict[str, object] = {
        "capture_scope_id": frozen_scope.capture_scope_id,
        "capture_scope_sha256": frozen_scope.content_sha256,
        "environment": CaptureEnvironment.STAGING,
        "research_catalog_id": frozen_scope.research_catalog_id,
        "research_catalog_sha256": frozen_scope.research_catalog_sha256,
        "applicability_catalog_id": frozen_scope.applicability_catalog_id,
        "applicability_catalog_sha256": frozen_scope.applicability_catalog_sha256,
        "source_coverage_catalog_id": frozen_scope.source_coverage_catalog_id,
        "source_coverage_catalog_sha256": frozen_scope.source_coverage_catalog_sha256,
        "slo_catalog_id": frozen_scope.slo_catalog_id,
        "slo_catalog_sha256": frozen_scope.slo_catalog_sha256,
        "source_registry_id": frozen_scope.source_registry_id,
        "source_registry_sha256": frozen_scope.source_registry_sha256,
        "semantic_type_registry_id": frozen_scope.semantic_type_registry_id,
        "semantic_type_registry_sha256": frozen_scope.semantic_type_registry_sha256,
        "partition_key": "2025-fy",
        "as_of": AS_OF,
        "started_at": STARTED_AT,
        "cells": cells,
        "created_at": CREATED_AT,
    }
    values.update(updates)
    return CaptureManifest(**values)


def _applicability(cell: CaptureCell, classification: str = "required", *, effective_at: datetime | None = None):
    return {cell.key: (classification, effective_at or SCOPE_EFFECTIVE_AT)}


def _evaluate(
    manifest: CaptureManifest,
    *,
    scope: CaptureScope | None = None,
    applicability: ApplicabilityMapping | None = None,
    source_coverage: SourceCoverageMapping | None = None,
    applicability_catalog_id: str = APPLICABILITY_ID,
    applicability_catalog_sha256: str = APPLICABILITY_SHA,
):
    frozen_scope = scope or _scope()
    default_cell = _cell()
    resolved_applicability = _applicability(default_cell) if applicability is None else applicability
    return evaluate_capture_manifest(
        frozen_scope,
        manifest,
        applicability_catalog_id=applicability_catalog_id,
        applicability_catalog_sha256=applicability_catalog_sha256,
        applicability=resolved_applicability,
        source_coverage=(
            _source_coverage_for_applicability(resolved_applicability) if source_coverage is None else source_coverage
        ),
        evaluated_at=EVALUATED_AT,
    )


def test_contracts_are_content_addressed_frozen_extra_forbid_and_source_neutral() -> None:
    requirement = _requirement()
    assert requirement.capture_requirement_id == "capture-requirement:" + requirement.content_sha256
    assert requirement.capture_requirement_id == _requirement(reverse=True).capture_requirement_id
    assert "source_id" not in CaptureRequirement.model_fields
    assert "primary_source" not in CaptureRequirement.model_fields
    assert "data_requirement_id" not in CaptureRequirement.model_fields
    assert "environment" not in CaptureScope.model_fields
    assert "environment" in CaptureManifest.model_fields

    with pytest.raises(ValidationError, match="frozen"):
        requirement.domain = DataDomain.MARKET_PRICES  # type: ignore[misc]
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        CaptureRequirement(**{**requirement.model_dump(), "vendor": "sec"})


def test_complete_manifest_evaluates_deterministically() -> None:
    cell = _cell()
    manifest = _manifest(cell)
    report = _evaluate(manifest, applicability=_applicability(cell))
    repeated = _evaluate(manifest, applicability=_applicability(cell))

    assert report.ready
    assert not report.blocking_reason_codes
    assert report.capture_evaluation_report_id == "capture-evaluation:" + report.content_sha256
    assert report == repeated


@pytest.mark.parametrize("classification", ["optional", "not_applicable"])
def test_explicit_optional_and_not_applicable_rows_are_complete_denominator_rows(classification: str) -> None:
    scope = _scope(classification=classification)
    cell = _cell(
        status=classification,
        applicability=classification,
        evidence=(),
        reason_codes=(f"capture.{classification}",),
    )
    report = _evaluate(
        _manifest(cell, scope=scope),
        scope=scope,
        applicability=_applicability(cell, classification),
    )
    assert report.ready


def test_missing_extra_and_duplicate_cells_fail_closed() -> None:
    expected = _cell()
    missing = _evaluate(_manifest(), applicability=_applicability(expected))
    assert any(code.startswith("cell.missing:") for code in missing.blocking_reason_codes)

    extra = _cell(capture_requirement_id="capture-requirement:" + "f" * 64)
    extra_report = _evaluate(_manifest(expected, extra), applicability=_applicability(expected))
    assert any(code.startswith("cell.extra:") for code in extra_report.blocking_reason_codes)

    duplicate = _evaluate(_manifest(expected, expected), applicability=_applicability(expected))
    assert any(code.startswith("cell.duplicate:") for code in duplicate.blocking_reason_codes)

    empty_denominator = _evaluate(_manifest(), applicability={})
    assert "applicability.empty" in empty_denominator.blocking_reason_codes
    assert any(
        code.startswith("applicability.requirement_missing:") for code in empty_denominator.blocking_reason_codes
    )


def test_required_cell_cannot_self_report_a_non_complete_or_not_applicable_result() -> None:
    missing = _cell(status="missing", evidence=(), reason_codes=("capture.no-row",))
    report = _evaluate(_manifest(missing), applicability=_applicability(missing))
    assert any(code.startswith("cell.required_not_complete:") for code in report.blocking_reason_codes)

    waived = _cell(
        status="not_applicable",
        applicability="not_applicable",
        evidence=(),
        reason_codes=("capture.waived",),
    )
    waived_report = _evaluate(_manifest(waived), applicability=_applicability(waived, "required"))
    assert any(code.startswith("cell.applicability_mismatch:") for code in waived_report.blocking_reason_codes)
    assert any(code.startswith("cell.required_not_complete:") for code in waived_report.blocking_reason_codes)


def test_complete_cell_with_empty_or_structurally_incomplete_evidence_fails_closed() -> None:
    empty = _cell(evidence=())
    empty_report = _evaluate(_manifest(empty), applicability=_applicability(empty))
    assert any(code.startswith("evidence.empty:") for code in empty_report.blocking_reason_codes)

    incomplete_evidence = CaptureRecordEvidence()
    incomplete = _cell(evidence=(incomplete_evidence,))
    report = _evaluate(_manifest(incomplete), applicability=_applicability(incomplete))
    assert any(code.startswith("evidence.missing_raw_id:") for code in report.blocking_reason_codes)
    assert any(code.startswith("evidence.missing_normalized_id:") for code in report.blocking_reason_codes)
    assert any(code.startswith("evidence.missing_confidence:") for code in report.blocking_reason_codes)


def test_future_knowledge_and_stale_rows_fail_even_when_status_is_complete() -> None:
    future = _cell(
        evidence=(
            _evidence(
                knowable_at=AS_OF + timedelta(minutes=1),
                recorded_at=STARTED_AT + timedelta(minutes=1),
            ),
        )
    )
    future_report = _evaluate(_manifest(future), applicability=_applicability(future))
    assert any(code.startswith("evidence.future_knowledge:") for code in future_report.blocking_reason_codes)

    stale = _cell(evidence=(_evidence(knowable_at=AS_OF - timedelta(days=2)),))
    stale_report = _evaluate(_manifest(stale), applicability=_applicability(stale))
    assert any(code.startswith("evidence.stale:") for code in stale_report.blocking_reason_codes)


def test_scope_hash_catalog_and_registry_drift_fail_closed() -> None:
    cell = _cell()
    other_scope_id, other_scope_sha = _binding("capture-scope", "a")
    other_source_registry_id, other_source_registry_sha = _binding("source-registry", "b")
    manifest = _manifest(
        cell,
        capture_scope_id=other_scope_id,
        capture_scope_sha256=other_scope_sha,
        source_registry_id=other_source_registry_id,
        source_registry_sha256=other_source_registry_sha,
    )
    report = _evaluate(manifest, applicability=_applicability(cell))
    assert "binding.capture_scope_id_mismatch" in report.blocking_reason_codes
    assert "binding.capture_scope_sha256_mismatch" in report.blocking_reason_codes
    assert "binding.source_registry_id_mismatch" in report.blocking_reason_codes
    assert "binding.source_registry_sha256_mismatch" in report.blocking_reason_codes

    other_applicability_id, other_applicability_sha = _binding("applicability", "c")
    input_report = _evaluate(
        _manifest(cell),
        applicability=_applicability(cell),
        applicability_catalog_id=other_applicability_id,
        applicability_catalog_sha256=other_applicability_sha,
    )
    assert "binding.applicability_input_id_mismatch" in input_report.blocking_reason_codes
    assert "binding.applicability_input_sha256_mismatch" in input_report.blocking_reason_codes


def test_applicability_effective_after_run_start_cannot_change_the_denominator() -> None:
    postdated = STARTED_AT + timedelta(seconds=1)
    scope = _scope(applicability_effective_at=postdated)
    cell = _cell()
    report = _evaluate(
        _manifest(cell, scope=scope),
        scope=scope,
        applicability=_applicability(cell, effective_at=postdated),
    )
    assert any(code.startswith("applicability.postdated:") for code in report.blocking_reason_codes)


def test_applicability_projection_rejects_tampering_and_scope_shrink() -> None:
    scope = _scope(projected_subject_ids=("issuer:alphabet", "issuer:microsoft"))
    alphabet = _cell()
    microsoft = _cell(subject=SubjectRef(kind=SubjectKind.ISSUER, id="issuer:microsoft"))
    full_projection = {
        **_applicability(alphabet),
        **_applicability(microsoft),
    }

    missing_row = _evaluate(
        _manifest(alphabet, scope=scope),
        scope=scope,
        applicability=full_projection,
    )
    assert any(code.startswith("cell.missing:") for code in missing_row.blocking_reason_codes)

    shrunken_projection = _evaluate(
        _manifest(alphabet, scope=scope),
        scope=scope,
        applicability=_applicability(alphabet),
    )
    assert "binding.applicability_projection_sha256_mismatch" in shrunken_projection.blocking_reason_codes

    default_scope = _scope()
    waived = _cell(
        status="optional",
        applicability="optional",
        evidence=(),
        reason_codes=("capture.optional",),
    )
    tampered_classification = _evaluate(
        _manifest(waived, scope=default_scope),
        scope=default_scope,
        applicability=_applicability(waived, "optional"),
    )
    assert "binding.applicability_projection_sha256_mismatch" in tampered_classification.blocking_reason_codes


def test_source_coverage_projection_rejects_environment_substitution_and_scope_shrink() -> None:
    scope = _scope()
    cell = _cell(evidence=(_evidence(source_coverage_entry_id="source-coverage-entry:" + "5" * 64),))
    applicability = _applicability(cell)
    coverage = _source_coverage_for_applicability(applicability)

    production_evidence = _evaluate(
        _manifest(cell, scope=scope),
        scope=scope,
        applicability=applicability,
        source_coverage=coverage,
    )
    assert any(
        code.startswith("evidence.unapproved_source_coverage_entry:")
        for code in production_evidence.blocking_reason_codes
    )

    staging_key = next(key for key in coverage if key[0] is CaptureEnvironment.STAGING)
    substituted = dict(coverage)
    substituted[staging_key] = ("source-coverage-entry:" + "5" * 64,)
    substituted_report = _evaluate(
        _manifest(cell, scope=scope),
        scope=scope,
        applicability=applicability,
        source_coverage=substituted,
    )
    assert "binding.source_coverage_projection_sha256_mismatch" in substituted_report.blocking_reason_codes

    staging_only = {key: entry_ids for key, entry_ids in coverage.items() if key[0] is CaptureEnvironment.STAGING}
    shrunken_report = _evaluate(
        _manifest(_cell(), scope=scope),
        scope=scope,
        applicability=applicability,
        source_coverage=staging_only,
    )
    assert "binding.source_coverage_projection_sha256_mismatch" in shrunken_report.blocking_reason_codes


def test_applicability_catalog_projects_mechanically_into_the_bound_capture_scope() -> None:
    requirement = _requirement()
    data_requirement = _data_requirement(requirement)
    catalog = _applicability_catalog(requirement, data_requirement)
    projection = project_capture_applicability(catalog, (requirement,), (data_requirement,))
    scope = CaptureScope(
        research_catalog_id=catalog.research_catalog_id,
        research_catalog_sha256=catalog.research_catalog_sha256,
        universe=catalog.universe,
        applicability_catalog_id=catalog.applicability_catalog_id,
        applicability_catalog_sha256=catalog.content_sha256,
        applicability_projection_sha256=canonical_applicability_projection_sha256(projection),
        source_coverage_catalog_id=SOURCE_COVERAGE_ID,
        source_coverage_catalog_sha256=SOURCE_COVERAGE_SHA,
        source_coverage_projection_sha256=canonical_source_coverage_projection_sha256(
            _source_coverage_for_applicability(projection)
        ),
        slo_catalog_id=SLO_ID,
        slo_catalog_sha256=SLO_SHA,
        source_registry_id=SOURCE_REGISTRY_ID,
        source_registry_sha256=SOURCE_REGISTRY_SHA,
        semantic_type_registry_id=SEMANTIC_REGISTRY_ID,
        semantic_type_registry_sha256=SEMANTIC_REGISTRY_SHA,
        requirements=(requirement,),
        effective_at=SCOPE_EFFECTIVE_AT,
        owner="data-platform",
    )

    resolved = applicability_mapping_from_catalog(scope, catalog, (data_requirement,))

    assert resolved == projection
    assert next(iter(resolved.values()))[0] == "required"

    shrunken = CaptureScope(
        **scope.model_dump(
            exclude={
                "capture_scope_id",
                "content_sha256",
                "applicability_projection_sha256",
            }
        ),
        applicability_projection_sha256=canonical_applicability_projection_sha256({}),
    )
    with pytest.raises(ValueError, match="shrunken or drifted"):
        applicability_mapping_from_catalog(shrunken, catalog, (data_requirement,))


def test_data_requirement_compilation_is_explicit_and_rejects_semantic_drift() -> None:
    capture_requirement = _requirement()
    data_requirement = _data_requirement(capture_requirement)

    compiled = compile_capture_requirement_bindings(
        (data_requirement,),
        (capture_requirement,),
    )
    assert compiled == {data_requirement.requirement_id: capture_requirement}

    drifted = _data_requirement(
        capture_requirement,
        valid_period_rule_id="partition.calendar:v1",
    )
    with pytest.raises(ValueError, match="semantics drift"):
        compile_capture_requirement_bindings((drifted,), (capture_requirement,))

    unknown = _data_requirement(
        capture_requirement,
        capture_requirement_id="capture-requirement:" + "f" * 64,
    )
    with pytest.raises(ValueError, match="unknown CaptureRequirement"):
        compile_capture_requirement_bindings((unknown,), (capture_requirement,))


def test_missing_mapping_policy_quality_and_lineage_fail_closed() -> None:
    evidence = _evidence(
        mapping_version=None,
        policy_versions={},
        quality_check_ids=(),
        lineage_sha256=None,
    )
    cell = _cell(evidence=(evidence,))
    report = _evaluate(_manifest(cell), applicability=_applicability(cell))
    assert any(code.startswith("evidence.missing_mapping_version:") for code in report.blocking_reason_codes)
    assert any(code.startswith("evidence.missing_policy:") for code in report.blocking_reason_codes)
    assert any(code.startswith("evidence.missing_quality:") for code in report.blocking_reason_codes)
    assert any(code.startswith("evidence.missing_lineage_sha256:") for code in report.blocking_reason_codes)


def test_wrong_semantic_type_missing_fields_and_failed_quality_fail_closed() -> None:
    evidence = _evidence(
        semantic_type_version="2.0.0",
        populated_fields=("revenue",),
        quality_status="fail",
    )
    cell = _cell(evidence=(evidence,))
    report = _evaluate(_manifest(cell), applicability=_applicability(cell))

    assert any(code.startswith("evidence.semantic_type_mismatch:") for code in report.blocking_reason_codes)
    assert any(code.startswith("evidence.required_fields_missing:") for code in report.blocking_reason_codes)
    assert any(code.startswith("evidence.quality_failed:") for code in report.blocking_reason_codes)


def test_invalid_raw_checksum_content_hash_and_time_order_are_rejected() -> None:
    with pytest.raises(ValidationError, match="raw_sha256"):
        _evidence(raw_sha256="not-a-sha")
    with pytest.raises(ValidationError, match="recorded_at must not precede knowable_at"):
        _evidence(recorded_at=AS_OF - timedelta(hours=2))

    scope_values = _scope().model_dump()
    scope_values["content_sha256"] = "f" * 64
    with pytest.raises(ValidationError, match="content_sha256 does not match canonical content"):
        CaptureScope(**scope_values)
