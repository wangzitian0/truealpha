"""Falsifiable Wave 4 data-engine tests for M4 targets, M5 cross-head consolidation, and backtest triggers."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import dagster as dg
import pytest
from data_engine.datahub.analyst_ratings import AnalystRatingItem, materialize_analyst_ratings
from data_engine.datahub.capture_core import fund_consolidation
from data_engine.lanes import triggers
from data_engine.lanes.backtest import BACKTEST_JOB_NAME

pytestmark = pytest.mark.deps


def test_fund_consolidation_sql_reads_served_heads_and_virtual_leaves() -> None:
    assert "from mart.served_head sh" in fund_consolidation._LINES_SQL
    assert "core.revenue" in fund_consolidation._LINES_SQL
    assert "core.gross_profit" in fund_consolidation._LINES_SQL
    assert "core.employees" in fund_consolidation._LINES_SQL
    assert "core.gppe" in fund_consolidation._LINES_SQL


def test_materialize_analyst_ratings_persists_target_price_and_track_record() -> None:
    executed: list[tuple[str, Any]] = []

    class _FakeCur:
        def execute(self, sql: str, params: Any = None) -> None:
            executed.append((sql, params))

        def fetchone(self) -> tuple[bool]:
            return (True,)

        def __enter__(self) -> _FakeCur:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    class _FakeConn:
        def cursor(self) -> _FakeCur:
            return _FakeCur()

        def execute(self, sql: str, params: Any = None) -> None:
            executed.append((sql, params))

    cutoff = datetime(2026, 4, 1, tzinfo=UTC)
    written = materialize_analyst_ratings(
        _FakeConn(),  # type: ignore[arg-type]
        run_id="capture-run:" + "a" * 64,
        cutoff=cutoff,
        ratings_data=[
            {
                "issuer_id": "11111111-1111-1111-1111-111111111111",
                "ratings": [
                    AnalystRatingItem(
                        analyst_id="a1",
                        rating=5,
                        target_price=Decimal("180"),
                        confidence=Decimal("0.85"),
                        knowable_at=datetime(2026, 1, 1, tzinfo=UTC),
                        outcome_end=datetime(2026, 3, 1, tzinfo=UTC),
                        realized_return=Decimal("0.15"),
                    )
                ],
            }
        ],
    )
    assert written == 1
    insert_sql, insert_params = next((s, p) for s, p in executed if "insert into mart.issuer_analyst_ratings" in s)
    assert "target_price" in insert_sql
    assert "track_record_score" in insert_sql
    assert Decimal("180.0000") in insert_params
    assert Decimal("1") in insert_params


def test_pipeline_trigger_sensor_dispatches_nightly_backtest(monkeypatch: pytest.MonkeyPatch) -> None:
    dedupe = str(uuid.uuid4())
    executed_at = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
    updates: list[Any] = []

    class _FakeRes:
        def __init__(self, rows: list[Any]) -> None:
            self._rows = rows

        def fetchall(self) -> list[Any]:
            return self._rows

    class _FakeConn:
        def execute(self, sql: str, params: Any = None) -> _FakeRes:
            if "from staging.pipeline_trigger_requests" in sql:
                return _FakeRes(
                    [
                        (
                            42,
                            executed_at,
                            dedupe,
                            BACKTEST_JOB_NAME,
                            False,
                            {
                                "definition_resource": "large_model_value_v0_definition_1_1_0.json",
                                "release_manifest_id": "release-manifest:test-bt",
                                "exploratory": True,
                                "start_cutoff": "2024-01-01T00:00:00Z",
                            },
                        )
                    ]
                )
            if "update staging.pipeline_trigger_requests" in sql:
                updates.append(params)
            return _FakeRes([])

        def commit(self) -> None:
            return None

        def __enter__(self) -> _FakeConn:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(triggers, "consume_head_rollback_requests", lambda _c: [])
    monkeypatch.setattr(triggers.db, "connect", lambda: _FakeConn())

    reqs = list(triggers.pipeline_trigger_sensor(dg.build_sensor_context()))
    assert len(reqs) == 1
    assert reqs[0].job_name == BACKTEST_JOB_NAME
    assert reqs[0].run_key == f"manual:{dedupe}"
    op_cfg = reqs[0].run_config["ops"]["run_nightly_backtest_op"]["config"]
    assert op_cfg["executed_at"] == executed_at.isoformat()
    assert op_cfg["definition_resource"] == "large_model_value_v0_definition_1_1_0.json"
    assert op_cfg["release_manifest_id"] == "release-manifest:test-bt"
    assert op_cfg["exploratory"] is True
    assert op_cfg["start_cutoff"] == "2024-01-01T00:00:00Z"
    assert updates == [(f"manual:{dedupe}", 42)]

    from data_engine.lanes.backtest import NightlyBacktestConfig, _backtest_kwargs_from_config

    kwargs = _backtest_kwargs_from_config(NightlyBacktestConfig(**op_cfg))
    assert kwargs["release_manifest_id"] == "release-manifest:test-bt"
    assert kwargs["exploratory"] is True


def test_sensor_targets_include_nightly_backtest_pipeline() -> None:
    target_names = {job.name for job in triggers.pipeline_trigger_sensor.jobs}
    assert BACKTEST_JOB_NAME in target_names


def test_pipeline_trigger_sensor_skips_unknown_job_name(monkeypatch: pytest.MonkeyPatch) -> None:
    dedupe = str(uuid.uuid4())
    executed_at = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
    updates: list[Any] = []

    class _FakeRes:
        def __init__(self, rows: list[Any]) -> None:
            self._rows = rows

        def fetchall(self) -> list[Any]:
            return self._rows

    class _FakeConn:
        def execute(self, sql: str, params: Any = None) -> _FakeRes:
            if "from staging.pipeline_trigger_requests" in sql:
                return _FakeRes([(77, executed_at, dedupe, "nonexistent_pipeline_job", False, {})])
            if "update staging.pipeline_trigger_requests" in sql:
                updates.append(params)
            return _FakeRes([])

        def commit(self) -> None:
            return None

        def __enter__(self) -> _FakeConn:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(triggers, "consume_head_rollback_requests", lambda _c: [])
    monkeypatch.setattr(triggers.db, "connect", lambda: _FakeConn())

    reqs = list(triggers.pipeline_trigger_sensor(dg.build_sensor_context()))
    assert reqs == []
    assert updates == [(f"invalid:{dedupe}", 77)]
