"""The nightly verdict `topt_baseline` (#1664).

Production and staging, 2026-11-11: GPPE answered 16 of 20 TOPT issuers in both environments. The Twelve Data seat
used 408 of 480 calls in production on 2026-11-10 and 320 of 320 in staging on 2026-11-05. The code server held
267 MiB against a budget of 260 MiB. No verdict showed any of it: `question_coverage@topt` was green with
"reports already current", and `runtime_shape` was green after a deploy.

Three groups of tests: the rules on plain numbers, the readers over a recording connection, and the deployed job over
Postgres. A rule test that no mutation can break proves nothing, so each boundary has a test on both sides of it.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import re
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import dagster as dg
import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub import market_prices
from data_engine.lanes import quality
from data_engine.quality import topt_baseline
from data_engine.quality.nightly_verdicts import SUMMARY_LIMIT, TICK_TAG
from data_engine.quality.topt_baseline import MIB, Row
from truealpha_contracts.common import canonical_sha256
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

    assert row == Row("Q1", "fail", "Q1 GPPE 16/20 (80.0%) against 95.0%")


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

    assert row == Row("R1", "fail", "R1 twelvedata 408/480 on 11-10 (85.0%) against 70.0%")


def test_r1_is_at_the_limit_of_staging_on_2026_11_05() -> None:
    row = topt_baseline.judge_quota(source="twelvedata", daily_calls={date(2026, 11, 5): 320}, share=320)

    assert row.state == "fail"
    assert "(100.0%)" in row.text


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

    assert row == Row("C1", "fail", "C1 code server 267.3/260 MiB, age 14.0 h")


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
        "schema": Row("S", "pass", "S ok"),
    }


def test_the_verdict_is_never_green_while_a_row_is_not_measured() -> None:
    """Unknown is not OK. Q3 compares two environments and cannot run in one, so four passing rows stay red."""
    judgement = topt_baseline.judge(**passing())

    assert judgement.ok is False
    assert [row.key for row in judgement.unmeasured] == ["Q3"]
    assert judgement.summary == "not measured: Q3"


def test_the_verdict_is_green_when_every_row_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(topt_baseline, "NOT_MEASURED", {})

    judgement = topt_baseline.judge(**passing())

    assert judgement.ok is True
    assert judgement.summary == "ok: Q1 ok; R1 ok; C1 ok; S ok"


def test_one_failing_row_makes_the_verdict_red_even_with_nothing_unmeasured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(topt_baseline, "NOT_MEASURED", {})
    rows = passing() | {"quota": Row("R1", "fail", "R1 over")}

    judgement = topt_baseline.judge(**rows)

    assert judgement.ok is False
    assert judgement.summary == "R1 over"


def test_the_summary_of_today_fits_the_column_and_names_every_failure() -> None:
    judgement = topt_baseline.judge(
        gppe=topt_baseline.judge_gppe(available=16, total=20, reported=(16, 20)),
        quota=topt_baseline.judge_quota(source="twelvedata", daily_calls={date(2026, 11, 10): 408}, share=480),
        server=server(267.3),
        schema=topt_baseline.judge_schema(differences=790),
    )

    assert judgement.summary == (
        "Q1 GPPE 16/20 (80.0%) against 95.0%; R1 twelvedata 408/480 on 11-10 (85.0%) against 70.0%;"
        " C1 code server 267.3/260 MiB, age 14.0 h; S schema differs from the chain in 790 places."
        " not measured: Q3"
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

    assert row == Row("Q1", "fail", "Q1 GPPE 16/20 (80.0%) against 95.0%")
    assert [params for _sql, params in connection.asked] == [("capture-run:abc",), ("capture-run:abc",)]
    assert "mart.topt_gppe_results" in connection.asked[0][0]
    assert "mart.question_coverage_report" in connection.asked[1][0]


def test_the_gppe_reader_without_a_report_row_is_red() -> None:
    row = topt_baseline.read_gppe(Recording([(20, 20)], []), "capture-run:abc")

    assert row.state == "fail"
    assert "no coverage report" in row.text


def test_the_window_starts_at_midnight_six_days_before_today() -> None:
    assert topt_baseline.window_start(NOW) == datetime(2026, 11, 5, tzinfo=UTC)


@pytest.mark.parametrize(
    ("environment", "share", "percent"), [("production", 480, "70.9%"), ("staging", 320, "106.3%")]
)
def test_the_quota_reader_judges_against_the_share_of_its_own_environment(
    environment: str, share: int, percent: str
) -> None:
    connection = Recording([(date(2026, 11, 10), 340)])

    row = topt_baseline.read_quota(connection, now=NOW, environment=environment, contract=load_environment_contract())

    assert row.text == f"R1 twelvedata 340/{share} on 11-10 ({percent}) against 70.0%"
    sql, params = connection.asked[0]
    assert params == ("twelvedata", datetime(2026, 11, 5, tzinfo=UTC), NOW)
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
    that cannot be read is red, the empty ledger is within its share, and Q3 is named."""
    monkeypatch.setattr(settings, "database_url", database_url)
    monkeypatch.setattr(topt_baseline, "IMAGE_DB_DIR", Path(__file__).resolve().parents[3] / "db")
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
    assert summary.startswith("failed: Q1 no served TOPT head"), "verdict() adds the prefix once"
    assert "failed: failed:" not in summary
    assert "C1 the size of the code server cannot be read" in summary
    assert "R1 twelvedata" not in summary.split("not measured")[0], "an empty ledger is inside its share"
    assert summary.endswith("not measured: Q3")
    assert "S schema" not in summary, "a database that ran the chain has no drift: the row S is green"
    assert json.loads(result.output_for_node("run_topt_baseline"))["failed"] == ["Q1", "C1"]
    assert len(summary) <= SUMMARY_LIMIT


