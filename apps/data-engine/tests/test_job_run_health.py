"""The nightly verdict `job_run_health` through the deployed job (#1466).

Staging, 2026-10-21: 68 runs since 2026-10-20 12:00 UTC, 62 SUCCESS and 6 FAILURE. A SEC timeout failed the
nightly `topt_live_pipeline`, and `head_reports_pipeline` failed three times. No verdict named either failure.

`fixtures/dagster_runs/staging_2026-10-21.csv` is the real finished-run history of that staging database
(713 runs since 2026-09-20). The tests load it into real Dagster run storage (the tables that Dagster creates),
run the job `job_run_health_check` as the schedule launches it, and read the verdict row.

Two groups of tests: the rules on plain `Run` records (no database), and the deployed job over Postgres.
"""

from __future__ import annotations

import csv
import json
import re
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import dagster as dg
import psycopg
import pytest
from dagster._core.test_utils import instance_for_test
from data_engine.config import settings
from data_engine.lanes import quality
from data_engine.quality import job_run_health
from data_engine.quality.job_run_health import Run
from data_engine.quality.nightly_verdicts import TICK_TAG
from truealpha_runtime.testing import isolated_test_database

FIXTURE = Path(__file__).parent / "fixtures" / "dagster_runs" / "staging_2026-10-21.csv"
#: A tick after the last run of the fixture (2026-10-21 10:05:57 UTC).
TICK = datetime(2026, 10, 21, 10, 6, tzinfo=UTC)
#: The jobs that fail by design when their own check is red.
VERDICT_JOBS = ("output_invariants_check", "runtime_shape_check")


def fixture_rows() -> list[tuple[str, str, datetime, bool]]:
    with FIXTURE.open() as handle:
        return [
            (row["job"], row["status"], datetime.fromisoformat(row["created_at_utc"]), row["scheduled"] == "true")
            for row in csv.DictReader(handle)
        ]


# --- the rules, on plain records ---------------------------------------------------------------------------


NOW = datetime(2026, 10, 21, 6, 45, tzinfo=UTC)


def runs_of(job: str, outcomes: str, *, scheduled: bool = True, end: datetime = NOW) -> list[Run]:
    """One run per character, oldest first: `s` success, `f` failure. The newest run is an hour before `end`."""
    return [
        Run(job, outcome == "f", end - timedelta(hours=len(outcomes) - index), scheduled)
        for index, outcome in enumerate(outcomes)
    ]


def newest_first(*groups: list[Run]) -> list[Run]:
    return sorted((run for group in groups for run in group), key=lambda run: run.created_at, reverse=True)


QUIET = runs_of("quiet_job", "s" * 40, end=NOW - timedelta(hours=1))
#: Other jobs that fail one run in eight: 5 of 40. The bound of the rate rule is then 12.5%.
FLEET = runs_of("fleet_job", "sssssssf" * 5, scheduled=False, end=NOW - timedelta(hours=1))


