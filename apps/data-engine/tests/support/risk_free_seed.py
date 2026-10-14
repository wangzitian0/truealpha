"""Test support: stored risk-free rates (#1255).

The live tick, the TOPT core engine and the nightly backtest read the rate of a cutoff from
`staging.risk_free_rates_daily`. A test that drives one of them stores the observation first.
The helpers here write a row with the column rules of the production writer. They make no network
call and use no object store. The raw bytes go to an in-memory store.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from data_engine import raw_store
from data_engine.datahub.risk_free import INSTRUMENT, RiskFreeRate
from data_engine.datahub.treasury_rates import CONFIDENCE, knowable_at_of
from truealpha_contracts.models import DataSource
from truealpha_runtime.testing import InMemoryRawObjectStore

#: A plausible 3-month rate that no old constant of the repository equals (0.05, 0.038, 0.0365).
DEFAULT_RATE = Decimal("0.0426")

_INSERT = """
insert into staging.risk_free_rates_daily
    (instrument, observation_date, annualized_rate, knowable_at, fetch_id, confidence, recorded_at)
values (%s, %s, %s, %s, %s, %s, coalesce(%s, clock_timestamp()))
"""


def seed_risk_free_rate(
    connection: Any,
    observation_date: date,
    rate: Decimal | str = DEFAULT_RATE,
    *,
    recorded_at: datetime | None = None,
    instrument: str = INSTRUMENT,
) -> int:
    """Store one observation. Return the id of its `raw.fetches` row."""
    fetch_id = raw_store.insert_fetch(
        connection,
        source=DataSource.US_TREASURY,
        source_record_id=f"test-seed:{observation_date.isoformat()}",
        body=f"{observation_date.isoformat()},{rate}".encode(),
        content_type="text/csv",
        fetched_at=datetime.now(UTC),
        store=InMemoryRawObjectStore(),
    )
    connection.execute(
        _INSERT,
        (
            instrument,
            observation_date,
            Decimal(str(rate)),
            knowable_at_of(observation_date),
            fetch_id,
            CONFIDENCE,
            recorded_at,
        ),
    )
    return fetch_id


def seed_rate_for_cutoff(connection: Any, cutoff: datetime, rate: Decimal | str = DEFAULT_RATE) -> date:
    """Store the observation of the day before the cutoff date. That is the newest one a cutoff can see."""
    observation_date = cutoff.astimezone(UTC).date() - timedelta(days=1)
    seed_risk_free_rate(connection, observation_date, rate)
    return observation_date


def stub_rate(rate: Decimal | str = DEFAULT_RATE, observation_date: date = date(2026, 7, 29)) -> RiskFreeRate:
    """A resolved rate for a test that fakes the database."""
    return RiskFreeRate(
        value=Decimal(str(rate)),
        observation_date=observation_date,
        knowable_at=knowable_at_of(observation_date),
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
    holds every cutoff that the tick tests use.
    """
    import psycopg

    days = [first + timedelta(days=n) for n in range((last - first).days + 1)]
    with psycopg.connect(database_url) as connection:
        fetch_id = raw_store.insert_fetch(
            connection,
            source=DataSource.US_TREASURY,
            source_record_id=f"test-series:{first.isoformat()}:{last.isoformat()}",
            body=f"{first.isoformat()}..{last.isoformat()},{rate}".encode(),
            content_type="text/csv",
            fetched_at=datetime.now(UTC),
            store=InMemoryRawObjectStore(),
        )
        with connection.cursor() as cursor:
            cursor.executemany(
                "insert into staging.risk_free_rates_daily "
                "(instrument, observation_date, annualized_rate, knowable_at, fetch_id, confidence) "
                "values (%s, %s, %s, %s, %s, %s)",
                [(INSTRUMENT, day, Decimal(str(rate)), knowable_at_of(day), fetch_id, CONFIDENCE) for day in days],
            )
        connection.commit()
