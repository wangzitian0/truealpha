import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from data_engine.capture import observations
from data_engine.config import settings
from truealpha_contracts import DataDomain, DataSource
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


def _observation(outcome=observations.ObservationOutcome.COMPLETE_RECORDS):
    nonce = uuid.uuid4().hex
    now = datetime.now(UTC)
    return observations.CaptureObservation(
        run_id=f"run:{nonce}",
        subject_id=f"company:test:{nonce}",
        domain=DataDomain.FINANCIAL_FACTS,
        partition_key="current",
        outcome=outcome,
        raw_refs=("raw.fetches:1",),
        domain_record_ids=("staging.financial_facts:1",)
        if outcome is observations.ObservationOutcome.COMPLETE_RECORDS
        else (),
        required_fields=("revenue",),
        observed_fields=("revenue",) if outcome is observations.ObservationOutcome.COMPLETE_RECORDS else (),
        min_knowable_at=now - timedelta(days=1),
        max_knowable_at=now - timedelta(days=1),
        observed_at=now,
        confidence=Decimal("0.9"),
        source=DataSource.SEC,
        mapping_version="test:1",
        detail="query returned no events" if outcome is observations.ObservationOutcome.COMPLETE_EMPTY else None,
    )


def test_observation_roundtrip_idempotency_and_append_only(conn):
    observation = _observation()
    record_id = observations.put(conn, observation)
    assert observations.put(conn, observation) == record_id
    assert observations.get(
        conn,
        observation.run_id,
        observation.subject_id,
        observation.domain,
        observation.partition_key,
    ) == (record_id, observation)
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        conn.execute("update staging.capture_observations set confidence = 0 where id = %s", (record_id,))


def test_empty_is_evidence_but_cannot_carry_a_fabricated_domain_record(conn):
    empty = _observation(observations.ObservationOutcome.COMPLETE_EMPTY)
    assert observations.put(conn, empty)
    with pytest.raises(ValueError, match="complete_empty"):
        observations.CaptureObservation(**{**empty.__dict__, "domain_record_ids": ("staging.corporate_actions:1",)})


def test_future_knowledge_and_missing_required_fields_fail_closed():
    observation = _observation()
    with pytest.raises(ValueError, match="future knowledge"):
        observations.CaptureObservation(
            **{**observation.__dict__, "max_knowable_at": observation.observed_at + timedelta(seconds=1)}
        )
    with pytest.raises(ValueError, match="missing required fields"):
        observations.CaptureObservation(**{**observation.__dict__, "observed_fields": ()})
