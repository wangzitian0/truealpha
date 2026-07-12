from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError
from truealpha_contracts import (
    CaptureCellRequirement,
    CaptureCellStatus,
    CaptureEnvironment,
    CaptureManifest,
    CaptureManifestCell,
    CaptureManifestStatus,
    CaptureRequirementLevel,
    CaptureScope,
    CaptureSubject,
    CaptureSubjectKind,
    DataDomain,
    DataSource,
)

AS_OF = datetime(2026, 5, 28, 23, 59, tzinfo=UTC)
DIGEST = "sha256:" + "a" * 64


def _scope(*, reverse: bool = False) -> CaptureScope:
    subjects = (
        CaptureSubject(
            subject_id="company:a",
            display_name="Company A",
            kind=CaptureSubjectKind.ISSUER,
            identifiers={"cik": "1"},
        ),
        CaptureSubject(
            subject_id="instrument:a",
            display_name="Company A common stock",
            kind=CaptureSubjectKind.INSTRUMENT,
            parent_subject_id="company:a",
            identifiers={"isin": "US0000000001"},
        ),
    )
    requirements = (
        CaptureCellRequirement(
            subject_id="company:a",
            domain=DataDomain.FINANCIAL_FACTS,
            partition_key="2025FY",
            level=CaptureRequirementLevel.REQUIRED,
            required_fields=("gross_profit", "revenue"),
            primary_source=DataSource.SEC,
            fallback_sources=(DataSource.MOOMOO,),
            minimum_confidence=Decimal("0.8"),
        ),
        CaptureCellRequirement(
            subject_id="instrument:a",
            domain=DataDomain.MARKET_PRICES,
            partition_key="2026-05-28",
            level=CaptureRequirementLevel.OPTIONAL,
            required_fields=("close",),
            primary_source=DataSource.YAHOO,
            maximum_age=timedelta(days=2),
        ),
    )
    return CaptureScope(
        scope_version="1",
        environment=CaptureEnvironment.STAGING,
        research_catalog_version="catalog:1",
        source_matrix_version="sources:1",
        slo_version="slo:1",
        universe_id="fund:topt",
        universe_version="2026-03-31",
        universe_membership_sha256="b" * 64,
        as_of=AS_OF,
        approved_by="test",
        subjects=tuple(reversed(subjects)) if reverse else subjects,
        requirements=tuple(reversed(requirements)) if reverse else requirements,
    )


def _complete_fact(*, future: bool = False) -> CaptureManifestCell:
    knowable = AS_OF + timedelta(minutes=1) if future else AS_OF - timedelta(days=1)
    return CaptureManifestCell(
        subject_id="company:a",
        domain=DataDomain.FINANCIAL_FACTS,
        partition_key="2025FY",
        status=CaptureCellStatus.COMPLETE,
        source=DataSource.SEC,
        raw_refs=("raw.fetches:1",),
        normalized_record_ids=("staging.financial_facts:1",),
        record_count=1,
        content_sha256="c" * 64,
        min_knowable_at=knowable,
        max_knowable_at=knowable,
        recorded_at=AS_OF + timedelta(minutes=2),
        confidence=Decimal("0.9"),
        mapping_version="sec-companyfacts:1",
    )


def _optional_missing() -> CaptureManifestCell:
    return CaptureManifestCell(
        subject_id="instrument:a",
        domain=DataDomain.MARKET_PRICES,
        partition_key="2026-05-28",
        status=CaptureCellStatus.UNAVAILABLE,
        detail="Fallback price source was unavailable.",
    )


def _manifest(*cells: CaptureManifestCell) -> CaptureManifest:
    return CaptureManifest(
        scope=_scope(),
        run_id="run:test",
        image_digest=DIGEST,
        as_of=AS_OF,
        started_at=AS_OF + timedelta(minutes=3),
        completed_at=AS_OF + timedelta(minutes=4),
        cells=cells,
    )


def test_scope_id_is_canonical_and_order_independent():
    assert _scope().capture_scope_id == _scope(reverse=True).capture_scope_id


def test_required_complete_and_optional_unavailable_can_pass():
    manifest = _manifest(_complete_fact(), _optional_missing())
    assert manifest.status is CaptureManifestStatus.PASS
    assert manifest.complete
    assert not manifest.blockers


def test_required_gap_and_future_knowledge_fail_closed():
    missing = CaptureManifestCell(
        subject_id="company:a",
        domain=DataDomain.FINANCIAL_FACTS,
        partition_key="2025FY",
        status=CaptureCellStatus.MISSING,
        detail="No normalized facts were produced.",
    )
    missing_manifest = _manifest(missing, _optional_missing())
    assert missing_manifest.status is CaptureManifestStatus.FAIL
    assert "required cell is missing" in missing_manifest.blockers[0]

    future_manifest = _manifest(_complete_fact(future=True), _optional_missing())
    assert future_manifest.status is CaptureManifestStatus.FAIL
    assert any("future knowledge" in blocker for blocker in future_manifest.blockers)


def test_complete_cell_cannot_omit_lineage_or_confidence():
    with pytest.raises(ValidationError, match="complete cells require"):
        CaptureManifestCell(
            subject_id="company:a",
            domain=DataDomain.FINANCIAL_FACTS,
            partition_key="2025FY",
            status=CaptureCellStatus.COMPLETE,
        )


def test_manifest_must_account_for_every_frozen_scope_cell():
    with pytest.raises(ValidationError, match="exactly match"):
        _manifest(_complete_fact())


def test_applicability_cannot_change_after_scope_freeze():
    changed = CaptureManifestCell(
        subject_id="company:a",
        domain=DataDomain.FINANCIAL_FACTS,
        partition_key="2025FY",
        status=CaptureCellStatus.NOT_APPLICABLE,
        detail="Run attempted to waive a required cell.",
    )
    manifest = _manifest(changed, _optional_missing())
    assert manifest.status is CaptureManifestStatus.FAIL
    assert any("applicability changed" in blocker for blocker in manifest.blockers)
