"""The deployable Dagster entrypoint (#27): the module infra2's Staging/Production
daemon + webserver load with `-m data_engine.dagster_defs`.

Import-time / definition-load assertions — no database, no network. Importing the
module and building its `Definitions` is exactly what `dagster -m` and CI collection
do, so this proves the deploy target loads hermetically, that the deployed job graph
carries NO fixture seeding (#429 invariant I2), and that the schedule carries #27's
idempotency semantics.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime

import dagster as dg
from data_engine import dagster_defs
from data_engine.dagster_defs import (
    CORE_STRATEGY_FIXTURE_CANARY_JOB_NAME,
    TOPT_LIVE_CRON,
    TOPT_LIVE_JOB_NAME,
    defs,
    fixture_canary_definitions,
    topt_live_schedule,
)
from data_engine.datahub.live_topt_pipeline import live_version_for


def test_defs_loads_and_exposes_only_the_live_pipeline() -> None:
    # `dagster -m data_engine.dagster_defs` resolves this object; it must build with
    # no database and expose exactly the real-source pipeline — the fixture canary
    # is NOT part of the deployed composition (#429 I2).
    assert isinstance(defs, dg.Definitions)
    assert defs.get_job_def(TOPT_LIVE_JOB_NAME) is not None
    assert defs.get_schedule_def(topt_live_schedule.name) is not None
    deployed_jobs = {job.name for job in defs.jobs}
    assert deployed_jobs == {TOPT_LIVE_JOB_NAME}
    assert CORE_STRATEGY_FIXTURE_CANARY_JOB_NAME not in deployed_jobs


def test_deployed_module_contains_no_fixture_seeding() -> None:
    # The deployed op must never seed golden-fixture inputs. The retired fixture
    # seeder is only reachable inside the explicitly named tests-only factory.
    op_source = inspect.getsource(dagster_defs.run_topt_live_tick)
    assert "seed_strategy_backtest_inputs" not in op_source
    assert "_load_corpus" not in op_source
    # Module-level imports carry no fixture seeder either — it is imported lazily
    # inside fixture_canary_definitions() alone.
    assert not hasattr(dagster_defs, "seed_strategy_backtest_inputs")


def test_schedule_is_enabled_hourly_with_tick_driven_identity() -> None:
    # ENABLED is deliberate (#27 appended acceptance: schedule running in Staging).
    assert topt_live_schedule.default_status == dg.DefaultScheduleStatus.RUNNING
    assert topt_live_schedule.cron_schedule == TOPT_LIVE_CRON

    # Same tick -> same run_key + executed_at (idempotent retry); distinct ticks ->
    # distinct run_key (two-cycle proof). No wall clock.
    tick = datetime(2026, 7, 20, 6, 15, 0, tzinfo=UTC)
    context = dg.build_schedule_context(scheduled_execution_time=tick)
    first = topt_live_schedule(context)
    second = topt_live_schedule(context)
    assert first.run_key == second.run_key == tick.isoformat()
    assert first.run_config["ops"]["run_topt_live_tick"]["config"]["executed_at"] == tick.isoformat()

    later = dg.build_schedule_context(scheduled_execution_time=datetime(2026, 7, 20, 7, 15, 0, tzinfo=UTC))
    assert topt_live_schedule(later).run_key != first.run_key


def test_live_version_is_tick_deterministic() -> None:
    tick = datetime(2026, 7, 20, 6, 15, 0, tzinfo=UTC)
    assert live_version_for(tick) == "live-20260720T0615"
    assert live_version_for(tick) == live_version_for(tick)  # retry-stable
    assert live_version_for(datetime(2026, 7, 20, 7, 15, 0, tzinfo=UTC)) != live_version_for(tick)


def test_fixture_canary_stays_buildable_and_explicitly_named() -> None:
    # The retired fixture path remains provable in tests, under a name that cannot
    # be mistaken for a real-source run.
    fixture_defs = fixture_canary_definitions()
    assert fixture_defs.get_job_def(CORE_STRATEGY_FIXTURE_CANARY_JOB_NAME) is not None
    assert "fixture" in CORE_STRATEGY_FIXTURE_CANARY_JOB_NAME
