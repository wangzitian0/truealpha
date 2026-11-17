"""Dynamic Point-in-Time (PIT) Universe Masking (#938 layer 1)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import psycopg

from data_engine.datahub.market_prices import parse_decimal, snap_to_last_xnys_session_of_month


class UniverseMaskReason:
    OK = "ok"
    UNLISTED = "unlisted"
    INSUFFICIENT_HISTORY = "unavailable:insufficient_history"
    SUSPENDED = "suspended"


@dataclass(frozen=True)
class UniverseMaskRecord:
    symbol: str
    cutoff_date: date
    eligible: bool
    reason_code: str
    resolution: str
    computed_at: datetime | None = None


def _bar_field(bar: Any, *keys: str) -> Any:
    for key in keys:
        if hasattr(bar, key):
            return getattr(bar, key)
        if isinstance(bar, Mapping) and key in bar:
            return bar[key]
    return None


def _extract_bar_date(bar: Any) -> date:
    raw = _bar_field(bar, "date", "datetime")
    if raw is None:
        raise ValueError(f"Unable to extract date from bar: {bar!r}")
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    if isinstance(raw, str):
        return date.fromisoformat(raw[:10])
    raise TypeError(f"Unsupported date type {type(raw)} in bar: {bar!r}")


def evaluate_symbol_pit(
    symbol: str,
    cutoff_date: date,
    *,
    listing_date: date | None = None,
    bars: Sequence[Any],
    min_periods: int = 12,
    resolution: str = "1M",
) -> UniverseMaskRecord:
    def mask(eligible: bool, reason: str) -> UniverseMaskRecord:
        return UniverseMaskRecord(symbol, cutoff_date, eligible, reason, resolution)

    if listing_date is not None and listing_date > cutoff_date:
        return mask(False, UniverseMaskReason.UNLISTED)

    pit_bars = [bar for bar in bars if _extract_bar_date(bar) <= cutoff_date]
    if not pit_bars:
        return mask(
            False, UniverseMaskReason.UNLISTED if listing_date is None else UniverseMaskReason.INSUFFICIENT_HISTORY
        )
    if len(pit_bars) < min_periods:
        return mask(False, UniverseMaskReason.INSUFFICIENT_HISTORY)

    latest = max(pit_bars, key=_extract_bar_date)
    expected = snap_to_last_xnys_session_of_month(cutoff_date) if resolution == "1M" else cutoff_date
    vol = parse_decimal(_bar_field(latest, "volume"))
    cls = parse_decimal(_bar_field(latest, "close"))
    if (
        _extract_bar_date(latest) != expected
        or bool(_bar_field(latest, "is_suspended"))
        or vol is None
        or vol <= 0
        or cls is None
        or cls <= 0
    ):
        return mask(False, UniverseMaskReason.SUSPENDED)
    return mask(True, UniverseMaskReason.OK)


def compute_universe_mask(
    symbols: Sequence[str],
    cutoff_dates: Sequence[date],
    prices: Sequence[Any],
    *,
    listing_dates: Mapping[str, date] | None = None,
    min_periods: int = 12,
    resolution: str = "1M",
) -> list[UniverseMaskRecord]:
    listing_dates_map = listing_dates or {}
    bars_by_symbol: dict[str, list[Any]] = {sym: [] for sym in symbols}
    for bar in prices:
        if (sym := _bar_field(bar, "symbol")) in bars_by_symbol:
            bars_by_symbol[sym].append(bar)
    return [
        evaluate_symbol_pit(
            symbol=sym,
            cutoff_date=cutoff,
            listing_date=listing_dates_map.get(sym),
            bars=bars_by_symbol.get(sym, []),
            min_periods=min_periods,
            resolution=resolution,
        )
        for cutoff in cutoff_dates
        for sym in symbols
    ]


def persist_universe_mask(connection: psycopg.Connection, records: Sequence[UniverseMaskRecord]) -> int:
    if not records:
        return 0
    query = (
        "insert into staging.universe_mask (symbol, cutoff_date, resolution, eligible, reason_code, computed_at) "
        "values (%s, %s, %s, %s, %s, clock_timestamp()) "
        "on conflict (symbol, cutoff_date, resolution) do update set "
        "eligible = excluded.eligible, reason_code = excluded.reason_code, computed_at = excluded.computed_at;"
    )
    rows = [(rec.symbol, rec.cutoff_date, rec.resolution, rec.eligible, rec.reason_code) for rec in records]
    with connection.cursor() as cur:
        cur.executemany(query, rows)
    return len(rows)


def compute_and_persist_universe_mask_from_db(
    connection: psycopg.Connection,
    symbols: Sequence[str],
    cutoff_dates: Sequence[date],
    *,
    adjust: str,
    source_table: str = "staging.market_prices_monthly",
    listing_dates: Mapping[str, date] | None = None,
    min_periods: int = 12,
    resolution: str = "1M",
) -> list[UniverseMaskRecord]:
    if not symbols or not cutoff_dates:
        return []
    query = (
        f"select symbol, trading_date, open, high, low, close, volume from {source_table} "
        "where symbol = any(%s) and trading_date <= %s and adjust = %s order by trading_date asc;"
    )
    with connection.cursor() as cur:
        cur.execute(query, (list(symbols), max(cutoff_dates), adjust))
        rows = cur.fetchall()
    keys = ("symbol", "date", "open", "high", "low", "close", "volume")
    records = compute_universe_mask(
        symbols=symbols,
        cutoff_dates=cutoff_dates,
        prices=[dict(zip(keys, row, strict=True)) for row in rows],
        listing_dates=listing_dates,
        min_periods=min_periods,
        resolution=resolution,
    )
    persist_universe_mask(connection, records)
    return records
