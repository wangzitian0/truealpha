"""Twelve Data daily market prices ingestion (#938 layer 1)."""

from __future__ import annotations

import json
import logging
import time
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import psycopg
from truealpha_contracts.environment_contract import load_environment_contract

from data_engine.config import settings
from data_engine.datahub._helpers import parse_decimal, safe_case_id_part, single_origin_confidence
from data_engine.datahub.fetch_key import FetchKey
from data_engine.sources import gateway

log = logging.getLogger(__name__)


def stored_symbols(connection: psycopg.Connection, *, known_at: datetime) -> tuple[str, ...]:
    """Primary listing symbols for scheduled frozen-corpus universes (#1522)."""
    from data_engine.datahub.capture_core import universe_corpus
    from data_engine.datahub.primary_listing import primary_symbols
    from data_engine.environment_universes import rule_environment

    contract, environment = load_environment_contract(), rule_environment()
    symbols: set[str] = set()
    for key, universe in contract.universes.items():
        if (
            universe.price_series
            and universe.membership.kind == "frozen_corpus"
            and contract.is_universe_scheduled(key, environment)
        ):
            corpus = universe_corpus.load_corpus(universe_corpus.TOPT_CORPUS_FILENAME)
            symbols.update(primary_symbols(connection, corpus["denominator"], known_at=known_at))
    return tuple(sorted(symbols))


TWELVE_DATA_BASE_URL = "https://api.twelvedata.com"
TWELVE_DATA_SEAT = "twelvedata"
TIME_SERIES_CALLER = "market_prices.fetch_time_series"
SPLIT_ADJUSTED = "splits"
UNADJUSTED = "none"
DEFAULT_ADJUST = SPLIT_ADJUSTED
DAILY_SERIES: tuple[str, ...] = (SPLIT_ADJUSTED, UNADJUSTED)
DAILY_LOOKBACK_YEARS = 3
MAX_BAR_LAG_SESSIONS = 1
_RAW_REF_PREFIX = "twelvedata:time_series"
_DAILY_TABLE = "staging.market_prices_daily"
_XNYS_TZ = ZoneInfo("America/New_York")
_XNYS_CLOSE_HOUR = 16
_XNYS_EARLY_CLOSE_HOUR = 13


