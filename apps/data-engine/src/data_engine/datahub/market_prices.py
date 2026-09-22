"""Twelve Data market prices ingestion and multi-resolution staging (#101).

Ingests 20 TOPT symbols for:
- 3 years daily (interval='1day')
- 10 years monthly (interval='1month')
via Twelve Data /time_series with outputsize=5000 (1 call per symbol per resolution,
40 calls total).

Provides:
- Built-in token bucket rate limiter (8 calls/min compliant)
- Exponential backoff on HTTP 429
- Snapping monthly dates to the month's last valid trading day (XNYS holidays)
- Persistence to staging.market_prices_daily and staging.market_prices_monthly
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
from typing import Any

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
        body is the vendor's answer, not an exception) like `twelve_data_origin`'s."""
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
        extra_params: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Fetch /time_series for symbol and interval with rate limiting and retry on 429."""
        vendor_symbol = symbol.replace(".", "/") if "." in symbol else symbol
        params: dict[str, str] = {
            "symbol": vendor_symbol,
            "interval": interval,
            "outputsize": str(outputsize),
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
) -> list[PriceBarRecord]:
    """Parse daily bars from Twelve Data /time_series payload into PriceBarRecord list."""
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
) -> list[PriceBarRecord]:
    """Parse monthly bars from Twelve Data /time_series payload, snapping dates to last XNYS session."""
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


def insert_market_prices_daily(
    connection: psycopg.Connection,
    records: Sequence[PriceBarRecord],
) -> int:
    """Upsert daily market price records into staging.market_prices_daily."""
    if not records:
        return 0

    query = """
        insert into staging.market_prices_daily (
            symbol, date, open, high, low, close, volume, source, ingested_at
        ) values (%s, %s, %s, %s, %s, %s, %s, %s, now())
        on conflict (symbol, date) do update set
            open = excluded.open,
            high = excluded.high,
            low = excluded.low,
            close = excluded.close,
            volume = excluded.volume,
            source = excluded.source,
            ingested_at = excluded.ingested_at;
    """
    rows = [
        (
            r.symbol,
            r.date,
            r.open,
            r.high,
            r.low,
            r.close,
            r.volume,
            r.source,
        )
        for r in records
    ]
    with connection.cursor() as cur:
        cur.executemany(query, rows)
    return len(rows)


def insert_market_prices_monthly(
    connection: psycopg.Connection,
    records: Sequence[PriceBarRecord],
) -> int:
    """Upsert monthly market price records into staging.market_prices_monthly."""
    if not records:
        return 0

    query = """
        insert into staging.market_prices_monthly (
            symbol, date, open, high, low, close, volume, source, resolution, ingested_at
        ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
        on conflict (symbol, date) do update set
            open = excluded.open,
            high = excluded.high,
            low = excluded.low,
            close = excluded.close,
            volume = excluded.volume,
            source = excluded.source,
            resolution = excluded.resolution,
            ingested_at = excluded.ingested_at;
    """
    rows = [
        (
            r.symbol,
            r.date,
            r.open,
            r.high,
            r.low,
            r.close,
            r.volume,
            r.source,
            r.resolution,
        )
        for r in records
    ]
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
    as_of: date | None = None,
) -> IngestionSummary:
    """Ingest 20 TOPT symbols for 3 years daily and 10 years monthly via Twelve Data /time_series.

    Issues 1 call per symbol per resolution (40 calls total for 20 symbols).
    """
    as_of_date = as_of or datetime.now(UTC).date()
    min_daily_date = as_of_date - timedelta(days=daily_lookback_years * 365 + 30)
    min_monthly_date = as_of_date - timedelta(days=monthly_lookback_years * 365 + 60)

    td_client = client or TwelveDataClient()

    all_daily_records: list[PriceBarRecord] = []
    all_monthly_records: list[PriceBarRecord] = []
    calls_made = 0

    for symbol in symbols:
        # 1 call for daily
        daily_payload = td_client.fetch_time_series(symbol, interval="1day", outputsize=5000)
        calls_made += 1
        daily_bars = parse_daily_bars(symbol, daily_payload, min_date=min_daily_date)
        all_daily_records.extend(daily_bars)

        # 1 call for monthly
        monthly_payload = td_client.fetch_time_series(symbol, interval="1month", outputsize=5000)
        calls_made += 1
        monthly_bars = parse_monthly_bars(symbol, monthly_payload, min_date=min_monthly_date)
        all_monthly_records.extend(monthly_bars)

    daily_inserted = 0
    monthly_inserted = 0
    if connection is not None:
        daily_inserted = insert_market_prices_daily(connection, all_daily_records)
        monthly_inserted = insert_market_prices_monthly(connection, all_monthly_records)

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
