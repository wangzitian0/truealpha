"""Real-Postgres tests for QQQ N-PORT constituent selection (#1820)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from data_engine.datahub.capture_core.universe_plane import (
    UNIVERSE_SOURCES,
    _insert_constituent_facts,
    build_denominator,
)
from data_engine.datahub.confidence_report import load_index_membership
from truealpha_contracts.models import DataSource

pytestmark = pytest.mark.deps


def _insert_raw_fetch(
    connection: psycopg.Connection[Any],
    *,
    source: DataSource,
    record_id: str,
    sha256: str,
    fetched_at: datetime,
) -> int:
    row = connection.execute(
        "insert into raw.fetches"
        " (source, source_record_id, payload_sha256, object_uri, content_type, byte_length, fetched_at, recorded_at)"
        " values (%s, %s, %s, %s, 'application/json', 2, %s, %s) returning id",
        (source.value, record_id, sha256, f"s3://truealpha-raw/test/{record_id}", fetched_at, fetched_at),
    ).fetchone()
    assert row is not None
    return int(row[0])


def test_qqq_build_denominator_and_confidence_prefer_active_nport_source_over_newer_legacy_nasdaq_rows(
    connection: psycopg.Connection[Any],
) -> None:
    """QQQ uses `source='nport'` even when legacy `nasdaq-index` rows have a newer `as_of` date."""
    qqq = UNIVERSE_SOURCES["qqq"]
    assert qqq.nport_ticker == "QQQ"

    legacy_time = datetime(2026, 11, 7, 8, 7, tzinfo=UTC)
    nport_time = datetime(2026, 8, 28, 20, 0, tzinfo=UTC)
    legacy_fetch = _insert_raw_fetch(
        connection,
        source=DataSource.NASDAQ_INDEX,
        record_id="test-legacy-qqq-1820",
        sha256="a" * 64,
        fetched_at=legacy_time,
    )
    nport_fetch = _insert_raw_fetch(
        connection,
        source=DataSource.NPORT,
        record_id="test-nport-qqq-1820",
        sha256="b" * 64,
        fetched_at=nport_time,
    )
    _insert_constituent_facts(
        connection,
        [
            (
                "qqq",
                date(2026, 11, 7),
                DataSource.NASDAQ_INDEX.value,
                legacy_fetch,
                "OLDLEG",
                "Legacy Co",
                None,
                9990001,
                "bbg000oldleg",
                legacy_time,
                "1000",
                "10",
            ),
            (
                "qqq",
                date(2026, 6, 30),
                DataSource.NPORT.value,
                nport_fetch,
                "AAPL",
                "Apple Inc.",
                Decimal("8.5"),
                320193,
                "bbg000b9xry4",
                nport_time,
                None,
                None,
            ),
        ],
    )

    denom = build_denominator(connection, qqq, report_date=date(2026, 6, 30))
    tickers = [row[3] for row in denom["instruments"]]
    assert tickers == ["AAPL"]
    assert "OLDLEG" not in tickers

    origins, notes = load_index_membership(connection, qqq, datetime(2026, 11, 25, tzinfo=UTC))
    assert notes["constituent_count"] == 1
    assert "listing:xnas:aapl" in origins["index_membership"]
    assert "listing:xnas:oldleg" not in origins["index_membership"]
    assert [o.origin_id for o in origins["index_membership"]["listing:xnas:aapl"]] == ["origin:nport:v1"]
    assert [o.lineage for o in origins["index_membership"]["listing:xnas:aapl"]] == ["sec-nport"]
