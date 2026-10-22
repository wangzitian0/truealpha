"""#1255: the resolver reads the rate of a cutoff from the stored Treasury series.

A cutoff sees the newest observation that was knowable at the cutoff, never one of its own day, and
never one older than `MAX_RATE_LAG_DAYS`. Without such an observation the resolver raises a named
error. No test here supplies a default, because the resolver has none.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from data_engine.datahub.production_topt.source_registrations import macro_registration_for
from data_engine.datahub.risk_free import (
    INSTRUMENT,
    MAX_RATE_LAG_DAYS,
    MISSING_REASON,
    MissingRiskFreeRate,
    RiskFreeRate,
    risk_free_rate_at,
)
from data_engine.datahub.treasury_rates import knowable_at_of
from truealpha_contracts.strategy import ExclusionReason, load_live_strategy_definition
from truealpha_runtime.testing import isolated_test_database

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "support"))
from macro_seed import seed_observation, seed_risk_free_rate  # noqa: E402

CUTOFF = datetime(2026, 10, 14, 22, 15, tzinfo=UTC)


@pytest.fixture(scope="module")
def database() -> Iterator[str]:
    with isolated_test_database("riskfree") as url:
        yield url


@pytest.fixture
def connection(database: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(database) as active:
        # TRUNCATE fires no row trigger, so it clears the append-only tables.
        active.execute("truncate mart.macro_series_daily, mart.macro_series, raw.fetches cascade")
        active.commit()
        yield active


def test_the_instrument_is_the_one_the_strategy_definition_declares() -> None:
    """The text is stored with every row. A rename of the enum value would orphan the history."""
    declared = load_live_strategy_definition().labor_efficiency.risk_free

    assert declared.instrument.value == INSTRUMENT == "us_treasury_bill_3_month"
    assert (declared.rule_id, declared.rate_unit) == ("per_cutoff_versioned_parameter", "annualized_decimal_fraction")
    assert declared.missing_rate_reason is ExclusionReason.MISSING_RISK_FREE_RATE_PARAMETER
    assert declared.missing_rate_reason.value == MISSING_REASON == "missing_risk_free_rate_parameter"


def test_the_lag_limit_is_twice_the_cadence_of_the_series() -> None:
    """The verdict turns red at the cadence. A tick stops at twice the cadence."""
    assert MAX_RATE_LAG_DAYS == 2 * macro_registration_for(INSTRUMENT).max_age_days == 10


def test_a_cutoff_takes_the_rate_of_the_previous_business_day(connection) -> None:
    """A Wednesday cutoff sees Tuesday. A Tuesday cutoff after a holiday weekend sees the Friday before."""
    seed_risk_free_rate(connection, date(2026, 10, 9), "0.0425")  # Friday
    seed_risk_free_rate(connection, date(2026, 10, 13), "0.0426")  # Tuesday: Columbus Day was Monday

    wednesday = risk_free_rate_at(connection, datetime(2026, 10, 14, 22, 15, tzinfo=UTC))
    tuesday = risk_free_rate_at(connection, datetime(2026, 10, 13, 22, 15, tzinfo=UTC))

    assert (wednesday.observation_date, wednesday.value) == (date(2026, 10, 13), Decimal("0.0426"))
    assert (tuesday.observation_date, tuesday.value) == (date(2026, 10, 9), Decimal("0.0425"))


@pytest.mark.parametrize("cutoff_day", [date(2026, 10, 10), date(2026, 10, 11), date(2026, 10, 12)])
def test_a_weekend_and_a_holiday_see_the_last_friday(connection, cutoff_day: date) -> None:
    seed_risk_free_rate(connection, date(2026, 10, 9), "0.0425")

    found = risk_free_rate_at(
        connection, datetime.combine(cutoff_day, datetime.min.time(), tzinfo=UTC) + timedelta(hours=22)
    )

    assert found.observation_date == date(2026, 10, 9)


def test_a_gap_of_ten_days_passes_and_eleven_raises(connection) -> None:
    seed_risk_free_rate(connection, date(2026, 10, 1), "0.0417")
    last_passing = datetime(2026, 10, 11, 22, 15, tzinfo=UTC)  # 10 days after the observation date
    first_failing = datetime(2026, 10, 12, 0, 0, tzinfo=UTC)  # 11 days after

    assert risk_free_rate_at(connection, last_passing).value == Decimal("0.0417")
    with pytest.raises(MissingRiskFreeRate, match="newest knowable observation: 2026-10-01") as raised:
        risk_free_rate_at(connection, first_failing)
    assert raised.value.newest == date(2026, 10, 1)
    assert raised.value.cutoff_at == first_failing


def test_no_observation_raises_a_named_error_that_carries_the_reason_code(connection) -> None:
    with pytest.raises(MissingRiskFreeRate) as raised:
        risk_free_rate_at(connection, CUTOFF)

    message = str(raised.value)
    assert message.startswith("missing_risk_free_rate_parameter: ")
    assert "us_treasury_bill_3_month" in message and "within 10 days" in message
    assert "2026-10-14T22:15:00+00:00" in message and "newest knowable observation: none stored" in message
    assert raised.value.newest is None
    assert isinstance(raised.value, RuntimeError)


def _vintages(connection) -> None:
    """Three vintages of 10-13. The first is the Treasury's own. A revision is knowable from the moment it was seen."""
    seed_risk_free_rate(connection, date(2026, 10, 13), "0.0426")  # knowable 10-14 00:00, the source's cut
    seed_risk_free_rate(connection, date(2026, 10, 13), "0.0420", knowable_at=datetime(2026, 10, 15, 21, 0, tzinfo=UTC))
    seed_risk_free_rate(connection, date(2026, 10, 13), "0.0431", knowable_at=datetime(2026, 10, 16, 21, 0, tzinfo=UTC))