def test_the_deployed_job_shows_the_drift_of_its_own_database_in_the_stored_summary(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "database_url", database_url)
    monkeypatch.setattr(topt_baseline, "IMAGE_DB_DIR", Path(__file__).resolve().parents[3] / "db")
    with psycopg.connect(database_url, autocommit=True) as connection:
        connection.execute("create table staging.drift_probe_job (id bigint primary key)")
    try:
        result = quality.topt_baseline_job.execute_in_process(
            tags={TICK_TAG: datetime(2026, 11, 11, 6, 26, tzinfo=UTC).isoformat()}, raise_on_error=False
        )
    finally:
        with psycopg.connect(database_url, autocommit=True) as connection:
            connection.execute("drop table staging.drift_probe_job")

    assert result.success
    with psycopg.connect(database_url) as reader:
        ok, summary = reader.execute(
            "select ok, summary from mart.nightly_verdicts where check_name = 'topt_baseline' "
            "order by recorded_at desc limit 1"
        ).fetchone()
    assert ok is False
    assert re.search(r"S schema differs from the chain in \d+ places", summary)
    assert "S" in json.loads(result.output_for_node("run_topt_baseline"))["failed"]


# --- the head, the rounding, the boundaries the first review found ---------------------------------------------


def test_a_withheld_or_stale_head_is_red_before_any_count() -> None:
    assert topt_baseline.judge_head(run_id=None, freshness="stale", availability="unavailable") == Row(
        "Q1", "fail", "Q1 the served TOPT head is withheld (unavailable)"
    )
    assert topt_baseline.judge_head(run_id="capture-run:a", freshness="stale", availability="available") == Row(
        "Q1", "fail", "Q1 the served TOPT head is stale"
    )
    assert topt_baseline.judge_head(run_id="capture-run:a", freshness="fresh", availability="available") is None


@pytest.mark.parametrize(
    ("peak", "text"),
    [(336, "70.0%"), (337, "70.3%")],
)
def test_a_percentage_is_rounded_in_the_direction_that_keeps_the_text_true(peak: int, text: str) -> None:
    """337 of 480 is 70.2%. A text that says 70.0% against 70.0% for a red row contradicts itself."""
    row = topt_baseline.judge_quota(source="twelvedata", daily_calls={date(2026, 11, 10): peak}, share=480)

    assert f"({text})" in row.text
    assert row.state == ("pass" if peak == 336 else "fail")


