"""Dynamic Point-in-Time (PIT) Universe Masking (#938 layer 1).

Provides:
- Point-in-Time (PIT) universe filtering:
  - If a stock has not had its IPO yet (listing_date > t), eligible=False, reason_code='unlisted'
  - If a stock is listed but historical lookback < min_periods (e.g. < 12 months for a 12-month factor),
    eligible=False, reason_code='unavailable:insufficient_history'
  - If stock is suspended or missing bar, eligible=False, reason_code='suspended'
  - Eligible and active: eligible=True, reason_code='ok'
- Strictly NO future data used (bars after cutoff date t are discarded before evaluation)
- Persistence to staging.universe_mask, keyed by (symbol, cutoff_date, resolution)

A mask row's ABSENCE means "ineligible" (fail-closed) -- the caller side of that contract
(`compile_factor_panel`/`compute_topk_dropout_weights` in the layer-2 adapter) is out of
this module's scope; this module only ever WRITES a row for every (symbol, cutoff) pair it
is asked to evaluate, so a caller reading "no row" as ineligible sees the true absence of a
decision, never a decision this module made and declined to persist.

`resolution` is part of the primary key (#938 contract item 2): the draft's PK was
(symbol, cutoff_date), so the same as_of writing 1D then 1M had the second overwrite the
first's row. Persisting is still a plain upsert -- this table is a deterministic
recomputation from the immutable price tables, not a raw ingested fact, so re-deriving the
same (symbol, cutoff_date, resolution) triple is expected and safe to replace in place.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import psycopg

from data_engine.datahub.market_prices import snap_to_last_xnys_session_of_month

log = logging.getLogger(__name__)


class UniverseMaskReason:
    """Reason codes for universe eligibility mask."""

    OK = "ok"
    UNLISTED = "unlisted"
    INSUFFICIENT_HISTORY = "unavailable:insufficient_history"
    SUSPENDED = "suspended"


@dataclass(frozen=True)
class UniverseMaskRecord:
    """One symbol's eligibility evaluation at a specific point-in-time cutoff date.

    `resolution` travels on the record itself (not just as a function argument that got
    dropped before persistence, #938 contract item 2): `persist_universe_mask` writes
    exactly the columns this dataclass carries, so a resolution the caller evaluated with
    but this record forgot could never reach the primary key.
    """

    symbol: str
    cutoff_date: date
    eligible: bool
    reason_code: str
    resolution: str
    computed_at: datetime | None = None


def _extract_bar_date(bar: Any) -> date:
    """Extract date from a bar object, dataclass, or dictionary."""
    if hasattr(bar, "date"):
        d = getattr(bar, "date")
    elif isinstance(bar, Mapping) and "date" in bar:
        d = bar["date"]
    elif isinstance(bar, Mapping) and "datetime" in bar:
        d = bar["datetime"]
    else:
        raise ValueError(f"Unable to extract date from bar: {bar!r}")

    if isinstance(d, date):
        return d
    if isinstance(d, str):
        return date.fromisoformat(d[:10])
    raise TypeError(f"Unsupported date type {type(d)} in bar: {bar!r}")


def _extract_bar_volume(bar: Any) -> Decimal | None:
    """Extract volume from a bar object or dictionary."""
    if hasattr(bar, "volume"):
        val = getattr(bar, "volume")
    elif isinstance(bar, Mapping) and "volume" in bar:
        val = bar["volume"]
    else:
        return None

    if val is None:
        return None
    try:
        return Decimal(str(val))
    except Exception:
        return None


def _extract_bar_close(bar: Any) -> Decimal | None:
    """Extract close price from a bar object or dictionary."""
    if hasattr(bar, "close"):
        val = getattr(bar, "close")
    elif isinstance(bar, Mapping) and "close" in bar:
        val = bar["close"]
    else:
        return None

    if val is None:
        return None
    try:
        return Decimal(str(val))
    except Exception:
        return None


def _extract_is_suspended(bar: Any) -> bool:
    """Extract explicit suspended flag if present."""
    if hasattr(bar, "is_suspended"):
        return bool(getattr(bar, "is_suspended"))
    if isinstance(bar, Mapping) and "is_suspended" in bar:
        return bool(bar["is_suspended"])
    return False


def evaluate_symbol_pit(
    symbol: str,
    cutoff_date: date,
    *,
    listing_date: date | None = None,
    bars: Sequence[Any],
    min_periods: int = 12,
    resolution: str = "1M",
) -> UniverseMaskRecord:
    """Evaluate PIT universe eligibility for one symbol at cutoff_date t.

    Rules:
    1. Unlisted check: if listing_date > cutoff_date, eligible=False, reason_code='unlisted'
    2. Strictly NO future data: filter bars where bar.date <= cutoff_date
    3. Insufficient history check: if count(bars <= cutoff_date) < min_periods,
       eligible=False, reason_code='unavailable:insufficient_history'
    4. Suspended / missing bar check: if latest bar is not for cutoff period,
       or volume is 0/None, or close is 0/None/negative,
       eligible=False, reason_code='suspended'
    5. Otherwise: eligible=True, reason_code='ok'
    """
    # Rule 1: IPO / listing date in the future
    if listing_date is not None and listing_date > cutoff_date:
        return UniverseMaskRecord(
            symbol=symbol,
            cutoff_date=cutoff_date,
            eligible=False,
            reason_code=UniverseMaskReason.UNLISTED,
            resolution=resolution,
        )

    # Rule 2: Strictly NO future data used
    pit_bars = [bar for bar in bars if _extract_bar_date(bar) <= cutoff_date]

    if not pit_bars:
        # If listing date was provided and in the past, it's missing history; else unlisted
        reason = (
            UniverseMaskReason.INSUFFICIENT_HISTORY
            if listing_date is not None and listing_date <= cutoff_date
            else UniverseMaskReason.UNLISTED
        )
        return UniverseMaskRecord(
            symbol=symbol,
            cutoff_date=cutoff_date,
            eligible=False,
            reason_code=reason,
            resolution=resolution,
        )

    # Rule 3: Lookback history check (< min_periods)
    if len(pit_bars) < min_periods:
        return UniverseMaskRecord(
            symbol=symbol,
            cutoff_date=cutoff_date,
            eligible=False,
            reason_code=UniverseMaskReason.INSUFFICIENT_HISTORY,
            resolution=resolution,
        )

    # Rule 4: Suspended or missing bar for the current cutoff session
    latest_bar = max(pit_bars, key=_extract_bar_date)
    latest_date = _extract_bar_date(latest_bar)

    # Check if latest bar matches the cutoff period
    if resolution == "1M":
        expected_session = snap_to_last_xnys_session_of_month(cutoff_date)
        # Matches if the latest bar date is the expected session, or in the same month
        has_current_bar = (latest_date == expected_session) or (
            latest_date.year == cutoff_date.year and latest_date.month == cutoff_date.month
        )
    else:
        # Daily: exact date match
        has_current_bar = latest_date == cutoff_date

    if not has_current_bar:
        return UniverseMaskRecord(
            symbol=symbol,
            cutoff_date=cutoff_date,
            eligible=False,
            reason_code=UniverseMaskReason.SUSPENDED,
            resolution=resolution,
        )

    # Check trading activity on latest bar
    if _extract_is_suspended(latest_bar):
        return UniverseMaskRecord(
            symbol=symbol,
            cutoff_date=cutoff_date,
            eligible=False,
            reason_code=UniverseMaskReason.SUSPENDED,
            resolution=resolution,
        )

    vol = _extract_bar_volume(latest_bar)
    cls = _extract_bar_close(latest_bar)

    if vol is None or vol <= 0 or cls is None or cls <= 0:
        return UniverseMaskRecord(
            symbol=symbol,
            cutoff_date=cutoff_date,
            eligible=False,
            reason_code=UniverseMaskReason.SUSPENDED,
            resolution=resolution,
        )

    # All checks passed
    return UniverseMaskRecord(
        symbol=symbol,
        cutoff_date=cutoff_date,
        eligible=True,
        reason_code=UniverseMaskReason.OK,
        resolution=resolution,
    )


def compute_universe_mask(
    symbols: Sequence[str],
    cutoff_dates: Sequence[date],
    prices: Sequence[Any],
    *,
    listing_dates: Mapping[str, date] | None = None,
    min_periods: int = 12,
    resolution: str = "1M",
) -> list[UniverseMaskRecord]:
    """Compute dynamic universe mask for multiple symbols across multiple cutoff dates.

    `prices` can be a sequence of PriceBarRecord or dictionaries with keys
    'symbol', 'date', 'close', 'volume'. `cutoff_dates` may span the full ingested
    history (#938 contract item 2's backfill requirement) -- nothing here bounds it to
    a single as_of date; that bound lived only in the lane's caller.
    """
    listing_dates_map = listing_dates or {}

    # Group bars by symbol
    bars_by_symbol: dict[str, list[Any]] = {sym: [] for sym in symbols}
    for bar in prices:
        sym = getattr(bar, "symbol", None) or (bar.get("symbol") if isinstance(bar, Mapping) else None)
        if sym in bars_by_symbol:
            bars_by_symbol[sym].append(bar)

    records: list[UniverseMaskRecord] = []
    for cutoff in cutoff_dates:
        for sym in symbols:
            rec = evaluate_symbol_pit(
                symbol=sym,
                cutoff_date=cutoff,
                listing_date=listing_dates_map.get(sym),
                bars=bars_by_symbol.get(sym, []),
                min_periods=min_periods,
                resolution=resolution,
            )
            records.append(rec)

    return records


def persist_universe_mask(
    connection: psycopg.Connection,
    records: Sequence[UniverseMaskRecord],
) -> int:
    """Upsert universe mask records into staging.universe_mask, keyed by
    (symbol, cutoff_date, resolution) (#938 contract item 2). Safe to upsert: this table
    is a deterministic recomputation from the immutable price tables, not a raw fact --
    see the module docstring."""
    if not records:
        return 0

    query = """
        insert into staging.universe_mask (
            symbol, cutoff_date, resolution, eligible, reason_code, computed_at
        ) values (%s, %s, %s, %s, %s, clock_timestamp())
        on conflict (symbol, cutoff_date, resolution) do update set
            eligible = excluded.eligible,
            reason_code = excluded.reason_code,
            computed_at = excluded.computed_at;
    """
    rows = [(r.symbol, r.cutoff_date, r.resolution, r.eligible, r.reason_code) for r in records]
    with connection.cursor() as cur:
        cur.executemany(query, rows)
    return len(rows)


def compute_and_persist_universe_mask_from_db(
    connection: psycopg.Connection,
    symbols: Sequence[str],
    cutoff_dates: Sequence[date],
    *,
    source_table: str = "staging.market_prices_monthly",
    listing_dates: Mapping[str, date] | None = None,
    min_periods: int = 12,
    resolution: str = "1M",
) -> list[UniverseMaskRecord]:
    """Load historical prices from database, compute PIT universe mask, and persist."""
    if not symbols or not cutoff_dates:
        return []

    max_cutoff = max(cutoff_dates)

    # Query only data up to max_cutoff
    query = f"""
        select symbol, trading_date, open, high, low, close, volume
        from {source_table}
        where symbol = any(%s) and trading_date <= %s
        order by trading_date asc;
    """
    with connection.cursor() as cur:
        cur.execute(query, (list(symbols), max_cutoff))
        rows = cur.fetchall()

    prices = [
        {
            "symbol": r[0],
            "date": r[1],
            "open": r[2],
            "high": r[3],
            "low": r[4],
            "close": r[5],
            "volume": r[6],
        }
        for r in rows
    ]

    records = compute_universe_mask(
        symbols=symbols,
        cutoff_dates=cutoff_dates,
        prices=prices,
        listing_dates=listing_dates,
        min_periods=min_periods,
        resolution=resolution,
    )

    persist_universe_mask(connection, records)
    return records
