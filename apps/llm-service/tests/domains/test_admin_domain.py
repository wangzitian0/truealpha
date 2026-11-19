"""Domain 7 (`admin`) HTTP + RBAC behavioral tests."""

from __future__ import annotations

import psycopg
import pytest
from fastapi.testclient import TestClient
from llm_service.main import app
from llm_service.services import admin_service

from ._helpers import auth_client


def test_admin_endpoints_enforce_auth_and_admin_rbac(monkeypatch: pytest.MonkeyPatch) -> None:
    anon = TestClient(app)
    assert anon.get("/api/v1/admin/ops").status_code == 401
    assert anon.post("/api/v1/admin/triggers", json={"universe": "topt"}).status_code == 401
    assert anon.get("/api/v1/admin/datahub").status_code == 401

    member_client, member_headers = auth_client(monkeypatch, principal_kind="member")
    assert member_client.get("/api/v1/admin/ops", headers=member_headers).status_code == 403
    assert (
        member_client.post("/api/v1/admin/triggers", json={"universe": "topt"}, headers=member_headers).status_code
        == 403
    )
    assert member_client.get("/api/v1/admin/datahub", headers=member_headers).status_code == 403


def test_admin_ops_triggers_and_datahub_usecases(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "llm_service.api.v1.admin.get_ops_overview",
        lambda: admin_service.OpsOverviewResponse(runs=[], app_git_sha="abc1234"),
    )
    monkeypatch.setattr(
        "llm_service.api.v1.admin.get_datahub_stats",
        lambda: admin_service.DatahubStatsResponse(capture_runs=[{"run_id": "cap:1"}]),
    )

    def _fake_trigger(
        principal_id: str, payload: admin_service.TriggerPipelineRequest
    ) -> admin_service.TriggerPipelineResponse:
        if payload.universe == "invalid-universe":
            raise ValueError("unsupported pipeline or universe")
        return admin_service.TriggerPipelineResponse(
            request_id=7,
            dedupe_key="dedupe-1",
            executed_at="2026-11-18T00:00:00+00:00",
            force_fetch=payload.force_fetch,
            job_name="topt_daily",
            run_key="manual:dedupe-1",
        )

    monkeypatch.setattr("llm_service.api.v1.admin.trigger_pipeline", _fake_trigger)

    admin_client, admin_headers = auth_client(monkeypatch, principal_kind="administrator")
    ops = admin_client.get("/api/v1/admin/ops", headers=admin_headers)
    trig_ok = admin_client.post(
        "/api/v1/admin/triggers", json={"universe": "topt", "force_fetch": True}, headers=admin_headers
    )
    trig_bad = admin_client.post("/api/v1/admin/triggers", json={"universe": "invalid-universe"}, headers=admin_headers)
    dh = admin_client.get("/api/v1/admin/datahub", headers=admin_headers)

    assert ops.status_code == 200 and ops.json()["app_git_sha"] == "abc1234"
    assert trig_ok.status_code == 202 and trig_ok.json()["job_name"] == "topt_daily"
    assert trig_bad.status_code == 400
    assert dh.status_code == 200 and dh.json()["capture_runs"][0]["run_id"] == "cap:1"


def test_admin_get_datahub_stats_and_ops_overview_fail_closed_on_db_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class _ExplodingCursor:
        def __init__(self, exc: Exception) -> None:
            self._exc = exc
            self.connection = self

        def execute(self, *_a: object, **_k: object) -> None:
            raise self._exc

        def fetchone(self) -> None:
            return None

        def fetchall(self) -> list[object]:
            return []

        def rollback(self) -> None:
            return None

    monkeypatch.setattr(
        "llm_service.services.admin_service.with_ops_reader",
        lambda fn, **_k: fn(_ExplodingCursor(psycopg.OperationalError("connection lost"))),
    )
    with pytest.raises(psycopg.OperationalError, match="connection lost"):
        admin_service.get_datahub_stats()
    with pytest.raises(psycopg.OperationalError, match="connection lost"):
        admin_service.get_ops_overview()


def test_admin_get_ops_overview_degrades_only_when_dagster_runs_table_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _MissingDagsterCursor:
        def __init__(self) -> None:
            self.connection = self
            self._last_sql = ""

        def execute(self, sql: str, *_a: object, **_k: object) -> None:
            self._last_sql = sql
            if "dagster.runs" in sql:
                raise psycopg.errors.UndefinedTable('relation "dagster.runs" does not exist')

        def fetchone(self) -> None:
            return None

        def fetchall(self) -> list[object]:
            return []

        def rollback(self) -> None:
            return None

    monkeypatch.setattr(
        "llm_service.services.admin_service.with_ops_reader",
        lambda fn, **_k: fn(_MissingDagsterCursor()),
    )
    overview = admin_service.get_ops_overview()
    assert overview.data_engine is None
    assert overview.runs == "unavailable"
