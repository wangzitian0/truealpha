"""The weekly standards lane runs the backfill, then module 6's theme purity (#772), then
the question-coverage report (#748) — with one configuration per universe. A run request
that configured only some of the ops would fail at launch, so the schedule is asserted here
rather than discovered on Sunday.

The order is the data dependency, not a preference: purity consumes the segment partitions
the backfill lands, and coverage counts the purity column."""

from __future__ import annotations

from datetime import UTC, datetime

import dagster as dg
from data_engine.lanes.standards import (
    STANDARD_BACKFILL_UNIVERSES,
    standard_backfill_pipeline_job,
    standard_backfill_schedule,
)


def test_the_job_chains_backfill_then_purity_then_coverage() -> None:
    """Each op consumes the one before it, so the chain is enforced by the dependency rather
    than by ordering luck: purity would classify an empty plane if it ran first, and coverage
    would count a column that had not been written yet."""
    assert [node.name for node in standard_backfill_pipeline_job.graph.node_defs] == [
        "run_standard_backfill",
        "run_theme_purity",
        "run_question_coverage",
    ]


def test_every_run_request_configures_every_op_for_its_universe() -> None:
    context = dg.build_schedule_context(scheduled_execution_time=datetime(2026, 9, 13, 9, 7, tzinfo=UTC))
    requests = list(standard_backfill_schedule.evaluate_tick(context).run_requests)
    assert [request.run_key for request in requests] == [
        f"2026-09-13T09:07:00+00:00:{universe}" for universe in STANDARD_BACKFILL_UNIVERSES
    ]
    for request, universe in zip(requests, STANDARD_BACKFILL_UNIVERSES, strict=True):
        ops = request.run_config["ops"]
        assert set(ops) == {"run_standard_backfill", "run_theme_purity", "run_question_coverage"}
        for op in ops.values():
            assert op["config"]["universe"] == universe
            assert op["config"]["executed_at"] == "2026-09-13T09:07:00+00:00"
    assert dg.validate_run_config(standard_backfill_pipeline_job, requests[0].run_config)


def test_the_weekly_run_covers_every_registered_standard() -> None:
    """The defect this pins: `StandardBackfillConfig.standard` defaulted to
    `"employees_total"`, and the schedule passes no standard — so the weekly run backfilled
    exactly one metric. `segment_revenue` had a plane, an adapter and a confidence policy
    (#804/#805) and was invoked by nothing deployed.

    init.md rule 22 is that the registry is the metric list. A default that names a metric is
    that list wearing a different hat, and the loop being generic (#799/#800) does not help if
    the only thing that runs it names one.
    """
    from data_engine.lanes.standards import standards_to_run
    from truealpha_contracts.standards import STANDARDS

    context = dg.build_schedule_context(scheduled_execution_time=datetime(2026, 9, 13, 9, 7, tzinfo=UTC))
    for request in standard_backfill_schedule.evaluate_tick(context).run_requests:
        selected = request.run_config["ops"]["run_standard_backfill"]["config"].get("standard", "")
        assert set(standards_to_run(selected)) == set(STANDARDS), (
            "a registered standard the weekly run never reaches is code that merely exists"
        )


def test_a_manual_run_can_still_bound_itself_to_one_standard() -> None:
    """The generalization must not cost the operator the narrow run — probing one metric over
    one universe is the source-research instrument the lane's docstring describes."""
    from data_engine.lanes.standards import standards_to_run

    assert standards_to_run("segment_revenue") == ("segment_revenue",)


def test_an_unknown_standard_fails_at_the_run_rather_than_silently_doing_nothing() -> None:
    """A typo used to reach `STANDARDS[name]` and raise KeyError deep in the backfill; naming
    the registered set at the boundary is the difference between a fixable message and a
    stack trace."""
    import pytest
    from data_engine.lanes.standards import standards_to_run

    with pytest.raises(ValueError, match="unknown standard"):
        standards_to_run("employees")
