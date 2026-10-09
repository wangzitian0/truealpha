"""Tests for nightly backtest lane (#758, Milestone M3).

This module tests definitions, schedule configuration, pending state, and simulation execution.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import dagster as dg
import pandas as pd
import psycopg
import pytest
from data_engine.lanes import backtest as lane
from data_engine.quality.nightly_verdicts import TICK_TAG
from factors.backtest.engine import BacktestResult


def test_backtest_lane_definitions() -> None:
    """The lane declares the job and schedule with the specified cron and timezone."""
    assert lane.BACKTEST_JOB_NAME in {job.name for job in lane.defs.jobs or ()}
    assert lane.nightly_backtest_schedule.name in {schedule.name for schedule in lane.defs.schedules or ()}
    assert lane.nightly_backtest_schedule.cron_schedule == lane.BACKTEST_CRON
    assert lane.nightly_backtest_schedule.execution_timezone == "UTC"

    tick = datetime(2026, 10, 9, 21, 30, tzinfo=UTC)
    request = lane.nightly_backtest_schedule(dg.build_schedule_context(scheduled_execution_time=tick))
    assert request.run_key == tick.isoformat()
    assert request.tags.get(TICK_TAG) == tick.isoformat()
    assert dg.validate_run_config(lane.nightly_backtest_pipeline_job, request.run_config)


def test_run_nightly_backtest_op_marks_pending_when_no_prices(monkeypatch: pytest.MonkeyPatch) -> None:
    """When staging tables hold no monthly price rows, the op records a pending verdict."""
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_conn.__enter__.return_value = mock_conn
    mock_cur.fetchone.return_value = None  # no prices

    monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: mock_conn)

    recorded_verdicts: list[dict] = []

    def fake_record(name: str, *, ran_at: datetime, ok: bool | None, summary: str, run_id: str) -> None:
        recorded_verdicts.append({"name": name, "ok": ok, "summary": summary})

    monkeypatch.setattr("data_engine.quality.nightly_verdicts.record", fake_record)

    context = dg.build_op_context(run_tags={TICK_TAG: "2026-10-09T21:30:00+00:00"})
    config = lane.NightlyBacktestConfig()

    lane.run_nightly_backtest_op(context, config)

    assert len(recorded_verdicts) == 1
    assert recorded_verdicts[0]["name"] == lane.NIGHTLY_BACKTEST_VERDICT
    assert recorded_verdicts[0]["ok"] is None  # pending
    assert "pending: no monthly prices" in recorded_verdicts[0]["summary"]


def test_run_nightly_backtest_op_executes_and_persists(monkeypatch: pytest.MonkeyPatch) -> None:
    """When prices exist, the op executes the backtest and reports success metadata."""
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_conn.__enter__.return_value = mock_conn
    mock_cur.fetchone.return_value = (1,)  # prices exist

    monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: mock_conn)

    sample_result = BacktestResult(
        run_id="backtest-run:" + "1" * 64,
        strategy_key="demo_topk_rank",
        strategy_version="v1",
        universe_id="topt",
        start_date="2023-01-01",
        end_date="2026-03-31",
        status="succeeded",
        cagr_monthly=0.185,
        sharpe_daily=1.24,
        max_dd_daily=-0.152,
        vol_daily=0.14,
        turnover_monthly=0.08,
        calmar_daily=1.21,
        metrics_payload={"alpha": 0.05},
        valuations_monthly=pd.DataFrame(),
        valuations_daily=pd.DataFrame(),
        trades=[{"symbol": "AAPL", "side": "BUY"}],
        error_message=None,
    )

    monkeypatch.setattr(lane, "run_backtest", lambda *args, **kwargs: sample_result)

    recorded_verdicts: list[dict] = []

    def fake_record(name: str, *, ran_at: datetime, ok: bool | None, summary: str, run_id: str) -> None:
        recorded_verdicts.append({"name": name, "ok": ok, "summary": summary})

    monkeypatch.setattr("data_engine.quality.nightly_verdicts.record", fake_record)

    context = dg.build_op_context(run_tags={TICK_TAG: "2026-10-09T21:30:00+00:00"})
    config = lane.NightlyBacktestConfig()

    lane.run_nightly_backtest_op(context, config)

    assert len(recorded_verdicts) == 1
    assert recorded_verdicts[0]["name"] == lane.NIGHTLY_BACKTEST_VERDICT
    assert recorded_verdicts[0]["ok"] is True
    assert "succeeded:" in recorded_verdicts[0]["summary"]

    metadata = context.get_output_metadata("result")
    assert metadata["run_id"] == sample_result.run_id
    assert metadata["strategy_key"] == "demo_topk_rank"
    assert metadata["status"] == "succeeded"
    assert metadata["cagr_monthly"] == 0.185
    assert metadata["trades_count"] == 1


def test_run_nightly_backtest_op_raises_failure_when_backtest_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """When simulation returns failed status, the op raises Failure and records ok=False."""
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_conn.__enter__.return_value = mock_conn
    mock_cur.fetchone.return_value = (1,)  # prices exist

    monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: mock_conn)

    sample_result = BacktestResult(
        run_id="backtest-run:" + "2" * 64,
        strategy_key="demo_topk_rank",
        strategy_version="v1",
        universe_id="topt",
        start_date="2023-01-01",
        end_date="2026-03-31",
        status="failed",
        cagr_monthly=0.0,
        sharpe_daily=0.0,
        max_dd_daily=0.0,
        vol_daily=0.0,
        turnover_monthly=0.0,
        calmar_daily=0.0,
        metrics_payload={},
        valuations_monthly=pd.DataFrame(),
        valuations_daily=pd.DataFrame(),
        trades=[],
        error_message="singular matrix inversion",
    )

    monkeypatch.setattr(lane, "run_backtest", lambda *args, **kwargs: sample_result)

    recorded_verdicts: list[dict] = []

    def fake_record(name: str, *, ran_at: datetime, ok: bool | None, summary: str, run_id: str) -> None:
        recorded_verdicts.append({"name": name, "ok": ok, "summary": summary})

    monkeypatch.setattr("data_engine.quality.nightly_verdicts.record", fake_record)

    context = dg.build_op_context(run_tags={TICK_TAG: "2026-10-09T21:30:00+00:00"})
    config = lane.NightlyBacktestConfig()

    with pytest.raises(dg.Failure, match="singular matrix inversion"):
        lane.run_nightly_backtest_op(context, config)

    assert len(recorded_verdicts) == 1
    assert recorded_verdicts[0]["name"] == lane.NIGHTLY_BACKTEST_VERDICT
    assert recorded_verdicts[0]["ok"] is False
    assert "singular matrix inversion" in recorded_verdicts[0]["summary"]
