"""The queries of `/admin/pipeline` run against real Dagster run storage and the real run-plan view (#1466).

The page reads `dagster.runs` once, grouped by job, then the event log of the newest failed run of each job,
and the start of the current stretch of the release build. The loader tests
(`apps/app-web/tests/admin-pipeline.test.ts`) use a fake client, which returns rows whatever the SQL says. They
also assert that the loader sends the exported SQL text. Here that text is read from the TypeScript source and
run on the tables that Dagster creates, over runs that Dagster itself wrote:

* a run whose op raised: Dagster writes `STEP_FAILURE`, then `PIPELINE_FAILURE`;
* a run that the run monitor killed: Dagster writes `PIPELINE_FAILURE` with a message and no step failure.
  `DagsterEventType.RUN_FAILURE` has the value `PIPELINE_FAILURE`; a filter on the text `RUN_FAILURE` matched no
  row, and a killed run printed "no error text in the event log".
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import dagster as dg
import psycopg
import pytest
from dagster._core.test_utils import instance_for_test
from truealpha_runtime.testing import isolated_test_database

ADMIN = Path(__file__).resolve().parents[3] / "apps" / "app-web" / "src" / "server" / "admin"
PIPELINE = ADMIN / "pipeline.ts"
OPS = ADMIN / "ops.ts"
KILLED_MESSAGE = "Run timed out due to taking longer than 10800 seconds to complete."


def loader_sql(path: Path, name: str) -> str:
    """The text of `export const <name> = `...`;` in a loader, with its `$n` parameters as named ones."""
    match = re.search(rf"export const {name} = `(.*?)`;", path.read_text(), re.S)
    assert match is not None, f"{path} no longer exports {name} as a template literal"
    return re.sub(r"\$(\d+)", r"%(p\1)s", match.group(1))


def loader_number(name: str) -> int:
    match = re.search(rf"export const {name} = (\d+);", PIPELINE.read_text())
    assert match is not None, f"{PIPELINE} no longer exports {name} as a number"
    return int(match.group(1))


@dg.op
def raises() -> None:
    raise RuntimeError("entity 7989d366 was minted without its birth alias\nsecond line")


@dg.job
def failing_job() -> None:
    raises()


@dg.op
def works() -> None:
    return None


@dg.job
def healthy_job() -> None:
    works()


@dg.job
def ancient_job() -> None:
    works()


@pytest.fixture(scope="module")
def runs() -> Iterator[tuple[str, dict[str, str]]]:
    """(database url, run ids by name). `killed` is 10 days old, `ancient` 120 days, `healthy` 3 days."""
    with isolated_test_database("pipeline_event_log") as url:
        separator = "&" if "?" in url else "?"
        storage = {"postgres": {"postgres_url": f"{url}{separator}options=-csearch_path%3Ddagster"}}
        with instance_for_test(overrides={"storage": storage}) as instance:
            by_op = failing_job.execute_in_process(instance=instance, raise_on_error=False)
            killed = instance.create_run_for_job(failing_job)
            instance.report_run_failed(killed, message=KILLED_MESSAGE)
            healthy = healthy_job.execute_in_process(instance=instance)
            ancient = ancient_job.execute_in_process(instance=instance)
        ids = {"op": by_op.run_id, "killed": killed.run_id, "healthy": healthy.run_id, "ancient": ancient.run_id}
        with psycopg.connect(url, autocommit=True) as connection:
            for name, days in (("killed", 10), ("healthy", 3), ("ancient", 120)):
                connection.execute(
                    "update dagster.runs set create_timestamp = (now() at time zone 'utc') - make_interval(days => %s) "
                    "where run_id = %s",
                    (days, ids[name]),
                )
        yield url, ids


def events(url: str, run_ids: list[str]) -> dict[str, dict[str, Any]]:
    with psycopg.connect(url) as connection:
        rows = connection.execute(loader_sql(PIPELINE, "FAILURE_EVENTS_SQL"), {"p1": run_ids}).fetchall()
    return {run_id: {"type": event_type, "event": json.loads(event)} for run_id, event_type, event in rows}


def test_each_failed_run_has_one_row_and_a_killed_run_has_its_run_failure(runs) -> None:
    url, ids = runs

    found = events(url, [ids["op"], ids["killed"]])

    assert set(found) == {ids["op"], ids["killed"]}, "one row per failed run"
    assert found[ids["op"]]["type"] == "STEP_FAILURE", "the step failure comes before the run failure"
    assert found[ids["killed"]]["type"] == "PIPELINE_FAILURE", "a killed run has no step failure"
    assert found[ids["killed"]]["event"]["dagster_event"]["message"] == KILLED_MESSAGE


def test_the_step_failure_carries_the_exception_of_the_op_as_its_innermost_cause(runs) -> None:
    url, ids = runs

    event = events(url, [ids["op"]])[ids["op"]]["event"]["dagster_event"]

    error = event["event_specific_data"]["error"]
    while error["cause"] is not None:
        error = error["cause"]
    assert error["message"].startswith("RuntimeError: entity 7989d366 was minted without its birth alias\n")


def test_the_jobs_query_counts_the_window_names_the_newest_failure_and_drops_a_job_that_left(runs) -> None:
    """`failing_job` has a failure 10 days ago (outside the 7-day window) and one now. `ancient_job` ran 120 days
    ago, beyond the listed days. `healthy_job` ran 3 days ago."""
    url, ids = runs
    parameters = {"p1": loader_number("PIPELINE_WINDOW_DAYS"), "p2": loader_number("PIPELINE_LISTED_DAYS")}

    with psycopg.connect(url) as connection:
        rows = {row[0]: row[1:] for row in connection.execute(loader_sql(PIPELINE, "JOBS_SQL"), parameters)}

    assert set(rows) == {"failing_job", "healthy_job"}, "a job with no run for the listed days is not listed"
    successes, failures, last_success, last_failure, last_failure_run_id = rows["failing_job"]
    assert (successes, failures, last_success) == (0, 1, None), "only the failure inside the window counts"
    assert last_failure is not None
    assert last_failure_run_id == ids["op"], "the newest failure of all time, not the 10-day-old one"
    assert rows["healthy_job"][:2] == (1, 0)
    assert rows["healthy_job"][2] is not None and rows["healthy_job"][3] is None and rows["healthy_job"][4] is None


def start_of(url: str, builds: list[tuple[str, int]], sha: str) -> tuple[datetime | None]:
    """RELEASE_START_SQL for `sha` over run plans of `builds` (git sha, day offset). The rows are rolled back."""
    base = datetime(2026, 10, 1, tzinfo=UTC)
    with psycopg.connect(url) as connection:
        connection.execute("set local session_replication_role = replica")  # run plans need a run row and a validator
        for index, (build, offset) in enumerate(builds):
            connection.execute(
                "insert into raw.production_topt_run_plans (run_id, release_manifest_id, content_sha256, payload, created_at) "
                "values (%s, %s, %s, %s::jsonb, %s)",
                (
                    f"capture-run:test{index}",
                    "release-manifest:" + "a" * 64,
                    f"{index:064x}",
                    json.dumps({"data_engine_git_sha": build}),
                    base + timedelta(days=offset),
                ),
            )
        row = connection.execute(loader_sql(OPS, "RELEASE_START_SQL"), {"p1": sha}).fetchone()
        connection.rollback()
    assert row is not None
    return (row[0],)


DAY = datetime(2026, 10, 1, tzinfo=UTC)


def test_the_release_start_is_the_first_run_of_the_current_stretch_of_the_build(runs) -> None:
    """A, B, A, A: the build A returned after B. Its release age counts from the second A, not from the first."""
    url, _ = runs
    builds = [("A", 0), ("B", 1), ("A", 2), ("A", 3)]

    assert start_of(url, builds, "A") == (DAY + timedelta(days=2),), "the stretch began at the second A"
    assert start_of(url, builds, "B") == (None,), "B has no run after the newest run of another build"
    assert start_of(url, builds, "C") == (None,), "a build with no run has no start"


def test_a_run_with_no_recorded_sha_is_not_another_build(runs) -> None:
    """C, unknown, C: the stamp is missing on one run plan. It must not start a new stretch of C."""
    url, _ = runs

    assert start_of(url, [("C", 0), ("unknown", 1), ("C", 2)], "C") == (DAY,)


def test_a_build_that_ran_once_starts_at_that_run(runs) -> None:
    url, _ = runs

    assert start_of(url, [("A", 0), ("B", 5)], "B") == (DAY + timedelta(days=5),)