def test_the_wilson_interval_matches_the_delivery_report() -> None:
    """One formula: the delivery report's `wilson` is the reference for the verdict's."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "common" / "runtime" / "src"))
    from truealpha_tools.delivery_report import wilson as reference

    for failures, n in [(0, 1), (1, 7), (3, 13), (2, 3), (15, 22), (5, 5), (0, 40)]:
        assert job_run_health.wilson(failures, n) == pytest.approx(reference(failures, n))


def test_two_scheduled_failures_in_a_row_are_red() -> None:
    judgement = job_run_health.judge(newest_first(FLEET, runs_of("nightly", "ssssff")), now=NOW)

    assert [job.job for job in judgement.red] == ["nightly"]
    assert judgement.red[0].red == ("last 2 scheduled runs failed",)


@pytest.mark.parametrize("others", [QUIET, FLEET], ids=["quiet-fleet", "failing-fleet"])
def test_one_scheduled_failure_is_green(others: list[Run]) -> None:
    """Also beside a fleet with no failure: one failure is an event, never a rate."""
    judgement = job_run_health.judge(newest_first(others, runs_of("nightly", "sssssf")), now=NOW)

    assert judgement.red == ()


def test_two_failures_with_a_success_between_are_green() -> None:
    judgement = job_run_health.judge(newest_first(FLEET, runs_of("nightly", "sssfsf")), now=NOW)

    assert judgement.red == ()


def test_a_weekly_schedule_shows_its_pair_across_the_thirty_days() -> None:
    """A weekly job has one scheduled run in 7 days. Its last two scheduled runs are a week apart. The newest run
    is inside the window and failed, so the pair is red."""
    weekly = runs_of("weekly", "f", end=NOW - timedelta(days=13)) + runs_of("weekly", "f", end=NOW - timedelta(days=6))
    judgement = job_run_health.judge(newest_first(FLEET, weekly), now=NOW)

    assert [job.job for job in judgement.red] == ["weekly"]
    assert judgement.red[0].red == ("last 2 scheduled runs failed",)


def test_a_later_success_of_any_trigger_clears_the_pair() -> None:
    """Two failed scheduled runs, then a manual re-run that succeeded: the job runs again."""
    weekly = (
        runs_of("weekly", "f", end=NOW - timedelta(days=13))
        + runs_of("weekly", "f", end=NOW - timedelta(days=6))
        + runs_of("weekly", "s", scheduled=False, end=NOW - timedelta(hours=2))
    )
    judgement = job_run_health.judge(newest_first(FLEET, weekly), now=NOW)

    assert judgement.red == ()


def test_a_manual_failure_after_the_pair_keeps_it_red() -> None:
    weekly = (
        runs_of("weekly", "f", end=NOW - timedelta(days=13))
        + runs_of("weekly", "f", end=NOW - timedelta(days=6))
        + runs_of("weekly", "f", scheduled=False, end=NOW - timedelta(hours=2))
    )
    judgement = job_run_health.judge(newest_first(FLEET, weekly), now=NOW)

    assert [job.job for job in judgement.red] == ["weekly"]


def test_a_removed_schedule_with_one_manual_success_in_the_window_is_green() -> None:
    """The pair is 25 days old, the schedule is gone, and a backfill succeeded this week."""
    stale = runs_of("backfilled", "ff", end=NOW - timedelta(days=25)) + runs_of(
        "backfilled", "s", scheduled=False, end=NOW - timedelta(days=2)
    )
    judgement = job_run_health.judge(newest_first(FLEET, stale), now=NOW)

    assert judgement.red == ()


def test_a_job_with_no_run_in_the_window_is_not_judged() -> None:
    """The pair of a job that ran last 20 days ago says nothing about this week: the job is not in the verdict."""
    stale = runs_of("stale", "ff", scheduled=True, end=NOW - timedelta(days=20))
    judgement = job_run_health.judge(newest_first(FLEET, stale), now=NOW)

    assert [job.job for job in judgement.jobs] == ["fleet_job"]


def test_the_input_order_does_not_change_the_verdict() -> None:
    runs = newest_first(FLEET, runs_of("nightly", "ssssff"))

    assert job_run_health.judge(reversed(runs), now=NOW) == job_run_health.judge(runs, now=NOW)


def test_the_window_of_the_admin_page_is_the_window_of_the_verdict() -> None:
    """One definition: `/admin/pipeline` counts the same days as the verdict judges."""
    source = (Path(__file__).resolve().parents[3] / "apps/app-web/src/server/admin/pipeline.ts").read_text()
    match = re.search(r"export const PIPELINE_WINDOW_DAYS = (\d+);", source)

    assert match is not None
    assert int(match.group(1)) == job_run_health.WINDOW_DAYS


def test_manual_failures_are_not_a_scheduled_pair() -> None:
    """The rule names the schedule: two failed manual launches of a busy job are not two failed nights."""
    busy = runs_of("busy", "s" * 20, scheduled=False) + runs_of("busy", "ff", scheduled=False, end=NOW)
    judgement = job_run_health.judge(newest_first(FLEET, busy), now=NOW)

    assert judgement.red == ()


def test_a_failure_rate_above_the_other_jobs_is_red() -> None:
    """3 of 8 runs failed: the lower end of the interval (14%) lies above the 0% of the other jobs."""
    judgement = job_run_health.judge(newest_first(QUIET, runs_of("flaky", "sfssfsfs", scheduled=False)), now=NOW)

    (red,) = judgement.red
    assert red.job == "flaky"
    assert red.red == ("3 of 8 runs failed, 14% to 69% against 0%",)


def test_the_bound_is_the_rate_of_the_other_jobs() -> None:
    """The same 3 of 8 is green when the other jobs fail as often: the bound moves with the data."""
    noisy = runs_of("noisy", "sf" * 20, scheduled=False, end=NOW - timedelta(hours=1))
    judgement = job_run_health.judge(newest_first(noisy, runs_of("flaky", "sfssfsfs", scheduled=False)), now=NOW)

    flaky = next(job for job in judgement.jobs if job.job == "flaky")
    assert flaky.others_rate == pytest.approx(0.5)
    assert flaky.red == ()


def test_a_job_is_not_judged_against_its_own_failures() -> None:
    """Left in, the failures of a job that fails all week would raise the bar it is judged against."""
    judgement = job_run_health.judge(newest_first(QUIET, runs_of("broken", "f" * 8, scheduled=False)), now=NOW)

    broken = next(job for job in judgement.jobs if job.job == "broken")
    assert broken.others_rate == 0.0
    assert broken.red != ()


def test_one_failure_is_never_a_rate_however_few_the_runs() -> None:
    judgement = job_run_health.judge(newest_first(QUIET, runs_of("rare", "sf", scheduled=False)), now=NOW)

    assert judgement.red == ()


def test_fewer_than_three_runs_give_no_judgement_by_rate() -> None:
    judgement = job_run_health.judge(newest_first(QUIET, runs_of("rare", "ff", scheduled=False)), now=NOW)

    assert judgement.red == ()


def test_runs_older_than_seven_days_are_not_judged_and_older_than_thirty_do_not_set_the_bound() -> None:
    old_failures = runs_of("old", "f" * 8, scheduled=True, end=NOW - timedelta(days=job_run_health.WINDOW_DAYS + 1))
    ancient = runs_of("ancient", "s" * 40, scheduled=False, end=NOW - timedelta(days=job_run_health.BASELINE_DAYS + 1))
    judgement = job_run_health.judge(newest_first(QUIET, old_failures, ancient), now=NOW)

    assert [job.job for job in judgement.jobs] == ["quiet_job"]
    # The 8 failures of `old` are inside the 30 days. The 40 successes of `ancient` are outside them.
    assert judgement.jobs[0].others_rate == 1.0


def test_the_summary_names_jobs_and_counts_and_nothing_else() -> None:
    judgement = job_run_health.judge(newest_first(FLEET, runs_of("nightly", "ssssff")), now=NOW)

    assert judgement.summary == "nightly (last 2 scheduled runs failed). 2 jobs, 7 of 46 runs failed in 7 days"
    assert len(judgement.summary) <= 300, "the column of mart.nightly_verdicts holds 300 characters"


# --- the deployed job over Postgres -----------------------------------------------------------------------


@pytest.fixture(scope="module")
def run_storage_url() -> Iterator[str]:
    """A database with the migrated schemas and the tables that Dagster creates in schema `dagster`."""
    with isolated_test_database("job_run_health") as url:
        separator = "&" if "?" in url else "?"
        storage = {"postgres": {"postgres_url": f"{url}{separator}options=-csearch_path%3Ddagster"}}
        with instance_for_test(overrides={"storage": storage}):
            pass
        yield url


def load_runs(url: str, rows: list[tuple[str, str, datetime, bool]]) -> None:
    """Replace the run history with `rows` (job, status, created_at UTC, scheduled)."""
    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute("delete from dagster.run_tags")
        connection.execute("delete from dagster.runs")
        with connection.cursor() as cursor:
            cursor.executemany(
                "insert into dagster.runs (run_id, pipeline_name, status, create_timestamp, update_timestamp) "
                "values (%s, %s, %s, %s, %s)",
                [
                    (f"fixture-{index}", job, status, created.replace(tzinfo=None), created.replace(tzinfo=None))
                    for index, (job, status, created, _scheduled) in enumerate(rows)
                ],
            )
            cursor.executemany(
                "insert into dagster.run_tags (run_id, key, value) values (%s, 'dagster/schedule_name', 'fixture')",
                [(f"fixture-{index}",) for index, row in enumerate(rows) if row[3]],
            )


def run_check(url: str, monkeypatch: pytest.MonkeyPatch, tick: datetime = TICK) -> dict[str, Any]:
    """Launch the job as the schedule does (a run tagged with its tick) and return the verdict row it wrote."""
    monkeypatch.setattr(settings, "database_url", url)
    result = quality.job_run_health_job.execute_in_process(tags={TICK_TAG: tick.isoformat()}, raise_on_error=False)
    assert result.success, "a red verdict is a dashboard row: the run that records it stays green"
    with psycopg.connect(url) as reader:
        row = reader.execute(
            "select ok, summary, dagster_run_id, ran_at from mart.nightly_verdicts "
            "where check_name = 'job_run_health' order by recorded_at desc limit 1"
        ).fetchone()
    assert row is not None, "the job records a verdict"
    return {
        "ok": row[0],
        "summary": row[1],
        "run_id": row[2],
        "ran_at": row[3],
        "output": json.loads(result.output_for_node("run_job_run_health")),
    }


def test_this_morning_on_staging_is_red_for_the_two_jobs_that_fail_by_design(
    run_storage_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real history. `output_invariants_check` failed 15 of its 22 runs in 7 days. `runtime_shape_check`
    failed 2 of its 3. The single failure of `topt_live_pipeline` and the three manual failures of
    `head_reports_pipeline` stay under the bound; `/admin/pipeline` shows them."""
    load_runs(run_storage_url, fixture_rows())

    verdict = run_check(run_storage_url, monkeypatch)

    assert verdict["ok"] is False
    assert verdict["ran_at"] == TICK
    assert [name for name in verdict["output"]["red"]] == ["output_invariants_check", "runtime_shape_check"]
    assert verdict["summary"].startswith("failed: output_invariants_check (15 of 22 runs failed")
    assert "runtime_shape_check (2 of 3 runs failed" in verdict["summary"]
    assert "topt_live_pipeline" not in verdict["summary"]
    assert "head_reports_pipeline" not in verdict["summary"]