def test_q1_text_of_a_larger_head_does_not_round_a_failure_up_to_the_limit() -> None:
    row = topt_baseline.judge_gppe(available=474, total=500, reported=(474, 500))

    assert row.state == "fail"
    assert row.text == "Q1 GPPE 474/500 (94.8%) against 95.0%"


def test_c1_text_just_above_the_budget_does_not_round_down_to_it() -> None:
    row = topt_baseline.judge_server(hwm_bytes=int(260.04 * MIB), age_seconds=OLD, budget_mib=BUDGET)

    assert row.state == "fail"
    assert row.text.startswith("C1 code server 260.1/260 MiB")


@pytest.mark.parametrize(("hours", "state"), [(12.99, "unmeasured"), (13.0, "pass"), (13.01, "pass")])
def test_c1_is_measured_from_13_hours_after_the_start_of_the_server(hours: float, state: str) -> None:
    row = server(255.0, hours * 3600)

    assert row.state == state


def test_the_constant_of_13_hours_is_the_method_of_the_contract() -> None:
    method = load_environment_contract().code_server_capacity.measured.method

    assert f"{topt_baseline.MATURE_SERVER_HOURS} hours after its start" in method


def test_the_quota_seat_is_the_seat_that_the_ledger_writer_uses_and_the_contract_names() -> None:
    assert topt_baseline.QUOTA_SOURCE == market_prices.TWELVE_DATA_SEAT
    assert topt_baseline.QUOTA_SOURCE in load_environment_contract().vendor_seats


def test_a_share_of_zero_is_inside_the_share_with_no_call_and_red_with_one() -> None:
    """Preview calls no Twelve Data endpoint: its share is 0. An empty ledger is as it should be."""
    quiet = topt_baseline.judge_quota(source="twelvedata", daily_calls={}, share=0)
    called = topt_baseline.judge_quota(source="twelvedata", daily_calls={date(2026, 11, 10): 1}, share=0)

    assert quiet.state == "pass"
    assert called.state == "fail"
    assert called.text == "R1 twelvedata 1 calls in 7 days against a share of 0"


def test_the_quota_reader_of_preview_passes_on_an_empty_ledger() -> None:
    row = topt_baseline.read_quota(Recording([]), now=NOW, environment="preview", contract=load_environment_contract())

    assert row.state == "pass"


def test_the_quota_query_has_an_upper_bound_at_the_tick() -> None:
    connection = Recording([])

    topt_baseline.read_quota(connection, now=NOW, environment="production", contract=load_environment_contract())

    sql, params = connection.asked[0]
    assert "called_at <= %s" in sql
    assert params[2] == NOW


# --- the readers over Postgres: the head, the rows, the report and the ledger -----------------------------------

UNIVERSE = "universe:topt-us-2026-03-31"
SEQUENCES = itertools.count()


@pytest.fixture(scope="module")
def seeded_url() -> Iterator[str]:
    with isolated_test_database("topt_baseline_seeded") as url:
        yield url


@pytest.fixture(scope="module")
def stale_url() -> Iterator[str]:
    with isolated_test_database("topt_baseline_stale") as url:
        yield url


def seed_head(connection: psycopg.Connection[Any], run_id: str, *, age_hours: float = 0.0) -> None:
    """Advance the pointer of the TOPT universe to `run_id`. The foreign key to the evidence graph is off for the
    insert: the test reads the served head and nothing upstream of it."""
    sha = canonical_sha256({"probe": run_id})
    sequence = next(SEQUENCES)
    connection.execute("set session_replication_role = replica")
    try:
        connection.execute(
            """
            insert into mart.current_pointer (pointer_id, content_sha256, environment, universe_id, universe_version,
                                              factor_id, target_run_id, sequence, previous_run_id, advanced_at)
            values (%s, %s, (select environment from mart.environment_identity), %s, 'topt-candidate-2026-03-31-v1',
                    'gross_profit_per_employee', %s, %s, %s, clock_timestamp() - %s * interval '1 hour')
            """,
            (
                f"current-pointer:{sha}",
                sha,
                UNIVERSE,
                run_id,
                sequence,
                None if sequence == 0 else "capture-run:before",
                age_hours,
            ),
        )
    finally:
        connection.execute("set session_replication_role = origin")


