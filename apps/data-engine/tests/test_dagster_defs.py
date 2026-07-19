"""The deployable Dagster entrypoint (#27): the module infra2's Staging/Production
daemon + webserver load with `-m data_engine.dagster_defs`.

These are import-time / definition-load assertions -- no database. Importing the
module and building its `Definitions` is exactly what `dagster -m` and CI
collection do, so this proves the deploy target loads without a live Postgres and
that the schedule carries #27's idempotency semantics.
"""

from __future__ import annotations

from datetime import UTC, datetime

import dagster as dg
from data_engine.dagster_defs import (
    CORE_STRATEGY_CANARY_CRON,
    CORE_STRATEGY_CANARY_JOB_NAME,
    core_strategy_canary_schedule,
    defs,
)


def test_defs_loads_and_exposes_the_canary_schedule() -> None:
    # `dagster -m data_engine.dagster_defs` resolves this object; it must build
    # with no database and expose the daemon's single recurring-run authority.
    assert isinstance(defs, dg.Definitions)
    assert defs.get_schedule_def(core_strategy_canary_schedule.name) is not None
    assert defs.get_job_def(CORE_STRATEGY_CANARY_JOB_NAME) is not None


def test_schedule_is_stopped_by_default_and_bounded_cadence() -> None:
    # Enabling the schedule in a live environment is a deliberate operator action,
    # not a deploy side effect.
    assert core_strategy_canary_schedule.default_status == dg.DefaultScheduleStatus.STOPPED
    assert core_strategy_canary_schedule.cron_schedule == CORE_STRATEGY_CANARY_CRON


def test_tick_time_drives_executed_at_and_run_key_is_idempotent() -> None:
    # Same tick -> same run_key + same executed_at (idempotent retry); distinct
    # ticks -> distinct run_key + executed_at (two-cycle proof). No wall clock.
    tick = datetime(2026, 7, 20, 6, 0, 0, tzinfo=UTC)
    context = dg.build_schedule_context(scheduled_execution_time=tick)

    first = core_strategy_canary_schedule(context)
    second = core_strategy_canary_schedule(context)
    assert first.run_key == second.run_key == tick.isoformat()
    assert first.run_config["ops"]["run_core_strategy_canary"]["config"]["executed_at"] == tick.isoformat()

    later = dg.build_schedule_context(scheduled_execution_time=datetime(2026, 7, 21, 6, 0, 0, tzinfo=UTC))
    assert core_strategy_canary_schedule(later).run_key != first.run_key
