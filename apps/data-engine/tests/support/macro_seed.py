"""Test support: stored macro observations and the risk-free rate (#1255).

The live tick, the TOPT core engine and the nightly backtest read the rate of a cutoff from
`mart.macro_series_daily`. A test that drives one of them stores the observation first.
The helpers here write a row with the column rules of the production writer. They make no network
call and use no object store. The raw bytes go to an in-memory store.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from data_engine import raw_store
from data_engine.datahub.risk_free import INSTRUMENT, RiskFreeRate
from data_engine.datahub.treasury_rates import CONFIDENCE, REGISTRATION, knowable_at_of, register_series
from truealpha_contracts.models import DataSource
from truealpha_runtime.testing import InMemoryRawObjectStore

#: A plausible 3-month rate that no old constant of the repository equals (0.05, 0.038, 0.0365).
DEFAULT_RATE = Decimal("0.0426")

_INSERT = """
insert into mart.macro_series_daily
    (series_id, observation_date, value, knowable_at, source, fetch_id, fetched_at, confidence, recorded_at)
values (%s, %s, %s, %s, %s, %s, %s, %s, coalesce(%s, clock_timestamp()))
"""


def _land(connection: Any, record_id: str, body: bytes) -> tuple[int, datetime]:
    """Store raw bytes. Return the `raw.fetches` id and the fetch time."""
    fetched_at = datetime.now(UTC)
    fetch_id = raw_store.insert_fetch(
        connection,
        source=DataSource.US_TREASURY,
        source_record_id=record_id,
        body=body,
        content_type="text/csv",
        fetched_at=fetched_at,
        store=InMemoryRawObjectStore(),
    )
    return fetch_id, fetched_at


def seed_observation(
    connection: Any,
    observation_date: date,
    value: Decimal | str = DEFAULT_RATE,
    *,
    series_id: str = INSTRUMENT,
    recorded_at: datetime | None = None,
    knowable_at: datetime | None = None,
    source: str = DataSource.US_TREASURY.value,
) -> int:
    """Store one observation. Return the id of its `raw.fetches` row.

    ``knowable_at`` defaults to the source's cut for the date. A test of a revision names the later
    instant at which the revised value was first seen, as the production writer does."""
    register_series(connection)
    fetch_id, fetched_at = _land(
        connection, f"test-seed:{series_id}:{observation_date.isoformat()}", f"{observation_date},{value}".encode()
    )
    connection.execute(
        _INSERT,
        (
            series_id,
            observation_date,
            Decimal(str(value)),
            knowable_at or knowable_at_of(observation_date),
            source,
            fetch_id,
            fetched_at,
            CONFIDENCE,
            recorded_at,
        ),
    )
    return fetch_id


def seed_risk_free_rate(
    connection: Any,
    observation_date: date,
    rate: Decimal | str = DEFAULT_RATE,
    *,
    recorded_at: datetime | None = None,
    knowable_at: datetime | None = None,
) -> int:
    """Store one observation of the risk-free instrument. Return the id of its `raw.fetches` row."""
    return seed_observation(connection, observation_date, rate, recorded_at=recorded_at, knowable_at=knowable_at)


def seed_rate_for_cutoff(connection: Any, cutoff: datetime, rate: Decimal | str = DEFAULT_RATE) -> date:
    """Store the observation of the day before the cutoff date. That is the newest one a cutoff can see."""
    observation_date = cutoff.astimezone(UTC).date() - timedelta(days=1)
    seed_risk_free_rate(connection, observation_date, rate)
    return observation_date


def stored_rate_for_cutoff(connection: Any, cutoff: datetime, rate: Decimal | str = DEFAULT_RATE) -> RiskFreeRate:
    """Store the observation of the day before the cutoff and read it back as the tick does, with its lineage."""
    from data_engine.datahub.risk_free import risk_free_rate_at

    seed_rate_for_cutoff(connection, cutoff, rate)
    return risk_free_rate_at(connection, cutoff)


def stub_rate(rate: Decimal | str = DEFAULT_RATE, observation_date: date = date(2026, 7, 29)) -> RiskFreeRate:
    """A resolved rate for a test that fakes the database."""
    return RiskFreeRate(
        series_id=INSTRUMENT,
        value=Decimal(str(rate)),
        observation_date=observation_date,
        knowable_at=knowable_at_of(observation_date),
        source=DataSource.US_TREASURY.value,
        fetch_id=1,
    )


def store_risk_free_series(
    database_url: str,
    *,
    first: date = date(2026, 3, 1),
    last: date = date(2026, 9, 30),
    rate: Decimal | str = DEFAULT_RATE,
) -> None:
    """One observation per calendar day from `first` to `last`, committed to one test database.

    A test world has no business-day calendar. Any day of the span, weekend or not, can be the day
    before a cutoff. So every cutoff of the span finds a rate that is one day old. The default span
    holds every cutoff that the tick tests use. All registered series get the rows, so the freshness
    view has nothing missing.
    """
    import psycopg

    days = [first + timedelta(days=n) for n in range((last - first).days + 1)]
    with psycopg.connect(database_url) as connection:
        register_series(connection)
        fetch_id, fetched_at = _land(
            connection, f"test-series:{first.isoformat()}:{last.isoformat()}", f"{first}..{last},{rate}".encode()
        )
        with connection.cursor() as cursor:
            cursor.executemany(
                _INSERT,
                [
                    (
                        item.series_id,
                        day,
                        Decimal(str(rate)),
                        knowable_at_of(day),
                        DataSource.US_TREASURY.value,
                        fetch_id,
                        fetched_at,
                        CONFIDENCE,
                        None,
                    )
                    for item in REGISTRATION.series
                    for day in days
                ],
            )
        connection.commit()