@pytest.mark.parametrize(
    ("cutoff", "expected"),
    [
        (datetime(2026, 10, 14, 22, 15, tzinfo=UTC), "0.0426"),
        (datetime(2026, 10, 15, 20, 59, tzinfo=UTC), "0.0426"),
        (datetime(2026, 10, 15, 21, 0, tzinfo=UTC), "0.0420"),
        (datetime(2026, 10, 16, 20, 59, 59, tzinfo=UTC), "0.0420"),
        (datetime(2026, 10, 16, 21, 0, tzinfo=UTC), "0.0431"),
        (datetime(2026, 10, 20, 22, 15, tzinfo=UTC), "0.0431"),
    ],
    ids=["original", "just-before-first-revision", "first-revision", "just-before-second", "second", "later"],
)
def test_the_newest_vintage_that_is_knowable_at_the_cutoff_answers(connection, cutoff: datetime, expected: str) -> None:
    """A revision seen on 10-16 must not reach a cutoff of 10-14: that is look-ahead."""
    _vintages(connection)

    assert risk_free_rate_at(connection, cutoff).value == Decimal(expected)


def test_a_revision_does_not_change_what_an_earlier_cutoff_saw(connection) -> None:
    """Red when the reader orders by the insert time alone: the revision would win at the early cutoff."""
    seed_risk_free_rate(connection, date(2026, 10, 13), "0.0426")
    early = datetime(2026, 10, 14, 22, 15, tzinfo=UTC)
    before = risk_free_rate_at(connection, early)

    seed_risk_free_rate(connection, date(2026, 10, 13), "0.0431", knowable_at=datetime(2026, 10, 16, 21, 0, tzinfo=UTC))

    assert risk_free_rate_at(connection, early) == before


def test_the_knowable_time_decides_between_vintages_not_the_insert_order(connection) -> None:
    """A vintage that is loaded late (a backfill of an earlier state) must not outrank a later one.

    Red when the reader orders by the insert time: the vintage stored last would answer."""
    seed_risk_free_rate(connection, date(2026, 10, 13), "0.0431", knowable_at=datetime(2026, 10, 16, 21, 0, tzinfo=UTC))
    seed_risk_free_rate(connection, date(2026, 10, 13), "0.0426")  # the source's own cut, stored second

    assert risk_free_rate_at(connection, datetime(2026, 10, 20, 22, 15, tzinfo=UTC)).value == Decimal("0.0431")
    assert risk_free_rate_at(connection, datetime(2026, 10, 14, 22, 15, tzinfo=UTC)).value == Decimal("0.0426")


def test_the_insert_order_breaks_a_tie_between_two_vintages_knowable_at_one_instant(connection) -> None:
    tied = datetime(2026, 10, 14, 21, 15, tzinfo=UTC)
    seed_risk_free_rate(connection, date(2026, 10, 13), "0.0426", recorded_at=tied)
    seed_risk_free_rate(connection, date(2026, 10, 13), "0.0427", recorded_at=tied)

    assert risk_free_rate_at(connection, CUTOFF).value == Decimal("0.0427")


