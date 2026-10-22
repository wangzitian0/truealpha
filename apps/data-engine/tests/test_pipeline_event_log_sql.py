"""The queries of `/admin/pipeline` run against real Dagster run storage (#1466).

The page reads `dagster.runs` once, grouped by job, and then the event log of the newest failed run of
each job. Its loader test (`apps/app-web/tests/admin-pipeline.test.ts`) uses a fake client, which returns
rows whatever the SQL says. Here the SQL text is read from the loader source and run on the tables that
Dagster creates, over runs that Dagster itself wrote:

* a run whose op raised: Dagster writes `STEP_FAILURE`, then `PIPELINE_FAILURE`;
* a run that the run monitor killed: Dagster writes `PIPELINE_FAILURE` with a message and no step failure.
  `DagsterEventType.RUN_FAILURE` has the value `PIPELINE_FAILURE`; a filter on the text `RUN_FAILURE`
  matched no row, and a killed run printed "no error text in the event log".
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import dagster as dg
import psycopg
import pytest
from dagster._core.test_utils import instance_for_test
from truealpha_runtime.testing import isolated_test_database

LOADER = Path(__file__).resolve().parents[3] / "apps" / "app-web" / "src" / "server" / "admin" / "pipeline.ts"
KILLED_MESSAGE = "Run timed out due to taking longer than 10800 seconds to complete."


def loader_sql(name: str) -> str:
    """The text of `export const <name> = `...`;` in the loader, with its `$n` parameters as named ones."""
    match = re.search(rf"export const {name} = `(.*?)`;", LOADER.read_text(), re.S)
    assert match is not None, f"{LOADER} no longer exports {name} as a template literal"
    return re.sub(r"\$(\d)", r"%(p\1)s", match.group(1))


@dg.op
def raises() -> None:
    raise RuntimeError("entity 7989d366 was minted without its birth alias\nsecond line")


@dg.job
def failing_job() -> None:
    raises()


@pytest.fixture(scope="module")
def failed_runs() -> Iterator[tuple[str, dict[str, str]]]:
    """(database url, {'op': run id of the op failure, 'killed': run id of the killed run})"""
    with isolated_test_database("pipeline_event_log") as url:
        separator = "&" if "?" in url else "?"
        storage = {"postgres": {"postgres_url": f"{url}{separator}options=-csearch_path%3Ddagster"}}
        with instance_for_test(overrides={"storage": storage}) as instance:
            by_op = failing_job.execute_in_process(instance=instance, raise_on_error=False)
            killed = instance.create_run_for_job(failing_job)
            instance.report_run_failed(killed, message=KILLED_MESSAGE)
        yield url, {"op": by_op.run_id, "killed": killed.run_id}


def events(url: str, run_ids: list[str]) -> dict[str, dict[str, Any]]:
    with psycopg.connect(url) as connection:
        rows = connection.execute(loader_sql("FAILURE_EVENTS_SQL"), {"p1": run_ids}).fetchall()
    return {run_id: {"type": event_type, "event": json.loads(event)} for run_id, event_type, event in rows}


def test_each_failed_run_has_one_row_and_a_killed_run_has_its_run_failure(failed_runs) -> None:
    url, runs = failed_runs

    found = events(url, [runs["op"], runs["killed"]])

    assert set(found) == {runs["op"], runs["killed"]}, "one row per failed run"
    assert found[runs["op"]]["type"] == "STEP_FAILURE", "the step failure comes before the run failure"
    assert found[runs["killed"]]["type"] == "PIPELINE_FAILURE", "a killed run has no step failure"
    assert found[runs["killed"]]["event"]["dagster_event"]["message"] == KILLED_MESSAGE


def test_the_step_failure_carries_the_exception_of_the_op_as_its_innermost_cause(failed_runs) -> None:
    url, runs = failed_runs

    event = events(url, [runs["op"]])[runs["op"]]["event"]["dagster_event"]

    error = event["event_specific_data"]["error"]
    while error["cause"] is not None:
        error = error["cause"]
    assert error["message"].startswith("RuntimeError: entity 7989d366 was minted without its birth alias\n")


def test_the_jobs_query_counts_the_two_runs_by_job_and_names_the_newest_failure(failed_runs) -> None:
    url, runs = failed_runs

    with psycopg.connect(url) as connection:
        rows = connection.execute(loader_sql("JOBS_SQL"), {"p1": 7, "p2": 100}).fetchall()

    assert len(rows) == 1
    job, successes, failures, last_success, last_failure, last_failure_run_id = rows[0]
    assert (job, successes, failures, last_success) == ("failing_job", 0, 2, None)
    assert last_failure is not None
    assert last_failure_run_id in {runs["op"], runs["killed"]}
