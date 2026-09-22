"""Twelve Data market prices ingestion and multi-resolution staging (#938 layer 1).

Ingests the 20 TOPT symbols for:
- 3 years daily (interval='1day')
- 10 years monthly (interval='1month')
via Twelve Data /time_series with outputsize=5000 (1 call per symbol per resolution,
40 calls total).

Provides:
- Built-in token bucket rate limiter (8 calls/min compliant)
- Exponential backoff on HTTP 429
- Snapping monthly dates to the month's last valid trading day (XNYS holidays)
- Append-only persistence to staging.market_prices_daily and staging.market_prices_monthly
  (`db/migrations/20260922T0700_datahub_market_prices_and_universe_mask.sql`): every
  insert is a new row, `transaction_time`/`confidence`/`raw_ref` are written explicitly
  from source properties, and the append-only trigger rejects any in-place UPDATE.

This is the narrow TOPT-backtest OHLCV feed, not the general KG-identity-keyed price
path (`staging.market_prices` / `staging.mvp_market_prices`, #0004/#0021). A future
`BacktestDataGateway.price_bars()` reader turns a row here into a
`truealpha_contracts.models.PriceBar`: this module's `transaction_time` IS that
`PriceBar.knowable_at` (the XNYS session close for the bar's own date, not an
insertion-clock default), and `recorded_at` is its `recorded_at` -- so
`BacktestDataset.reject_lookahead` has a real domain to check against (#938).
"""

from __future__ import annotations

import calendar
import json
import logging
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

import psycopg

from data_engine.config import settings
from data_engine.sources import gateway

log = logging.getLogger(__name__)

# The 20 TOPT symbols spanning the 20 issuers of the TOPT universe
DEFAULT_TOPT_SYMBOLS: tuple[str, ...] = (
    "AAPL",
    "MSFT",
    "GOOGL",
    "AMZN",
    "NVDA",
    "META",
    "TSLA",
    "BRK.B",
    "AVGO",
    "COST",
    "XOM",
    "JPM",
    "JNJ",
    "LLY",
    "MA",
    "MU",
    "NFLX",
    "V",
    "WMT",
    "ABBV",
)

TWELVE_DATA_BASE_URL = "https://api.twelvedata.com"

# Twelve Data's `/time_series` `adjust` parameter is sent explicitly on every call
# (#938 contract item 4). Left unset, the vendor's own default governs the returned
# OHLC, and a silent change to that default would restate every historical bar with no
# way to tell a real market restatement from a vendor policy change. "splits" mirrors
# `open`/`high`/`low`/`close` staying comparable to the adjusted_close consumers expect
# (dividends are handled at the return-calculation layer, not the price series). The
# value actually sent travels with every row (`adjust` column) so a future change to
# this constant does not make old and new rows indistinguishable either.
DEFAULT_ADJUST = "splits"

# Declared single-source confidence grade: this pipeline fetches Twelve Data alone, with
# no independent corroborating origin (unlike `production_topt.market_price_adapter`'s
# multi-origin adapter). One `PRICE_GRADE_STEP` below that adapter's
# `PRIMARY_PRICE_CONFIDENCE` (0.85) for a primary, corroborated close -- a declared
# policy grade, not a per-row measurement.
SINGLE_SOURCE_CONFIDENCE = Decimal("0.75")

_RAW_REF_PREFIX = "twelvedata:time_series"


# -----------------------------------------------------------------------------
# Exchange Calendar & Snapping (XNYS / NYSE)
# -----------------------------------------------------------------------------


