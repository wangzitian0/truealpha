import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from data_engine import raw_store
from data_engine.config import settings
from data_engine.normalizers import sec_filings
from factors.shared import entity_resolution as er
from truealpha_contracts import DataSource
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


def test_filing_document_and_envelope_are_idempotent_and_make_no_semantic_claim(conn):
    nonce = uuid.uuid4().hex
    issuer_id = f"company:test:{nonce}"
    er.ensure_entity(conn, issuer_id, "company", "Filing Test")
    raw_id = raw_store.insert_fetch(
        conn,
        source=DataSource.SEC,
        source_record_id=f"filing:test:{nonce}",
        body=b"<html><body>Test annual filing.</body></html>",
        content_type="text/html",
        fetched_at=datetime.now(UTC),
    )
    kwargs = {
        "raw_fetch_id": raw_id,
        "issuer_id": issuer_id,
        "accession": f"0000000000-{nonce}",
        "form": "10-K",
        "filing_period": date(2025, 12, 31),
        "document_name": "annual.htm",
        "source_url": "https://www.sec.gov/test/annual.htm",
        "knowable_at": datetime.now(UTC) - timedelta(seconds=1),
    }
    first = sec_filings.normalize_document(conn, **kwargs)
    second = sec_filings.normalize_document(conn, **kwargs)
    assert second == first
    payload = conn.execute("select payload from staging.filing_extractions where id = %s", (first[1],)).fetchone()[0]
    assert payload == {"semantic_claims": 0}
    assert sec_filings.accepted_semantic_extraction_ids(conn, [first[1]]) == ()
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        conn.execute("delete from staging.filing_documents where id = %s", (first[0],))
