import hashlib
import os
from decimal import Decimal

import psycopg
import pytest
from data_engine.config import settings
from data_engine.financial_facts_pipeline import (
    SAMPLE_ISSUERS,
    capture_and_write_all,
    capture_and_write_issuer,
)
from truealpha_contracts import RawIngestionEnvelope, RawObjectRef


class MemoryRawObjectStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def store(self, capture) -> RawIngestionEnvelope:
        content_sha256 = hashlib.sha256(capture.body).hexdigest()
        ref = RawObjectRef(
            bucket="financial-facts-fixtures",
            key=content_sha256,
            sha256=content_sha256,
            byte_length=len(capture.body),
            content_type=capture.content_type,
        )
        existing = self.objects.setdefault(ref.uri, capture.body)
        if existing != capture.body:
            raise ValueError("content-addressed raw object collision")
        return RawIngestionEnvelope(
            source=capture.source,
            source_record_id=capture.source_record_id,
            object=ref,
            fetched_at=capture.fetched_at,
            source_published_at=capture.source_published_at,
            metadata=capture.metadata,
        )

    def get(self, ref: RawObjectRef) -> bytes:
        body = self.objects[ref.uri]
        if hashlib.sha256(body).hexdigest() != ref.sha256:
            raise ValueError("raw object checksum mismatch")
        return body


@pytest.fixture
def connection():
    try:
        active = psycopg.connect(settings.database_url, connect_timeout=3, autocommit=False)
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs the required integration coverage")
    try:
        yield active
    finally:
        active.rollback()
        active.close()


def test_jpm_lands_total_assets_and_revenue_but_not_gross_profit(connection) -> None:
    written = capture_and_write_issuer(connection, "jpm", raw_store=MemoryRawObjectStore())

    # JPM (a bank) reports no GrossProfit XBRL tag -- extract_gross_profit
    # correctly returns None, and this pipeline must not fabricate a
    # substitute; it lands only what the filing actually supports.
    assert set(written) == {"total_assets", "revenue"}


def test_every_sample_issuer_lands_total_assets(connection) -> None:
    written = capture_and_write_all(connection, raw_store=MemoryRawObjectStore())

    assert set(written) == set(SAMPLE_ISSUERS)
    for ticker, metrics in written.items():
        assert "total_assets" in metrics, f"{ticker} should always report Assets"


def test_capture_lands_a_real_raw_fetches_row_with_readback(connection) -> None:
    store = MemoryRawObjectStore()

    capture_and_write_issuer(connection, "nvda", raw_store=store)

    row = connection.execute(
        "select source, source_record_id, metadata from raw.fetches where source = 'sec' and source_record_id = %s",
        ("companyfacts:0001045810",),
    ).fetchone()
    assert row is not None
    source, source_record_id, metadata = row
    assert source == "sec"
    assert source_record_id == "companyfacts:0001045810"
    assert metadata["fixture_only"] is True
    assert metadata["ticker"] == "NVDA"
    assert len(store.objects) == 1


def test_staging_rows_carry_lineage_back_to_the_raw_fetch(connection) -> None:
    capture_and_write_issuer(connection, "adm", raw_store=MemoryRawObjectStore())

    rows = connection.execute(
        "select metric, unified_id, source, raw_ref, mapping_version, confidence "
        "from staging.financial_facts where unified_id = 'issuer.adm'"
    ).fetchall()
    by_metric = {row[0]: row for row in rows}
    # ADM actively files two disagreeing revenue tags in the same 10-K
    # (sec_financial_facts.extract_revenue's own docstring) -- revenue
    # correctly lands as an unresolved gap, not a guess.
    assert set(by_metric) == {"total_assets", "gross_profit"}
    for metric, unified_id, source, raw_ref, mapping_version, confidence in rows:
        assert unified_id == "issuer.adm"
        assert source == "sec"
        assert raw_ref.startswith("raw.fetches:")
        assert mapping_version == "sec-companyfacts:1"
        assert confidence == Decimal("0.98")


def test_ensure_kg_entity_creates_the_referenced_issuer(connection) -> None:
    capture_and_write_issuer(connection, "shop", raw_store=MemoryRawObjectStore())

    row = connection.execute(
        "select entity_type, display_name from staging.kg_entities where id = 'issuer.shop'"
    ).fetchone()
    assert row == ("company", "Shopify Inc.")


def test_replaying_the_same_capture_is_idempotent(connection) -> None:
    store = MemoryRawObjectStore()

    first = capture_and_write_issuer(connection, "ddog", raw_store=store)
    second = capture_and_write_issuer(connection, "ddog", raw_store=store)

    assert first == second
    count = connection.execute(
        "select count(*) from staging.financial_facts where unified_id = 'issuer.ddog'"
    ).fetchone()[0]
    assert count == len(first)


def test_staging_financial_facts_is_append_only(connection) -> None:
    capture_and_write_issuer(connection, "meta", raw_store=MemoryRawObjectStore())

    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        connection.execute("update staging.financial_facts set value = 0 where unified_id = 'issuer.meta'")
