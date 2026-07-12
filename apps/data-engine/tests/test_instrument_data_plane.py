import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from data_engine import instruments
from data_engine.config import settings
from factors.shared import entity_resolution as er
from truealpha_runtime.testing import skip_or_fail

psycopg = pytest.importorskip("psycopg")

T1 = datetime(2026, 5, 28, tzinfo=UTC)
T2 = datetime(2026, 7, 11, tzinfo=UTC)
T3 = datetime(2026, 7, 11, 1, tzinfo=UTC)
REF1 = "raw.fetches:0"
REF2 = "raw.fetches:1"


@pytest.fixture
def conn():
    try:
        connection = psycopg.connect(settings.database_url, connect_timeout=3)
    except psycopg.OperationalError:
        skip_or_fail("no reachable Postgres (make runtime-up && make db-migrate)")
    if connection.execute("select to_regclass('staging.instruments')").fetchone()[0] is None:
        connection.close()
        skip_or_fail("instrument tables missing (make db-migrate)")
    yield connection
    connection.rollback()
    connection.close()


def test_two_share_classes_remain_distinct_under_one_issuer(conn):
    nonce = uuid.uuid4().hex[:10]
    issuer_id = f"company:test:{nonce}"
    goog = f"instrument:isin:US02079K1079-{nonce}"
    googl = f"instrument:isin:US02079K3059-{nonce}"
    er.ensure_entity(conn, issuer_id, "company", "Alphabet Test")

    for instrument_id, isin, ticker in (
        (goog, f"US02079K1079{nonce}", f"GOOG{nonce}"),
        (googl, f"US02079K3059{nonce}", f"GOOGL{nonce}"),
    ):
        instruments.ensure_instrument(conn, instrument_id, "equity_common", ticker)
        assert instruments.assert_issuer_link(
            conn,
            instrument_id=instrument_id,
            issuer_id=issuer_id,
            valid_from="2026-03-31",
            transaction_time=T2,
            confidence=Decimal("0.98"),
            source="openfigi_ranked",
            raw_ref=REF1,
            mapping_version="test:1",
        )
        assert instruments.assert_identifier(
            conn,
            instrument_id=instrument_id,
            identifier_type="isin",
            identifier_value=isin,
            valid_from="2026-03-31",
            transaction_time=T1,
            confidence=Decimal("1"),
            source="nport",
            raw_ref=REF1,
            mapping_version="test:1",
        )
        assert instruments.assert_listing(
            conn,
            listing_id=f"listing:vendor:US.{ticker}",
            instrument_id=instrument_id,
            venue_code="US",
            ticker=ticker,
            currency="USD",
            trading_timezone="America/New_York",
            trading_calendar="XNYS",
            price_policy="raw_plus_actions",
            is_primary=True,
            valid_from="2026-07-12",
            transaction_time=T2,
            confidence=Decimal("0.98"),
            source="openfigi_ranked",
            raw_ref=REF1,
            mapping_version="test:1",
        )

    assert instruments.resolve_instrument(conn, "isin", f"US02079K1079{nonce}", as_of=T1) == goog
    assert instruments.resolve_instrument(conn, "isin", f"US02079K3059{nonce}", as_of=T1) == googl
    assert goog != googl
    linked_issuers = conn.execute(
        "select distinct issuer_id from staging.instrument_issuer_links where instrument_id in (%s, %s)",
        (goog, googl),
    ).fetchall()
    assert linked_issuers == [(issuer_id,)]


def test_semantic_retry_is_idempotent_but_changed_raw_vintage_appends(conn):
    nonce = uuid.uuid4().hex[:10]
    issuer_id = f"company:test:{nonce}"
    instrument_id = f"instrument:isin:US{nonce}"
    er.ensure_entity(conn, issuer_id, "company", "Retry Test")
    instruments.ensure_instrument(conn, instrument_id, "equity_common", "Retry Test")

    kwargs = {
        "instrument_id": instrument_id,
        "issuer_id": issuer_id,
        "valid_from": "2026-03-31",
        "confidence": Decimal("0.9"),
        "source": "openfigi_sec_name_fallback",
        "raw_ref": REF1,
        "mapping_version": "test:1",
    }
    assert instruments.assert_issuer_link(conn, transaction_time=T2, **kwargs)
    assert instruments.assert_issuer_link(conn, transaction_time=T3, **kwargs) is None
    kwargs["raw_ref"] = REF2
    assert instruments.assert_issuer_link(conn, transaction_time=T3, **kwargs)
    assert (
        conn.execute(
            "select count(*) from staging.instrument_issuer_links where instrument_id = %s",
            (instrument_id,),
        ).fetchone()[0]
        == 2
    )


def test_membership_is_pit_and_append_only(conn):
    nonce = uuid.uuid4().hex[:10]
    issuer_id = f"company:test:{nonce}"
    fund_id = f"etf:test:{nonce}"
    instrument_id = f"instrument:isin:US{nonce}"
    er.ensure_entity(conn, issuer_id, "company", "Membership Test")
    er.ensure_entity(conn, fund_id, "etf", "Fund Test")
    instruments.ensure_instrument(conn, instrument_id, "equity_common", "Membership Test")
    row_id = instruments.assert_membership(
        conn,
        universe_id=fund_id,
        universe_version="2026-03-31",
        fund_id=fund_id,
        issuer_id=issuer_id,
        instrument_id=instrument_id,
        listing_id=None,
        valid_from="2026-03-31",
        transaction_time=T1,
        confidence=Decimal("1"),
        source="nport",
        raw_ref=REF1,
        mapping_version="test:1",
    )
    assert row_id is not None
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        conn.execute("update staging.universe_memberships set confidence = 0 where id = %s", (row_id,))