def easter_sunday(year: int) -> date:
    """Compute Easter Sunday using the Meeus/Jones/Butcher Gregorian algorithm."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l_val = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_val) // 451
    month = (h + l_val - 7 * m + 114) // 31
    day = ((h + l_val - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def xnys_holidays(year: int) -> set[date]:
    """Compute observed New York Stock Exchange (XNYS) market holidays for a given year."""
    holidays: set[date] = set()

    # 1. New Year's Day (Jan 1)
    ny = date(year, 1, 1)
    if ny.weekday() == 6:  # Sunday -> Monday Jan 2 observed
        holidays.add(date(year, 1, 2))
    elif ny.weekday() != 5:  # Saturday -> per NYSE rule 7.2, not observed on Friday Dec 31
        holidays.add(ny)

    # 2. Martin Luther King, Jr. Day: 3rd Monday in January
    first_mon_jan = 1 + (0 - date(year, 1, 1).weekday()) % 7
    holidays.add(date(year, 1, first_mon_jan + 14))

    # 3. Washington's Birthday (Presidents' Day): 3rd Monday in February
    first_mon_feb = 1 + (0 - date(year, 2, 1).weekday()) % 7
    holidays.add(date(year, 2, first_mon_feb + 14))

    # 4. Good Friday: Friday before Easter Sunday
    easter = easter_sunday(year)
    holidays.add(easter - timedelta(days=2))

    # 5. Memorial Day: last Monday in May
    last_may = date(year, 5, 31)
    holidays.add(last_may - timedelta(days=last_may.weekday()))

    # 6. Juneteenth National Independence Day (June 19, observed since 2021)
    if year >= 2021:
        june19 = date(year, 6, 19)
        if june19.weekday() == 5:  # Saturday -> Friday June 18
            holidays.add(date(year, 6, 18))
        elif june19.weekday() == 6:  # Sunday -> Monday June 20
            holidays.add(date(year, 6, 20))
        else:
            holidays.add(june19)

    # 7. Independence Day (July 4)
    july4 = date(year, 7, 4)
    if july4.weekday() == 5:
        holidays.add(date(year, 7, 3))
    elif july4.weekday() == 6:
        holidays.add(date(year, 7, 5))
    else:
        holidays.add(july4)

    # 8. Labor Day: 1st Monday in September
    first_mon_sep = 1 + (0 - date(year, 9, 1).weekday()) % 7
    holidays.add(date(year, 9, first_mon_sep))

    # 9. Thanksgiving Day: 4th Thursday in November
    first_thu_nov = 1 + (3 - date(year, 11, 1).weekday()) % 7
    holidays.add(date(year, 11, first_thu_nov + 21))

    # 10. Christmas Day (Dec 25)
    xmas = date(year, 12, 25)
    if xmas.weekday() == 5:
        holidays.add(date(year, 12, 24))
    elif xmas.weekday() == 6:
        holidays.add(date(year, 12, 26))
    else:
        holidays.add(xmas)

    return holidays


def is_xnys_trading_day(d: date) -> bool:
    """Check if date `d` is a regular trading session on XNYS."""
    if d.weekday() >= 5:
        return False
    return d not in xnys_holidays(d.year)


def last_xnys_session_of_month(year: int, month: int) -> date:
    """Return the date of the last valid XNYS trading day of the specified month."""
    _, last_day = calendar.monthrange(year, month)
    curr = date(year, month, last_day)
    while not is_xnys_trading_day(curr):
        curr -= timedelta(days=1)
    return curr


def snap_to_last_xnys_session_of_month(d: date) -> date:
    """Snap any date to the last valid XNYS trading session of its month."""
    return last_xnys_session_of_month(d.year, d.month)


def most_recent_xnys_session(d: date) -> date:
    """The most recent XNYS trading session at or before `d`.

    Every 1D cutoff and confidence-window computation reads this instead of a raw wall
    clock date: a weekend or a market holiday has no daily bar, and comparing a raw
    `d` against a bar history the same way `evaluate_symbol_pit` does for 1M would misread
    the whole universe as `suspended` on that date for the same reason #938 contract 1
    documents for the monthly cutoff.
    """
    curr = d
    while not is_xnys_trading_day(curr):
        curr -= timedelta(days=1)
    return curr


_XNYS_TZ = ZoneInfo("America/New_York")
_XNYS_CLOSE_HOUR = 16
_XNYS_EARLY_CLOSE_HOUR = 13


@lru_cache(maxsize=32)
def xnys_early_close_days(year: int) -> set[date]:
    """Compute scheduled New York Stock Exchange (XNYS) 13:00 ET early-close days for a given year.

    XNYS observes three scheduled early closes a year at 13:00 ET:
    1. Day after Thanksgiving (always the 4th Friday in November).
    2. December 24 (Christmas Eve), when it is an XNYS trading day.
    3. July 3 (day before Independence Day), when it is an XNYS trading day (i.e. when July 4
       falls on Tue, Wed, Thu, Fri; note that when July 4 is Saturday, July 3 is the observed
       full holiday; when July 4 is Sunday, July 3 is Friday and regular hours, so early
       close is when July 4 is Tue-Fri).
    """
    early_closes: set[date] = set()

    # 1. Day after Thanksgiving (always the 4th Friday in November)
    first_thu_nov = 1 + (3 - date(year, 11, 1).weekday()) % 7
    thanksgiving = date(year, 11, first_thu_nov + 21)
    early_closes.add(thanksgiving + timedelta(days=1))

    # 2. December 24 (Christmas Eve), when it is an XNYS trading day
    xmas_eve = date(year, 12, 24)
    if is_xnys_trading_day(xmas_eve):
        early_closes.add(xmas_eve)

    # 3. July 3 (day before Independence Day), when it is an XNYS trading day
    # (i.e. when July 4 falls on Tue, Wed, Thu, Fri)
    july4 = date(year, 7, 4)
    if july4.weekday() in (1, 2, 3, 4) and is_xnys_trading_day(date(year, 7, 3)):
        early_closes.add(date(year, 7, 3))

    return early_closes


def xnys_session_close_utc(trading_date: date) -> datetime:
    """The XNYS session close instant for `trading_date`, in UTC.

    This is `transaction_time` for a settled bar: the earliest instant its price was
    publicly knowable, independent of when this pipeline happened to fetch or backfill
    it (`recorded_at` carries that separately). A deterministic function of the calendar
    date alone -- never `datetime.now()` (AGENTS.md: "Write transaction_time explicitly
    from a source property, never an insertion-clock default").
    """
    close_hour = (
        _XNYS_EARLY_CLOSE_HOUR if trading_date in xnys_early_close_days(trading_date.year) else _XNYS_CLOSE_HOUR
    )
    local_close = datetime(trading_date.year, trading_date.month, trading_date.day, close_hour, 0, tzinfo=_XNYS_TZ)
    return local_close.astimezone(UTC)


# -----------------------------------------------------------------------------
# Rate Limiter: Token Bucket
# -----------------------------------------------------------------------------


class TokenBucketRateLimiter:
    """Thread-safe Token Bucket rate limiter compliant with vendor quotas.

    Default: 8 calls per minute (0.1333 tokens/sec) with capacity 8.
    """

    def __init__(
        self,
        rate_per_minute: float = 8.0,
        capacity: float = 8.0,
        time_fn: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        if rate_per_minute <= 0:
            raise ValueError("rate_per_minute must be positive")
        self.rate_per_second = rate_per_minute / 60.0
        self.capacity = capacity
        self.tokens = capacity
        self.time_fn = time_fn
        self.sleep_fn = sleep_fn
        self.last_update = time_fn()
        self._lock = threading.Lock()

    def acquire(self, tokens: float = 1.0) -> float:
        """Acquire `tokens`. Blocks/sleeps if insufficient tokens are available.

        Returns the number of seconds waited.
        """
        with self._lock:
            now = self.time_fn()
            elapsed = max(0.0, now - self.last_update)
            self.last_update = now
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate_per_second)

            if self.tokens < tokens:
                deficit = tokens - self.tokens
                wait_time = deficit / self.rate_per_second
                self.tokens = 0.0
                self.last_update = now + wait_time
                self.sleep_fn(wait_time)
                return wait_time

            self.tokens -= tokens
            return 0.0


# -----------------------------------------------------------------------------
# Twelve Data Client
# -----------------------------------------------------------------------------


class TwelveDataRateLimitError(Exception):
    """Raised when Twelve Data returns 429 and retries are exhausted."""


class TwelveDataApiError(Exception):
    """Raised when Twelve Data returns an API error."""


@dataclass(frozen=True)
class PriceBarRecord:
    symbol: str
    date: date
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    volume: Decimal | None
    source: str = "twelvedata"
    resolution: str = "1D"  # '1D' or '1M'


def parse_decimal(value: Any) -> Decimal | None:
    """Parse numeric fields to Decimal safely."""
    if value is None:
        return None
    val_str = str(value).strip()
    if not val_str or val_str.lower() in ("null", "nan", "none"):
        return None
    try:
        return Decimal(val_str)
    except (InvalidOperation, ValueError):
        return None


class TwelveDataClient:
    """Client for Twelve Data /time_series endpoint with rate limiting and exponential backoff."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = TWELVE_DATA_BASE_URL,
        rate_limiter: TokenBucketRateLimiter | None = None,
        transport_fn: Callable[[str], tuple[int, bytes]] | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        max_retries: int = 5,
        backoff_base: float = 2.0,
    ) -> None:
        self.api_key: str = api_key or str(getattr(settings, "twelve_data_api_key", ""))
        self.base_url = base_url.rstrip("/")
        self.rate_limiter = rate_limiter or TokenBucketRateLimiter(sleep_fn=sleep_fn)
        self.transport_fn = transport_fn or self._default_transport
        self.sleep_fn = sleep_fn
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.call_count = 0

    def _default_transport(self, url: str) -> tuple[int, bytes]:
        """Through the external call ledger (#729): every Twelve Data request this
        client makes is one row in `staging.api_call_ledger`, status-honest (a 4xx/5xx
        body is the vendor's answer, not an exception) like `twelve_data_origin`'s. The
        row is admitted and attributed to a run only when the caller has bound
        `gateway.capacity_scope()`/`gateway.run_scope()` around it -- `lanes.market_data`
        does (#938 contract item 3); a caller that does not is only ledgered, not gated."""
        req = urllib.request.Request(url, headers={"User-Agent": "TrueAlpha-DataEngine/1.0"})
        status, body = gateway.urlopen(
            "twelvedata", "time_series", req, caller="market_prices.fetch_time_series", timeout=30
        )
        return status or 0, body

    def fetch_time_series(
        self,
        symbol: str,
        interval: str,
        outputsize: int = 5000,
        adjust: str = DEFAULT_ADJUST,
        extra_params: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Fetch /time_series for symbol and interval with rate limiting and retry on 429.

        `adjust` is always sent explicitly (#938 contract item 4): see `DEFAULT_ADJUST`.
        """
        vendor_symbol = symbol.replace(".", "/") if "." in symbol else symbol
        params: dict[str, str] = {
            "symbol": vendor_symbol,
            "interval": interval,
            "outputsize": str(outputsize),
            "adjust": adjust,
            "apikey": self.api_key,
        }
        if extra_params:
            params.update(extra_params)

        query = urllib.parse.urlencode(params)
        url = f"{self.base_url}/time_series?{query}"

        for attempt in range(self.max_retries + 1):
            # Token bucket wait
            self.rate_limiter.acquire(1.0)
            self.call_count += 1

            status, body = self.transport_fn(url)
            try:
                data = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                data = {}

            # Handle 429
            is_429 = status == 429 or data.get("code") == 429 or "api credits" in str(data.get("message", "")).lower()
            if is_429:
                if attempt < self.max_retries:
                    wait_seconds = self.backoff_base**attempt
                    log.warning(
                        "Twelve Data 429 for %s (%s). Backing off for %.1f s (attempt %d/%d)",
                        symbol,
                        interval,
                        wait_seconds,
                        attempt + 1,
                        self.max_retries,
                    )
                    self.sleep_fn(wait_seconds)
                    continue
                raise TwelveDataRateLimitError(
                    f"Twelve Data 429 rate limit exceeded for {symbol} after {self.max_retries} retries"
                )

            if status >= 400 or data.get("status") == "error":
                code = data.get("code", status)
                msg = data.get("message", "API error")
                raise TwelveDataApiError(f"Twelve Data error {code}: {msg}")

            return data

        raise TwelveDataRateLimitError(f"Rate limit retries exhausted for {symbol}")


# -----------------------------------------------------------------------------
# Transformation & Ingestion Pipeline
# -----------------------------------------------------------------------------


def parse_daily_bars(
    symbol: str,
    payload: dict[str, Any],
    min_date: date | None = None,
    *,
    now: datetime,
) -> list[PriceBarRecord]:
    """Parse daily bars from Twelve Data /time_series payload into PriceBarRecord list.

    A daily bar is asserted only once its own XNYS session has actually CLOSED as of
    `now` -- instant precision, symmetric with `parse_monthly_bars` (#939 third-round
    High 2: this function had NO closed-vs-still-open gate at all through two rounds of
    fixing the identical defect in the monthly parser. `staging.market_prices_daily`
    carries the exact same `check (recorded_at >= transaction_time)` as the monthly
    table, and Twelve Data's `interval=1day` response, while today's session is still
    open, also includes a running "today so far" row. The production cron happens to
    always run after close (75 min buffer in EDT, 15 min in EST) so scheduled runs
    never hit this, but any manual Materialize/backfill/retry during market hours did,
    and rolled back the whole op -- daily AND monthly AND the mask backfill, on the
    same uncommitted connection -- exactly the failure mode `lanes.market_data`'s
    `_refresh_market_data` docstring itself names as the risk of a fix applied to only
    one of two structurally identical call sites.
    """
    records: list[PriceBarRecord] = []
    values = payload.get("values", [])
    if not isinstance(values, list):
        return records

    for item in values:
        if not isinstance(item, dict):
            continue
        dt_str = item.get("datetime", "")
        try:
            bar_date = date.fromisoformat(dt_str[:10])
        except (ValueError, TypeError):
            continue

        if now < xnys_session_close_utc(bar_date):
            # This session has not closed yet as of `now` -- the vendor's
            # still-in-progress "today" row, not a settled daily bar.
            continue
        if min_date is not None and bar_date < min_date:
            continue

        records.append(
            PriceBarRecord(
                symbol=symbol,
                date=bar_date,
                open=parse_decimal(item.get("open")),
                high=parse_decimal(item.get("high")),
                low=parse_decimal(item.get("low")),
                close=parse_decimal(item.get("close")),
                volume=parse_decimal(item.get("volume")),
                source="twelvedata",
                resolution="1D",
            )
        )
    return records


def parse_monthly_bars(
    symbol: str,
    payload: dict[str, Any],
    min_date: date | None = None,
    *,
    now: datetime,
) -> list[PriceBarRecord]:
    """Parse monthly bars from Twelve Data /time_series payload, snapping dates to last XNYS session.

    A monthly bar is asserted only once its month's own XNYS session has actually
    CLOSED as of `now` -- compared at INSTANT precision, never by date (#939 follow-up
    finding: a first version of this fix compared `snapped_date > as_of_date`, two
    `date`s. On the current month's own last XNYS session, BEFORE that session's own
    16:00 ET close, `snapped_date == as_of_date` -- not `>` -- so the row was NOT
    skipped, its `transaction_time` (that day's own close, still in the future) was
    still ahead of the real `recorded_at`, and `staging.market_prices_monthly`'s
    `check (recorded_at >= transaction_time)` fired anyway: the crash this function
    exists to prevent shrank from "~20/21 trading days a month" to "the one day a
    month before its own close", but a manual Materialize/backfill/debug run in that
    window -- or any scheduler retiming that erodes the 15-75 minute buffer the
    production cron's post-close time happens to leave -- still hits it).

    `now` is a required instant, not a `date` with a silent real-clock fallback: the
    previous `as_of: date | None = None` signature let a caller (or a test) supply a
    calendar day while the actual close-or-not determination silently needed
    time-of-day precision that no `date` can carry, and every caller had to
    independently know to pass one that was already past its own close. Requiring the
    caller's own instant here removes that guesswork -- the only sound way to answer
    "has this month closed" is to compare against the moment being asked from, not a
    day snapped off it.
    """
    records: list[PriceBarRecord] = []
    values = payload.get("values", [])
    if not isinstance(values, list):
        return records

    for item in values:
        if not isinstance(item, dict):
            continue
        dt_str = item.get("datetime", "")
        try:
            raw_date = date.fromisoformat(dt_str[:10])
        except (ValueError, TypeError):
            continue

        snapped_date = snap_to_last_xnys_session_of_month(raw_date)
        if now < xnys_session_close_utc(snapped_date):
            # This bar's month has not closed as of `now` -- this is the vendor's
            # in-progress month-to-date row, not a settled monthly bar. Instant
            # precision, not `snapped_date > now.date()`: on the month's own last
            # session, before its own close, the two dates are EQUAL, and a
            # date-only comparison would let it through.
            continue
        if min_date is not None and snapped_date < min_date:
            continue

        records.append(
            PriceBarRecord(
                symbol=symbol,
                date=snapped_date,
                open=parse_decimal(item.get("open")),
                high=parse_decimal(item.get("high")),
                low=parse_decimal(item.get("low")),
                close=parse_decimal(item.get("close")),
                volume=parse_decimal(item.get("volume")),
                source="twelvedata",
                resolution="1M",
            )
        )
    return records


def _latest_vintages(
    connection: psycopg.Connection,
    table: str,
    records: Sequence[PriceBarRecord],
) -> dict[tuple[str, date], tuple[Any, Any, Any, Any, Any, str]]:
    """The latest known (open, high, low, close, volume, adjust) per (symbol,
    trading_date) already in `table`, for exactly the (symbol, date) pairs `records`
    is about to write.

    Append-only means every GENUINE change gets its own row -- it does not mean every
    re-fetch of unchanged history piles up an identical row. This pipeline refetches the
    full 3y/10y lookback on every scheduled run (#938's lane), so without this check an
    append-only insert would grow each table by ~5000 rows per symbol per run forever.

    `adjust` is part of the comparison (#939 review Medium), not just OHLCV: it is the
    one other field every appended row persists explicitly per-call
    (`insert_market_prices_daily`/`_monthly`'s `adjust` parameter) and that can
    legitimately change between pipeline runs. Comparing OHLCV alone let a re-ingest
    under a NEW adjust policy whose values happened to come back numerically identical
    to the prior vintage (no split/dividend between the two policies for this date) be
    swallowed as "unchanged" -- the row's `adjust` column silently stayed on the OLD
    policy forever, which is exactly the policy drift an append-only history exists to
    make visible.
    """
    if not records:
        return {}
    symbols = sorted({r.symbol for r in records})
    dates = sorted({r.date for r in records})
    query = f"""
        select distinct on (symbol, trading_date) symbol, trading_date, open, high, low, close, volume, adjust
        from {table}
        where symbol = any(%s) and trading_date = any(%s)
        order by symbol, trading_date, recorded_at desc;
    """
    with connection.cursor() as cur:
        cur.execute(query, (symbols, dates))
        rows = cur.fetchall()
    return {(row[0], row[1]): (row[2], row[3], row[4], row[5], row[6], row[7]) for row in rows}


def _provenance(record: PriceBarRecord, *, adjust: str) -> tuple[datetime, Decimal, str]:
    """The (transaction_time, confidence, raw_ref) triple every appended row carries.

    See the module docstring and `xnys_session_close_utc`/`SINGLE_SOURCE_CONFIDENCE` for
    what each one means and why. `raw_ref` documents provenance without claiming a
    `raw.fetches` object pointer this pipeline does not write -- it never persists to
    `raw.fetches`/object storage (unlike `production_topt`'s adapters), which stays a
    known gap, not a pretended one.

    `raw_ref` includes `adjust` (#939 review Low): without it, two rows for the same
    (symbol, date) persisted under different adjust policies got an IDENTICAL raw_ref,
    so the provenance reference alone could not tell which policy a given vintage came
    from.
    """
    raw_ref = (
        f"{_RAW_REF_PREFIX}:{record.source}:{record.symbol}:{record.resolution}:{record.date.isoformat()}:{adjust}"
    )
    return xnys_session_close_utc(record.date), SINGLE_SOURCE_CONFIDENCE, raw_ref


def insert_market_prices_daily(
    connection: psycopg.Connection,
    records: Sequence[PriceBarRecord],
    *,
    adjust: str = DEFAULT_ADJUST,
) -> int:
    """Append daily bars to staging.market_prices_daily.

    Never upserts (#938 contract item 1): there is no unique constraint on
    (symbol, trading_date) to conflict on, so a genuinely changed date is always a new
    vintage row, and `trg_market_prices_daily_append_only` rejects any UPDATE/DELETE
    outright. A record identical to the latest known vintage for its (symbol, date) is
    skipped (`_latest_vintages`) rather than appended again.
    """
    if not records:
        return 0

    existing = _latest_vintages(connection, "staging.market_prices_daily", records)
    query = """
        insert into staging.market_prices_daily (
            symbol, trading_date, open, high, low, close, volume,
            source, adjust, transaction_time, confidence, raw_ref
        ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
    """
    rows = []
    for r in records:
        if existing.get((r.symbol, r.date)) == (r.open, r.high, r.low, r.close, r.volume, adjust):
            continue
        transaction_time, confidence, raw_ref = _provenance(r, adjust=adjust)
        rows.append(
            (
                r.symbol,
                r.date,
                r.open,
                r.high,
                r.low,
                r.close,
                r.volume,
                r.source,
                adjust,
                transaction_time,
                confidence,
                raw_ref,
            )
        )
    if not rows:
        return 0
    with connection.cursor() as cur:
        cur.executemany(query, rows)
    return len(rows)


def insert_market_prices_monthly(
    connection: psycopg.Connection,
    records: Sequence[PriceBarRecord],
    *,
    adjust: str = DEFAULT_ADJUST,
) -> int:
    """Append monthly bars to staging.market_prices_monthly. See `insert_market_prices_daily`."""
    if not records:
        return 0

    existing = _latest_vintages(connection, "staging.market_prices_monthly", records)
    query = """
        insert into staging.market_prices_monthly (
            symbol, trading_date, open, high, low, close, volume,
            source, adjust, transaction_time, confidence, raw_ref
        ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
    """
    rows = []
    for r in records:
        if existing.get((r.symbol, r.date)) == (r.open, r.high, r.low, r.close, r.volume, adjust):
            continue
        transaction_time, confidence, raw_ref = _provenance(r, adjust=adjust)
        rows.append(
            (
                r.symbol,
                r.date,
                r.open,
                r.high,
                r.low,
                r.close,
                r.volume,
                r.source,
                adjust,
                transaction_time,
                confidence,
                raw_ref,
            )
        )
    if not rows:
        return 0
    with connection.cursor() as cur:
        cur.executemany(query, rows)
    return len(rows)


@dataclass(frozen=True)
class IngestionSummary:
    total_symbols: int
    total_calls: int
    daily_records_count: int
    monthly_records_count: int
    daily_inserted: int
    monthly_inserted: int
    daily_records: list[PriceBarRecord]
    monthly_records: list[PriceBarRecord]


def ingest_twelve_data_market_prices(
    symbols: Sequence[str] = DEFAULT_TOPT_SYMBOLS,
    client: TwelveDataClient | None = None,
    connection: psycopg.Connection | None = None,
    daily_lookback_years: int = 3,
    monthly_lookback_years: int = 10,
    now: datetime | None = None,
    adjust: str = DEFAULT_ADJUST,
) -> IngestionSummary:
    """Ingest 20 TOPT symbols for 3 years daily and 10 years monthly via Twelve Data /time_series.

    Issues 1 call per symbol per resolution (40 calls total for 20 symbols). Every call
    passes `adjust` explicitly (#938 contract item 4).

    `now` (an instant, not a date) is the single source of truth for "as of when":
    the lookback-window floor (`min_daily_date`/`min_monthly_date`) and the
    closed-vs-still-open determination `parse_monthly_bars` makes both derive from it,
    so they cannot silently disagree about what day it is (#939 follow-up finding: a
    prior version took a `date` here for the lookback floor and separately let
    `parse_monthly_bars` default to its own independent `datetime.now(UTC)` call for
    the closed/open check -- two clocks that only happened to agree because nothing
    forced them to run microseconds apart).
    """
    now_instant = now or datetime.now(UTC)
    as_of_date = now_instant.date()
    min_daily_date = as_of_date - timedelta(days=daily_lookback_years * 365 + 30)
    min_monthly_date = as_of_date - timedelta(days=monthly_lookback_years * 365 + 60)

    td_client = client or TwelveDataClient()

    all_daily_records: list[PriceBarRecord] = []
    all_monthly_records: list[PriceBarRecord] = []
    calls_made = 0

    for symbol in symbols:
        # 1 call for daily
        daily_payload = td_client.fetch_time_series(symbol, interval="1day", outputsize=5000, adjust=adjust)
        calls_made += 1
        daily_bars = parse_daily_bars(symbol, daily_payload, min_date=min_daily_date, now=now_instant)
        all_daily_records.extend(daily_bars)

        # 1 call for monthly
        monthly_payload = td_client.fetch_time_series(symbol, interval="1month", outputsize=5000, adjust=adjust)
        calls_made += 1
        monthly_bars = parse_monthly_bars(symbol, monthly_payload, min_date=min_monthly_date, now=now_instant)
        all_monthly_records.extend(monthly_bars)

    daily_inserted = 0
    monthly_inserted = 0
    if connection is not None:
        daily_inserted = insert_market_prices_daily(connection, all_daily_records, adjust=adjust)
        monthly_inserted = insert_market_prices_monthly(connection, all_monthly_records, adjust=adjust)

    return IngestionSummary(
        total_symbols=len(symbols),
        total_calls=calls_made,
        daily_records_count=len(all_daily_records),
        monthly_records_count=len(all_monthly_records),
        daily_inserted=daily_inserted,
        monthly_inserted=monthly_inserted,
        daily_records=all_daily_records,
        monthly_records=all_monthly_records,
    )
