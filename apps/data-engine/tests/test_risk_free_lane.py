"""#1255: the market-data job carries the Treasury op, and the two ops are independent.

The job runs through Dagster's own executor, the op bodies are the deployed ones, and the database
is a clone of the migrated template. Only the HTTP layer under `gateway.urlopen` is fake, so the
request, the ledger row, the object store, `raw.fetches` and the table are the real path. The price
op is replaced by a recorder, because its own tests are in `test_market_data_lane.py`.

The wall clock decides which years the op asks for. So the files here are built for the years of
today, and no assertion names a calendar year.
"""

from __future__ import annotations

import io
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from decimal import Decimal

import dagster as dg
import psycopg
import pytest
from data_engine import raw_store
from data_engine.config import settings
from data_engine.datahub.risk_free import INSTRUMENT
from data_engine.lanes import market_data
from data_engine.quality import nightly_verdicts
from data_engine.sources import gateway
from truealpha_runtime.testing import InMemoryRawObjectStore, isolated_test_database

TICK = "2026-10-14T21:15:00+00:00"
THIS_YEAR = datetime.now(UTC).year
YEARS = [THIS_YEAR, THIS_YEAR - 1, THIS_YEAR - 2, THIS_YEAR - 3]


def _file(year: int) -> bytes:
    """A real-shaped yearly file: the 2026 header, two business days, 3 Mo of 4.26 and 4.25 percent."""
    return (
        "\n".join(
            [
                'Date,"1 Mo","1.5 Month","2 Mo","3 Mo","4 Mo"',
                f"01/05/{year},4.10,4.15,4.15,4.26,4.32",
                f"01/02/{year},4.13,4.13,4.13,4.25,4.29",
            ]
        )
    ).encode()


