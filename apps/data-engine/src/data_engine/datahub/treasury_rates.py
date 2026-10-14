"""The U.S. Treasury source of the risk-free rate (#1255).

Source: "Daily Treasury Par Yield Curve Rates" of the U.S. Department of the Treasury. The site
publishes one public CSV per calendar year. No key is needed. The file lists one row per business
day, newest first. The column "3 Mo" holds the 3-month rate in percent, for example ``4.26``.

    Date,"1 Mo","1.5 Month","2 Mo","3 Mo","4 Mo",...
    10/13/2026,4.10,4.15,4.15,4.26,4.32,...

The column set changes between years. The 2023 file has no "1.5 Month". So the parser finds each
column by its name, never by its position.

What this module does:

1. ``fetch_year`` asks for one year through ``gateway.urlopen`` (ledger seat ``us-treasury``,
   capacity declared in ``LEDGER_CAPACITIES``, init.md rule 6).
2. ``parse_par_yield_csv`` turns the bytes into dated observations. It refuses a malformed file by
   name. It never repairs one.
3. ``refresh_risk_free_rates`` lands the bytes in object storage (``raw.fetches``) and appends the
   new or revised observations to ``staging.risk_free_rates_daily``. A second run over the same
   bytes inserts nothing.

Point in time: ``knowable_at`` is 00:00 UTC of the day after the observation date. The Treasury
publishes a day's rate in the afternoon of that day, and a backtest cutoff must never see a rate of
its own day. ``datahub.risk_free`` reads the table.

Corroboration class B (docs/architecture-decisions/A3): one authoritative origin, domain bounds in
the parser, and a CHECK constraint on ``knowable_at`` in the table. Rule 19 (init.md): the rights
and budget attestation of this source is ``missing``, as it is for every source except the model
provider. It does not block.
"""

from __future__ import annotations

import csv
import io
import logging
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import psycopg
from truealpha_contracts.models import DataSource
from truealpha_contracts.ports import RawObjectStore

from data_engine import raw_store
from data_engine.datahub.risk_free import INSTRUMENT
from data_engine.sources import gateway

log = logging.getLogger(__name__)

SOURCE = DataSource.US_TREASURY
#: The ledger endpoint name, the ``raw.fetches`` record prefix and the query type of the Treasury URL.
ENDPOINT = "daily_treasury_yield_curve"
BASE_URL = "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv"
#: The agent text that the Twelve Data client sends.
USER_AGENT = "TrueAlpha-DataEngine/1.0"
REQUEST_TIMEOUT_SECONDS = 30

DATE_COLUMN = "Date"
#: The column that carries ``INSTRUMENT``, in percent.
RATE_COLUMN = "3 Mo"
DATE_FORMAT = "%m/%d/%Y"

#: How many earlier years the first run fetches, in addition to the current year.
BACKFILL_YEARS = 3

#: Declared policy grade, not a per-row measurement. The Treasury publishes the series itself.
#: No second origin corroborates it. 0.92 is the grade of a complete SEC filing set.
CONFIDENCE = Decimal("0.92")

#: An advisory lock key. It makes two writers (the schedule and a manual replay) take turns.
_LOCK_NAME = "staging.risk_free_rates_daily"


class TreasuryFetchError(RuntimeError):
    """The Treasury did not answer with a file."""


class TreasuryFormatError(ValueError):
    """The file is not the file that this parser knows. The message names the row and the cause."""


@dataclass(frozen=True)
class Observation:
    """One business day of the 3-month series."""

    observation_date: date
    #: A decimal fraction. The file says percent: ``4.26`` becomes ``Decimal("0.0426")``.
    annualized_rate: Decimal


def year_url(year: int) -> str:
    return f"{BASE_URL}/{year}/all?type={ENDPOINT}&field_tdr_date_value={year}&page&_format=csv"


def knowable_at_of(observation_date: date) -> datetime:
    """00:00 UTC of the day after the observation: a rate is never known on its own day."""
    return datetime.combine(observation_date + timedelta(days=1), time.min, tzinfo=UTC)


