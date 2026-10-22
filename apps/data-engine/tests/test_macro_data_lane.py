"""#1255: the macro lane loads the Treasury series every day and records the verdict `macro_freshness`.

The tests drive the deployed job `macro_data_refresh_pipeline`. The HTTP layer under
`gateway.urlopen` is a stub that answers with fixed files. The gate, the ledger, the raw store,
the database writes and the freshness view are the real ones. The gate runs on a fake clock, so the
declared pace of one request per second costs no real second.

A file for the current year holds the three most recent days, so the newest observation is as old as
the test sets it. The files of earlier years hold their last day.
"""

from __future__ import annotations

import importlib
import io
import sys
import urllib.error
import urllib.request
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import dagster as dg
import psycopg
import pytest
from data_engine import raw_store
from data_engine.config import settings
from data_engine.datahub.risk_free import INSTRUMENT
from data_engine.datahub.treasury_rates import REGISTRATION
from data_engine.lanes import LANE_MODULES, macro_data, nightly_verdict_names
from data_engine.quality import nightly_verdicts
from data_engine.sources import gateway
from truealpha_runtime.testing import InMemoryRawObjectStore, isolated_test_database, load_tool

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
from macro_seed import seed_observation  # noqa: E402

TICK = "2026-10-20T20:30:00+00:00"
SERIES = [item.series_id for item in REGISTRATION.series]
store = InMemoryRawObjectStore()


@pytest.fixture(scope="module")
def module_database() -> Iterator[str]:
    with isolated_test_database("macrolane") as url:
        yield url