def test_a_newer_date_beats_a_newer_vintage_of_an_older_date(connection) -> None:
    seed_risk_free_rate(connection, date(2026, 10, 9), "0.0425", recorded_at=datetime(2026, 10, 20, tzinfo=UTC))
    seed_risk_free_rate(
        connection, date(2026, 10, 13), "0.0426", recorded_at=datetime(2026, 10, 14, 21, 15, tzinfo=UTC)
    )

    assert risk_free_rate_at(connection, CUTOFF).observation_date == date(2026, 10, 13)


def test_a_cutoff_never_sees_a_rate_of_its_own_day(connection) -> None:
    """The row of 10-13 becomes knowable at 10-14 00:00. One microsecond before, a cutoff sees 10-09."""
    seed_risk_free_rate(connection, date(2026, 10, 9), "0.0425")
    seed_risk_free_rate(connection, date(2026, 10, 13), "0.0426")
    knowable = knowable_at_of(date(2026, 10, 13))

    assert risk_free_rate_at(connection, knowable).observation_date == date(2026, 10, 13), "knowable_at <= cutoff"
    assert risk_free_rate_at(connection, knowable - timedelta(microseconds=1)).observation_date == date(2026, 10, 9)
    assert risk_free_rate_at(connection, datetime(2026, 10, 13, 23, 59, 59, tzinfo=UTC)).observation_date == date(
        2026, 10, 9
    )


def test_a_later_observation_is_invisible_to_an_earlier_cutoff(connection) -> None:
    """No look-ahead: a backtest cutoff of the past is answered by the past."""
    seed_risk_free_rate(connection, date(2026, 3, 30), "0.0380")
    seed_risk_free_rate(connection, date(2026, 6, 29), "0.0365")
    seed_risk_free_rate(connection, date(2026, 10, 13), "0.0426")

    assert risk_free_rate_at(connection, datetime(2026, 3, 31, 23, 59, 59, tzinfo=UTC)).value == Decimal("0.0380")
    assert risk_free_rate_at(connection, datetime(2026, 6, 30, 23, 59, 59, tzinfo=UTC)).value == Decimal("0.0365")
    with pytest.raises(MissingRiskFreeRate, match="newest knowable observation: 2026-06-29"):
        risk_free_rate_at(connection, datetime(2026, 7, 31, 23, 59, 59, tzinfo=UTC))


def test_a_row_of_another_series_does_not_answer(connection) -> None:
    """The 10-year yield is stored, and the risk-free rate is the 3-month yield: no answer."""
    seed_observation(connection, date(2026, 10, 13), "0.0999", series_id="us_treasury_note_10_year")

    with pytest.raises(MissingRiskFreeRate, match="none stored"):
        risk_free_rate_at(connection, CUTOFF)


def test_the_answer_carries_its_lineage(connection) -> None:
    fetch_id = seed_risk_free_rate(connection, date(2026, 10, 13), "0.0426")

    found = risk_free_rate_at(connection, CUTOFF)

    assert found == RiskFreeRate(
        series_id="us_treasury_bill_3_month",
        value=Decimal("0.0426"),
        observation_date=date(2026, 10, 13),
        knowable_at=datetime(2026, 10, 14, 0, 0, tzinfo=UTC),
        source="us-treasury",
        fetch_id=fetch_id,
    )
    assert isinstance(found.value, Decimal)
    assert found.lineage() == {
        "series_id": "us_treasury_bill_3_month",
        "value": "0.0426",
        "observation_date": "2026-10-13",
        "knowable_at": "2026-10-14T00:00:00+00:00",
        "source": "us-treasury",
        "fetch_id": fetch_id,
    }


def test_the_source_of_the_answer_is_the_source_the_row_states(connection) -> None:
    seed_observation(connection, date(2026, 10, 13), "0.0426", source="fred")

    assert risk_free_rate_at(connection, CUTOFF).source == "fred"


def test_the_lag_is_counted_on_the_utc_date_of_the_cutoff(connection) -> None:
    """23:30 on 10-14 in New York is 03:30 on 10-15 in UTC, so the lag to 10-04 is 11 days there."""
    seed_risk_free_rate(connection, date(2026, 10, 4), "0.0420")
    new_york = timezone(timedelta(hours=-4))

    with pytest.raises(MissingRiskFreeRate):
        risk_free_rate_at(connection, datetime(2026, 10, 14, 23, 30, tzinfo=new_york))
    assert risk_free_rate_at(connection, datetime(2026, 10, 14, 19, 30, tzinfo=new_york)).value == Decimal("0.0420")


def test_a_cutoff_without_a_time_zone_is_refused(connection) -> None:
    with pytest.raises(ValueError, match="time zone"):
        risk_free_rate_at(connection, datetime(2026, 10, 14, 22, 15))