def fetch_year(year: int) -> bytes:
    """The CSV bytes of one year, through the external call gateway. Raise on any status but 200."""
    request = urllib.request.Request(year_url(year), headers={"User-Agent": USER_AGENT})
    status, body = gateway.urlopen(
        SOURCE.value, ENDPOINT, request, caller="treasury_rates.fetch_year", timeout=REQUEST_TIMEOUT_SECONDS
    )
    if status != 200:
        raise TreasuryFetchError(f"the Treasury answered HTTP {status} for the {year} file ({len(body)} bytes)")
    return body


def parse_par_yield_csv(body: bytes, *, year: int) -> tuple[Observation, ...]:
    """The observations of one yearly file, oldest first.

    A row without a "3 Mo" value is skipped. The parser trusts no column position.
    A file or a row that breaks the format raises ``TreasuryFormatError``. These cases break it:

    * a header without "Date" or "3 Mo";
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
    if DATE_COLUMN not in header or RATE_COLUMN not in header:
        raise TreasuryFormatError(f"the {year} file has no {DATE_COLUMN!r} and {RATE_COLUMN!r} header: {header[:6]}")
    date_index, rate_index = header.index(DATE_COLUMN), header.index(RATE_COLUMN)
    found: dict[date, Decimal] = {}
    seen: set[date] = set()
    for number, row in enumerate(rows[1:], start=2):
        if len(row) != len(header):
            raise TreasuryFormatError(f"row {number}: {len(row)} fields, but the header has {len(header)}")
        observation_date = _parse_date(row[date_index], number, year)
        if observation_date in seen:
            raise TreasuryFormatError(f"row {number}: the date {observation_date.isoformat()} appears twice")
        seen.add(observation_date)
        text_value = row[rate_index].strip()
        if text_value:
            found[observation_date] = _parse_percent(text_value, number)
    return tuple(Observation(day, rate) for day, rate in sorted(found.items()))


def _parse_date(text: str, number: int, year: int) -> date:
    try:
        observation_date = datetime.strptime(text.strip(), DATE_FORMAT).date()
    except ValueError as error:
        raise TreasuryFormatError(f"row {number}: the date {text!r} is not {DATE_FORMAT}") from error
    if observation_date.year != year:
        raise TreasuryFormatError(f"row {number}: the date {observation_date.isoformat()} is outside {year}")
    return observation_date


def _parse_percent(text: str, number: int) -> Decimal:
    """A percent text as a decimal fraction. No binary float is involved."""
    try:
        percent = Decimal(text)
    except InvalidOperation as error:
        raise TreasuryFormatError(f"row {number}: the {RATE_COLUMN} value {text!r} is not a decimal") from error
    if not percent.is_finite() or not Decimal(0) <= percent <= Decimal(100):
        raise TreasuryFormatError(f"row {number}: the {RATE_COLUMN} value {text!r} is outside 0 to 100 percent")
    return percent.scaleb(-2)


_LATEST_OF_YEAR_SQL = """
select distinct on (observation_date) observation_date, annualized_rate
from staging.risk_free_rates_daily
where instrument = %s and observation_date >= %s and observation_date < %s
order by observation_date, recorded_at desc, id desc
"""

_INSERT_SQL = """
insert into staging.risk_free_rates_daily
    (instrument, observation_date, annualized_rate, knowable_at, fetch_id, confidence)
