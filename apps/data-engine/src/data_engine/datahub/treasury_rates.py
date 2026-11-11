"""U.S. Treasury daily par yield curve ingestion and append-only persistence (#1255)."""

from __future__ import annotations

import csv
import io
import logging
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import psycopg
from truealpha_contracts.models import DataSource
from truealpha_contracts.ports import RawObjectStore

from data_engine import raw_store
from data_engine.datahub.fetch_key import FetchKey
from data_engine.datahub.production_topt.source_registrations import (
    MACRO_REGISTRATIONS,
    AsOfAxis,
    MacroSeries,
    MacroSourceRegistration,
    knowable_instant,
)
from data_engine.sources import gateway

log = logging.getLogger(__name__)

SOURCE = DataSource.US_TREASURY
REGISTRATION: MacroSourceRegistration = next(item for item in MACRO_REGISTRATIONS if item.ledger_seat == SOURCE.value)
ENDPOINT = "daily_treasury_yield_curve"
BASE_URL = "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv"
USER_AGENT = "TrueAlpha-DataEngine/1.0"
REQUEST_TIMEOUT_SECONDS = 30

DATE_COLUMN = "Date"
DATE_FORMAT = "%m/%d/%Y"
CONFIDENCE = Decimal("0.92")
COMPLETE_FROM = (12, 29)
_LOCK_NAME = "mart.macro_series_daily"

_UPSERT_CATALOG_SQL = """
insert into mart.macro_series (series_id, label, unit, source_id, max_age_days)
values (%s, %s, %s, %s, %s)
on conflict (series_id) do update
set label = excluded.label, unit = excluded.unit, source_id = excluded.source_id,
    max_age_days = excluded.max_age_days, updated_at = clock_timestamp()
where (mart.macro_series.label, mart.macro_series.unit, mart.macro_series.source_id, mart.macro_series.max_age_days)
    is distinct from (excluded.label, excluded.unit, excluded.source_id, excluded.max_age_days)
"""

_LATEST_OF_YEAR_SQL = """
select series_id, observation_date, value
from mart.macro_series_current
where series_id = any(%s) and observation_date >= %s and observation_date < %s
"""

_INSERT_SQL = """
insert into mart.macro_series_daily
    (series_id, observation_date, value, knowable_at, source, fetch_id, fetched_at, confidence)
values (%s, %s, %s, %s, %s, %s, %s, %s)
"""


class TreasuryFetchError(RuntimeError):
    """The Treasury did not answer with a file."""


class TreasuryFormatError(ValueError):
    """The file does not conform to the expected CSV contract."""


class TreasuryRefreshError(RuntimeError):
    """One or more years failed during refresh."""


@dataclass(frozen=True)
class Observation:
    observation_date: date
    value: Decimal


@dataclass(frozen=True)
class YearResult:
    year: int
    inserted: int
    fetch_id: int | None
    observations: int
    reused: bool = False


def year_url(year: int) -> str:
    return f"{BASE_URL}/{year}/all?type={ENDPOINT}&field_tdr_date_value={year}&page&_format=csv"


def knowable_at_of(observation_date: date) -> datetime:
    return knowable_instant(AsOfAxis.BUSINESS_DAY, observation_date)


def fetch_key_of(year: int, *, on: date) -> FetchKey:
    return FetchKey(seat=SOURCE.value, endpoint=ENDPOINT, canonical_entity_id=f"year:{year}", as_of=on.isoformat())


def has_landed(connection: psycopg.Connection[Any], key: FetchKey) -> bool:
    row = connection.execute(
        "select exists (select 1 from raw.fetches where source = %s and source_record_id = %s)",
        (key.seat, key.source_record_id),
    ).fetchone()
    return bool(row and row[0])


