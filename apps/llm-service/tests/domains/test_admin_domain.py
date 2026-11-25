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


def test_admin_head_rollback_rbac_and_mcp_fact_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    anon = TestClient(app)
    rollback_body = {
        "universe_id": "universe:topt-us-v1",
        "universe_version": "2026-11-01",
        "factor_id": "gross_profit_per_employee",
        "target_run_id": f"capture-run:{'a' * 64}",
        "reason": "corrupt upstream extraction on latest tick",
    }
    assert anon.post("/api/v1/admin/head-rollback", json=rollback_body).status_code == 401

    member_client, member_headers = auth_client(monkeypatch, principal_kind="member")
    assert (
        member_client.post("/api/v1/admin/head-rollback", json=rollback_body, headers=member_headers).status_code == 403
    )

    def _fake_rollback(
        principal_id: str, payload: admin_service.HeadRollbackRequest
    ) -> admin_service.HeadRollbackResponse:
        if not payload.target_run_id.startswith("capture-run:"):
            raise ValueError("target_run_id must start with 'capture-run:'")
        return admin_service.HeadRollbackResponse(
            request_id=42,
            dedupe_key="rb-1",
            environment="staging",
            universe_id=payload.universe_id,
            universe_version=payload.universe_version,
            factor_id=payload.factor_id,
            target_run_id=payload.target_run_id,
            reason=payload.reason,
            requested_at="2026-11-24T12:00:00Z",
        )

    monkeypatch.setattr("llm_service.api.v1.admin.request_head_rollback", _fake_rollback)
    admin_client, admin_headers = auth_client(monkeypatch, principal_kind="administrator")
    rb_ok = admin_client.post("/api/v1/admin/head-rollback", json=rollback_body, headers=admin_headers)
    assert rb_ok.status_code == 202 and rb_ok.json()["request_id"] == 42

    rb_bad = admin_client.post(
        "/api/v1/admin/head-rollback",
        json={**rollback_body, "target_run_id": "invalid-run"},
        headers=admin_headers,
    )
    assert rb_bad.status_code == 400

    from llm_service.mcp_server import build_mcp_server
    from llm_service.services.auth_service import PrincipalRecord, access_context_for
    from mcp.server.fastmcp.exceptions import ToolError

    admin_ctx = access_context_for(
        PrincipalRecord(principal_id="p:admin", tenant_id="t:admin", principal_kind="administrator")
    )
    member_ctx = access_context_for(
        PrincipalRecord(principal_id="p:member", tenant_id="t:member", principal_kind="member")
    )

    sample_stats = admin_service.DatahubStatsResponse(
        served_heads=[
            {
                "universe_id": "universe:topt-us-v1",
                "run_id": None,
                "freshness": "withheld",
                "availability": "unavailable",
            }
        ],
        question_coverage=[
            {
                "universe_id": "universe:topt-us-v1",
                "payload": {
                    "previous_run_id": "capture-run:prev",
                    "questions": {"q1": {"answered": 10, "denominator": 12, "change": {"answered": -1}}},
                },
            }
        ],
    )
    monkeypatch.setattr(
        "llm_service.mcp_server.get_ops_overview",
        lambda: admin_service.OpsOverviewResponse(
            runs=[], app_git_sha="mcp1234", served_heads=sample_stats.served_heads
        ),
    )
    monkeypatch.setattr("llm_service.mcp_server.get_datahub_stats", lambda: sample_stats)

    server = build_mcp_server()

    async def _run_mcp() -> None:
        monkeypatch.setattr("llm_service.mcp_server.resolve_mcp_access_context", lambda: admin_ctx)
        _, ops_snap = await server.call_tool("admin_ops_snapshot", {})  # type: ignore[misc]
        assert ops_snap["app_git_sha"] == "mcp1234"
        assert len(ops_snap["served_heads"]) == 1

        _, dh_facts = await server.call_tool("admin_datahub_facts", {})  # type: ignore[misc]
        assert dh_facts["diff"]["withheld_head_count"] == 1
        assert dh_facts["diff"]["question_deltas"]["universe:topt-us-v1"]["deltas"]["q1"] == {"answered": -1}

        monkeypatch.setattr("llm_service.mcp_server.resolve_mcp_access_context", lambda: member_ctx)
        with pytest.raises(ToolError, match="administrator principal required"):
            await server.call_tool("admin_ops_snapshot", {})
        with pytest.raises(ToolError, match="administrator principal required"):
            await server.call_tool("admin_datahub_facts", {})

    asyncio.run(_run_mcp())


