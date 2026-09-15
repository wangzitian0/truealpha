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


def test_each_op_body_actually_runs(monkeypatch) -> None:
    """The gap this closes: `run_theme_purity` imported a module that does not exist
    (`production_topt.governed_read`; `governed_head` lives in `question_coverage`). Every
    test above asserted the lane's SHAPE — the ops, their order, their config — and none of
    them executed a body, so a `ModuleNotFoundError` would have waited until Sunday 09:07 UTC
    to appear. mypy caught it; this is the check that catches the next one.

    The no-governed-head branch is the one to drive: it reaches every import and every
    settings read in the body, touches the database only to ask for a head, and returns
    without writing — so it needs no fixture beyond a connection that answers nothing.
    """
    import json as _json

    import psycopg
    from data_engine.lanes.standards import run_theme_purity

    class _NoHead:
        def execute(self, *_a, **_k):
            return self

        def fetchone(self):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    from data_engine.lanes.standards import StandardBackfillConfig

    monkeypatch.setattr(psycopg, "connect", lambda *_a, **_k: _NoHead())
    context = dg.build_op_context()
    config = StandardBackfillConfig(executed_at="2026-09-13T09:07:00+00:00", universe="topt")
    out = _json.loads(run_theme_purity(context, config, "{}"))
    assert out == {"universe": "topt", "rows": 0, "reason": "no_governed_head"}


def test_theme_purity_finds_the_head_the_capture_tick_registers_in_every_deployment(monkeypatch) -> None:
    """#826. On staging this op logged "no governed head" on every weekly tick and wrote no
    q6 row, while `run_question_coverage` in the SAME run found the head and persisted its
    report. The pointer's `environment` is the capture tier the A1 registration stamps
    (`CaptureEnvironment.PRODUCTION`, for staging's real-vendor capture too); the op asked for
    `settings.app_env`, which is `staging` there.

    The test above cannot see that: its connection answers nothing to every query, so it
    passes whatever environment the op asks for. This one answers ONLY for the stamped tier,
    and runs with the deployment set to staging, which is where the defect lived.
    """
    import json as _json

    import psycopg
    from data_engine.config import settings
    from data_engine.datahub.production_topt import theme_purity
    from data_engine.lanes.standards import StandardBackfillConfig, run_theme_purity
    from truealpha_contracts.common import CaptureEnvironment

    stamped = CaptureEnvironment.PRODUCTION.value  # what a1_evidence.register_run_evidence writes
    head = ("universe:topt-us-2026-03-31", "capture-run:" + "a" * 64, datetime(2026, 9, 14, 22, 45, tzinfo=UTC))

    class _Pointer:
        def __init__(self) -> None:
            self._row = None

        def execute(self, sql, params=()):
            asked_for_stamped_tier = "current_pointer_head" in sql and stamped in tuple(params)
            self._row = head if asked_for_stamped_tier else None
            return self

        def fetchone(self):
            return self._row

        def commit(self) -> None:
            return None

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    monkeypatch.setattr(settings, "app_env", "staging")
    monkeypatch.setattr(psycopg, "connect", lambda *_a, **_k: _Pointer())
    materialized = []
    monkeypatch.setattr(
        theme_purity, "materialize_theme_purity", lambda _c, **kwargs: materialized.append(kwargs) or ()
    )
    # The corpus the op names its members from (#849); the pointer stub answers no corpus query.
    from data_engine.datahub.standards.planner import UniverseIssuer
    from data_engine.lanes import standards as lane

    monkeypatch.setattr(
        lane, "universe_issuers", lambda _c, _u: [UniverseIssuer("issuer:lei:X", "NFLX", "listing:x", None)]
    )

    config = StandardBackfillConfig(executed_at="2026-09-20T09:07:00+00:00", universe="topt")
    out = _json.loads(run_theme_purity(dg.build_op_context(), config, "{}"))

    assert out.get("reason") != "no_governed_head", "staging must find the head its own capture registered"
    assert out["run_id"] == head[1]
    assert materialized == [{"run_id": head[1], "cutoff": head[2], "tickers": {"issuer:lei:X": "NFLX"}}], (
        "rows are written for THAT run, at ITS cutoff, and the classifier is told the ticker (#849)"
    )
