"""The nightly verdict `fetch_keys_once` over a real call ledger (#1411 K7, #1418 R2.B2).

Each test writes rows into `staging.api_call_ledger` of an isolated database, on a UTC day of its
own, and judges that day. The ticks themselves are in
`production_topt/test_fetch_keys_through_tick.py`; here the rows are exact, so each rule of the
verdict has a case that fails when the rule is removed.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import psycopg
import pytest
from data_engine.quality import fetch_keys_once
from data_engine.quality.nightly_verdicts import SUMMARY_LIMIT, bounded
from truealpha_runtime.testing import isolated_test_database

KEY = "sec|companyfacts|issuer:cik:0000320193|2026-10-20"


@pytest.fixture(scope="module")
def connection() -> Iterator[psycopg.Connection]:
    with isolated_test_database("fetchkeysonce") as url, psycopg.connect(url, autocommit=True) as active:
        yield active


def _row(
    connection: psycopg.Connection,
    when: datetime,
    *,
    key: str | None,
    ok: bool = True,
    cost: int = 1,
    source: str = "sec",
    endpoint: str = "companyfacts",
) -> None:
    connection.execute(
        """
        insert into staging.api_call_ledger (source, endpoint, caller, called_at, ok, cost, fetch_key)
        values (%s, %s, 'test', %s, %s, %s, %s)
        """,
        (source, endpoint, when, ok, cost, key),
    )


def _noon(day: date) -> datetime:
    return datetime.combine(day, datetime.min.time(), tzinfo=UTC) + timedelta(hours=12)


def test_a_day_without_a_successful_vendor_call_is_pending_not_green(connection: psycopg.Connection) -> None:
    day = date(2026, 1, 1)
    judgement = fetch_keys_once.evaluate(connection, day=day)
    assert judgement.pending and not judgement.red
    assert "nothing to judge" in judgement.summary


def test_a_day_whose_calls_carry_no_key_is_red_because_nothing_can_be_counted(connection: psycopg.Connection) -> None:
    day = date(2026, 1, 2)
    _row(connection, _noon(day), key=None, endpoint="submissions")
    judgement = fetch_keys_once.evaluate(connection, day=day)
    assert judgement.red and not judgement.pending
    assert judgement.unkeyed_by_endpoint == (("sec", "submissions", 1),)
    assert "no call carries a fetch key" in judgement.summary


def test_a_key_with_one_successful_row_after_a_failed_attempt_is_green(connection: psycopg.Connection) -> None:
    day = date(2026, 1, 3)
    _row(connection, _noon(day), key=KEY, ok=False)
    _row(connection, _noon(day) + timedelta(minutes=1), key=KEY)
    judgement = fetch_keys_once.evaluate(connection, day=day)
    assert not judgement.red and judgement.keyed == 1 and judgement.duplicates == ()
    assert "no key fetched twice" in judgement.summary


def test_a_key_with_two_successful_rows_is_red_and_the_summary_names_it(connection: psycopg.Connection) -> None:
    day = date(2026, 1, 4)
    _row(connection, _noon(day), key=KEY)
    _row(connection, _noon(day) + timedelta(hours=9), key=KEY)
    judgement = fetch_keys_once.evaluate(connection, day=day)
    assert judgement.red and judgement.duplicates == ((KEY, 2),)
    assert f"{KEY} x2" in judgement.summary and "1 key(s) fetched twice" in judgement.summary


def test_the_same_key_on_two_utc_days_is_two_fetches_of_two_days_and_not_a_duplicate(
    connection: psycopg.Connection,
) -> None:
    """The edge of the day is 00:00 UTC: a row at 23:59:59 belongs to its own day, and so does 00:00:00."""
    first, second = date(2026, 1, 5), date(2026, 1, 6)
    _row(connection, datetime.combine(first, datetime.max.time(), tzinfo=UTC).replace(microsecond=0), key=KEY)
    _row(connection, datetime.combine(second, datetime.min.time(), tzinfo=UTC), key=KEY)
    assert not fetch_keys_once.evaluate(connection, day=first).red
    assert not fetch_keys_once.evaluate(connection, day=second).red


def test_a_model_call_that_spends_no_quota_is_not_counted(connection: psycopg.Connection) -> None:
    day = date(2026, 1, 7)
    _row(connection, _noon(day), key=None, cost=0, source="model", endpoint="chat")
    assert fetch_keys_once.evaluate(connection, day=day).pending


def test_many_duplicates_keep_the_summary_inside_the_verdict_column_and_the_judgement_holds_them_all(
    connection: psycopg.Connection,
) -> None:
    day = date(2026, 1, 8)
    for index in range(40):
        for minute in (0, 1):
            _row(
                connection,
                _noon(day) + timedelta(minutes=minute),
                key=f"sec|companyfacts|issuer:cik:{index:010d}|2026-10-20",
            )
    judgement = fetch_keys_once.evaluate(connection, day=day)
    assert len(judgement.duplicates) == 40 and "37 more" in judgement.summary
    assert len(bounded(judgement.summary)) <= SUMMARY_LIMIT


@pytest.mark.parametrize(
    ("config_day", "tick", "now", "expected"),
    [
        (
            None,
            datetime(2026, 10, 21, 6, 45, tzinfo=UTC),
            datetime(2026, 10, 21, 6, 46, tzinfo=UTC),
            date(2026, 10, 20),
        ),
        (None, None, datetime(2026, 10, 21, 9, 0, tzinfo=UTC), date(2026, 10, 20)),
        (
            "2026-10-01",
            datetime(2026, 10, 21, 6, 45, tzinfo=UTC),
            datetime(2026, 10, 21, 6, 46, tzinfo=UTC),
            date(2026, 10, 1),
        ),
    ],
)
def test_the_judged_day_is_the_day_before_the_tick_unless_a_replay_names_one(
    config_day: str | None, tick: datetime | None, now: datetime, expected: date
) -> None:
    assert fetch_keys_once.judged_day(config_day, tick, now=now) == expected
