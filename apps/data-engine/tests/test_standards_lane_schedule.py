"""The weekly standards lane runs the backfill and then the question-coverage report (#748)
with one configuration per universe; a run request that configured only one op would fail
at launch, so the schedule is asserted here rather than discovered on Sunday."""

from __future__ import annotations

from datetime import UTC, datetime

import dagster as dg
from data_engine.lanes.standards import (
    STANDARD_BACKFILL_UNIVERSES,
    standard_backfill_pipeline_job,
    standard_backfill_schedule,
)


def test_the_job_chains_backfill_then_coverage() -> None:
    assert [node.name for node in standard_backfill_pipeline_job.graph.node_defs] == [
        "run_standard_backfill",
        "run_question_coverage",
    ]


def test_every_run_request_configures_both_ops_for_its_universe() -> None:
    context = dg.build_schedule_context(scheduled_execution_time=datetime(2026, 9, 13, 9, 7, tzinfo=UTC))
    requests = list(standard_backfill_schedule.evaluate_tick(context).run_requests)
    assert [request.run_key for request in requests] == [
        f"2026-09-13T09:07:00+00:00:{universe}" for universe in STANDARD_BACKFILL_UNIVERSES
    ]
    for request, universe in zip(requests, STANDARD_BACKFILL_UNIVERSES, strict=True):
        ops = request.run_config["ops"]
        assert set(ops) == {"run_standard_backfill", "run_question_coverage"}
        for op in ops.values():
            assert op["config"]["universe"] == universe
            assert op["config"]["executed_at"] == "2026-09-13T09:07:00+00:00"
    assert dg.validate_run_config(standard_backfill_pipeline_job, requests[0].run_config)
