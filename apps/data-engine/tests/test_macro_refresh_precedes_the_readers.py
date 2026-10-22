"""#1255: every job that reads the risk-free rate fires shortly after the daily macro refresh.

A job that reads the rate raises `MissingRiskFreeRate` when the stored series holds no usable
observation. The series is filled by `macro_data_refresh_pipeline`. A reader that fires long
after the refresh of its night is safe. A reader that fires before the refresh of its night reads a
series that is a day old, and on the first night after a deploy it reads an empty series.

The test finds the readers in the code. It does not list them from memory. A module that calls
`risk_free_rate_at` is a reader, and the test names the job that runs it. A new reader module
turns the test red until its job is named here.

The order is measured on the clock of one night: the number of hours from the refresh to the reader,
modulo one day. A reader later than `MAX_HOURS_AFTER_THE_REFRESH` has fired before the refresh of its
own night.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from data_engine.lanes import backtest, capture, macro_data

REPO = Path(__file__).resolve().parents[3]
SOURCE = REPO / "apps" / "data-engine" / "src"
MAX_HOURS_AFTER_THE_REFRESH = 6


def _jobs_of_the_readers() -> dict[str, set[str]]:
    """The readers by module, with the Dagster jobs that run them.

    `run_topt_pipeline` reads the rate once per capture tick (TOPT, QQQ, S&P 500 and the canary).
    `run_core_strategy_backtest` reads one rate per cutoff of the nightly backtest."""
    return {
        "apps/data-engine/src/data_engine/datahub/production_topt/composition.py": {
            tick.job_name for tick in capture.TICKS
        },
        "apps/data-engine/src/data_engine/datahub/backtest.py": {backtest.BACKTEST_JOB_NAME},
    }


def _modules_that_read_the_rate() -> set[str]:
    """Every file under src that calls `risk_free_rate_at`, except the module that defines it."""
    found = set()
    for path in SOURCE.rglob("*.py"):
        if path.name == "risk_free.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call):
                name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
                if name == "risk_free_rate_at":
                    found.add(path.relative_to(REPO).as_posix())
    return found


def _minutes_of_day(expression: str) -> list[int]:
    from truealpha_runtime.testing import load_tool

    cron = load_tool("scheduler_liveness").Cron.parse(expression)
    return sorted(hour * 60 + minute for hour in cron.hours for minute in cron.minutes)


def late_readers(refresh: str, readers: dict[str, list[str]], *, max_hours: int = MAX_HOURS_AFTER_THE_REFRESH):
    """The (job, cron, hours) of each reader fire that comes more than `max_hours` after the refresh of its night."""
    (refresh_minute,) = _minutes_of_day(refresh)
    found = []
    for job, expressions in sorted(readers.items()):
        for expression in expressions:
            for minute in _minutes_of_day(expression):
                hours = ((minute - refresh_minute) % (24 * 60)) / 60
                if hours > max_hours:
                    found.append((job, expression, round(hours, 2)))
    return found


def _deployed_crons(jobs: set[str]) -> dict[str, list[str]]:
    from data_engine.dagster_defs import defs

    crons: dict[str, list[str]] = {}
    for schedule in defs.schedules or ():
        if schedule.job_name in jobs:
            expressions = (
                [schedule.cron_schedule] if isinstance(schedule.cron_schedule, str) else schedule.cron_schedule
            )
            crons.setdefault(schedule.job_name, []).extend(expressions)
    return crons


def _deployed_refresh_cron() -> str:
    from data_engine.dagster_defs import defs

    (schedule,) = [s for s in defs.schedules or () if s.job_name == macro_data.MACRO_DATA_REFRESH_JOB_NAME]
    assert isinstance(schedule.cron_schedule, str)
    return schedule.cron_schedule


def test_the_readers_found_in_the_code_are_the_readers_the_test_names() -> None:
    """Red when a new module reads the rate: name its job in `_jobs_of_the_readers`, then the order test covers it."""
    assert _modules_that_read_the_rate() == set(_jobs_of_the_readers())


def test_every_reader_job_has_a_deployed_schedule() -> None:
    jobs = {job for names in _jobs_of_the_readers().values() for job in names}
    assert {"topt_live_pipeline", "qqq_live_pipeline", "canary_live_pipeline", "nightly_backtest_pipeline"} <= jobs
    assert set(_deployed_crons(jobs)) == jobs, "a reader job without a schedule would be unordered"


def test_the_refresh_fires_before_every_reader_of_its_night() -> None:
    jobs = {job for names in _jobs_of_the_readers().values() for job in names}
    refresh = _deployed_refresh_cron()
    readers = _deployed_crons(jobs)

    assert refresh == macro_data.MACRO_DATA_REFRESH_CRON
    assert late_readers(refresh, readers) == []


def test_the_deployed_order_in_hours_after_the_refresh() -> None:
    """The measured order, so a reader of the next lane can see where it sits (UTC, production and staging)."""
    jobs = {job for names in _jobs_of_the_readers().values() for job in names}
    refresh = _deployed_refresh_cron()
    (refresh_minute,) = _minutes_of_day(refresh)
    hours = {
        job: sorted(round(((m - refresh_minute) % 1440) / 60, 2) for e in expressions for m in _minutes_of_day(e))
        for job, expressions in _deployed_crons(jobs).items()
    }
    assert hours["nightly_backtest_pipeline"] == [1.25], "the earliest reader: 21:45 UTC, 75 minutes after the refresh"
    assert min(h for values in hours.values() for h in values) == 1.25
    assert max(h for values in hours.values() for h in values) <= MAX_HOURS_AFTER_THE_REFRESH


@pytest.mark.parametrize(
    ("refresh", "expected_job"),
    [
        ("30 23 * * *", "nightly_backtest_pipeline"),
        ("0 22 * * *", "nightly_backtest_pipeline"),
        ("0 6 * * *", "topt_live_pipeline"),
        ("50 0 * * *", "nightly_backtest_pipeline"),
    ],
    ids=["after-the-backtest", "after-the-backtest-by-a-minute", "in-the-morning", "just-after-midnight"],
)
def test_a_refresh_scheduled_after_a_reader_is_a_violation(refresh: str, expected_job: str) -> None:
    """The mutation: a refresh at 23:30 UTC kept every other test green before this one."""
    jobs = {job for names in _jobs_of_the_readers().values() for job in names}

    violations = late_readers(refresh, _deployed_crons(jobs))

    assert any(job == expected_job for job, _, _ in violations), violations


def test_a_reader_scheduled_before_the_refresh_is_a_violation() -> None:
    """A reader at 20:07 UTC fires 23 hours and 37 minutes after the refresh of the evening before."""
    readers = {"some_reader": ["7 20 * * *"], "nightly_backtest_pipeline": ["45 21 * * 1-5"]}

    assert late_readers("30 20 * * *", readers) == [("some_reader", "7 20 * * *", 23.62)]
