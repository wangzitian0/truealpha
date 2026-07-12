import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from data_engine import raw_store
from data_engine.capture import manifest as manifest_builder
from data_engine.capture import observations
from data_engine.config import settings
from truealpha_contracts import (
    CaptureCellRequirement,
    CaptureEnvironment,
    CaptureManifestStatus,
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
    yield connection
    connection.rollback()
    connection.close()


def _scope(nonce: str, now: datetime) -> CaptureScope:
    subject_id = f"instrument:test:{nonce}"
    return CaptureScope(
        scope_version=f"test:{nonce}",
        environment=CaptureEnvironment.LOCAL,
        research_catalog_version="test:1",
        source_matrix_version="test:1",
        slo_version="test:1",
        universe_id=f"universe:test:{nonce}",
        universe_version="1",
        universe_membership_sha256="a" * 64,
        as_of=now - timedelta(days=1),
        approved_by="pytest",
        subjects=(
            CaptureSubject(
                subject_id=subject_id,
                display_name="Test Instrument",
                kind=CaptureSubjectKind.INSTRUMENT,
            ),
        ),
        requirements=(
            CaptureCellRequirement(
                subject_id=subject_id,
                domain=DataDomain.CORPORATE_ACTIONS,
                partition_key="current",
                level=CaptureRequirementLevel.REQUIRED,
                required_fields=("action_type",),
                primary_source=DataSource.YAHOO,
                maximum_age=timedelta(days=1),
                minimum_confidence=Decimal("0.8"),
            ),
        ),
    )


def test_successful_empty_query_is_complete_without_fabricating_an_action(conn):
    nonce = uuid.uuid4().hex
    now = datetime.now(UTC)
    scope = _scope(nonce, now)
    raw_id = raw_store.insert_fetch(
        conn,
        source=DataSource.YAHOO,
        source_record_id=f"actions:test:{nonce}",
        body=b'{"events":{}}',
        content_type="application/json",
        fetched_at=now - timedelta(seconds=1),
    )
    requirement = scope.requirements[0]
    observations.put(
        conn,
        observations.CaptureObservation(
            run_id=f"run:{nonce}",
            subject_id=requirement.subject_id,
            domain=requirement.domain,
            partition_key=requirement.partition_key,
            outcome=observations.ObservationOutcome.COMPLETE_EMPTY,
            raw_refs=(raw_store.raw_ref(raw_id),),
            domain_record_ids=(),
            required_fields=requirement.required_fields,
            observed_fields=(),
            min_knowable_at=None,
            max_knowable_at=None,
            observed_at=now,
            confidence=Decimal("0.8"),
            source=DataSource.YAHOO,
            mapping_version="test:1",
            detail="The source query succeeded and returned no actions.",
        ),
    )
    result = manifest_builder.build(
        conn,
        scope=scope,
        run_id=f"run:{nonce}",
        image_digest="sha256:" + "b" * 64,
        as_of=now,
        started_at=now - timedelta(seconds=2),
        completed_at=now,
    )
    assert result.status is CaptureManifestStatus.PASS
    assert result.cells[0].normalized_record_ids[0].startswith("staging.capture_observations:")
    assert not conn.execute(
        "select exists(select 1 from staging.corporate_actions where instrument_id = %s)",
        (requirement.subject_id,),
    ).fetchone()[0]


def test_missing_observation_fails_required_cell(conn):
    nonce = uuid.uuid4().hex
    now = datetime.now(UTC)
    result = manifest_builder.build(
        conn,
        scope=_scope(nonce, now),
        run_id=f"run:{nonce}",
        image_digest="sha256:" + "c" * 64,
        as_of=now,
        started_at=now - timedelta(seconds=1),
        completed_at=now,
    )
    assert result.status is CaptureManifestStatus.FAIL
    assert "required cell is missing" in result.blockers[0]