def seed_gppe(
    connection: psycopg.Connection[Any], run_id: str, rows: list[tuple[str, str, str | None, int | None]]
) -> None:
    """One GPPE row per tuple: issuer, availability, availability_status, gppe. The rows satisfy the checks of the
    table: an available row holds its values and no reason, an unavailable row holds a reason and no value. The
    foreign key to the invocation is off for the insert: the test reads the result table and nothing upstream."""
    connection.execute("set session_replication_role = replica")
    try:
        for issuer, availability, status, gppe in rows:
            sha = hashlib.sha256(f"{run_id}:{issuer}".encode()).hexdigest()
            available = availability == "available"
            connection.execute(
                """
                insert into mart.topt_gppe_results (result_id, content_sha256, invocation_id, snapshot_id, run_id,
                    release_manifest_id, universe_id, universe_version, universe_sha256, cutoff, issuer_id,
                    instrument_id, listing_id, operating_branch, operating_metric, availability, confidence,
                    freshness, reason_codes, input_observation_ids, gppe_definition_id, gppe_definition_sha256,
                    payload, operating_efficiency, capital_adjusted_gross_profit, gppe, availability_status)
                values (%s, %s, %s, 'snapshot:x', %s, 'release:x', %s, 'v1', %s, now(), %s,
                        'instrument:x', 'listing:x', 'non_financial', 'capital_adjusted_gppe', %s, 0.5, 'fresh',
                        %s, %s, 'definition:x', %s, '{}', %s, %s, %s, %s)
                """,
                (
                    f"topt-gppe-result:{sha}",
                    sha,
                    f"invocation:{run_id}",
                    run_id,
                    UNIVERSE,
                    sha,
                    issuer,
                    availability,
                    [] if available else ["missing_gross_profit"],
                    ["o1", "o2", "o3", "o4"],
                    sha,
                    gppe,
                    gppe,
                    gppe,
                    status,
                ),
            )
    finally:
        connection.execute("set session_replication_role = origin")


def seed_report(connection: psycopg.Connection[Any], run_id: str, answered: int, denominator: int) -> None:
    payload = {"questions": {"q1": {"answered": answered, "denominator": denominator}}}
    sha = canonical_sha256(payload | {"run_id": run_id})
    connection.execute(
        """
        insert into mart.question_coverage_report (report_id, content_sha256, universe_id, run_id, cutoff,
                                                   requirements_sha256, payload)
        values (%s, %s, %s, %s, now(), %s, %s)
        """,
        (f"question-coverage-report:{sha}", sha, UNIVERSE, run_id, sha, json.dumps(payload)),
    )


def issuers(available: int, total: int) -> list[tuple[str, str, str | None, int | None]]:
    return [
        (
            f"issuer:{index:02d}",
            "available" if index < available else "unavailable",
            None,
            100 if index < available else None,
        )
        for index in range(total)
    ]


def test_q1_of_the_served_head_matches_the_numbers_of_today(seeded_url: str) -> None:
    """16 of 20 issuers have a GPPE and the report says so: the row is red with the numbers of 2026-11-11."""
    with psycopg.connect(seeded_url, autocommit=True) as connection:
        seed_head(connection, "capture-run:today")
        seed_gppe(connection, "capture-run:today", issuers(16, 20))
        seed_report(connection, "capture-run:today", 16, 20)

        row = topt_baseline.read_q1(connection)

    assert row == Row("Q1", "fail", "Q1 GPPE 16/20 (80.0%) against 95.0%")


