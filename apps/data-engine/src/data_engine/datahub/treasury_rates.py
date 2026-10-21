"""The U.S. Treasury source of the macro base data (#1255).

Source: "Daily Treasury Par Yield Curve Rates" of the U.S. Department of the Treasury. The site
publishes one public CSV per calendar year. No key is needed. The file lists one row per business
day, newest first. A cell holds a rate in percent, for example ``4.26``.

    Date,"1 Mo","1.5 Month","2 Mo","3 Mo","4 Mo",...,"2 Yr",...,"10 Yr",...
    10/13/2026,4.10,4.15,4.15,4.26,4.32,...

The column set changes between years. The 2023 file has no "1.5 Month". So the parser finds each
column by its name, never by its position. The series and their columns are declared once, in
``MACRO_REGISTRATIONS`` (``source_registrations.py``).

What this module does:

1. ``fetch_year`` asks for one year through ``gateway.urlopen`` (ledger seat ``us-treasury``,
   capacity declared in ``LEDGER_CAPACITIES``, init.md rule 6). One request returns every series of
   the year.
2. ``parse_par_yield_csv`` turns the bytes into dated observations of each series. It refuses a
   malformed file by name. It never repairs one.
3. ``refresh_macro_series`` lands the bytes in object storage (``raw.fetches``) and appends the new or
   revised observations to ``mart.macro_series_daily``. A second run over the same bytes inserts
   nothing.

Single fetch per key (#1411): the key of a request is the file of one year on one UTC day. A key that
has landed is not fetched again. A manual replay on the same day makes no request.

Point in time: ``knowable_at`` is 00:00 UTC of the day after the observation date. The Treasury obtains
its input quotes at about 3:30 PM New York time on the observation day. That instant is before 21:00 UTC
in every season, so the next 00:00 UTC is after the publication. A backtest cutoff never sees a rate of
its own day.

Corroboration class B (docs/architecture-decisions/A3): one authoritative origin, domain bounds in
the parser, and a CHECK constraint on ``knowable_at`` in the table. Rule 19 (init.md): the rights and
budget attestation of this source is ``missing``, as it is for every source except the model
provider. It does not block.
"""

from __future__ import annotations

import csv
import io
import logging
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import psycopg
from truealpha_contracts.models import DataSource
from truealpha_contracts.ports import RawObjectStore

from data_engine import raw_store
from data_engine.datahub.fetch_key import FetchKey
from data_engine.datahub.production_topt.source_registrations import (
    MACRO_REGISTRATIONS,
    MacroSeries,
    MacroSourceRegistration,
)
from data_engine.sources import gateway

log = logging.getLogger(__name__)

SOURCE = DataSource.US_TREASURY
#: The registration that owns the Treasury file. The lane and the tests read the series from it.
REGISTRATION: MacroSourceRegistration = next(item for item in MACRO_REGISTRATIONS if item.ledger_seat == SOURCE.value)
#: The ledger endpoint name, the ``raw.fetches`` record prefix and the query type of the Treasury URL.
ENDPOINT = "daily_treasury_yield_curve"
BASE_URL = "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv"
#: The User-Agent header that this module sends to the Treasury. It has the same text as the Twelve Data client.
USER_AGENT = "TrueAlpha-DataEngine/1.0"
REQUEST_TIMEOUT_SECONDS = 30

DATE_COLUMN = "Date"
DATE_FORMAT = "%m/%d/%Y"

#: Declared policy grade, not a per-row measurement. The Treasury publishes the series itself.
#: No second origin corroborates it. 0.92 is the grade of a complete SEC filing set.
CONFIDENCE = Decimal("0.92")

#: An advisory lock key. It makes two writers (the schedule and a manual replay) take turns.
_LOCK_NAME = "mart.macro_series_daily"


class TreasuryFetchError(RuntimeError):
    """The Treasury did not answer with a file."""


class TreasuryFormatError(ValueError):
    """The file is not the file that this parser knows. The message names the row and the cause."""


@dataclass(frozen=True)
class Observation:
    """One business day of one series."""

    observation_date: date
    #: A decimal fraction. The file says percent: ``4.26`` becomes ``Decimal("0.0426")``.
    value: Decimal


def year_url(year: int) -> str:
    return f"{BASE_URL}/{year}/all?type={ENDPOINT}&field_tdr_date_value={year}&page&_format=csv"


def knowable_at_of(observation_date: date) -> datetime:
    """00:00 UTC of the day after the observation: a rate is never known on its own day."""
    return datetime.combine(observation_date + timedelta(days=1), time.min, tzinfo=UTC)


def fetch_key_of(year: int, *, on: date) -> FetchKey:
    """The key of one request: the file of ``year``, on the UTC day ``on`` (#1411)."""
    return FetchKey(seat=SOURCE.value, endpoint=ENDPOINT, canonical_entity_id=f"year:{year}", as_of=on.isoformat())


def has_landed(connection: psycopg.Connection[Any], key: FetchKey) -> bool:
    """Whether the bytes of ``key`` are in ``raw.fetches``. The check reads no payload from object storage."""
    row = connection.execute(
        "select exists (select 1 from raw.fetches where source = %s and source_record_id = %s)",
        (key.seat, key.source_record_id),
    ).fetchone()
    return bool(row and row[0])


def fetch_year(year: int) -> bytes:
    """The CSV bytes of one year, through the external call gateway. Raise on any status but 200."""
    request = urllib.request.Request(year_url(year), headers={"User-Agent": USER_AGENT})
    status, body = gateway.urlopen(
        SOURCE.value, ENDPOINT, request, caller="treasury_rates.fetch_year", timeout=REQUEST_TIMEOUT_SECONDS
    )
    if status != 200:
        raise TreasuryFetchError(f"the Treasury answered HTTP {status} for the {year} file ({len(body)} bytes)")
    return body