def easter_sunday(year: int) -> date:
    a_val, b_val, c_val = year % 19, year // 100, year % 100
    d_val, e_val = b_val // 4, b_val % 4
    f_val = (b_val + 8) // 25
    g_val = (b_val - f_val + 1) // 3
    h_val = (19 * a_val + b_val - d_val - g_val + 15) % 30
    i_val, k_val = c_val // 4, c_val % 4
    l_val = (32 + 2 * e_val + 2 * i_val - h_val - k_val) % 7
    m_val = (a_val + 11 * h_val + 22 * l_val) // 451
    return date(year, (h_val + l_val - 7 * m_val + 114) // 31, ((h_val + l_val - 7 * m_val + 114) % 31) + 1)


def _nth_weekday(year: int, month: int, weekday: int, nth: int) -> date:
    first = 1 + (weekday - date(year, month, 1).weekday()) % 7
    return date(year, month, first + 7 * (nth - 1))


def _observed_holiday(day: date, *, observe_saturday: bool = True) -> date | None:
    if day.weekday() == 5:
        return day - timedelta(days=1) if observe_saturday else None
    return day + timedelta(days=1) if day.weekday() == 6 else day


def xnys_holidays(year: int) -> set[date]:
    last_may = date(year, 5, 31)
    candidates: list[date | None] = [
        _observed_holiday(date(year, 1, 1), observe_saturday=False),
        _nth_weekday(year, 1, 0, 3),
        _nth_weekday(year, 2, 0, 3),
        easter_sunday(year) - timedelta(days=2),
        last_may - timedelta(days=last_may.weekday()),
        _observed_holiday(date(year, 6, 19)) if year >= 2021 else None,
        _observed_holiday(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),
        _nth_weekday(year, 11, 3, 4),
        _observed_holiday(date(year, 12, 25)),
        date(2025, 1, 9) if year == 2025 else None,
    ]
    return {day for day in candidates if day is not None}


def is_xnys_trading_day(day: date) -> bool:
    return day.weekday() < 5 and day not in xnys_holidays(day.year)


def most_recent_xnys_session(day: date) -> date:
    curr = day
    while not is_xnys_trading_day(curr):
        curr -= timedelta(days=1)
    return curr


def oldest_session_within_lag(latest: date) -> date:
    oldest = latest
    for _ in range(MAX_BAR_LAG_SESSIONS):
        oldest = most_recent_xnys_session(oldest - timedelta(days=1))
    return oldest


@lru_cache(maxsize=32)
def xnys_early_close_days(year: int) -> set[date]:
    early_closes = {_nth_weekday(year, 11, 3, 4) + timedelta(days=1)}
    if is_xnys_trading_day(xmas_eve := date(year, 12, 24)):
        early_closes.add(xmas_eve)
    if date(year, 7, 4).weekday() in (1, 2, 3, 4) and is_xnys_trading_day(date(year, 7, 3)):
        early_closes.add(date(year, 7, 3))
    return early_closes


def xnys_session_close_utc(trading_date: date) -> datetime:
    hour = _XNYS_EARLY_CLOSE_HOUR if trading_date in xnys_early_close_days(trading_date.year) else _XNYS_CLOSE_HOUR
    return datetime(trading_date.year, trading_date.month, trading_date.day, hour, 0, tzinfo=_XNYS_TZ).astimezone(UTC)


class TwelveDataRateLimitError(Exception):
    pass


class TwelveDataApiError(Exception):
    pass


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
    resolution: str = "1D"


class TwelveDataClient:
    """Client for Twelve Data /time_series endpoint with rate-limit backoff."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = TWELVE_DATA_BASE_URL,
        transport_fn: Callable[[str], tuple[int, bytes]] | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        max_retries: int = 5,
        backoff_base: float = 2.0,
        caller: str = TIME_SERIES_CALLER,
    ) -> None:
        self.api_key: str = api_key or str(getattr(settings, "twelve_data_api_key", ""))
        self.base_url = base_url.rstrip("/")
        self.transport_fn = transport_fn or self._default_transport
        self.sleep_fn = sleep_fn
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.caller = caller
        self.call_count = 0

    def _default_transport(self, url: str) -> tuple[int, bytes]:
        params = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        identity = FetchKey.for_request(
            TWELVE_DATA_SEAT,
            "time_series",
            params,
            entity_param="symbol",
            as_of_params=("end_date", "date"),
            default_as_of=gateway.utc_day(),
            exclude=("apikey",),
        )
        with httpx.Client(headers={"User-Agent": "TrueAlpha-DataEngine/1.0"}, timeout=30.0) as client:
            response = gateway.http_get(
                client, TWELVE_DATA_SEAT, "time_series", url, identity=identity, caller=self.caller
            )
        return int(response.status_code), bytes(response.content)

    def fetch_time_series(
        self,
        symbol: str,
        interval: str,
        outputsize: int = 5000,
        adjust: str = DEFAULT_ADJUST,
        extra_params: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        params = {
            "symbol": symbol,
            "interval": interval,
            "outputsize": str(outputsize),
            "adjust": adjust,
            "apikey": self.api_key,
            **(extra_params or {}),
        }
        url = f"{self.base_url}/time_series?{urllib.parse.urlencode(params)}"
        for attempt in range(self.max_retries + 1):
            self.call_count += 1
            status, body = self.transport_fn(url)
            try:
                data = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                data = {}
            if status == 429 or data.get("code") == 429 or "api credits" in str(data.get("message", "")).lower():
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
                raise TwelveDataApiError(
                    f"Twelve Data error {data.get('code', status)}: {data.get('message', 'API error')}"
                )
            return data
        raise TwelveDataRateLimitError(f"Rate limit retries exhausted for {symbol}")


def _parse_bars(
    symbol: str,
    payload: dict[str, Any],
    min_date: date | None,
    *,
    now: datetime,
    resolution: str,
) -> list[PriceBarRecord]:
    if not isinstance(values := payload.get("values", []), list):
        return []
    records: list[PriceBarRecord] = []
    for item in values:
        if not isinstance(item, dict):
            continue
        try:
            bar_date = date.fromisoformat(str(item.get("datetime", ""))[:10])
        except (ValueError, TypeError):
            continue
        if now < xnys_session_close_utc(bar_date) or (min_date is not None and bar_date < min_date):
            continue
        records.append(
            PriceBarRecord(
                symbol,
                bar_date,
                parse_decimal(item.get("open")),
                parse_decimal(item.get("high")),
                parse_decimal(item.get("low")),
                parse_decimal(item.get("close")),
                parse_decimal(item.get("volume")),
                "twelvedata",
                resolution,
            )
        )
    return records


def parse_daily_bars(
    symbol: str, payload: dict[str, Any], min_date: date | None = None, *, now: datetime
) -> list[PriceBarRecord]:
    return _parse_bars(symbol, payload, min_date, now=now, resolution="1D")


def _latest_vintages(
    connection: psycopg.Connection, table: str, records: Sequence[PriceBarRecord]
) -> dict[tuple[str, date, str], tuple[Any, Any, Any, Any, Any, str]]:
    if not records:
        return {}
    query = (
        f"select distinct on (symbol, trading_date, adjust) symbol, trading_date, adjust, open, high, low, close, volume, source "
        f"from {table} where symbol = any(%s) and trading_date = any(%s) order by symbol, trading_date, adjust, recorded_at desc;"
    )
    with connection.cursor() as cur:
        cur.execute(query, (sorted({r.symbol for r in records}), sorted({r.date for r in records})))
        rows = cur.fetchall()
    return {(r[0], r[1], r[2]): (r[3], r[4], r[5], r[6], r[7], str(r[8])) for r in rows}


def _provenance(record: PriceBarRecord, *, adjust: str) -> tuple[datetime, Decimal, str]:
    safe_symbol = safe_case_id_part(record.symbol, "symbol")
    date_iso = record.date.isoformat()
    if record.source == "yahoo":
        raw_ref = f"yahoo:chart:{record.symbol}:{record.resolution}:{date_iso}:{adjust}"
        provider_id, lineage = "origin:yahoo:v1", "yahoo"
    else:
        raw_ref = f"{_RAW_REF_PREFIX}:{record.source}:{record.symbol}:{record.resolution}:{date_iso}:{adjust}"
        provider_id, lineage = "origin:twelve-data:v1", "twelve-data"
    confidence = single_origin_confidence(
        case_id=f"market-prices:{safe_symbol}:{date_iso}",
        provider_id=provider_id,
        lineage=lineage,
        evidence_id=f"bar:{safe_symbol}:{date_iso}",
    )
    return xnys_session_close_utc(record.date), confidence, raw_ref


def _insert_market_prices(
    connection: psycopg.Connection, table: str, records: Sequence[PriceBarRecord], *, adjust: str
) -> int:
    if not records:
        return 0
    existing = _latest_vintages(connection, table, records)
    rows: list[tuple[Any, ...]] = []
    for r in records:
        prev = existing.get((r.symbol, r.date, adjust))
        if prev is not None and (
            (r.source == "yahoo" and prev[5] == "twelvedata")
            or prev == (r.open, r.high, r.low, r.close, r.volume, r.source)
        ):
            continue
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
                *_provenance(r, adjust=adjust),
            )
        )
    if not rows:
        return 0
    query = (
        f"insert into {table} (symbol, trading_date, open, high, low, close, volume, source, adjust, transaction_time, confidence, raw_ref) "
        "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);"
    )
    with connection.cursor() as cur:
        cur.executemany(query, rows)
    return len(rows)


def insert_market_prices_daily(
    connection: psycopg.Connection, records: Sequence[PriceBarRecord], *, adjust: str = DEFAULT_ADJUST
) -> int:
    return _insert_market_prices(connection, _DAILY_TABLE, records, adjust=adjust)


@dataclass(frozen=True)
class IngestionSummary:
    total_symbols: int
    total_calls: int
    daily_records_count: int
    daily_inserted: int
    daily_records: list[PriceBarRecord]
    unadjusted_daily_records_count: int = 0
    unadjusted_daily_inserted: int = 0
    unadjusted_daily_records: list[PriceBarRecord] = field(default_factory=list)


def ingest_twelve_data_market_prices(
    symbols: Sequence[str],
    client: TwelveDataClient | None = None,
    connection: psycopg.Connection | None = None,
    daily_lookback_years: int = DAILY_LOOKBACK_YEARS,
    now: datetime | None = None,
    adjust: str = DEFAULT_ADJUST,
) -> IngestionSummary:
    now_instant = now or datetime.now(UTC)
    min_daily_date = daily_lookback_floor(now_instant, daily_lookback_years)
    td_client = client or TwelveDataClient()
    all_daily: list[PriceBarRecord] = []
    all_unadjusted_daily: list[PriceBarRecord] = []
    calls_made = 0
    fetch_unadjusted = adjust != UNADJUSTED

    for symbol in symbols:
        all_daily.extend(
            parse_daily_bars(
                symbol, td_client.fetch_time_series(symbol, "1day", adjust=adjust), min_daily_date, now=now_instant
            )
        )
        calls_made += 1
        if fetch_unadjusted:
            all_unadjusted_daily.extend(
                parse_daily_bars(
                    symbol,
                    td_client.fetch_time_series(symbol, "1day", adjust=UNADJUSTED),
                    min_daily_date,
                    now=now_instant,
                )
            )
            calls_made += 1

    daily_ins = unadj_ins = 0
    if connection is not None:
        daily_ins = insert_market_prices_daily(connection, all_daily, adjust=adjust)
        unadj_ins = insert_market_prices_daily(connection, all_unadjusted_daily, adjust=UNADJUSTED)
    return IngestionSummary(
        len(symbols),
        calls_made,
        len(all_daily),
        daily_ins,
        all_daily,
        len(all_unadjusted_daily),
        unadj_ins,
        all_unadjusted_daily,
    )


def daily_lookback_floor(now: datetime, years: int = DAILY_LOOKBACK_YEARS) -> date:
    return now.date() - timedelta(days=years * 365 + 30)


def ingest_daily_series(
    symbol: str, *, client: TwelveDataClient, connection: psycopg.Connection, now: datetime
) -> dict[str, int]:
    floor = daily_lookback_floor(now)
    bars: dict[str, list[PriceBarRecord]] = {}
    for adjust in DAILY_SERIES:
        bars[adjust] = parse_daily_bars(symbol, client.fetch_time_series(symbol, "1day", adjust=adjust), floor, now=now)
        if not bars[adjust]:
            raise TwelveDataApiError(f"Twelve Data answered no closed daily bar for {symbol} (adjust={adjust})")
    return {adjust: insert_market_prices_daily(connection, records, adjust=adjust) for adjust, records in bars.items()}


def ingest_yahoo_daily_series(
    connection: psycopg.Connection,
    symbol: str,
    raw_bytes: bytes,
    *,
    now: datetime,
) -> dict[str, int]:
    """Persist split-adjusted and unadjusted daily bars from one captured Yahoo chart payload."""
    from data_engine.sources.yahoo import parse_yahoo_daily_series

    try:
        payload = json.loads(raw_bytes.decode("utf-8"), parse_float=Decimal)
    except (UnicodeDecodeError, ValueError):
        return {SPLIT_ADJUSTED: 0, UNADJUSTED: 0}
    if not isinstance(payload, dict):
        return {SPLIT_ADJUSTED: 0, UNADJUSTED: 0}
    split_adj, unadj = parse_yahoo_daily_series(symbol, payload, daily_lookback_floor(now), now=now)
    return {
        SPLIT_ADJUSTED: insert_market_prices_daily(connection, split_adj, adjust=SPLIT_ADJUSTED),
        UNADJUSTED: insert_market_prices_daily(connection, unadj, adjust=UNADJUSTED),
    }