def test_q1_passes_when_every_issuer_has_a_value_and_the_report_agrees(seeded_url: str) -> None:
    with psycopg.connect(seeded_url, autocommit=True) as connection:
        seed_head(connection, "capture-run:full")
        seed_gppe(connection, "capture-run:full", issuers(20, 20))
        seed_report(connection, "capture-run:full", 20, 20)

        row = topt_baseline.read_q1(connection)

    assert row == Row("Q1", "pass", "Q1 GPPE 20/20 (100.0%) against 95.0%")


def test_q1_counts_an_issuer_as_the_report_does_not_as_the_availability_column_does(seeded_url: str) -> None:
    """Two rows keep `availability = 'available'` but carry the status `stale`. The report counts 18 of 20. The
    numerator must be 18, so that the row names the shortfall and does not report a mismatch."""
    rows = issuers(20, 20)
    rows[0] = (rows[0][0], "available", "stale", 100)
    rows[1] = (rows[1][0], "available", "stale", 100)
    with psycopg.connect(seeded_url, autocommit=True) as connection:
        seed_head(connection, "capture-run:status")
        seed_gppe(connection, "capture-run:status", rows)
        seed_report(connection, "capture-run:status", 18, 20)

        row = topt_baseline.read_q1(connection)

    assert row == Row("Q1", "fail", "Q1 GPPE 18/20 (90.0%) against 95.0%")


def test_q1_is_red_when_the_report_of_the_head_says_other_numbers(seeded_url: str) -> None:
    with psycopg.connect(seeded_url, autocommit=True) as connection:
        seed_head(connection, "capture-run:differs")
        seed_gppe(connection, "capture-run:differs", issuers(20, 20))
        seed_report(connection, "capture-run:differs", 16, 20)

        row = topt_baseline.read_q1(connection)

    assert row == Row("Q1", "fail", "Q1 the report says 16/20, the GPPE rows say 20/20")


def test_q1_of_a_head_with_no_stored_report_is_red(seeded_url: str) -> None:
    with psycopg.connect(seeded_url, autocommit=True) as connection:
        seed_head(connection, "capture-run:noreport")
        seed_gppe(connection, "capture-run:noreport", issuers(20, 20))

        row = topt_baseline.read_q1(connection)

    assert row.state == "fail"
    assert "no coverage report" in row.text


def test_q1_of_a_stale_head_is_red_although_its_rows_are_complete(stale_url: str) -> None:
    with psycopg.connect(stale_url, autocommit=True) as connection:
        seed_head(connection, "capture-run:old", age_hours=100)
        seed_gppe(connection, "capture-run:old", issuers(20, 20))
        seed_report(connection, "capture-run:old", 20, 20)

        row = topt_baseline.read_q1(connection)

    assert row == Row("Q1", "fail", "Q1 the served TOPT head is stale")


def test_the_quota_reader_counts_utc_days_inside_the_window_and_nothing_outside_it(seeded_url: str) -> None:
    """340 calls on 11-10. A call on 11-04 (before the window) and a call on 11-12 (after the tick) do not count."""
    with psycopg.connect(seeded_url, autocommit=True) as connection:
        for day, count in (
            (datetime(2026, 11, 10, 12, tzinfo=UTC), 340),
            (datetime(2026, 11, 4, 23, 59, tzinfo=UTC), 900),
            (datetime(2026, 11, 12, 1, tzinfo=UTC), 900),
        ):
            connection.execute(
                "insert into staging.api_call_ledger (source, endpoint, called_at, caller, ok) "
                "select %s, '/time_series', %s, 'test', true from generate_series(1, %s)",
                ("twelvedata", day, count),
            )
        connection.execute(
            "insert into staging.api_call_ledger (source, endpoint, called_at, caller, ok) "
            "select 'yahoo', '/chart', %s, 'test', true from generate_series(1, 900)",
            (datetime(2026, 11, 10, 12, tzinfo=UTC),),
        )

        row = topt_baseline.read_quota(
            connection, now=NOW, environment="production", contract=load_environment_contract()
        )

    assert row == Row("R1", "fail", "R1 twelvedata 340/480 on 11-10 (70.9%) against 70.0%")