def test_this_morning_on_staging_is_green_without_the_two_jobs_that_fail_by_design(
    run_storage_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other 13 jobs, with their real failures: 1 of 12 for `topt_live_pipeline` and 3 of 13 for
    `head_reports_pipeline` in the 22 hours before the tick. None of them is red."""
    load_runs(run_storage_url, [row for row in fixture_rows() if row[0] not in VERDICT_JOBS])

    verdict = run_check(run_storage_url, monkeypatch)

    assert verdict["ok"] is True
    assert verdict["summary"].startswith("ok: 13 jobs")
    assert verdict["output"]["red"] == []


def test_a_pair_of_scheduled_failures_injected_into_that_history_turns_it_red(
    run_storage_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    healthy = [row for row in fixture_rows() if row[0] not in VERDICT_JOBS]
    injected = [
        ("market_data_refresh_pipeline", "FAILURE", TICK - timedelta(hours=3), True),
        ("market_data_refresh_pipeline", "FAILURE", TICK - timedelta(hours=1), True),
    ]
    load_runs(run_storage_url, healthy + injected)

    verdict = run_check(run_storage_url, monkeypatch)

    assert verdict["ok"] is False
    assert verdict["output"]["red"] == ["market_data_refresh_pipeline"]
    assert "market_data_refresh_pipeline (last 2 scheduled runs failed)" in verdict["summary"]


def test_runs_that_did_not_finish_and_runs_a_person_cancelled_say_nothing_about_a_job(
    run_storage_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two scheduled failures with a cancelled run between them are two failures in a row. If the cancelled run
    counted as a success, the pair would be broken and the verdict would stay green."""
    healthy = [row for row in fixture_rows() if row[0] not in VERDICT_JOBS]
    unfinished = [
        ("model_key_health", status, TICK - timedelta(minutes=minutes), True)
        for minutes, status in ((120, "FAILURE"), (90, "CANCELED"), (60, "FAILURE"), (30, "STARTED"))
    ]
    load_runs(run_storage_url, healthy + unfinished)

    verdict = run_check(run_storage_url, monkeypatch)

    assert verdict["ok"] is False
    assert verdict["output"]["red"] == ["model_key_health"]
    assert "model_key_health (last 2 scheduled runs failed)" in verdict["summary"]


def test_a_history_with_no_finished_run_is_pending_not_green(
    run_storage_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    load_runs(run_storage_url, [])

    verdict = run_check(run_storage_url, monkeypatch)

    assert verdict["ok"] is None, "nothing to judge is pending: a green row would claim a health that was not read"
    assert "no finished run" in verdict["summary"]


# --- registration ------------------------------------------------------------------------------------------


def test_the_verdict_is_registered_once_and_the_schedule_runs_before_runtime_shape() -> None:
    assert quality.NIGHTLY_VERDICTS.count("job_run_health") == 1
    assert quality.job_run_health_schedule.job_name == quality.JOB_RUN_HEALTH_JOB_NAME == "job_run_health_check"
    request = quality.job_run_health_schedule(
        dg.build_schedule_context(scheduled_execution_time=datetime(2026, 10, 22, 6, 20, tzinfo=UTC))
    )
    assert request.tags[TICK_TAG] == "2026-10-22T06:20:00+00:00"
    assert quality.JOB_RUN_HEALTH_CRON == "20 6 * * *"
