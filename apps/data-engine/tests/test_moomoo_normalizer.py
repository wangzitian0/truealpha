import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from data_engine import instruments, raw_store
from data_engine.config import settings
from data_engine.normalizers import moomoo
from factors.shared import entity_resolution as er
from truealpha_contracts import DataSource
from truealpha_runtime.testing import skip_or_fail

psycopg = pytest.importorskip("psycopg")
SAMPLE = Path(__file__).resolve().parents[1] / "samples" / "moomoo" / "NICE.json"


@pytest.fixture
def conn():
    try:
        connection = psycopg.connect(settings.database_url, connect_timeout=3)
    except psycopg.OperationalError:
        skip_or_fail("no reachable Postgres (make runtime-up && make db-migrate)")
    yield connection
    connection.rollback()
    connection.close()


def _raw(conn, nonce: str, endpoint: str, payload) -> int:
    return raw_store.insert_fetch(
        conn,
        source=DataSource.MOOMOO,
        source_record_id=f"{endpoint}:test:{nonce}",
        body=json.dumps(payload).encode(),
        content_type="application/json",
        fetched_at=datetime.now(UTC),
    )


def test_moomoo_domains_normalize_with_vendor_knowability_and_replay(conn):
    sample = json.loads(SAMPLE.read_bytes())
    nonce = uuid.uuid4().hex[:10]
    issuer_id = f"company:test:{nonce}"
    instrument_id = f"instrument:isin:US{nonce}"
    listing_id = f"listing:vendor:US.TEST{nonce}"
    er.ensure_entity(conn, issuer_id, "company", "Moomoo Test")
    instruments.ensure_instrument(conn, instrument_id, "equity_common", "Moomoo Test")

    consensus_raw = _raw(conn, nonce, "consensus", sample["analyst_consensus"]["data"])
    ratings_raw = _raw(conn, nonce, "ratings", sample["rating_summary"]["data"])
    segments_raw = _raw(conn, nonce, "segments", sample["financials_revenue_breakdown"]["data"])
    dividends_raw = _raw(conn, nonce, "dividends", sample["dividends"]["data"])

    forecasts = moomoo.normalize_consensus(conn, raw_fetch_id=consensus_raw, issuer_id=issuer_id)
    ratings = moomoo.normalize_ratings(conn, raw_fetch_id=ratings_raw, issuer_id=issuer_id)
    segments = moomoo.normalize_segments(conn, raw_fetch_id=segments_raw, issuer_id=issuer_id)
    dividends = moomoo.normalize_dividends(
        conn,
        raw_fetch_id=dividends_raw,
        instrument_id=instrument_id,
        listing_id=listing_id,
    )
    assert len(forecasts) == 1
    assert ratings
    assert len(segments) == 6
    assert dividends
    assert moomoo.normalize_consensus(conn, raw_fetch_id=consensus_raw, issuer_id=issuer_id) == forecasts
    assert moomoo.normalize_ratings(conn, raw_fetch_id=ratings_raw, issuer_id=issuer_id) == ratings
    assert moomoo.normalize_segments(conn, raw_fetch_id=segments_raw, issuer_id=issuer_id) == segments
    assert (
        moomoo.normalize_dividends(
            conn,
            raw_fetch_id=dividends_raw,
            instrument_id=instrument_id,
            listing_id=listing_id,
        )
        == dividends
    )

    fetched_at = conn.execute("select fetched_at from raw.fetches where id = %s", (ratings_raw,)).fetchone()[0]
    assert conn.execute(
        "select bool_and(transaction_time >= %s) from staging.analyst_rating_events where company_id = %s",
        (fetched_at, issuer_id),
    ).fetchone()[0]


def test_moomoo_schema_drift_fails_loudly(conn):
    nonce = uuid.uuid4().hex[:10]
    issuer_id = f"company:test:{nonce}"
    er.ensure_entity(conn, issuer_id, "company", "Broken Moomoo Test")
    raw_id = _raw(conn, nonce, "segments", {"period": "2025/FY"})
    with pytest.raises(ValueError, match="period/currency/breakdown/screen dates"):
        moomoo.normalize_segments(conn, raw_fetch_id=raw_id, issuer_id=issuer_id)