# --- S: the schema against the chain ---------------------------------------------------------------------------


def test_s_is_green_only_at_zero_differences() -> None:
    assert topt_baseline.judge_schema(differences=0) == Row("S", "pass", "S schema matches the chain")
    assert topt_baseline.judge_schema(differences=1) == Row("S", "fail", "S schema differs from the chain in 1 places")


def test_s_shows_the_numbers_of_staging_and_production_of_2026_11_11() -> None:
    assert topt_baseline.judge_schema(differences=790).text == "S schema differs from the chain in 790 places"
    assert topt_baseline.judge_schema(differences=5).text == "S schema differs from the chain in 5 places"


def test_s_that_could_not_be_compared_is_red_and_names_the_failure_kind_only() -> None:
    row = topt_baseline.judge_schema(differences=None, failure="OperationalError")

    assert row == Row("S", "fail", "S the schema could not be compared (OperationalError)")


def test_s_masks_the_password_variable_for_the_reference_build_and_puts_it_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`db/roles.sql` sets the app login password when the variable is set. The check changes no credential."""
    monkeypatch.setenv("APP_SERVICE_DB_PASSWORD", "never-applied")
    seen: dict[str, str | None] = {}

    def build(*_args: object, **_kwargs: object):
        seen["during"] = os.environ.get("APP_SERVICE_DB_PASSWORD")
        raise RuntimeError("stop after the look")

    from truealpha_tools import schema_drift

    monkeypatch.setattr(schema_drift, "reference_database", build)

    row = topt_baseline.read_schema("postgresql://unused")

    assert seen["during"] is None
    assert os.environ["APP_SERVICE_DB_PASSWORD"] == "never-applied"
    assert row == Row("S", "fail", "S the schema could not be compared (RuntimeError)")


def test_s_passes_the_chain_directory_of_the_image_and_states_that_the_reference_sits_on_the_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def build(url: str, **kwargs: object):
        seen["url"], seen["kwargs"] = url, kwargs
        raise RuntimeError("stop after the look")

    from truealpha_tools import schema_drift

    monkeypatch.setattr(schema_drift, "reference_database", build)

    topt_baseline.read_schema("postgresql://unused")

    assert seen["url"] == "postgresql://unused"
    assert seen["kwargs"] == {"db_dir": Path("/app/db"), "allow_remote": True}


def test_s_of_a_database_that_ran_the_chain_is_green_and_leaves_no_reference_behind(database_url: str) -> None:
    repo_db = Path(__file__).resolve().parents[3] / "db"

    row = topt_baseline.read_schema(database_url, db_dir=repo_db)

    assert row == Row("S", "pass", "S schema matches the chain")
    with psycopg.connect(database_url, autocommit=True) as admin:
        left = admin.execute(
            "select count(*) from pg_database where datname like 'truealpha_chain_reference_%'"
        ).fetchone()
    assert left == (0,), "the reference database is dropped at the end"


def test_s_counts_the_places_of_a_drifted_database(database_url: str) -> None:
    """A relation that the chain never declared: one extra relation, its columns, its constraints."""
    repo_db = Path(__file__).resolve().parents[3] / "db"
    with psycopg.connect(database_url, autocommit=True) as connection:
        connection.execute("create table staging.drift_probe (id bigint primary key, note text)")
    try:
        row = topt_baseline.read_schema(database_url, db_dir=repo_db)
    finally:
        with psycopg.connect(database_url, autocommit=True) as connection:
            connection.execute("drop table staging.drift_probe")

    assert row.state == "fail"
    match = re.fullmatch(r"S schema differs from the chain in (\d+) places", row.text)
    assert match is not None and int(match.group(1)) >= 3


def test_s_of_a_missing_chain_directory_is_red(database_url: str, tmp_path: Path) -> None:
    row = topt_baseline.read_schema(database_url, db_dir=tmp_path)

    assert row.state == "fail"
    assert row.text.startswith("S the schema could not be compared (")
