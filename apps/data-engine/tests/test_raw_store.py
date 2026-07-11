"""raw_store integration: needs BOTH Postgres and reachable object storage
(make runtime-up locally, service containers in CI) — skips cleanly otherwise.
Object writes are content-addressed and harmless to a shared dev MinIO; the
pointer rows ride a rolled-back transaction."""

import uuid
from datetime import UTC, datetime

import pytest
from data_engine import raw_store
from data_engine.config import settings
from truealpha_contracts import DataSource
from truealpha_runtime.testing import skip_or_fail

psycopg = pytest.importorskip("psycopg")


@pytest.fixture
def conn():
    try:
        c = psycopg.connect(settings.database_url, connect_timeout=3)
    except psycopg.OperationalError:
        skip_or_fail("no reachable Postgres (make runtime-up && make db-migrate)")
    yield c
    c.rollback()
    c.close()


@pytest.fixture
def s3(conn):
    store = raw_store.object_store()
    try:
        store.ensure_bucket()
    except Exception:
        skip_or_fail("no reachable object storage (make runtime-up)")
    return store


def test_fetch_roundtrip_dedupe_and_payload_readback(conn, s3):
    record_id = f"test:{uuid.uuid4().hex[:10]}"
    body = b'{"hello": "raw"}'
    fetched_at = datetime.now(UTC)

    assert not raw_store.already_fetched(conn, source=DataSource.SEC, source_record_id=record_id)
    fetch_id = raw_store.insert_fetch(
        conn,
        source=DataSource.SEC,
        source_record_id=record_id,
        body=body,
        content_type="application/json",
        fetched_at=fetched_at,
    )
    assert raw_store.raw_ref(fetch_id) == f"raw.fetches:{fetch_id}"
    assert raw_store.already_fetched(conn, source=DataSource.SEC, source_record_id=record_id)

    # identical bytes -> collapses onto the existing pointer row
    again = raw_store.insert_fetch(
        conn,
        source=DataSource.SEC,
        source_record_id=record_id,
        body=body,
        content_type="application/json",
        fetched_at=datetime.now(UTC),
    )
    assert again == fetch_id

    # different bytes for the same record -> a NEW vintage
    revised = raw_store.insert_fetch(
        conn,
        source=DataSource.SEC,
        source_record_id=record_id,
        body=b'{"hello": "restated"}',
        content_type="application/json",
        fetched_at=datetime.now(UTC),
    )
    assert revised != fetch_id

    # checksum-verified read-back through the pointer
    assert raw_store.get_payload(conn, fetch_id) == body


def test_json_helper(conn, s3):
    record_id = f"test:{uuid.uuid4().hex[:10]}"
    fetch_id = raw_store.insert_json_fetch(
        conn,
        source=DataSource.OPENFIGI,
        source_record_id=record_id,
        payload=[{"a": 1}],
        fetched_at=datetime.now(UTC),
        metadata={"isins": ["X"]},
    )
    assert raw_store.get_payload(conn, fetch_id) == b'[{"a": 1}]'