@pytest.fixture
def database(module_database: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    with psycopg.connect(module_database) as clean:
        # TRUNCATE fires no row trigger, so it clears the append-only tables.
        clean.execute("truncate mart.macro_series_daily, mart.macro_series, raw.fetches cascade")
        clean.commit()
    monkeypatch.setattr(settings, "database_url", module_database)
    monkeypatch.setattr(raw_store, "object_store", lambda: store)
    yield module_database


@pytest.fixture
def connection(database: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(database) as active:
        yield active


def _today(connection: psycopg.Connection) -> date:
    """The UTC date of the database clock. The freshness view counts age against it."""
    return connection.execute("select (now() at time zone 'UTC')::date").fetchone()[0]


class _Response:
    def __init__(self, body: bytes) -> None:
        self._body, self.status = body, 200

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _file(year: int, newest_age_days: int) -> bytes:
    """A Treasury file of `year`. The newest row of all files is `newest_age_days` old.

    The three newest days sit in the file of their own year. A year without such a day holds one old
    row (or none, for the current year), so the age of the newest row never depends on the date of the run.
    """
    today = datetime.now(UTC).date()
    recent = [today - timedelta(days=newest_age_days + n) for n in range(3)]
    days = [day for day in recent if day.year == year]
    if not days and year < today.year:
        days = [date(year, 1, 2)]
    rows = [f"{day.month:02d}/{day.day:02d}/{day.year},4.26,4.79,5.21" for day in days]
    return "\n".join(['Date,"3 Mo","2 Yr","10 Yr"', *rows]).encode()


def _treasury(monkeypatch: pytest.MonkeyPatch, *, newest_age_days: int = 1, status: int = 200) -> list[int]:
    """The HTTP layer under `gateway.urlopen`. Returns the list of years that were asked for."""
    asked: list[int] = []

    def urlopen(request: urllib.request.Request, timeout: float) -> _Response:
        year = int(request.full_url.split("/daily-treasury-rates.csv/")[1].split("/")[0])
        asked.append(year)
        if status != 200:
            raise urllib.error.HTTPError(request.full_url, status, "error", {}, io.BytesIO(b"error"))  # type: ignore[arg-type]
        return _Response(_file(year, newest_age_days))

    monkeypatch.setattr(gateway.urllib.request, "urlopen", urlopen)
    return asked


class _Clock:
    """Fake time for the gate: sleeping moves the clock, so the declared pace costs no real second."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def fast_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """`capacity_scope()` binds the deployed gate with its declared pace on a fake clock."""
    clock = _Clock()
    bound = gateway.CapacityGate(clock=clock, sleep=clock.sleep, recent_calls=None, spent_since=lambda _s, _t: 0)
    real_scope = gateway.capacity_scope
    monkeypatch.setattr(gateway, "capacity_scope", lambda given=None: real_scope(given or bound))


@pytest.fixture
def written(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    monkeypatch.setattr(nightly_verdicts, "record", lambda name, **kwargs: rows.append({"check": name, **kwargs}))
    return rows


def _run_job() -> dg.ExecuteInProcessResult:
    return macro_data.macro_data_refresh_pipeline_job.execute_in_process(
        tags={nightly_verdicts.TICK_TAG: TICK}, raise_on_error=False
    )


def _ledger_years(call_ledger: gateway.MemoryLedger) -> list[int]:
    return [
        int(row.request_uri.split("/daily-treasury-rates.csv/")[1].split("/")[0])
        for row in call_ledger
        if row.source == "us-treasury"
    ]


# -- the wiring: two triggers, one registered lane, one verdict --------------------------------------


def test_the_lane_is_registered_and_declares_the_verdict_it_records() -> None:
    assert "data_engine.lanes.macro_data" in LANE_MODULES
    assert macro_data.NIGHTLY_VERDICTS == ("macro_freshness",)
    assert "macro_freshness" in nightly_verdict_names()
    assert nightly_verdicts.is_valid_name("macro_freshness")


def test_the_verdict_is_watched_with_the_cadence_of_the_schedule() -> None:
    """tools/nightly_verdicts.json names the job and the 24 hours of the daily schedule."""
    expectation = load_tool("nightly_verdicts").load_expectations()["macro_freshness"]
    assert (expectation.job, expectation.cadence_hours) == (macro_data.MACRO_DATA_REFRESH_JOB_NAME, 24)


def test_the_deployed_root_holds_the_job_and_its_daily_schedule() -> None:
    """Trigger one is the schedule, trigger two is a manual launch of the same job (AGENTS.md: two triggers)."""
    from data_engine.dagster_defs import defs

    assert macro_data.MACRO_DATA_REFRESH_JOB_NAME in {job.name for job in defs.jobs or ()}
    (schedule,) = [item for item in defs.schedules or () if item.job_name == macro_data.MACRO_DATA_REFRESH_JOB_NAME]
    assert schedule.cron_schedule == "30 20 * * *" and schedule.execution_timezone == "UTC"
    job = defs.get_job_def(macro_data.MACRO_DATA_REFRESH_JOB_NAME)
    assert [node.name for node in job.graph.nodes] == ["refresh_macro_data_op"], "a manual launch needs no config"


def test_the_schedule_stamps_its_tick_so_the_verdict_is_dated_by_it() -> None:
    tick = datetime(2026, 10, 20, 20, 30, tzinfo=UTC)

    result = macro_data.macro_data_refresh_schedule.evaluate_tick(
        dg.build_schedule_context(scheduled_execution_time=tick)
    )

    ((request),) = result.run_requests
    assert request.tags[nightly_verdicts.TICK_TAG] == tick.isoformat()
    assert request.run_key == tick.isoformat()


def test_the_schedule_runs_before_every_consumer_of_the_night() -> None:
    """The market data refresh (21:15 UTC), the backtest (21:45 UTC) and the TOPT ticks (22:15, 22:45 UTC)."""
    hour, minute = 20, 30
    assert macro_data.MACRO_DATA_REFRESH_CRON == f"{minute} {hour} * * *"
    from data_engine.lanes import backtest, capture, market_data

    consumers = [market_data.MARKET_DATA_REFRESH_CRON, backtest.BACKTEST_CRON, capture.live_topt_cron("production")]
    for cron in consumers:
        consumer_minute, consumer_hour = (int(part) for part in cron.split()[:2])
        assert (hour, minute) < (consumer_hour, consumer_minute), cron


@pytest.mark.parametrize(
    ("app_env", "expected"),
    [
        ("production", dg.DefaultScheduleStatus.RUNNING),
        ("staging", dg.DefaultScheduleStatus.RUNNING),
        ("development", dg.DefaultScheduleStatus.STOPPED),
    ],
)
def test_the_schedule_runs_where_the_tick_and_the_backtest_read_the_rate(
    monkeypatch: pytest.MonkeyPatch, app_env: str, expected: dg.DefaultScheduleStatus
) -> None:
    """The decorator reads `settings.app_env` when the module loads, so the test loads it again."""
    monkeypatch.setattr(settings, "app_env", app_env)
    try:
        reloaded = importlib.reload(macro_data)
        assert reloaded.macro_data_refresh_schedule.default_status == expected
    finally:
        monkeypatch.undo()
        importlib.reload(macro_data)


# -- the freshness judgement: red when the newest value is older than the cadence allows ---------------


def _seed_newest(connection: psycopg.Connection, ages: dict[str, int]) -> None:
    today = _today(connection)
    for series_id, age in ages.items():
        seed_observation(connection, today - timedelta(days=age), "0.04", series_id=series_id)
    connection.commit()


def test_the_cadence_of_the_registration_is_five_days() -> None:
    assert REGISTRATION.max_age_days == 5


def test_a_newest_value_at_the_cadence_is_fresh_and_one_day_older_is_red(connection) -> None:
    _seed_newest(connection, {series_id: 5 for series_id in SERIES})
    fresh = macro_data.judge_freshness(connection)
    assert fresh.ok and fresh.summary.startswith("newest observations: us_treasury_bill_3_month "), fresh.summary
    connection.execute("truncate mart.macro_series_daily, mart.macro_series, raw.fetches cascade")
    connection.commit()

    _seed_newest(connection, {series_id: 6 for series_id in SERIES})
    stale = macro_data.judge_freshness(connection)

    assert not stale.ok
    for series_id in SERIES:
        assert f"{series_id}: newest observation" in stale.summary and "is 6 days old, the limit is 5" in stale.summary


def test_one_stale_series_turns_the_verdict_red_and_names_that_series_only(connection) -> None:
    _seed_newest(connection, {SERIES[0]: 1, SERIES[1]: 1, SERIES[2]: 9})

    verdict = macro_data.judge_freshness(connection)

    assert not verdict.ok
    assert verdict.summary.startswith(f"{SERIES[2]}: newest observation") and "9 days old" in verdict.summary
    assert SERIES[0] not in verdict.summary and SERIES[1] not in verdict.summary


def test_a_series_without_any_observation_turns_the_verdict_red(connection) -> None:
    _seed_newest(connection, {SERIES[0]: 1, SERIES[1]: 1})

    verdict = macro_data.judge_freshness(connection)

    assert not verdict.ok
    assert f"{SERIES[2]}: no observation" in verdict.summary


def test_an_empty_database_is_red_for_every_series(connection) -> None:
    verdict = macro_data.judge_freshness(connection)

    assert not verdict.ok
    assert verdict.summary.count("no observation") == len(SERIES)


def test_the_newest_vintage_of_the_newest_date_decides_the_age(connection) -> None:
    """An older date stored later does not make the series look fresher."""
    today = _today(connection)
    seed_observation(connection, today - timedelta(days=8), "0.04", series_id=SERIES[0])
    for series_id in SERIES[1:]:
        seed_observation(connection, today - timedelta(days=1), "0.04", series_id=series_id)
    connection.commit()

    verdict = macro_data.judge_freshness(connection)

    assert (
        not verdict.ok
        and f"{SERIES[0]}: newest observation {(today - timedelta(days=8)).isoformat()}" in verdict.summary
    )


# -- the op, through the job -----------------------------------------------------------------------


def test_the_first_run_loads_every_year_in_the_ledger_and_records_a_green_verdict(
    database, monkeypatch, written, call_ledger
) -> None:
    asked = _treasury(monkeypatch)
    expected_years = list(range(datetime.now(UTC).year, REGISTRATION.first_year - 1, -1))

    result = _run_job()

    assert result.success
    assert asked == expected_years
    assert _ledger_years(call_ledger) == expected_years, "every request is a ledger row of the seat"
    ((row),) = written
    assert (row["check"], row["ok"]) == ("macro_freshness", True)
    assert row["summary"].startswith("newest observations: ")
    assert row["ran_at"] == datetime.fromisoformat(TICK)
    with psycopg.connect(database) as reader:
        counts = dict(
            reader.execute("select series_id, count(*) from mart.macro_series_daily group by series_id").fetchall()
        )
    assert set(counts) == set(SERIES) and len(set(counts.values())) == 1, "every series has the same days"


def test_a_replay_on_the_same_day_makes_no_request_and_records_a_green_verdict(
    database, monkeypatch, written, call_ledger
) -> None:
    """Manual replay: the job launched again on the day of its first run reuses the landed files."""
    asked = _treasury(monkeypatch)
    assert _run_job().success
    asked.clear()
    before = len(_ledger_years(call_ledger))

    assert _run_job().success

    assert asked == [] and len(_ledger_years(call_ledger)) == before, "single fetch per key: no second request"
    assert [(row["check"], row["ok"]) for row in written] == [("macro_freshness", True)] * 2


def test_a_stale_source_fails_the_run_and_records_a_red_verdict_but_keeps_what_it_loaded(
    database, monkeypatch, written
) -> None:
    _treasury(monkeypatch, newest_age_days=9)

    result = _run_job()

    assert not result.success
    ((row),) = written
    assert (row["check"], row["ok"]) == ("macro_freshness", False)
    assert row["summary"].startswith("failed: ") and "is 9 days old, the limit is 5" in row["summary"], row["summary"]
    with psycopg.connect(database) as reader:
        assert reader.execute("select count(*) from mart.macro_series_daily").fetchone()[0] > 0, "the load is kept"


def test_a_treasury_outage_records_a_red_verdict_without_the_exception_text(
    database, monkeypatch, written, call_ledger
) -> None:
    _treasury(monkeypatch, status=503)

    result = _run_job()

    assert not result.success
    ((row),) = written
    assert (row["check"], row["ok"]) == ("macro_freshness", False)
    assert row["summary"].startswith("failed: TreasuryRefreshError"), row["summary"]
    # The summary is published on the public health endpoint: the type, never the text of the error.
    for leak in ("503", "HTTP", "Treasury answered", "home.treasury.gov", "year"):
        assert leak not in row["summary"], (leak, row["summary"])
    assert {ledger.status_code for ledger in call_ledger if ledger.source == "us-treasury"} == {503}


def test_the_risk_free_series_is_loaded_by_the_lane(database, monkeypatch, written) -> None:
    _treasury(monkeypatch)
    assert _run_job().success

    with psycopg.connect(database) as reader:
        row = reader.execute(
            "select series_id, source, value from mart.macro_series_status where series_id = %s", (INSTRUMENT,)
        ).fetchone()
    assert row == (INSTRUMENT, "us-treasury", Decimal("0.0426"))