def fetch_year(year: int, identity: FetchKey) -> bytes:
    request = urllib.request.Request(year_url(year), headers={"User-Agent": USER_AGENT})
    status, body = gateway.urlopen(
        SOURCE.value,
        ENDPOINT,
        request,
        identity=identity,
        caller="treasury_rates.fetch_year",
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    if status != 200:
        raise TreasuryFetchError(f"the Treasury answered HTTP {status} for the {year} file ({len(body)} bytes)")
    return body


def _parse_date(text: str, number: int, year: int) -> date:
    try:
        observation_date = datetime.strptime(text.strip(), DATE_FORMAT).date()
    except ValueError as error:
        raise TreasuryFormatError(f"row {number}: the date {text!r} is not {DATE_FORMAT}") from error
    if observation_date.year != year:
        raise TreasuryFormatError(f"row {number}: the date {observation_date.isoformat()} is outside {year}")
    return observation_date


def _parse_percent(text: str, number: int, column: str) -> Decimal:
    try:
        percent = Decimal(text)
    except InvalidOperation as error:
        raise TreasuryFormatError(f"row {number}: the {column} value {text!r} is not a decimal") from error
    if not percent.is_finite() or not Decimal(0) <= percent <= Decimal(100):
        raise TreasuryFormatError(f"row {number}: the {column} value {text!r} is outside 0 to 100 percent")
    return percent.scaleb(-2)


def parse_par_yield_csv(
    body: bytes, *, year: int, series: Sequence[MacroSeries] = REGISTRATION.series
) -> dict[str, tuple[Observation, ...]]:
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise TreasuryFormatError(f"the {year} file is not UTF-8 text: {error}") from error
    rows = [row for row in csv.reader(io.StringIO(text)) if row]
    if not rows:
        raise TreasuryFormatError(f"the {year} file is empty")
    header = rows[0]
    wanted = [DATE_COLUMN, *(item.source_column for item in series)]
    missing = [name for name in wanted if name not in header]
    if missing:
        raise TreasuryFormatError(f"the {year} file has no {missing} header column: {header[:6]}")
    date_index = header.index(DATE_COLUMN)
    index_of = {item.series_id: header.index(item.source_column) for item in series}
    found: dict[str, dict[date, Decimal]] = {item.series_id: {} for item in series}
    seen: set[date] = set()
    for number, row in enumerate(rows[1:], start=2):
        if len(row) != len(header):
            raise TreasuryFormatError(f"row {number}: {len(row)} fields, but the header has {len(header)}")
        observation_date = _parse_date(row[date_index], number, year)
        if observation_date in seen:
            raise TreasuryFormatError(f"row {number}: the date {observation_date.isoformat()} appears twice")
        seen.add(observation_date)
        for item in series:
            text_value = row[index_of[item.series_id]].strip()
            if text_value:
                found[item.series_id][observation_date] = _parse_percent(text_value, number, item.source_column)
    return {
        series_id: tuple(Observation(day, value) for day, value in sorted(values.items()))
        for series_id, values in found.items()
    }


def register_series(connection: psycopg.Connection[Any], registration: MacroSourceRegistration = REGISTRATION) -> None:
    with connection.cursor() as cursor:
        cursor.executemany(
            _UPSERT_CATALOG_SQL,
            [
                (item.series_id, item.label, item.unit, registration.source_id, registration.max_age_days)
                for item in registration.series
            ],
        )


def _knowable_at(observation_date: date, *, revised: bool, seen_at: datetime) -> datetime:
    cut = knowable_at_of(observation_date)
    return max(cut, seen_at) if revised else cut


def persist_observations(
    connection: psycopg.Connection[Any],
    observations: Mapping[str, Sequence[Observation]],
    *,
    year: int,
    fetch_id: int,
    fetched_at: datetime,
) -> int:
    connection.execute("select pg_advisory_xact_lock(hashtext(%s))", (_LOCK_NAME,))
    stored = {
        (series_id, observation_date): value
        for series_id, observation_date, value in connection.execute(
            _LATEST_OF_YEAR_SQL, (list(observations), date(year, 1, 1), date(year + 1, 1, 1))
        ).fetchall()
    }
    fresh = [
        (
            series_id,
            item.observation_date,
            item.value,
            _knowable_at(
                item.observation_date, revised=(series_id, item.observation_date) in stored, seen_at=fetched_at
            ),
            SOURCE.value,
            fetch_id,
            fetched_at,
            CONFIDENCE,
        )
        for series_id, items in observations.items()
        for item in items
        if stored.get((series_id, item.observation_date)) != item.value
    ]
    with connection.cursor() as cursor:
        cursor.executemany(_INSERT_SQL, fresh)
    return len(fresh)


def _holds_every_series(connection: psycopg.Connection[Any], year: int, series_ids: Sequence[str]) -> bool:
    row = connection.execute(
        "select count(distinct series_id) from mart.macro_series_daily "
        "where series_id = any(%s) and observation_date >= %s and observation_date < %s",
        (list(series_ids), date(year, *COMPLETE_FROM), date(year + 1, 1, 1)),
    ).fetchone()
    return bool(row and row[0] == len(series_ids))


def years_to_fetch(
    connection: psycopg.Connection[Any], *, today: date, registration: MacroSourceRegistration = REGISTRATION
) -> list[int]:
    years = [today.year]
    if today.month == 1:
        years.append(today.year - 1)
    series_ids = [item.series_id for item in registration.series]
    for year in range(today.year - 1, registration.first_year - 1, -1):
        if year not in years and not _holds_every_series(connection, year, series_ids):
            years.append(year)
    return years


def _refresh_year(
    connection: psycopg.Connection[Any],
    year: int,
    *,
    fetch: Callable[[int, FetchKey], bytes],
    clock: Callable[[], datetime],
    store: RawObjectStore | None,
    registration: MacroSourceRegistration,
) -> YearResult:
    key = fetch_key_of(year, on=clock().astimezone(UTC).date())
    if has_landed(connection, key):
        return YearResult(year=year, inserted=0, fetch_id=None, observations=0, reused=True)
    body = fetch(year, key)
    fetched_at = clock()
    observations = parse_par_yield_csv(body, year=year, series=registration.series)
    fetch_id = raw_store.insert_fetch(
        connection,
        source=SOURCE,
        source_record_id=key.source_record_id,
        body=body,
        content_type="text/csv",
        fetched_at=fetched_at,
        metadata={
            "year": year,
            "url": year_url(year),
            "series": {item.series_id: item.source_column for item in registration.series},
        },
        store=store,
    )
    inserted = persist_observations(connection, observations, year=year, fetch_id=fetch_id, fetched_at=fetched_at)
    return YearResult(
        year=year,
        inserted=inserted,
        fetch_id=fetch_id,
        observations=sum(len(items) for items in observations.values()),
    )


def refresh_macro_series(
    connection: psycopg.Connection[Any],
    *,
    today: date,
    fetch: Callable[[int, FetchKey], bytes] = fetch_year,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    store: RawObjectStore | None = None,
    registration: MacroSourceRegistration = REGISTRATION,
) -> list[YearResult]:
    register_series(connection, registration)
    connection.commit()
    results: list[YearResult] = []
    failures: list[str] = []
    first_error: Exception | None = None
    for year in years_to_fetch(connection, today=today, registration=registration):
        try:
            results.append(
                _refresh_year(connection, year, fetch=fetch, clock=clock, store=store, registration=registration)
            )
            connection.commit()
        except Exception as error:  # noqa: BLE001
            connection.rollback()
            log.exception("treasury %s file failed", year)
            failures.append(f"{year}: {type(error).__name__}: {error}")
            first_error = first_error or error
    if first_error is not None:
        raise TreasuryRefreshError(
            f"{len(failures)} of {len(failures) + len(results)} Treasury years failed: {'; '.join(failures)}"
        ) from first_error
    return results


__all__ = [
    "CONFIDENCE",
    "REGISTRATION",
    "Observation",
    "TreasuryFetchError",
    "TreasuryFormatError",
    "TreasuryRefreshError",
    "YearResult",
    "fetch_key_of",
    "fetch_year",
    "has_landed",
    "knowable_at_of",
    "parse_par_yield_csv",
    "persist_observations",
    "refresh_macro_series",
    "register_series",
    "year_url",
    "years_to_fetch",
]