def parse_par_yield_csv(
    body: bytes, *, year: int, series: Sequence[MacroSeries] = REGISTRATION.series
) -> dict[str, tuple[Observation, ...]]:
    """The observations of each series of one yearly file, oldest first.

    A cell without a value is skipped. The parser trusts no column position.
    A file or a row that breaks the format raises ``TreasuryFormatError``. These cases break it:

    * a header without "Date" or without the column of a series;
    * a row with another number of fields than the header;
    * a date that is not ``MM/DD/YYYY``, or that lies outside ``year``;
    * a date that appears twice;
    * a value that is not a finite decimal, or that lies outside 0 to 100 percent.
    """
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


def _parse_date(text: str, number: int, year: int) -> date:
    try:
        observation_date = datetime.strptime(text.strip(), DATE_FORMAT).date()
    except ValueError as error:
        raise TreasuryFormatError(f"row {number}: the date {text!r} is not {DATE_FORMAT}") from error
    if observation_date.year != year:
        raise TreasuryFormatError(f"row {number}: the date {observation_date.isoformat()} is outside {year}")
    return observation_date


def _parse_percent(text: str, number: int, column: str) -> Decimal:
    """A percent text as a decimal fraction. No binary float is involved."""
    try:
        percent = Decimal(text)
    except InvalidOperation as error:
        raise TreasuryFormatError(f"row {number}: the {column} value {text!r} is not a decimal") from error
    if not percent.is_finite() or not Decimal(0) <= percent <= Decimal(100):
        raise TreasuryFormatError(f"row {number}: the {column} value {text!r} is outside 0 to 100 percent")
    return percent.scaleb(-2)


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


def register_series(connection: psycopg.Connection[Any], registration: MacroSourceRegistration = REGISTRATION) -> None:
    """Write the catalog rows of the registration. A row that did not change stays as it was."""
    with connection.cursor() as cursor:
        cursor.executemany(
            _UPSERT_CATALOG_SQL,
            [
                (item.series_id, item.label, item.unit, registration.source_id, registration.max_age_days)
                for item in registration.series
            ],
        )


def persist_observations(
    connection: psycopg.Connection[Any],
    observations: Mapping[str, Sequence[Observation]],
    *,
    year: int,
    fetch_id: int,
    fetched_at: datetime,
) -> int:
    """Append the observations that are new or that differ from the newest stored vintage.

    Return how many rows were inserted. The table is append-only, so a revised value is a new row
    and an unchanged value is no row. The advisory lock makes the check and the insert one step
    against a second writer.
    """
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
            knowable_at_of(item.observation_date),
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


def years_to_fetch(
    connection: psycopg.Connection[Any], *, today: date, registration: MacroSourceRegistration = REGISTRATION
) -> list[int]:
    """The years that one run asks for, in priority order.

    The current year always. In January, the year before too: a run that failed on the last
    business day of December would otherwise leave that day missing for ever. Each earlier year since
    ``registration.first_year`` that does not hold a row of every series yet.
    """
    years = [today.year]
    if today.month == 1:
        years.append(today.year - 1)
    series_ids = [item.series_id for item in registration.series]
    for year in range(today.year - 1, registration.first_year - 1, -1):
        if year not in years and not _holds_every_series(connection, year, series_ids):
            years.append(year)
    return years


def _holds_every_series(connection: psycopg.Connection[Any], year: int, series_ids: Sequence[str]) -> bool:
    row = connection.execute(
        "select count(distinct series_id) from mart.macro_series_daily "
        "where series_id = any(%s) and observation_date >= %s and observation_date < %s",
        (list(series_ids), date(year, 1, 1), date(year + 1, 1, 1)),
    ).fetchone()
    return bool(row and row[0] == len(series_ids))


class TreasuryRefreshError(RuntimeError):
    """One or more years failed. The years that did not fail are committed."""


@dataclass(frozen=True)
class YearResult:
    year: int
    #: Rows appended to the table by this run.
    inserted: int
    #: The ``raw.fetches`` row of the file. None when the key had landed before and no request was made.
    fetch_id: int | None
    #: Observations of the file, over all series. 0 when no request was made.
    observations: int
    #: True when the file of this day had landed before and this run made no request.
    reused: bool = False


def _refresh_year(
    connection: psycopg.Connection[Any],
    year: int,
    *,
    fetch: Callable[[int], bytes],
    clock: Callable[[], datetime],
    store: RawObjectStore | None,
    registration: MacroSourceRegistration,
) -> YearResult:
    fetched_at = clock()
    key = fetch_key_of(year, on=fetched_at.astimezone(UTC).date())
    if has_landed(connection, key):
        return YearResult(year=year, inserted=0, fetch_id=None, observations=0, reused=True)
    body = fetch(year)
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
    fetch: Callable[[int], bytes] = fetch_year,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    store: RawObjectStore | None = None,
    registration: MacroSourceRegistration = REGISTRATION,
) -> list[YearResult]:
    """Write the catalog, then fetch, store and append the years that ``years_to_fetch`` names.

    Commit after each year. A failed year rolls back its own work, and the next year still runs: a
    failed backfill must not block the current year. At the end, any failure raises
    ``TreasuryRefreshError``.
    """
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
        except Exception as error:  # noqa: BLE001 - recorded, and raised after the last year
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
