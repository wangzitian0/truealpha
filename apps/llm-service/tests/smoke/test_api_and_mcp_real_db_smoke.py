"""Real-Postgres smoke tests covering all 7 OpenAPI domains, MCP tools, and role boundaries (#1803)."""

from __future__ import annotations

import psycopg
import pytest
from fastapi.testclient import TestClient
from llm_service.main import app
from llm_service.mcp_server import build_mcp_server

from ._smoke_support import ADMIN_EMAIL, MEMBER_EMAIL, call_mcp_tool, smoke_app_service_db

pytestmark = [pytest.mark.deps, pytest.mark.smoke]


def test_all_seven_domains_http_and_mcp_on_real_postgres_as_app_service_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise all 7 HTTP domains and MCP read tools against real migrated Postgres as app_service_login."""
    with smoke_app_service_db(monkeypatch, "llm_api_smoke") as ctx:
        with TestClient(app) as client:
            # 1. system probes read real Postgres views without falling back to "unknown"
            livez = client.get("/livez")
            assert livez.status_code == 200
            health = client.get("/health")
            assert health.status_code == 200
            health_payload = health.json()
            assert isinstance(health_payload["governed_pointers"], list), health_payload
            assert isinstance(health_payload["nightly_verdicts"], list), health_payload

            # 2. auth domain + role boundary
            assert client.get("/api/v1/panel/universes").status_code == 401
            member_login = client.post("/api/v1/auth/login", json={"email": MEMBER_EMAIL, "password": ctx.password})
            assert member_login.status_code == 200, member_login.text
            session_member = client.get("/api/v1/auth/session")
            assert session_member.status_code == 200
            assert session_member.json()["authenticated"] is True
            assert session_member.json()["principal_kind"] == "member"
            assert client.get("/api/v1/admin/ops").status_code == 403

            assert client.post("/api/v1/auth/logout").status_code == 200
            admin_login = client.post("/api/v1/auth/login", json={"email": ADMIN_EMAIL, "password": ctx.password})
            assert admin_login.status_code == 200, admin_login.text
            session_admin = client.get("/api/v1/auth/session")
            assert session_admin.status_code == 200
            assert session_admin.json()["principal_kind"] == "administrator"

            # 3. panel / strategy / entities / library read routes over real mart + app schemas
            for path in (
                "/api/v1/panel/universes",
                "/api/v1/panel/coverage",
                "/api/v1/panel/macro",
                "/api/v1/strategy/rankings",
                "/api/v1/strategy/backtests",
                "/api/v1/entities/holdings",
                "/api/v1/entities/themes",
                "/api/v1/entities/trace?issuer_id=issuer:smoke",
                "/api/v1/entities/issuer:smoke",
                "/api/v1/entities/issuer:smoke/supply-chain",
                "/api/v1/entities/issuer:smoke/analyst-ratings",
                "/api/v1/library/documents",
            ):
                resp = client.get(path)
                assert resp.status_code == 200, f"GET {path} -> {resp.status_code}: {resp.text}"

            # 4. admin read + queued trigger & head-rollback mutations
            ops_resp = client.get("/api/v1/admin/ops")
            assert ops_resp.status_code == 200, ops_resp.text
            ops_data = ops_resp.json()
            for key in ("dependencies", "served_heads", "nightly_verdicts", "trigger_queue", "rollback_requests"):
                assert key in ops_data

            datahub_resp = client.get("/api/v1/admin/datahub")
            assert datahub_resp.status_code == 200, datahub_resp.text
            datahub_data = datahub_resp.json()
            for key in (
                "sources",
                "oracle_outcomes",
                "question_coverage",
                "served_heads",
                "served_artifacts",
                "output_invariants",
                "llm_usage",
                "backfill_health",
                "capacity",
                "vendor_accounts",
                "model_1302_by_day",
            ):
                assert key in datahub_data

            trigger_resp = client.post("/api/v1/admin/triggers", json={"universe": "qqq", "force_fetch": False})
            assert trigger_resp.status_code == 202, trigger_resp.text
            trigger_dedupe = trigger_resp.json()["dedupe_key"]

            rollback_resp = client.post(
                "/api/v1/admin/head-rollback",
                json={
                    "universe_id": "universe:topt-smoke",
                    "universe_version": "v1",
                    "factor_id": "gross_profit_per_employee",
                    "target_run_id": "capture-run:smoke-prev",
                    "reason": "smoke test",
                },
            )
            assert rollback_resp.status_code == 202, rollback_resp.text
            rollback_dedupe = rollback_resp.json()["dedupe_key"]

            ops_after = client.get("/api/v1/admin/ops").json()
            assert any(row["dedupe_key"] == trigger_dedupe for row in ops_after["trigger_queue"])
            assert any(row["dedupe_key"] == rollback_dedupe for row in ops_after["rollback_requests"])

            # 5. real fact rows seeded into DB appear identically via HTTP and admin MCP tools
            with psycopg.connect(ctx.superuser_url, autocommit=True) as su_conn:
                su_conn.execute(
                    "insert into staging.ingestion_health_log (source, metric, value, note) "
                    "values ('smoke_source', 'smoke_metric', 42, 'real db smoke')"
                )
                su_conn.execute(
                    "insert into mart.output_invariant_results "
                    "(dagster_run_id, checked_at, invariant_id, kind, population, violation_count, deferred, sample_rows) "
                    "values ('dagster-smoke-1', now(), 'smoke-invariant-1', 'fact', 10, 0, false, '[]'::jsonb)"
                )

            datahub_with_facts = client.get("/api/v1/admin/datahub").json()
            assert any(row["metric"] == "smoke_metric" for row in datahub_with_facts["backfill_health"])
            assert any(row["invariant_id"] == "smoke-invariant-1" for row in datahub_with_facts["output_invariants"])

        mcp = build_mcp_server()
        assert isinstance(call_mcp_tool(mcp, "panel_universes"), dict)
        assert isinstance(call_mcp_tool(mcp, "panel_macro"), dict)
        assert isinstance(call_mcp_tool(mcp, "strategy_rankings", {"request": {}}), dict)
        assert isinstance(call_mcp_tool(mcp, "strategy_backtests", {"request": {}}), dict)
        assert isinstance(call_mcp_tool(mcp, "entities_holdings", {"request": {}}), dict)
        assert isinstance(call_mcp_tool(mcp, "entities_themes", {"request": {"theme_id": "ai-infrastructure"}}), dict)
        assert isinstance(call_mcp_tool(mcp, "entities_detail", {"request": {"issuer_id": "issuer:smoke"}}), dict)
        assert isinstance(call_mcp_tool(mcp, "library_documents_list"), dict)
        assert isinstance(call_mcp_tool(mcp, "admin_ops_snapshot"), dict)
        facts_payload = call_mcp_tool(mcp, "admin_datahub_facts")
        assert any(row["metric"] == "smoke_metric" for row in facts_payload["facts"]["backfill_health"])
        assert any(row["invariant_id"] == "smoke-invariant-1" for row in facts_payload["facts"]["output_invariants"])


def test_smoke_role_enforces_privilege_boundaries_and_fails_on_forbidden_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Confirm the smoke connection uses a real least-privilege role that fails on unauthorized tables."""
    with smoke_app_service_db(monkeypatch, "llm_priv_smoke") as ctx:
        with psycopg.connect(ctx.smoke_url) as conn:
            conn.execute("set local role app_ops_reader")
            conn.execute("select 1 from mart.served_head limit 1").fetchall()
            conn.execute("select 1 from mart.output_invariant_results limit 1").fetchall()
            conn.execute("select 1 from staging.ingestion_health_log limit 1").fetchall()
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("select 1 from app.principal_credentials limit 1").fetchall()
