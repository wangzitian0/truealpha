"""Normalize one immutable Yahoo chart response into prices and actions."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from truealpha_contracts import canonical_sha256

from data_engine import raw_store
from data_engine.normalizers import lineage

MAPPING_VERSION = "yahoo-chart:1"
PRICE_CONFIDENCE = Decimal("0.8")
ACTION_CONFIDENCE = Decimal("0.8")


def _result(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Yahoo chart payload must be an object")
    chart = payload.get("chart")
    if not isinstance(chart, dict) or chart.get("error") is not None:
        error = chart.get("error") if isinstance(chart, dict) else chart
        raise ValueError(f"Yahoo chart response error: {error}")
    results = chart.get("result")
    if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
        raise ValueError("Yahoo chart payload must contain exactly one result")
    return results[0]


def _market_close(day: date, timezone_name: str) -> datetime:
    return datetime.combine(day, time(16), tzinfo=ZoneInfo(timezone_name)).astimezone(UTC)


def _existing_price_id(
    conn,
    *,
    instrument_id: str,
    listing_id: str,
    trading_date: date,
    knowable_at: datetime,
    raw_ref: str,
) -> int:
    row = conn.execute(
        """
        select id from staging.market_prices
        where instrument_id = %s and listing_id = %s and trading_date = %s
          and source = 'yahoo' and transaction_time = %s and raw_ref = %s
          and mapping_version = %s
        """,
        (instrument_id, listing_id, trading_date, knowable_at, raw_ref, MAPPING_VERSION),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"could not recover Yahoo price row for {instrument_id}/{trading_date}")
    return row[0]


def _semantic_price_id(
    conn,
    *,
    instrument_id: str,
    listing_id: str,
    trading_date: date,
    knowable_at: datetime,
    values: dict[str, Any],
    adjusted_close: Any,
    currency: str,
) -> int | None:
    row = conn.execute(
        """
        select id from staging.market_prices
        where instrument_id = %s and listing_id = %s and trading_date = %s
          and transaction_time = %s and source = 'yahoo' and mapping_version = %s
          and open = %s and high = %s and low = %s and close = %s
          and adjusted_close = %s and volume = %s and currency = %s
          and price_policy = 'raw_plus_actions'
        order by id limit 1
        """,
        (
            instrument_id,
            listing_id,
            trading_date,
            knowable_at,
            MAPPING_VERSION,
            Decimal(str(values["open"])),
            Decimal(str(values["high"])),
            Decimal(str(values["low"])),
            Decimal(str(values["close"])),
            Decimal(str(adjusted_close)),
            int(values["volume"]),
            currency,
        ),
    ).fetchone()
    return None if row is None else row[0]


def normalize_fetch(
    conn,
    *,
    raw_fetch_id: int,
    issuer_id: str,
    instrument_id: str,
    listing_id: str,
    symbol: str,
    currency: str = "USD",
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    payload = json.loads(raw_store.get_payload(conn, raw_fetch_id))
    fetched_at = conn.execute("select fetched_at from raw.fetches where id = %s", (raw_fetch_id,)).fetchone()[0]
    result = _result(payload)
    timestamps = result.get("timestamp") or []
    indicators = result.get("indicators")
    if not isinstance(indicators, dict) or not isinstance(indicators.get("quote"), list):
        raise ValueError("Yahoo chart payload lost indicators.quote")
    quote = indicators["quote"][0]
    if not isinstance(quote, dict):
        raise ValueError("Yahoo chart quote must be an object")
    adjusted_sets = indicators.get("adjclose") or []
    adjusted = adjusted_sets[0].get("adjclose") if adjusted_sets else quote.get("close")
    if not isinstance(adjusted, list):
        raise ValueError("Yahoo chart payload lost adjusted close series")
    timezone_name = str(result.get("meta", {}).get("exchangeTimezoneName") or "America/New_York")
    raw_ref = raw_store.raw_ref(raw_fetch_id)
    recorded_at = datetime.now(UTC)
    price_ids: list[int] = []

    required_series: dict[str, list[Any]] = {}
    for field in ("open", "high", "low", "close", "volume"):
        series = quote.get(field)
        if not isinstance(series, list):
            raise ValueError("Yahoo chart quote lost an OHLCV series")
        required_series[field] = series
    for index, timestamp in enumerate(timestamps):
        values = {field: series[index] for field, series in required_series.items()}
        if any(values[field] is None for field in ("open", "high", "low", "close", "volume")):
            continue
        trading_date = datetime.fromtimestamp(timestamp, tz=UTC).date()
        knowable_at = _market_close(trading_date, timezone_name)
        if knowable_at > fetched_at:
            continue
        semantic_id = _semantic_price_id(
            conn,
            instrument_id=instrument_id,
            listing_id=listing_id,
            trading_date=trading_date,
            knowable_at=knowable_at,
            values=values,
            adjusted_close=adjusted[index],
            currency=currency,
        )
        row = (
            None
            if semantic_id is not None
            else conn.execute(
                """
            insert into staging.market_prices
                (unified_id, instrument_id, listing_id, symbol, trading_date,
                 open, high, low, close, adjusted_close, volume, transaction_time,
                 recorded_at, source, raw_ref, currency, confidence, mapping_version, price_policy)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, 'yahoo', %s, %s, %s, %s, 'raw_plus_actions')
            on conflict do nothing returning id
            """,
                (
                    issuer_id,
                    instrument_id,
                    listing_id,
                    symbol,
                    trading_date,
                    Decimal(str(values["open"])),
                    Decimal(str(values["high"])),
                    Decimal(str(values["low"])),
                    Decimal(str(values["close"])),
                    Decimal(str(adjusted[index])),
                    int(values["volume"]),
                    knowable_at,
                    recorded_at,
                    raw_ref,
                    currency,
                    PRICE_CONFIDENCE,
                    MAPPING_VERSION,
                ),
            ).fetchone()
        )
        record_id = (
            semantic_id
            if semantic_id is not None
            else row[0]
            if row is not None
            else _existing_price_id(
                conn,
                instrument_id=instrument_id,
                listing_id=listing_id,
                trading_date=trading_date,
                knowable_at=knowable_at,
                raw_ref=raw_ref,
            )
        )
        price_ids.append(record_id)
        lineage.link(
            conn,
            table="market_prices",
            record_id=record_id,
            raw_ref=raw_ref,
            mapping_version=MAPPING_VERSION,
        )

    action_ids: list[int] = []
    events = result.get("events") or {}
    for event_kind, action_type in (("dividends", "cash_dividend"), ("splits", "split")):
        by_timestamp = events.get(event_kind) or {}
        if not isinstance(by_timestamp, dict):
            raise ValueError(f"Yahoo chart events.{event_kind} must be an object")
        for event in by_timestamp.values():
            if not isinstance(event, dict) or event.get("date") is None:
                raise ValueError(f"Yahoo {event_kind} event lost date")
            event_day = datetime.fromtimestamp(int(event["date"]), tz=UTC).date()
            knowable_at = _market_close(event_day, timezone_name)
            amount = Decimal(str(event["amount"])) if action_type == "cash_dividend" else None
            numerator = event.get("numerator")
            denominator = event.get("denominator")
            if action_type == "split":
                if numerator is None or denominator in (None, 0):
                    raise ValueError("Yahoo split lost numerator/denominator")
                ratio = Decimal(str(numerator)) / Decimal(str(denominator))
            else:
                ratio = None
            event_id = "action:" + canonical_sha256(
                {
                    "instrument_id": instrument_id,
                    "source": "yahoo",
                    "action_type": action_type,
                    "event_day": event_day.isoformat(),
                    "amount": str(amount) if amount is not None else None,
                    "ratio": str(ratio) if ratio is not None else None,
                }
            )
            existing_semantic = conn.execute(
                """
                select id from staging.corporate_actions
                where action_event_id = %s and instrument_id = %s and listing_id = %s
                  and action_type = %s and ex_date = %s and effective_date = %s
                  and ratio is not distinct from %s and cash_amount is not distinct from %s
                  and currency is not distinct from %s and source = 'yahoo'
                  and mapping_version = %s
                order by id limit 1
                """,
                (
                    event_id,
                    instrument_id,
                    listing_id,
                    action_type,
                    event_day,
                    event_day,
                    ratio,
                    amount,
                    currency if action_type == "cash_dividend" else None,
                    MAPPING_VERSION,
                ),
            ).fetchone()
            row = (
                None
                if existing_semantic is not None
                else conn.execute(
                    """
                insert into staging.corporate_actions
                    (action_event_id, instrument_id, listing_id, action_type,
                     ex_date, effective_date, ratio, cash_amount, currency,
                     transaction_time, recorded_at, confidence, source, raw_ref, mapping_version)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, 'yahoo', %s, %s)
                on conflict do nothing returning id
                """,
                    (
                        event_id,
                        instrument_id,
                        listing_id,
                        action_type,
                        event_day,
                        event_day,
                        ratio,
                        amount,
                        currency if action_type == "cash_dividend" else None,
                        knowable_at,
                        recorded_at,
                        ACTION_CONFIDENCE,
                        raw_ref,
                        MAPPING_VERSION,
                    ),
                ).fetchone()
            )
            if row is None and existing_semantic is None:
                row = conn.execute(
                    """
                    select id from staging.corporate_actions
                    where action_event_id = %s and transaction_time = %s
                      and source = 'yahoo' and raw_ref = %s and mapping_version = %s
                    """,
                    (event_id, knowable_at, raw_ref, MAPPING_VERSION),
                ).fetchone()
            if existing_semantic is not None:
                record_id = existing_semantic[0]
            else:
                if row is None:
                    raise RuntimeError(f"could not persist or recover Yahoo action {event_id}")
                record_id = row[0]
            action_ids.append(record_id)
            lineage.link(
                conn,
                table="corporate_actions",
                record_id=record_id,
                raw_ref=raw_ref,
                mapping_version=MAPPING_VERSION,
            )
    return tuple(sorted(set(price_ids))), tuple(sorted(set(action_ids)))