def test_admin_service_populates_capacity_facts_and_validates_rollback(monkeypatch: pytest.MonkeyPatch) -> None:
    class _DatahubCursor:
        def __init__(self) -> None:
            self.connection = self
            self._rows: list[dict[str, object]] = []

        def execute(self, sql: str, *_a: object, **_k: object) -> None:
            low = sql.lower()
            if "from mart.environment_identity" in low:
                self._rows = [{"environment": "staging"}]
            elif "from staging.api_call_ledger l" in low and "calls_24h" in low:
                self._rows = [
                    {
                        "source": "sec",
                        "calls_24h": 15,
                        "failed_24h": 0,
                        "rate_limited_24h": 0,
                        "calls_7d": 40,
                        "failed_7d": 1,
                    }
                ]
            elif "from staging.api_call_ledger l" in low and "status_429" in low:
                self._rows = [
                    {
                        "day": "2026-11-24",
                        "source": "zhipu",
                        "status_429": 2,
                        "code_1302": 1,
                        "total_calls": 10,
                        "failed_calls": 3,
                    }
                ]
            elif "from mart.output_invariant_results" in low:
                self._rows = [
                    {
                        "invariant_id": "selected-weights-close",
                        "kind": "hard",
                        "population": 10,
                        "violation_count": 0,
                        "deferred": False,
                        "sample_rows": [],
                        "dagster_run_id": "run-1",
                        "checked_at": "2026-11-24T00:00:00Z",
                    }
                ]
            elif "from staging.model_invocations" in low:
                self._rows = [
                    {
                        "day": "2026-11-24",
                        "provider": "zhipu",
                        "served_model": "glm-4.5",
                        "standard": "gppe",
                        "calls": 5,
                        "prompt_tokens": 100,
                        "completion_tokens": 50,
                        "cost": "0.12",
                        "error_calls": 0,
                        "refusal_calls": 0,
                    }
                ]
            elif "from staging.ingestion_health_log" in low:
                self._rows = [
                    {
                        "id": 1,
                        "source": "sec",
                        "metric": "backfill_ok",
                        "value": "1",
                        "note": "ok",
                        "logged_at": "2026-11-24T00:00:00Z",
                    }
                ]
            elif "from staging.pipeline_trigger_requests" in low:
                self._rows = [
                    {
                        "request_id": 1,
                        "job_name": "topt_live_pipeline",
                        "requested_by": "p:admin",
                        "dedupe_key": "d-1",
                        "force_fetch": False,
                        "launched_run_key": None,
                        "executed_at": "2026-11-24T00:00:00Z",
                        "requested_at": "2026-11-24T00:00:00Z",
                        "consumed_at": None,
                    }
                ]
            elif "from staging.head_rollback_requests" in low:
                self._rows = [
                    {
                        "request_id": 2,
                        "environment": "staging",
                        "universe_id": "universe:topt-us-v1",
                        "universe_version": "2026-11-01",
                        "factor_id": "gross_profit_per_employee",
                        "target_run_id": f"capture-run:{'a' * 64}",
                        "reason": "rollback",
                        "requested_by": "p:admin",
                        "dedupe_key": "rb-1",
                        "result_pointer_id": None,
                        "requested_at": "2026-11-24T00:00:00Z",
                        "consumed_at": None,
                    }
                ]
            else:
                self._rows = []

        def fetchall(self) -> list[dict[str, object]]:
            return list(self._rows)

        def fetchone(self) -> dict[str, object] | None:
            return self._rows[0] if self._rows else None

        def rollback(self) -> None:
            return None

    monkeypatch.setattr("llm_service.services.admin_service.with_ops_reader", lambda fn, **_k: fn(_DatahubCursor()))
    stats = admin_service.get_datahub_stats()
    assert len(stats.vendor_accounts) > 0
    assert len(stats.capacity) > 0
    assert stats.model_1302_by_day[0]["code_1302"] == 1
    assert stats.output_invariants[0]["invariant_id"] == "selected-weights-close"
    assert stats.llm_usage[0]["served_model"] == "glm-4.5"
    assert stats.backfill_health[0]["metric"] == "backfill_ok"
    assert stats.trigger_queue[0]["request_id"] == 1
    assert stats.rollback_requests[0]["request_id"] == 2

    inserted: list[tuple[object, ...]] = []

    class _RuntimeCursor:
        def execute(self, _sql: str, params: tuple[object, ...] = ()) -> None:
            inserted.append(params)

        def fetchone(self) -> dict[str, object]:
            return {"request_id": 99, "requested_at": "2026-11-24T12:05:00Z"}

    monkeypatch.setattr("llm_service.services.admin_service.with_app_runtime", lambda fn, **_k: fn(_RuntimeCursor()))
    res = admin_service.request_head_rollback(
        "p:admin",
        admin_service.HeadRollbackRequest(
            universe_id="universe:topt-us-v1",
            universe_version="2026-11-01",
            target_run_id=f"capture-run:{'a' * 64}",
            reason="revert bad run",
        ),
    )
    assert res.request_id == 99 and res.environment == "staging"
    assert len(inserted) == 1

    with pytest.raises(ValueError, match="target_run_id"):
        admin_service.request_head_rollback(
            "p:admin",
            admin_service.HeadRollbackRequest(
                universe_id="universe:topt-us-v1",
                universe_version="2026-11-01",
                target_run_id="bad-prefix",
                reason="revert",
            ),
        )

    with pytest.raises(ValueError, match="does not match declared environment"):
        admin_service.request_head_rollback(
            "p:admin",
            admin_service.HeadRollbackRequest(
                universe_id="universe:topt-us-v1",
                universe_version="2026-11-01",
                target_run_id=f"capture-run:{'a' * 64}",
                reason="revert",
                environment="production",
            ),
        )
