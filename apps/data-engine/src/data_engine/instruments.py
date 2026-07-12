"""Point-in-time instrument, listing, and universe membership repository."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal


def ensure_instrument(conn, instrument_id: str, instrument_type: str, display_name: str) -> None:
    conn.execute(
        """
        insert into staging.instruments (instrument_id, instrument_type, display_name)
        values (%s, %s, %s)
        on conflict (instrument_id) do nothing
        """,
        (instrument_id, instrument_type, display_name),
    )


def assert_issuer_link(
    conn,
    *,
    instrument_id: str,
    issuer_id: str,
    valid_from: str,
    transaction_time: datetime,
    confidence: Decimal,
    source: str,
    raw_ref: str,
    mapping_version: str,
) -> int | None:
    latest = conn.execute(
        """
        select issuer_id, confidence, source, raw_ref, mapping_version
        from staging.instrument_issuer_links
        where instrument_id = %s
        order by transaction_time desc, id desc limit 1
        """,
        (instrument_id,),
    ).fetchone()
    expected = (issuer_id, confidence, source, raw_ref, mapping_version)
    if latest is not None and tuple(map(str, latest)) == tuple(map(str, expected)):
        return None
    row = conn.execute(
        """
        insert into staging.instrument_issuer_links
            (instrument_id, issuer_id, valid_time, transaction_time, confidence,
             source, raw_ref, mapping_version)
        values (%s, %s, daterange(%s::date, null, '[)'), %s, %s, %s, %s, %s)
        on conflict do nothing returning id
        """,
        (
            instrument_id,
            issuer_id,
            valid_from,
            transaction_time,
            confidence,
            source,
            raw_ref,
            mapping_version,
        ),
    ).fetchone()
    return None if row is None else row[0]


def assert_identifier(
    conn,
    *,
    instrument_id: str,
    identifier_type: str,
    identifier_value: str,
    valid_from: str,
    transaction_time: datetime,
    confidence: Decimal,
    source: str,
    raw_ref: str,
    mapping_version: str,
) -> int | None:
    latest = conn.execute(
        """
        select instrument_id, confidence, source, raw_ref, mapping_version
        from staging.instrument_identifiers
        where identifier_type = %s and identifier_value = %s
        order by transaction_time desc, id desc limit 1
        """,
        (identifier_type, identifier_value),
    ).fetchone()
    expected = (instrument_id, confidence, source, raw_ref, mapping_version)
    if latest is not None and tuple(map(str, latest)) == tuple(map(str, expected)):
        return None
    row = conn.execute(
        """
        insert into staging.instrument_identifiers
            (instrument_id, identifier_type, identifier_value, valid_time,
             transaction_time, confidence, source, raw_ref, mapping_version)
        values (%s, %s, %s, daterange(%s::date, null, '[)'), %s, %s, %s, %s, %s)
        on conflict do nothing returning id
        """,
        (
            instrument_id,
            identifier_type,
            identifier_value,
            valid_from,
            transaction_time,
            confidence,
            source,
            raw_ref,
            mapping_version,
        ),
    ).fetchone()
    return None if row is None else row[0]


def assert_listing(
    conn,
    *,
    listing_id: str,
    instrument_id: str,
    venue_code: str,
    ticker: str,
    currency: str,
    trading_timezone: str,
    trading_calendar: str,
    price_policy: str,
    is_primary: bool,
    valid_from: str,
    transaction_time: datetime,
    confidence: Decimal,
    source: str,
    raw_ref: str,
    mapping_version: str,
) -> int | None:
    latest = conn.execute(
        """
        select instrument_id, venue_code, ticker, currency, trading_timezone,
               trading_calendar, price_policy, is_primary, confidence, source,
               raw_ref, mapping_version
        from staging.listings where listing_id = %s
        order by transaction_time desc, id desc limit 1
        """,
        (listing_id,),
    ).fetchone()
    expected = (
        instrument_id,
        venue_code,
        ticker,
        currency,
        trading_timezone,
        trading_calendar,
        price_policy,
        is_primary,
        confidence,
        source,
        raw_ref,
        mapping_version,
    )
    if latest is not None and tuple(map(str, latest)) == tuple(map(str, expected)):
        return None
    row = conn.execute(
        """
        insert into staging.listings
            (listing_id, instrument_id, venue_code, ticker, currency,
             trading_timezone, trading_calendar, price_policy, is_primary,
             valid_time, transaction_time, confidence, source, raw_ref, mapping_version)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                daterange(%s::date, null, '[)'), %s, %s, %s, %s, %s)
        on conflict do nothing returning id
        """,
        (
            listing_id,
            instrument_id,
            venue_code,
            ticker,
            currency,
            trading_timezone,
            trading_calendar,
            price_policy,
            is_primary,
            valid_from,
            transaction_time,
            confidence,
            source,
            raw_ref,
            mapping_version,
        ),
    ).fetchone()
    return None if row is None else row[0]


def assert_membership(
    conn,
    *,
    universe_id: str,
    universe_version: str,
    fund_id: str | None,
    issuer_id: str,
    instrument_id: str,
    listing_id: str | None,
    valid_from: str,
    transaction_time: datetime,
    confidence: Decimal,
    source: str,
    raw_ref: str,
    mapping_version: str,
) -> int | None:
    latest = conn.execute(
        """
        select fund_id, issuer_id, listing_id, confidence, source, raw_ref, mapping_version
        from staging.universe_memberships
        where universe_id = %s and universe_version = %s and instrument_id = %s
        order by transaction_time desc, id desc limit 1
        """,
        (universe_id, universe_version, instrument_id),
    ).fetchone()
    expected = (fund_id, issuer_id, listing_id, confidence, source, raw_ref, mapping_version)
    if latest is not None and tuple(map(str, latest)) == tuple(map(str, expected)):
        return None
    row = conn.execute(
        """
        insert into staging.universe_memberships
            (universe_id, universe_version, fund_id, issuer_id, instrument_id,
             listing_id, valid_time, transaction_time, confidence, source,
             raw_ref, mapping_version)
        values (%s, %s, %s, %s, %s, %s, daterange(%s::date, null, '[)'),
                %s, %s, %s, %s, %s)
        on conflict do nothing returning id
        """,
        (
            universe_id,
            universe_version,
            fund_id,
            issuer_id,
            instrument_id,
            listing_id,
            valid_from,
            transaction_time,
            confidence,
            source,
            raw_ref,
            mapping_version,
        ),
    ).fetchone()
    return None if row is None else row[0]


def resolve_instrument(conn, identifier_type: str, value: str, *, as_of: datetime) -> str | None:
    row = conn.execute(
        """
        select instrument_id from staging.instrument_identifiers
        where identifier_type = %s and identifier_value = %s and transaction_time <= %s
        order by transaction_time desc, confidence desc, id desc limit 1
        """,
        (identifier_type, value, as_of),
    ).fetchone()
    return None if row is None else row[0]
