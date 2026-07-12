import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from data_engine.capture import observations, source_results
from data_engine.config import settings
from truealpha_contracts import (
    CaptureCellRequirement,
    CaptureRequirementLevel,
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


def _result(nonce: str, source: DataSource, fields: tuple[str, ...], records: tuple[str, ...]):
    now = datetime.now(UTC)
    return source_results.CaptureSourceResult(
        run_id=f"run:{nonce}",
        subject_id=f"company:test:{nonce}",
        domain=DataDomain.FINANCIAL_FACTS,
        partition_key="current",
        source=source,
        outcome=source_results.SourceResultOutcome.SUCCESS,
        raw_refs=(f"raw.fetches:{1 if source is DataSource.SEC else 2}",),
        domain_record_ids=records,
        observed_fields=fields,
        min_knowable_at=now - timedelta(days=2),
        max_knowable_at=now - timedelta(days=1),
        observed_at=now,
        confidence=Decimal("1") if source is DataSource.SEC else Decimal("0.8"),
        mapping_version=f"{source.value}:test:1",
    )


def test_field_level_fallbacks_finalize_one_complete_observation(conn):
    nonce = uuid.uuid4().hex
    requirement = CaptureCellRequirement(
        subject_id=f"company:test:{nonce}",
        domain=DataDomain.FINANCIAL_FACTS,
        partition_key="current",
        level=CaptureRequirementLevel.REQUIRED,
        required_fields=("revenue", "gross_profit"),
        primary_source=DataSource.SEC,
        fallback_sources=(DataSource.MOOMOO,),
        minimum_confidence=Decimal("0.8"),
    )
    sec_result = _result(nonce, DataSource.SEC, ("revenue",), ("staging.financial_facts:1",))
    moomoo_result = _result(nonce, DataSource.MOOMOO, ("gross_profit",), ("staging.financial_facts:2",))
    assert source_results.put(conn, sec_result)
    assert source_results.put(conn, sec_result)  # idempotent returns the same positive ID
    source_results.put(conn, moomoo_result)
    observation_id = source_results.finalize(conn, run_id=f"run:{nonce}", requirement=requirement)
    stored = observations.get(
        conn,
        f"run:{nonce}",
        requirement.subject_id,
        requirement.domain,
        requirement.partition_key,
    )
    assert stored is not None and stored[0] == observation_id
    observation = stored[1]
    assert observation.outcome is observations.ObservationOutcome.COMPLETE_RECORDS
    assert set(observation.observed_fields) == {"revenue", "gross_profit"}
    assert observation.source is DataSource.SEC
    assert observation.confidence == Decimal("0.8")


def test_missing_required_fallback_field_fails_final_observation(conn):
    nonce = uuid.uuid4().hex
    requirement = CaptureCellRequirement(
        subject_id=f"company:test:{nonce}",
        domain=DataDomain.FINANCIAL_FACTS,
        partition_key="current",
        level=CaptureRequirementLevel.REQUIRED,
        required_fields=("revenue", "gross_profit"),
        primary_source=DataSource.SEC,
    )
    source_results.put(
        conn,
        _result(nonce, DataSource.SEC, ("revenue",), ("staging.financial_facts:1",)),
    )
    source_results.finalize(conn, run_id=f"run:{nonce}", requirement=requirement)
    stored = observations.get(
        conn,
        f"run:{nonce}",
        requirement.subject_id,
        requirement.domain,
        requirement.partition_key,
    )
    assert stored is not None
    assert stored[1].outcome is observations.ObservationOutcome.FAILED
    assert "gross_profit" in (stored[1].detail or "")
