import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from data_engine.capture import repository
from data_engine.config import settings
from truealpha_contracts import (
    CaptureCellRequirement,
    CaptureCellStatus,
    CaptureEnvironment,
    CaptureManifest,
    CaptureManifestCell,
    CaptureRequirementLevel,
    CaptureScope,
    CaptureSubject,
    CaptureSubjectKind,
    DataDomain,
    DataSource,
)
from truealpha_runtime.testing import skip_or_fail

psycopg = pytest.importorskip("psycopg")


@pytest.fixture
def conn():
    try:
        connection = psycopg.connect(settings.database_url, connect_timeout=3)
    except psycopg.OperationalError:
        skip_or_fail("no reachable Postgres (make runtime-up && make db-migrate)")
    if connection.execute("select to_regclass('staging.capture_scopes')").fetchone()[0] is None:
        connection.close()
        skip_or_fail("capture manifest tables missing (make db-migrate)")
    yield connection
    connection.rollback()
    connection.close()


def _manifest() -> CaptureManifest:
    nonce = uuid.uuid4().hex
    now = datetime(2026, 7, 12, tzinfo=UTC)
    subject_id = f"company:test:{nonce}"
    scope = CaptureScope(
        scope_version=f"test:{nonce}",
        environment=CaptureEnvironment.LOCAL,
        research_catalog_version="catalog:test",
        source_matrix_version="sources:test",
        slo_version="slo:test",
        universe_id=f"universe:test:{nonce}",
        universe_version="1",
        universe_membership_sha256="a" * 64,
        as_of=now,
        approved_by="pytest",
        subjects=(
            CaptureSubject(
                subject_id=subject_id,
                display_name="Test Company",
                kind=CaptureSubjectKind.ISSUER,
                identifiers={"cik": "1"},
            ),
        ),
        requirements=(
            CaptureCellRequirement(
                subject_id=subject_id,
                domain=DataDomain.FINANCIAL_FACTS,
                partition_key="2025FY",
                level=CaptureRequirementLevel.REQUIRED,
                required_fields=("revenue",),
                primary_source=DataSource.SEC,
                minimum_confidence=Decimal("0.8"),
            ),
        ),
    )
    cell = CaptureManifestCell(
        subject_id=subject_id,
        domain=DataDomain.FINANCIAL_FACTS,
        partition_key="2025FY",
        status=CaptureCellStatus.COMPLETE,
        source=DataSource.SEC,
        raw_refs=("raw.fetches:1",),
        normalized_record_ids=(f"staging.financial_facts:{nonce}",),
        record_count=1,
        content_sha256="b" * 64,
        min_knowable_at=now - timedelta(days=1),
        max_knowable_at=now - timedelta(days=1),
        recorded_at=now,
        confidence=Decimal("0.9"),
        mapping_version="test:1",
    )
    return CaptureManifest(
        scope=scope,
        run_id=f"run:{nonce}",
        image_digest="sha256:" + "c" * 64,
        as_of=now + timedelta(minutes=2),
        started_at=now + timedelta(minutes=1),
        completed_at=now + timedelta(minutes=2),
        cells=(cell,),
    )


def test_scope_and_manifest_roundtrip_are_idempotent(conn):
    manifest = _manifest()
    assert repository.put_manifest(conn, manifest)
    assert not repository.put_manifest(conn, manifest)
    assert repository.get_scope(conn, manifest.scope.capture_scope_id) == manifest.scope
    assert repository.get_manifest(conn, manifest.capture_manifest_id) == manifest

    cell_count = conn.execute(
        "select count(*) from staging.capture_manifest_cells where capture_manifest_id = %s",
        (manifest.capture_manifest_id,),
    ).fetchone()[0]
    assert cell_count == 1


def test_capture_evidence_rows_are_append_only(conn):
    manifest = _manifest()
    repository.put_manifest(conn, manifest)
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        conn.execute(
            "update staging.capture_manifests set status = 'fail' where capture_manifest_id = %s",
            (manifest.capture_manifest_id,),
        )
