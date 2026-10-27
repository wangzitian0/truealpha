"""#758: the nightly backtest lane, its schedule and its verdict.

The op body is `datahub.backtest.run_core_strategy_backtest`. These tests replace it to prove
the lane's own contract: the verdict it records for a pending, a green and a red run, and the
environments where its schedule runs. `test_backtest_integration.py` runs the real body.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import dagster as dg
import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub.backtest import BacktestOutcome, BacktestPending
from data_engine.lanes import backtest as lane
from data_engine.quality.nightly_verdicts import TICK_TAG
from factors.backtest.engine import Performance
from truealpha_runtime.testing import load_tool

TICK = "2026-10-12T21:45:00+00:00"


def test_backtest_lane_definitions() -> None:
    """The schedule passes its tick both as the tag and as `executed_at`."""
    assert lane.BACKTEST_JOB_NAME in {job.name for job in lane.defs.jobs or ()}
    assert lane.nightly_backtest_schedule.cron_schedule == lane.BACKTEST_CRON == "45 21 * * 1-5"
    assert lane.nightly_backtest_schedule.execution_timezone == "UTC"

    tick = datetime(2026, 10, 12, 21, 45, tzinfo=UTC)
    request = lane.nightly_backtest_schedule(dg.build_schedule_context(scheduled_execution_time=tick))
    assert request.run_key == tick.isoformat()
    assert request.tags.get(TICK_TAG) == tick.isoformat()
    assert request.run_config["ops"]["run_nightly_backtest_op"]["config"] == {"executed_at": tick.isoformat()}
    assert dg.validate_run_config(lane.nightly_backtest_pipeline_job, request.run_config)


@pytest.mark.parametrize(
    ("app_env", "status"),
    [
        ("production", dg.DefaultScheduleStatus.RUNNING),
        ("prod", dg.DefaultScheduleStatus.RUNNING),
        (" Staging ", dg.DefaultScheduleStatus.RUNNING),
        ("dev", dg.DefaultScheduleStatus.STOPPED),
        ("ci", dg.DefaultScheduleStatus.STOPPED),
    ],
)
def test_the_schedule_runs_in_staging_and_production(app_env: str, status: dg.DefaultScheduleStatus) -> None:
    """Staging evidence must exist before production evidence (#758 decision 6)."""
    assert lane.schedule_status(app_env) == status
    assert lane.nightly_backtest_schedule.default_status == lane.schedule_status(settings.app_env)


def test_the_schedule_stays_outside_the_staging_capture_tick() -> None:
    from data_engine.lanes.capture import tick_cron

    backtest_minute_field, backtest_hour_field = lane.BACKTEST_CRON.split()[:2]
    backtest_minute = int(backtest_hour_field) * 60 + int(backtest_minute_field)
    minute, hour = tick_cron("topt", "staging").split()[:2]
    assert int(hour) * 60 + int(minute) - backtest_minute >= 60, "the staging tick starts an hour or more later"


#: The projection must end before the backtest reads its rows. The projector takes about 100 seconds.
MIN_PROJECTION_LEAD = timedelta(minutes=15)


def test_the_projection_fires_at_least_15_minutes_before_the_backtest_on_every_backtest_day() -> None:
    """#1270: history rows are append-only, so a projector fix reaches a backtest only through a
    projector run. A projection on Sunday alone left five backtests a week on rows up to six days old.

    Both crons come from the deployed lanes. The estate's own cron reader (`tools/scheduler_liveness.py`)
    parses them. Red on `7 10 * * 0`: no projection fires on a weekday.
    """
    from data_engine.lanes import strategy_history

    cron = load_tool("scheduler_liveness").Cron
    projection, backtest = cron.parse(strategy_history.STRATEGY_HISTORY_CRON), cron.parse(lane.BACKTEST_CRON)
    monday = datetime(2026, 10, 12, tzinfo=UTC)
    backtests = backtest.fires_between(monday, monday + timedelta(days=7))

    assert backtests, "the backtest must fire in the sampled week, or this test proves nothing"
    for fired in backtests:
        day_start = datetime.combine(fired.date(), time.min, tzinfo=UTC)
        assert projection.fires_between(day_start, fired - MIN_PROJECTION_LEAD), (
            f"no projection fires on {fired.date().isoformat()} at least 15 minutes before the backtest at "
            f"{fired.strftime('%H:%M')} UTC"
        )


def test_the_projection_schedule_runs_where_the_backtest_runs() -> None:
    """The projection feeds the backtest, so the two schedules share one definition of their environments.
    A projection that stays stopped in staging leaves the staging backtest on rows that no run refreshes."""
    from data_engine.lanes import strategy_history

    assert strategy_history.schedule_status is lane.schedule_status
    assert strategy_history.strategy_history_schedule.default_status == lane.schedule_status(settings.app_env)


@pytest.fixture
def verdicts(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    recorded: list[dict] = []

    def fake_record(name: str, *, ran_at: datetime, ok: bool | None, summary: str, run_id: str) -> None:
        recorded.append({"name": name, "ok": ok, "summary": summary})

    monkeypatch.setattr("data_engine.quality.nightly_verdicts.record", fake_record)
    connection = MagicMock()
    connection.__enter__.return_value = connection
    monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: connection)
    return recorded


def _context() -> dg.OpExecutionContext:
    return dg.build_op_context(run_tags={TICK_TAG: TICK})


def test_a_run_without_quarter_end_inputs_is_pending(monkeypatch: pytest.MonkeyPatch, verdicts: list[dict]) -> None:
    def pending(_connection, *, tick):
        raise BacktestPending("no quarter-end strategy inputs")

    monkeypatch.setattr(lane, "run_core_strategy_backtest", pending)

    lane.run_nightly_backtest_op(_context(), lane.NightlyBacktestConfig(executed_at=TICK))

    assert verdicts == [{"name": "nightly_backtest", "ok": None, "summary": "pending: no quarter-end strategy inputs"}]


def test_a_written_run_is_green_and_reports_its_identity(monkeypatch: pytest.MonkeyPatch, verdicts: list[dict]) -> None:
    stats = Performance(Decimal("0.2"), Decimal("0.1"), Decimal("0.2"), Decimal("0.5"), Decimal("0.15"), None, None)
    outcome = BacktestOutcome(
        run_id="backtest-run:" + "1" * 64,
        inserted=True,
        strategy_key="large_model_value_v0",
        cutoffs=12,
        trades=30,
        strategy=stats,
        benchmark=stats,
    )
    seen: dict[str, datetime] = {}

    def written(_connection, *, tick):
        seen["tick"] = tick
        return outcome

    monkeypatch.setattr(lane, "run_core_strategy_backtest", written)
    context = _context()

    lane.run_nightly_backtest_op(context, lane.NightlyBacktestConfig(executed_at=TICK))

    assert seen["tick"] == datetime(2026, 10, 12, 21, 45, tzinfo=UTC), "the run is dated by its tick"
    assert [(item["ok"], item["summary"].split(":")[0]) for item in verdicts] == [(True, "succeeded")]
    assert "large_model_value_v0 cutoffs=12 trades=30" in verdicts[0]["summary"]
    metadata = context.get_output_metadata("result")
    assert metadata["strategy_key"] == "large_model_value_v0"
    assert metadata["run_id"] == outcome.run_id


def test_a_failed_run_is_red_and_fails_the_op(monkeypatch: pytest.MonkeyPatch, verdicts: list[dict]) -> None:
    def broken(_connection, *, tick):
        raise RuntimeError("no cutoff of 12 selected an issuer")

    monkeypatch.setattr(lane, "run_core_strategy_backtest", broken)

    with pytest.raises(RuntimeError, match="selected an issuer"):
        lane.run_nightly_backtest_op(_context(), lane.NightlyBacktestConfig(executed_at=TICK))

    assert [item["ok"] for item in verdicts] == [False]
