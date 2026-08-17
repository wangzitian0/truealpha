"""The data-driven universe plane (#539): parse real bytes, land, publish, resolve.

The cassette is the exact byte string the index operator answered on 2026-08-17
(sha-anchored, #569's pattern); the round-trip test drives the same functions the
refresh job runs, against the real database, and proves the capture pipeline can
resolve its universe from the governed head — the property that replaced the
checked-in corpus file this plane exists to eliminate.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from pathlib import Path

import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub.production_topt.universe_corpus import corpus_list_version
from data_engine.datahub.production_topt.universe_plane import (
    UNIVERSE_SOURCES,
    build_denominator,
    current_head_mapping_sha,
    parse_nasdaq_index_rows,
    publish_universe_list,
    resolve_universe_corpus,
)
from truealpha_contracts.models import DataSource

_CASSETTE = Path(__file__).parent / "cassettes" / "nasdaq100_index.bcc3fb15.json"
_CASSETTE_SHA = "bcc3fb150a3988b699eba12e798e950e0ec0e7d55f4d99765c69dd528f087518"


@pytest.fixture
def connection():
    try:
        active = psycopg.connect(settings.database_url, connect_timeout=3, autocommit=False)
    except psycopg.OperationalError as error:
        pytest.skip(f"no local database: {error}")
    try:
        yield active
    finally:
        active.rollback()
        active.close()


def _bytes() -> bytes:
    body = _CASSETTE.read_bytes()
    assert hashlib.sha256(body).hexdigest() == _CASSETTE_SHA, "cassette is not the operator's bytes"
    return body


def test_the_operator_bytes_parse_to_the_full_index() -> None:
    rows = parse_nasdaq_index_rows(_bytes())
    assert len(rows) == 102
    tickers = {row["ticker"] for row in rows}
    assert {"AAPL", "NVDA", "GOOG", "GOOGL"} <= tickers
    assert all(row["name"] for row in rows)


def test_a_truncated_response_refuses_to_parse() -> None:
    import json

    payload = json.loads(_bytes())
    payload["data"]["data"]["rows"] = payload["data"]["data"]["rows"][:40]
    with pytest.raises(ValueError, match="only 40 rows"):
        parse_nasdaq_index_rows(json.dumps(payload).encode())


def test_land_publish_resolve_round_trip(connection) -> None:
    """Rows land with lineage; the published head resolves as a validated corpus."""
    source = UNIVERSE_SOURCES["qqq"]
    body = _bytes()
    fetched_at = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
    from data_engine import raw_store

    fetch_id = raw_store.insert_fetch(
        connection,
        source=DataSource.NASDAQ_INDEX,
        source_record_id="index-constituents:qqq:test",
        body=body,
        content_type="application/json",
        fetched_at=fetched_at,
        recorded_at=fetched_at,
    )
    for i, row in enumerate(parse_nasdaq_index_rows(body)):
        connection.execute(
            """
            insert into staging.etf_constituent_facts
                (etf_symbol, as_of, source, raw_fetch_id, ticker, company_name, cik, figi, knowable_at)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                "qqq",
                date(2026, 8, 17),
                DataSource.NASDAQ_INDEX.value,
                fetch_id,
                row["ticker"],
                row["name"],
                100000 + i,  # resolution is exercised live by the refresh; here the
                f"bbg{i:09d}",  # plane's own shape and the publish/resolve legs are under test
                fetched_at,
            ),
        )

    denominator = build_denominator(connection, source, report_date=date(2026, 6, 30))
    assert denominator["instrument_count"] == 102
    assert denominator["obligation_count"] == 408

    assert current_head_mapping_sha(connection, source) != denominator["instrument_mapping_sha256"]
    contract_id, sequence = publish_universe_list(
        connection, source, report_date=date(2026, 6, 30), note="round-trip test"
    )
    assert contract_id.startswith("universe-list:")
    assert current_head_mapping_sha(connection, source) == denominator["instrument_mapping_sha256"]

    corpus = resolve_universe_corpus(connection, source.head_kind)
    version = corpus_list_version(corpus)
    assert len(version.members) == 102
    assert version.universe.universe_id == "universe:qqq-us-2026-06-30"


def test_resolving_an_unpublished_universe_fails_loud(connection) -> None:
    with pytest.raises(LookupError, match="no published universe head"):
        resolve_universe_corpus(connection, "universe-list:never-published")