values (%s, %s, %s, %s, %s, %s)
"""


def persist_observations(
    connection: psycopg.Connection[Any], observations: Sequence[Observation], *, year: int, fetch_id: int
) -> int:
    """Append the observations that are new or that differ from the newest stored vintage.

    Return how many rows were inserted. The table is append-only, so a revised value is a new row
    and an unchanged value is no row. The advisory lock makes the check and the insert one step
    against a second writer.
    """
    connection.execute("select pg_advisory_xact_lock(hashtext(%s))", (_LOCK_NAME,))
    stored = dict(
        connection.execute(_LATEST_OF_YEAR_SQL, (INSTRUMENT, date(year, 1, 1), date(year + 1, 1, 1))).fetchall()
    )
    fresh = [
        (
            INSTRUMENT,
            item.observation_date,
            item.annualized_rate,
            knowable_at_of(item.observation_date),
            fetch_id,
            CONFIDENCE,
        )
        for item in observations
        if stored.get(item.observation_date) != item.annualized_rate
    ]
    with connection.cursor() as cursor:
        cursor.executemany(_INSERT_SQL, fresh)
    return len(fresh)


def years_to_fetch(connection: psycopg.Connection[Any], *, today: date) -> list[int]:
    """The years that one run asks for, in priority order.

    The current year always. In January, the year before too: a run that failed on the last
    business day of December would otherwise leave that day missing for ever. Each of the
    ``BACKFILL_YEARS`` earlier years that holds no row yet.
    """
    years = [today.year]
    if today.month == 1:
        years.append(today.year - 1)
    for year in range(today.year - 1, today.year - 1 - BACKFILL_YEARS, -1):
        if year not in years and not _holds_rows(connection, year):
            years.append(year)
    return years


def _holds_rows(connection: psycopg.Connection[Any], year: int) -> bool:
    row = connection.execute(
        "select exists (select 1 from staging.risk_free_rates_daily "
        "where instrument = %s and observation_date >= %s and observation_date < %s)",
        (INSTRUMENT, date(year, 1, 1), date(year + 1, 1, 1)),
    ).fetchone()
    return bool(row and row[0])


class TreasuryRefreshError(RuntimeError):
    """One or more years failed. The years that did not fail are committed."""


@dataclass(frozen=True)
class YearResult:
    year: int
    fetch_id: int
    #: Rows of the file that carry a "3 Mo" value.
    observations: int
    #: Rows appended to the table by this run.
    inserted: int


def _refresh_year(
    connection: psycopg.Connection[Any],
    year: int,
    *,
    fetch: Callable[[int], bytes],
    clock: Callable[[], datetime],
    store: RawObjectStore | None,
) -> YearResult:
    body = fetch(year)
    observations = parse_par_yield_csv(body, year=year)
    fetch_id = raw_store.insert_fetch(
        connection,
        source=SOURCE,
        source_record_id=f"{ENDPOINT}:{year}",
        body=body,
        content_type="text/csv",
        fetched_at=clock(),
        metadata={"year": year, "url": year_url(year), "column": RATE_COLUMN, "instrument": INSTRUMENT},
        store=store,
    )
    inserted = persist_observations(connection, observations, year=year, fetch_id=fetch_id)
    return YearResult(year=year, fetch_id=fetch_id, observations=len(observations), inserted=inserted)


def refresh_risk_free_rates(
    connection: psycopg.Connection[Any],
    *,
    today: date,
    fetch: Callable[[int], bytes] = fetch_year,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    store: RawObjectStore | None = None,
) -> list[YearResult]:
    """Fetch, store and append the years that ``years_to_fetch`` names. Commit after each year.

    A failed year rolls back its own work, and the next year still runs: a failed backfill must not
    block the current year. At the end, any failure raises ``TreasuryRefreshError``.
    """
    results: list[YearResult] = []
    failures: list[str] = []
    first_error: Exception | None = None
    for year in years_to_fetch(connection, today=today):
        try:
            results.append(_refresh_year(connection, year, fetch=fetch, clock=clock, store=store))
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
    "BACKFILL_YEARS",
    "CONFIDENCE",
    "RATE_COLUMN",
    "Observation",
    "TreasuryFetchError",
    "TreasuryFormatError",
    "TreasuryRefreshError",
    "YearResult",
    "fetch_year",
    "knowable_at_of",
    "parse_par_yield_csv",
    "persist_observations",
    "refresh_risk_free_rates",
    "year_url",
    "years_to_fetch",
]
