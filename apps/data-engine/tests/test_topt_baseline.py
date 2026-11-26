"""Falsifiable unit tests for `data_engine.quality.topt_baseline` (#1647, #1664)."""

from __future__ import annotations

from datetime import date

import psycopg
import pytest
from data_engine.quality import topt_baseline

pytestmark = pytest.mark.deps


def test_judgement_ok_ignores_unmeasured_rows_and_fails_on_fail() -> None:
    passing = topt_baseline.Row("Q1", "pass", "ok")
    unmeasured = topt_baseline.Row("C1", "unmeasured", "C1 code server 180/240 MiB, age 0.5 h, measured at 13 h")
    failing = topt_baseline.Row("R1", "fail", "over share")

    assert topt_baseline.Judgement((passing, unmeasured)).ok is True
    assert topt_baseline.Judgement((passing, failing)).ok is False


def test_judge_quota_passes_within_share_even_above_utilization_ceiling() -> None:
    day = date(2026, 11, 24)
    high_util = topt_baseline.judge_quota(source="twelvedata", daily_calls={day: 400}, share=480)
    assert high_util.state == "pass"
    assert "high utilization" in high_util.text

    over_share = topt_baseline.judge_quota(source="twelvedata", daily_calls={day: 481}, share=480)
    assert over_share.state == "fail"


def test_read_schema_checks_namespaces_and_role_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    executed_sqls: list[str] = []

    class _FakeResult:
        def __init__(self, sql: str) -> None:
            self._sql = sql

        def fetchall(self) -> list[tuple[str]]:
            if "pg_namespace" in self._sql:
                return [("app",), ("mart",), ("raw",), ("staging",)]
            return []

        def fetchone(self) -> tuple[int] | None:
            if "pg_roles" in self._sql:
                return (1,)
            return None

    class _FakeConn:
        read_only = False

        def __enter__(self) -> _FakeConn:
            return self

        def __exit__(self, *_exc: object) -> bool:
            return False

        def execute(self, sql: str, _params: object = None) -> _FakeResult:
            executed_sqls.append(sql)
            return _FakeResult(sql)

    monkeypatch.setattr(psycopg, "connect", lambda *_a, **_k: _FakeConn())
    row = topt_baseline.read_schema("postgresql://readonly.example/truealpha")
    assert row.state == "pass"
    assert not any("create database" in sql.lower() or "drop database" in sql.lower() for sql in executed_sqls)