@pytest.fixture
def database(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    with isolated_test_database("riskfreelane") as url:
        monkeypatch.setattr(settings, "database_url", url)
        monkeypatch.setattr(raw_store, "object_store", lambda: store)
        yield url


store = InMemoryRawObjectStore()


class _Response:
    def __init__(self, body: bytes) -> None:
        self._body, self.status = body, 200

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _treasury(monkeypatch: pytest.MonkeyPatch, answer: Callable[[int], int | bytes]) -> list[int]:
    """The HTTP layer under `gateway.urlopen`. `answer(year)` is the file, or an HTTP error status."""
    asked: list[int] = []

    def urlopen(request: urllib.request.Request, timeout: float) -> _Response:
        year = int(request.full_url.split("/daily-treasury-rates.csv/")[1].split("/")[0])
        asked.append(year)
        reply = answer(year)
        if isinstance(reply, int):
            raise urllib.error.HTTPError(request.full_url, reply, "error", {}, io.BytesIO(b"error"))  # type: ignore[arg-type]
        return _Response(reply)

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


class _RecordingGate(gateway.CapacityGate):
    """The deployed gate, plus a list of what it admitted."""

    admitted: list[str]

    def admit(self, source: str) -> None:
        self.admitted.append(source)
        super().admit(source)


@pytest.fixture(autouse=True)
def gate(monkeypatch: pytest.MonkeyPatch) -> _RecordingGate:
    """`capacity_scope()` binds this gate. Its pace is the declared pace, on a fake clock."""
    clock = _Clock()
    bound = _RecordingGate(clock=clock, sleep=clock.sleep, recent_calls=None, spent_since=lambda _s, _t: 0)
    bound.admitted = []
    real_scope = gateway.capacity_scope
    monkeypatch.setattr(gateway, "capacity_scope", lambda given=None: real_scope(given or bound))
    return bound


@pytest.fixture
def price_op(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The price op, reduced to a recorder: it ran, or it did not."""
    ran: list[str] = []
    monkeypatch.setattr(market_data, "_refresh_market_data", lambda *_a, **_k: ran.append("prices") or {})
    monkeypatch.setattr(
        market_data, "judge_bar_freshness", lambda *_a, **_k: market_data.BarFreshness(ok=True, summary="newest bars")
    )
    monkeypatch.setattr(nightly_verdicts, "record", lambda *_a, **_k: None)
    return ran


def _run_job() -> dg.ExecuteInProcessResult:
    return market_data.market_data_refresh_pipeline_job.execute_in_process(
        tags={nightly_verdicts.TICK_TAG: TICK}, raise_on_error=False
    )


def _stored(url: str) -> list[tuple[date, Decimal]]:
    with psycopg.connect(url) as reader:
        return reader.execute(
            "select observation_date, annualized_rate from staging.risk_free_rates_daily "
            "where instrument = %s order by observation_date",
            (INSTRUMENT,),
        ).fetchall()


def _ledger_years(call_ledger: gateway.MemoryLedger) -> list[str]:
    return [
        row.request_uri.split("/daily-treasury-rates.csv/")[1].split("/")[0]
        for row in call_ledger
        if row.source == "us-treasury"
    ]


# -- the wiring --------------------------------------------------------------------------------


def test_the_job_holds_both_ops_and_neither_depends_on_the_other() -> None:
    graph = market_data.market_data_refresh_pipeline_job.graph
    names = {node.name for node in graph.nodes}

    assert names == {"refresh_market_data_op", "refresh_risk_free_rates_op"}
    inputs = {invocation.name: dependencies for invocation, dependencies in graph.dependencies.items()}
    assert inputs == {"refresh_market_data_op": {}, "refresh_risk_free_rates_op": {}}, (
        "no op takes an input from the other: a failure of one cannot skip the other"
    )


def test_the_daily_schedule_launches_the_job_that_holds_the_treasury_op() -> None:
    """The two triggers of the series: the schedule below, and a manual launch of the same job."""
    assert market_data.market_data_refresh_schedule.job_name == market_data.MARKET_DATA_REFRESH_JOB_NAME
    assert market_data.MARKET_DATA_REFRESH_CRON == "15 21 * * 1-5"
    assert market_data.MARKET_DATA_REFRESH_JOB_NAME in {job.name for job in market_data.defs.jobs or ()}
    assert market_data.refresh_risk_free_rates_op.name == "refresh_risk_free_rates_op"


# -- the run -----------------------------------------------------------------------------------


def test_the_first_run_backfills_four_years_and_every_request_is_in_the_ledger(
    database, monkeypatch, price_op, call_ledger, gate
) -> None:
    asked = _treasury(monkeypatch, _file)

    result = _run_job()

    assert result.success
    assert price_op == ["prices"]
    assert asked == YEARS == [int(year) for year in _ledger_years(call_ledger)]
    assert gate.admitted == ["us-treasury"] * 4, "the gate admitted each request before it was made"
    assert [row.ok for row in call_ledger if row.source == "us-treasury"] == [True] * 4
    assert len(_stored(database)) == 8, "two business days in each of four files"
    assert _stored(database)[-1] == (date(THIS_YEAR, 1, 5), Decimal("0.0426"))
    with psycopg.connect(database) as reader:
        assert reader.execute(
            "select count(*) from raw.fetches where source = 'us-treasury' and content_type = 'text/csv'"
        ).fetchone() == (4,)


def test_the_op_reports_what_it_appended(database, monkeypatch, price_op) -> None:
    _treasury(monkeypatch, _file)

    result = _run_job()

    event = next(
        item for item in result.events_for_node("refresh_risk_free_rates_op") if item.event_type_value == "STEP_OUTPUT"
    )
    metadata = {key: value.value for key, value in event.event_specific_data.metadata.items()}  # type: ignore[union-attr]
    assert metadata == {"years": YEARS, "observations": 8, "appended": 8}


def test_a_second_run_asks_for_one_year_and_inserts_nothing(database, monkeypatch, price_op, call_ledger) -> None:
    asked = _treasury(monkeypatch, _file)
    assert _run_job().success
    before = _stored(database)
    asked.clear()
    call_ledger.clear()

    assert _run_job().success

    assert asked == [THIS_YEAR]
    assert _ledger_years(call_ledger) == [str(THIS_YEAR)]
    assert _stored(database) == before, "no duplicate row"
    with psycopg.connect(database) as reader:
        assert reader.execute("select count(*) from raw.fetches where source = 'us-treasury'").fetchone() == (4,)


def test_a_spent_daily_budget_stops_the_op_before_any_request(database, monkeypatch, price_op, gate) -> None:
    asked = _treasury(monkeypatch, _file)
    gate.spent_since = lambda _source, _since: 10**6

    result = _run_job()

    assert not result.success
    assert asked == [], "the Treasury was never asked"
    assert price_op == ["prices"]
    assert _stored(database) == []


# -- independence ----------------------------------------------------------------------------------


def test_a_treasury_failure_fails_the_run_and_the_price_refresh_still_ran(database, monkeypatch, price_op) -> None:
    _treasury(monkeypatch, lambda _year: 503)

    result = _run_job()

    assert not result.success
    assert price_op == ["prices"], "the Treasury op failed and the price op still ran"
    assert _stored(database) == []
    failure = next(item for item in result.all_events if item.is_step_failure)
    assert failure.step_key == "refresh_risk_free_rates_op"
    assert failure.event_specific_data.error.cause.cls_name == "TreasuryRefreshError"  # type: ignore[union-attr]


def test_a_price_failure_does_not_stop_the_treasury_op(database, monkeypatch, price_op) -> None:
    def crash(*_a: object, **_k: object) -> None:
        raise RuntimeError("Twelve Data is down")

    monkeypatch.setattr(market_data, "_refresh_market_data", crash)
    _treasury(monkeypatch, _file)

    result = _run_job()

    assert not result.success
    assert len(_stored(database)) == 8, "the price op failed and the Treasury op still ran"


def test_one_bad_year_costs_that_year_only(database, monkeypatch, price_op) -> None:
    _treasury(monkeypatch, lambda year: b"<html>maintenance</html>" if year == THIS_YEAR - 2 else _file(year))

    result = _run_job()

    assert not result.success
    assert price_op == ["prices"]
    assert {day.year for day, _rate in _stored(database)} == {THIS_YEAR, THIS_YEAR - 1, THIS_YEAR - 3}
