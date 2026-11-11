"""The nightly verdict `topt_baseline` (#1664).

Production and staging, 2026-11-11: GPPE answered 16 of 20 TOPT issuers in both environments. The Twelve Data seat
used 408 of 480 calls in production on 2026-11-10 and 320 of 320 in staging on 2026-11-05. The code server held
267 MiB against a budget of 260 MiB. No verdict showed any of it: `question_coverage@topt` was green with
"reports already current", and `runtime_shape` was green after a deploy.

Three groups of tests: the rules on plain numbers, the readers over a recording connection, and the deployed job over
Postgres. A rule test that no mutation can break proves nothing, so each boundary has a test on both sides of it.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import dagster as dg
import psycopg
import pytest
from data_engine.config import settings
from data_engine.lanes import quality
from data_engine.quality import topt_baseline
from data_engine.quality.nightly_verdicts import SUMMARY_LIMIT, TICK_TAG
from data_engine.quality.topt_baseline import MIB, Row
from truealpha_contracts.environment_contract import load_environment_contract
from truealpha_runtime.testing import isolated_test_database

NOW = datetime(2026, 11, 11, 6, 25, tzinfo=UTC)
BUDGET = 260
OLD = 14 * 3600.0
YOUNG = 3 * 3600.0


def server(mib: float, age: float | None = OLD) -> Row:
    return topt_baseline.judge_server(hwm_bytes=int(mib * MIB), age_seconds=age, budget_mib=BUDGET)


# --- Q1: GPPE availability ----------------------------------------------------------------------------------


def test_q1_is_red_on_the_numbers_of_both_environments_today() -> None:
    row = topt_baseline.judge_gppe(available=16, total=20, reported=(16, 20))

    assert row == Row("Q1", "fail", "Q1 GPPE 16/20 (80%) against 95%")


@pytest.mark.parametrize(("available", "state"), [(20, "pass"), (19, "pass"), (18, "fail"), (0, "fail")])
def test_q1_passes_at_95_percent_and_not_below(available: int, state: str) -> None:
    row = topt_baseline.judge_gppe(available=available, total=20, reported=(available, 20))

    assert row.state == state


def test_q1_is_red_when_the_head_holds_no_gppe_row() -> None:
    row = topt_baseline.judge_gppe(available=0, total=0, reported=(0, 0))

    assert row.state == "fail"
    assert "holds no GPPE row" in row.text


def test_q1_is_red_when_the_stored_report_disagrees_with_the_rows() -> None:
    """The dashboard shows the report. A report that differs from the rows it summarises is a defect, also when
    the rows alone would pass."""
    row = topt_baseline.judge_gppe(available=20, total=20, reported=(16, 20))

    assert row.state == "fail"
    assert row.text == "Q1 the report says 16/20, the GPPE rows say 20/20"


def test_q1_is_red_when_no_report_names_the_head() -> None:
    row = topt_baseline.judge_gppe(available=20, total=20, reported=None)

    assert row.state == "fail"
    assert "no coverage report" in row.text


# --- R1: the share of the Twelve Data seat --------------------------------------------------------------------


def test_r1_names_the_busiest_day_of_production_today() -> None:
    row = topt_baseline.judge_quota(
        source="twelvedata", daily_calls={date(2026, 11, 9): 222, date(2026, 11, 10): 408}, share=480
    )

    assert row == Row("R1", "fail", "R1 twelvedata 408/480 on 11-10 (85%) against 70%")


def test_r1_is_at_the_limit_of_staging_on_2026_11_05() -> None:
    row = topt_baseline.judge_quota(source="twelvedata", daily_calls={date(2026, 11, 5): 320}, share=320)

    assert row.state == "fail"
    assert "(100%)" in row.text


@pytest.mark.parametrize(("peak", "state"), [(336, "pass"), (337, "fail")])
def test_r1_passes_at_70_percent_and_not_above(peak: int, state: str) -> None:
    row = topt_baseline.judge_quota(source="twelvedata", daily_calls={date(2026, 11, 10): peak}, share=480)

    assert row.state == state


@pytest.mark.parametrize("share", [None, 0])
def test_r1_is_red_without_a_share_to_judge_against(share: int | None) -> None:
    row = topt_baseline.judge_quota(source="twelvedata", daily_calls={date(2026, 11, 10): 1}, share=share)

    assert row.state == "fail"


def test_r1_with_no_call_in_the_window_passes_and_says_so() -> None:
    row = topt_baseline.judge_quota(source="twelvedata", daily_calls={}, share=480)

    assert row == Row("R1", "pass", "R1 twelvedata 0/480 in 7 days")


# --- C1: the memory of the code server ------------------------------------------------------------------------


def test_c1_is_red_on_the_reading_of_today() -> None:
    row = server(267.3)

    assert row == Row("C1", "fail", "C1 code server 267.3/260 MiB, age 14 h")


def test_c1_passes_at_the_budget_and_fails_one_byte_above_it() -> None:
    at = topt_baseline.judge_server(hwm_bytes=BUDGET * MIB, age_seconds=OLD, budget_mib=BUDGET)
    above = topt_baseline.judge_server(hwm_bytes=BUDGET * MIB + 1, age_seconds=OLD, budget_mib=BUDGET)

    assert at.state == "pass"
    assert above.state == "fail"


def test_c1_of_a_young_server_below_the_budget_is_not_measured() -> None:
    """The high-water mark only grows. A server that started 3 hours ago has not shown its size: never green."""
    row = server(255.0, YOUNG)

    assert row.state == "unmeasured"
    assert "measured at 13 h" in row.text


def test_c1_of_a_young_server_above_the_budget_is_red_at_once() -> None:
    assert server(261.0, YOUNG).state == "fail"


def test_c1_with_an_unknown_age_is_never_green() -> None:
    assert server(255.0, None).state == "unmeasured"


def test_c1_is_red_when_the_size_cannot_be_read() -> None:
    row = topt_baseline.judge_server(hwm_bytes=None, age_seconds=OLD, budget_mib=BUDGET)

    assert row.state == "fail"


# --- the verdict ----------------------------------------------------------------------------------------------


def passing() -> dict[str, Row]:
    return {
        "gppe": Row("Q1", "pass", "Q1 ok"),
        "quota": Row("R1", "pass", "R1 ok"),
        "server": Row("C1", "pass", "C1 ok"),
    }


def test_the_verdict_is_never_green_while_a_row_is_not_measured() -> None:
    """Unknown is not OK. Q3 and S1 to S3 cannot run in one environment, so three passing rows stay red."""
    judgement = topt_baseline.judge(**passing())

    assert judgement.ok is False
    assert [row.key for row in judgement.unmeasured] == ["Q3", "S1", "S2", "S3"]
    assert judgement.summary == "not measured: Q3, S1, S2, S3"


def test_the_verdict_is_green_when_every_row_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(topt_baseline, "NOT_MEASURED", {})

    judgement = topt_baseline.judge(**passing())

    assert judgement.ok is True
    assert judgement.summary == "ok: Q1 ok; R1 ok; C1 ok"


def test_one_failing_row_makes_the_verdict_red_even_with_nothing_unmeasured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(topt_baseline, "NOT_MEASURED", {})
    rows = passing() | {"quota": Row("R1", "fail", "R1 over")}

    judgement = topt_baseline.judge(**rows)

    assert judgement.ok is False
    assert judgement.summary == "failed: R1 over"


def test_the_summary_of_today_fits_the_column_and_names_every_failure() -> None:
    judgement = topt_baseline.judge(
        gppe=topt_baseline.judge_gppe(available=16, total=20, reported=(16, 20)),
        quota=topt_baseline.judge_quota(source="twelvedata", daily_calls={date(2026, 11, 10): 408}, share=480),
        server=server(267.3),
    )

    assert judgement.summary == (
        "failed: Q1 GPPE 16/20 (80%) against 95%; R1 twelvedata 408/480 on 11-10 (85%) against 70%;"
        " C1 code server 267.3/260 MiB, age 14 h. not measured: Q3, S1, S2, S3"
    )
    assert len(judgement.summary) <= SUMMARY_LIMIT


# --- the readers ----------------------------------------------------------------------------------------------


class Answer:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows


class Recording:
    """A connection that answers each `execute` in turn and keeps what it was asked."""

    def __init__(self, *answers: list[tuple[Any, ...]]) -> None:
        self.answers = list(answers)
        self.asked: list[tuple[str, tuple[Any, ...]]] = []

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> Answer:
        self.asked.append((sql, params))
        return Answer(self.answers.pop(0))


def test_the_gppe_reader_asks_for_the_run_of_the_head_and_compares_with_its_report() -> None:
    connection = Recording([(20, 16)], [(16, 20)])

    row = topt_baseline.read_gppe(connection, "capture-run:abc")

    assert row == Row("Q1", "fail", "Q1 GPPE 16/20 (80%) against 95%")
    assert [params for _sql, params in connection.asked] == [("capture-run:abc",), ("capture-run:abc",)]
    assert "mart.topt_gppe_results" in connection.asked[0][0]
    assert "mart.question_coverage_report" in connection.asked[1][0]


def test_the_gppe_reader_without_a_report_row_is_red() -> None:
    row = topt_baseline.read_gppe(Recording([(20, 20)], []), "capture-run:abc")

    assert row.state == "fail"
    assert "no coverage report" in row.text


def test_the_window_starts_at_midnight_six_days_before_today() -> None:
    assert topt_baseline.window_start(NOW) == datetime(2026, 11, 5, tzinfo=UTC)


@pytest.mark.parametrize(("environment", "share", "percent"), [("production", 480, "71%"), ("staging", 320, "106%")])
def test_the_quota_reader_judges_against_the_share_of_its_own_environment(
    environment: str, share: int, percent: str
) -> None:
    connection = Recording([(date(2026, 11, 10), 340)])

    row = topt_baseline.read_quota(connection, now=NOW, environment=environment, contract=load_environment_contract())

    assert row.text == f"R1 twelvedata 340/{share} on 11-10 ({percent}) against 70%"
    sql, params = connection.asked[0]
    assert params == ("twelvedata", datetime(2026, 11, 5, tzinfo=UTC))
    assert "staging.api_call_ledger" in sql


def test_the_age_of_the_server_is_the_uptime_minus_its_start(tmp_path: Path) -> None:
    (tmp_path / "1").mkdir()
    (tmp_path / "1" / "stat").write_text("1 (dagster api) S 0 1 1 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 5000 123\n")
    (tmp_path / "uptime").write_text("100000.50 90000.00\n")

    assert topt_baseline.server_age_seconds(tmp_path, ticks_per_second=100) == pytest.approx(99950.5)


def test_the_age_of_a_server_that_cannot_be_read_is_unknown(tmp_path: Path) -> None:
    assert topt_baseline.server_age_seconds(tmp_path, ticks_per_second=100) is None


def test_the_server_reader_reports_the_hwm_that_its_reader_gives(tmp_path: Path) -> None:
    (tmp_path / "1").mkdir()
    (tmp_path / "1" / "stat").write_text("1 (dagster api) S 0 1 1 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 5000 123\n")
    (tmp_path / "uptime").write_text("200000.00 90000.00\n")

    row = topt_baseline.read_server(budget_mib=BUDGET, proc_root=tmp_path, reader=lambda _root: 267 * MIB)

    assert row.state == "fail"
    assert row.text.startswith("C1 code server 267.0/260 MiB")


# --- registration ---------------------------------------------------------------------------------------------


def test_the_verdict_is_registered_once_and_the_schedule_runs_between_job_run_health_and_runtime_shape() -> None:
    assert quality.NIGHTLY_VERDICTS.count("topt_baseline") == 1
    assert quality.topt_baseline_schedule.job_name == quality.TOPT_BASELINE_JOB_NAME == "topt_baseline_check"
    request = quality.topt_baseline_schedule(
        dg.build_schedule_context(scheduled_execution_time=datetime(2026, 11, 12, 6, 25, tzinfo=UTC))
    )
    assert request.tags[TICK_TAG] == "2026-11-12T06:25:00+00:00"
    assert quality.topt_baseline_schedule.cron_schedule == quality.TOPT_BASELINE_CRON == "25 6 * * *"
    minute = {name: int(cron.split()[0]) for name, cron in _crons().items()}
    assert minute["job_run_health"] < minute["topt_baseline"] < minute["runtime_shape"]


def _crons() -> dict[str, str]:
    return {
        "job_run_health": quality.JOB_RUN_HEALTH_CRON,
        "topt_baseline": quality.TOPT_BASELINE_CRON,
        "runtime_shape": quality.RUNTIME_SHAPE_CRON,
    }


# --- the deployed job over Postgres ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def database_url() -> Iterator[str]:
    with isolated_test_database("topt_baseline") as url:
        yield url


def test_the_deployed_job_records_a_red_verdict_and_ends_green(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty warehouse has no TOPT head and no ledger row, and the test process is no code server. The row
    that cannot be read is red, the empty ledger is within its share, and Q3 and S1 to S3 are named."""
    monkeypatch.setattr(settings, "database_url", database_url)
    tick = datetime(2026, 11, 11, 6, 25, tzinfo=UTC)

    result = quality.topt_baseline_job.execute_in_process(tags={TICK_TAG: tick.isoformat()}, raise_on_error=False)

    assert result.success, "a red verdict is a dashboard row: the run that records it stays green"
    with psycopg.connect(database_url) as reader:
        row = reader.execute(
            "select ok, summary from mart.nightly_verdicts where check_name = 'topt_baseline' "
            "order by recorded_at desc limit 1"
        ).fetchone()
    assert row is not None, "the job records a verdict"
    ok, summary = row
    assert ok is False
    assert "Q1 no governed TOPT head" in summary
    assert "C1 the size of the code server cannot be read" in summary
    assert "R1 twelvedata" not in summary.split("not measured")[0], "an empty ledger is inside its share"
    assert summary.endswith("not measured: Q3, S1, S2, S3")
    assert len(summary) <= SUMMARY_LIMIT
