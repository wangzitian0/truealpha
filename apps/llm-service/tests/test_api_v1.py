"""Tests for unified OpenAPI 3.1 auth, documents, research, admin, and XHS card endpoints."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import bcrypt
import pytest
from fastapi.testclient import TestClient
from llm_service.main import app
from llm_service.services import (
    admin_service,
    auth_service,
    entities_service,
    library_service,
    strategy_service,
)
from pydantic import SecretStr

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.contract
def test_committed_openapi_json_matches_live_fastapi_spec() -> None:
    app.openapi_schema = None
    committed = json.loads((REPO_ROOT / "apps/app-web/openapi.json").read_text(encoding="utf-8"))
    assert app.openapi() == committed


def test_documents_validation_and_rls_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    assert library_service.assert_non_empty_text("  note:1  ", "id") == "note:1"
    assert library_service.assert_stable_id("report:abc123", "source") == "report:abc123"
    assert library_service.clamp_list_limit(None) == 50
    assert library_service.clamp_list_limit(0) == 1
    assert library_service.clamp_list_limit(999) == 200

    for bad_id in ("", "   ", "report:latest", "report:head", "bad space"):
        with pytest.raises(ValueError):
            library_service.assert_stable_id(bad_id, "source")

    assert library_service.parse_before_cursor(None, None) is None
    ts, doc = library_service.parse_before_cursor("2026-07-18T00:00:00.123456Z", "document:1") or ("", "")
    assert ts == "2026-07-18T00:00:00.123456Z" and doc == "document:1"
    for bad_ts, bad_doc in (("not-a-date", "document:1"), ("2026-07-18T00:00:00Z", "document:latest")):
        with pytest.raises(ValueError):
            library_service.parse_before_cursor(bad_ts, bad_doc)

    # Verify owner RLS transaction GUCs are set on document queries.
    executed: list[tuple[str, Any]] = []

    class _FakeCur:
        def execute(self, sql: str, params: Any = None) -> None:
            executed.append((sql.strip(), params))

        def fetchone(self) -> dict[str, Any]:
            return {
                "document_id": "document:abc",
                "tenant_id": "tenant:1",
                "owner_principal_id": "principal:1",
                "created_at": datetime(2026, 7, 1, tzinfo=UTC),
                "revision_id": "revision:xyz",
                "source_artifact_id": "note:test",
                "artifact_sha256": "a" * 64,
                "artifact_byte_length": 5,
                "artifact_content_type": "text/html; charset=utf-8",
            }

        def fetchall(self) -> list[dict[str, Any]]:
            return []

        def __enter__(self) -> _FakeCur:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    class _FakeConn:
        def cursor(self) -> _FakeCur:
            return _FakeCur()

        def __enter__(self) -> _FakeConn:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr("llm_service.services._db.psycopg.connect", lambda *_a, **_k: _FakeConn())
    monkeypatch.setattr(library_service, "_put_s3_bytes", lambda *_a: ("documents/p1/aa/hash", "a" * 64, 5))

    principal = auth_service.PrincipalRecord(principal_id="principal:1", tenant_id="tenant:1", principal_kind="member")
    ctx = auth_service.access_context_for(principal)
    created = library_service.create_document(ctx, source_artifact_id="note:test", content_bytes=b"hello")
    assert created.document.document_id == "document:abc"
    assert any("truealpha.tenant_id" in sql and params == ("tenant:1",) for sql, params in executed)
    assert any("truealpha.principal_id" in sql and params == ("principal:1",) for sql, params in executed)


def test_unified_card_export_prefers_governed_artifact_and_persists_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixtures_dir = REPO_ROOT / "common/runtime/tests/fixtures"
    import sys

    sys.path.insert(0, str(fixtures_dir))
    from strategy_run_fixture import FixtureStrategyRunRepository  # type: ignore[import-not-found]
    from truealpha_runtime.research_report_mart import MartResearchReadRepository

    class _FakeModules:
        def supply_chain_exposure(self, **_k: Any) -> None:
            return None

        def analyst_ratings(self, **_k: Any) -> None:
            return None

    fake_repo = MartResearchReadRepository(
        strategy_repository=FixtureStrategyRunRepository(),
        issuer_modules=_FakeModules(),
    )
    principal = auth_service.PrincipalRecord(principal_id="principal:1", tenant_id="tenant:1", principal_kind="member")
    monkeypatch.setattr(auth_service, "resolve_principal_from_token", lambda *_a, **_k: principal)
    monkeypatch.setattr(library_service, "_lookup_governed_card_artifact", lambda **_k: None)
    monkeypatch.setattr(
        library_service,
        "create_document",
        lambda *_a, **_k: library_service.CreateDocumentResponse(
            document=library_service.DocumentDto(
                document_id="document:card-saved",
                tenant_id="tenant:1",
                owner_principal_id="principal:1",
                created_at="2026-07-01T00:00:00Z",
            ),
            revision=library_service.DocumentRevisionDto(
                revision_id="revision:card-saved",
                document_id="document:card-saved",
                tenant_id="tenant:1",
                owner_principal_id="principal:1",
                source_artifact_id="card:test",
                artifact_sha256="b" * 64,
                artifact_byte_length=100,
                artifact_content_type="text/html; charset=utf-8",
                created_at="2026-07-01T00:00:00Z",
            ),
        ),
    )

    resp = library_service.export_or_render_card(
        auth_service.access_context_for(principal),
        library_service.CardExportRequest(
            card_kind=library_service.CardKind.COMPANY,
            issuer_id="issuer:nvda",
            cutoff="2026-06-30T23:59:59Z",
            format="html",
            persist=True,
        ),
        research_repo=fake_repo,
    )
    assert resp.card_id.startswith("card:")
    assert resp.render_mode == "cold_verdict_html"
    assert resp.document_id == "document:card-saved"
    assert resp.rendered_html and "<!DOCTYPE html>" in resp.rendered_html

    # Also test HTTP route wiring with admin access check.
    monkeypatch.setattr(admin_service, "get_ops_overview", lambda **_k: admin_service.OpsOverviewResponse(runs=[]))
    denied = TestClient(app).get("/api/v1/admin/ops")
    assert denied.status_code == 403


@pytest.mark.criterion("R5.E9")
def test_real_postgres_auth_admin_ledger_documents_rls_and_served_head_withholding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real-Postgres gate (#1716): auth login/session, ledger coverage SQL, documents RLS, and fail-closed served_head."""
    import uuid

    import psycopg
    from llm_service.config import settings
    from truealpha_runtime.testing import isolated_test_database

    with isolated_test_database("api_v1_e2e") as db:
        db_url = str(db)
        monkeypatch.setattr(settings, "database_url", db_url)
        monkeypatch.setattr(settings, "app_env", "ci")
        monkeypatch.setattr(settings, "secret_key", SecretStr("test-session-secret-for-ci"))
        pw_hash = bcrypt.hashpw(b"correct-horse-battery", bcrypt.gensalt(rounds=4)).decode("ascii")
        stored_blobs: dict[str, bytes] = {}
        monkeypatch.setattr(library_service, "create_s3_client", lambda *_a, **_k: object())

        def _put_blob(_c: object, *, bucket: str, key: str, data: bytes, **_k: object) -> dict[str, object]:
            stored_blobs[f"{bucket}/{key}"] = data
            return {}

        monkeypatch.setattr(library_service, "put_object_bytes", _put_blob)
        monkeypatch.setattr(
            library_service,
            "read_object_bytes",
            lambda _c, *, bucket, key: stored_blobs[f"{bucket}/{key}"],
        )

        with psycopg.connect(db_url) as conn:
            conn.execute("insert into app.tenants (tenant_id) values ('tenant:t1') on conflict do nothing")
            conn.execute(
                "insert into app.principals (principal_id, tenant_id, principal_kind) values "
                "('principal:adm', 'tenant:t1', 'administrator'), ('principal:mem', 'tenant:t1', 'member')"
            )
            conn.execute(
                "insert into app.principal_credentials (principal_id, email, hashed_password) values "
                "('principal:adm', 'adm@example.com', %s), ('principal:mem', 'mem@example.com', %s)",
                (pw_hash, pw_hash),
            )
            # Ledger rows testing by_reason + keys_twice + run_share
            for idx, (fk, rr, rk) in enumerate(
                [("k1", None, "r1"), ("k1", None, "r1"), ("k2", "probe", None), ("k3", "probe", None)]
            ):
                conn.execute(
                    "insert into staging.api_call_ledger (source, endpoint, called_at, caller, ok, fetch_key, repeat_reason, run_key) "
                    "values ('sec', 'companyfacts', (date_trunc('day', now() at time zone 'UTC') at time zone 'UTC') + (%s * interval '1 minute'), 'test', true, %s, %s, %s)",
                    (idx, fk, rr, rk),
                )
            # Strategy decisions bound to older available capture vs newer unavailable capture pointer
            issuer = str(uuid.uuid4())
            old_run, new_run = f"strategy-run:{'1' * 64}", f"strategy-run:{'2' * 64}"
            camp_payload = {
                "campaign_policy_id": "test.policy:v1",
                "environment": "local_test",
                "cutoff": "2026-07-01T00:00:00Z",
                "universe_refs": [
                    {
                        "content_sha256": "d" * 64,
                        "universe_id": "universe:topt-us-2026-03-31",
                        "universe_version": "2026-03-31",
                    }
                ],
            }
            camp_row = conn.execute(
                "select 'capture-campaign:' || raw.canonical_sha256(jsonb_build_object('kind', 'capture-campaign', 'identity', %s::jsonb)), "
                "raw.canonical_sha256(%s::jsonb)",
                (json.dumps(camp_payload), json.dumps(camp_payload)),
            ).fetchone()
            assert camp_row is not None
            camp_id, camp_sha = camp_row
            conn.execute(
                "insert into raw.capture_campaigns (campaign_id, content_sha256, policy_id, environment, cutoff, cutoff_canonical, universe_refs) "
                "values (%s, %s, 'test.policy:v1', 'local_test', '2026-07-01T00:00:00Z', '2026-07-01T00:00:00Z', %s::jsonb)",
                (camp_id, camp_sha, json.dumps(camp_payload["universe_refs"])),
            )
            sp_id, scope_id = f"schedule-policy:{'e' * 64}", f"capture-scope:{'f' * 64}"
            conn.execute(
                "insert into raw.capture_schedule_policies "
                "(schedule_policy_id, content_sha256, policy_version, demanded_cadence, provider_availability_cadence, freshness_max_age, retry_policy, payload) "
                "values (%s, %s, 'test-policy:v1', interval '1 day', 'manual-only:v1', interval '30 days', '{}'::jsonb, '{}'::jsonb)",
                (sp_id, "e" * 64),
            )
            caps: list[str] = []
            for seq, (srun, ssha, cut) in enumerate(
                [
                    (old_run, "1" * 64, "2026-06-01T00:00:00Z"),
                    (new_run, "2" * 64, "2026-07-01T00:00:00Z"),
                ],
                start=1,
            ):
                run_payload = {
                    "campaign_id": camp_id,
                    "run_sequence": seq,
                    "schedule_policy_id": sp_id,
                    "capture_scope_id": scope_id,
                }
                cap_row = conn.execute(
                    "select 'capture-run:' || raw.canonical_sha256(jsonb_build_object('kind', 'capture-run', 'identity', %s::jsonb)), "
                    "raw.canonical_sha256(%s::jsonb)",
                    (json.dumps(run_payload), json.dumps(run_payload)),
                ).fetchone()
                assert cap_row is not None
                cid, csha = cap_row
                caps.append(cid)
                conn.execute(
                    "insert into staging.evidence_nodes (node_id, kind, content_sha256, valid_from, transaction_time, recorded_at) "
                    "values (%s, 'capture_run', %s, '2026-03-31', now(), now())",
                    (cid, cid.split(":", 1)[1]),
                )
                conn.execute(
                    "insert into raw.capture_runs (run_id, campaign_id, run_sequence, schedule_policy_id, capture_scope_id, content_sha256) "
                    "values (%s, %s, %s, %s, %s, %s)",
                    (cid, camp_id, seq, sp_id, scope_id, csha),
                )
                conn.execute(
                    "insert into mart.strategy_runs (strategy_run_id, content_sha256, strategy_key, strategy_version, "
                    "definition_content_sha256, corpus_sha256, claim_ceiling, executed_at) "
                    "values (%s, %s, 'large_model_value_v0', '1.0.0', %s, %s, 'preview', %s)",
                    (srun, ssha, "a" * 64, "b" * 64, cut),
                )
                conn.execute(
                    "insert into mart.strategy_run_capture_bindings (capture_run_id, strategy_run_id, bound_by) values (%s, %s, 'tick')",
                    (cid, srun),
                )
                conn.execute(
                    "insert into mart.strategy_decisions (strategy_decision_id, content_sha256, strategy_run_id, issuer_id, "
                    "cutoff_at, capital_adjusted_labor_efficiency, tier, current_price_to_sales, target_price_to_sales, "
                    "valuation_gap, eligible, outcome, rank, target_weight) "
                    "values (%s, %s, %s, %s, %s, '10', 'tech', '2', '4', '0.5', true, 'selected', 1, '1.0')",
                    (f"strategy-decision:{ssha}", ssha, srun, issuer, cut),
                )
            old_cap, new_cap = caps
            # Set current_pointer to stale (>30d -> availability='unavailable') new_cap for both strategy and fund_consolidation
            conn.execute(
                "insert into mart.current_pointer (pointer_id, content_sha256, environment, universe_id, "
                "universe_version, factor_id, target_run_id, sequence, previous_run_id, advanced_at) "
                "values (%s, %s, (select environment from mart.environment_identity), 'universe:topt-us-2026-03-31', "
                "'v1', 'f', %s, 1, %s, now() - interval '40 days'), "
                "(%s, %s, (select environment from mart.environment_identity), 'universe:qqq-us-2026-03-31', "
                "'v1', 'f', %s, 1, %s, now() - interval '40 days')",
                (
                    f"current-pointer:{'9' * 64}",
                    "9" * 64,
                    new_cap,
                    old_cap,
                    f"current-pointer:{'8' * 64}",
                    "8" * 64,
                    new_cap,
                    old_cap,
                ),
            )
            conn.execute(
                "insert into mart.fund_virtual_company (run_id, fund_id, fund_name, report_period, transaction_time, "
                "cutoff, definition_version, definition_sha256, weighted_valuation_gap, valuation_basis, "
                "total_weight_pct, resolved_weight_pct, valued_weight_pct, lines, valued_lines, confidence, "
                "reason_codes, availability_status, source_evidence_status, factor_validation_status) "
                "values (%s, 'etf:series:ZZCI', 'CI Fund', '2026-06-30', '2026-06-01T00:00:00Z', '2026-06-01T00:00:00Z', 'v0', "
                "%s, '0.4200', 'price_to_gross_profit', '99', '99', '90', 2, 2, '0.9', '{}', 'available', 'verified', 'not_evaluated')",
                (old_cap, "c" * 64),
            )
            conn.execute(
                "insert into mart.issuer_theme_purity (run_id, issuer_id, cik, theme_id, theme, definition_version, definition_sha256, "
                "cutoff, period_end, partition_id, theme_share, consolidated_revenue, in_theme_revenue, out_of_theme_revenue, "
                "unclassified_revenue, partition_residual, segments, confidence, reason_codes, extractor, availability_status, "
                "source_evidence_status, factor_validation_status) "
                "values (%s, %s, 4242, 'ai-infra', 'AI-INFRA', 'v0', %s, '2026-06-01T00:00:00Z', '2025-12-31', %s, '0.5', '100', '50', '50', '0', '0', "
                "2, '0.9', '{}', 'rule:test', 'available', 'verified', 'not_evaluated')",
                (old_cap, issuer, "a" * 64, f"segment-partition:{'b' * 64}"),
            )
            conn.commit()

        # Unavailable heads must withhold strategy, theme, and ETF outputs without falling back to old_cap
        withheld = entities_service.get_company_profile_payload(issuer_id=issuer, database_url=db_url)
        assert withheld["strategy_decision"] is None and withheld["theme_purity"] == []
        assert entities_service.get_theme_purity_leaderboard(theme_id="ai-infra", database_url=db_url)["count"] == 0
        assert (
            entities_service.get_etf_profile_payload(fund_id="etf:series:ZZCI", database_url=db_url)[
                "weighted_valuation_gap"
            ]
            is None
        )

        client = TestClient(app)
        login_res = client.post(
            "/api/v1/auth/login", json={"email": "adm@example.com", "password": "correct-horse-battery"}
        )
        assert login_res.status_code == 200
        assert client.get("/api/v1/auth/session").json()["principal_id"] == "principal:adm"

        dh = client.get("/api/v1/admin/datahub")
        assert dh.status_code == 200
        dh_json = dh.json()
        assert "capture_runs" in dh_json and "runs" not in dh_json
        assert "heads" not in dh_json and "served_heads" in dh_json and "nightly_verdicts" in dh_json
        cov = dh_json["ledger_coverage"][0]
        assert cov["by_reason"] == {"probe": 2} and cov["keys_twice"] == 1

        ops = client.get("/api/v1/admin/ops")
        assert ops.status_code == 200
        ops_json = ops.json()
        assert "pointers" not in ops_json and "quota_today" not in ops_json

        created = client.post(
            "/api/v1/library/documents", json={"source_artifact_id": "note:rls", "content": "rls-body"}
        )
        assert created.status_code == 201
        doc_id = created.json()["document"]["document_id"]
        assert any(d["document_id"] == doc_id for d in client.get("/api/v1/library/documents").json()["documents"])

        # Switch session to member -> RLS must hide adm's document
        client.post("/api/v1/auth/logout")
        assert (
            client.post(
                "/api/v1/auth/login", json={"email": "mem@example.com", "password": "correct-horse-battery"}
            ).status_code
            == 200
        )
        assert not any(d["document_id"] == doc_id for d in client.get("/api/v1/library/documents").json()["documents"])
        assert client.get("/api/v1/admin/datahub").status_code == 403


def test_entity_detail_resolves_candidates_once_and_avoids_duplicate_strategy_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import inspect

    page_calls = 0
    identity_resolve_calls = 0
    mart_conn_calls = 0
    executed_sqls: list[str] = []

    def _fake_page(*_a: Any, **_k: Any) -> strategy_service.StrategyRunPageResponse:
        nonlocal page_calls
        page_calls += 1
        raise AssertionError("get_entity_detail must not load the whole strategy pool via get_strategy_run_page")

    class _RecordingCur:
        def __init__(self) -> None:
            self._last_sql = ""

        def execute(self, sql: str, _params: Any = None) -> None:
            self._last_sql = sql
            executed_sqls.append(sql)

        def fetchall(self) -> list[dict[str, Any]]:
            if "from mart.entity_display_resolution" in self._last_sql:
                return [{"issuer_id": "issuer:cik:0000723125", "ticker": "MU", "display_name": "Micron"}]
            return []

        def fetchone(self) -> dict[str, Any] | None:
            if "from mart.strategy_decisions" in self._last_sql:
                return {
                    "issuer_id": "11111111-1111-1111-1111-111111111111",
                    "strategy_run_id": "strategy-run:1",
                    "corpus_sha256": "a" * 64,
                    "cutoff_at": "2026-06-30T00:00:00Z",
                    "eligible": True,
                    "tier": "high",
                    "capital_adjusted_labor_efficiency": "120000",
                    "labor_efficiency_metric": "gppe_uniform_charge_v0",
                    "valuation_gap": "0.15",
                    "current_price_to_sales": None,
                    "target_price_to_sales": None,
                    "current_price_to_gross_profit": "4.0",
                    "target_price_to_gross_profit": "4.6",
                    "outcome": "eligible",
                    "exclusion_reason": None,
                    "rank": 1,
                    "target_weight": "0.05",
                    "peg": "1.2",
                    "peg_rank": 1,
                    "peg_reason_codes": [],
                    "availability_status": "available",
                    "confidence": "0.9",
                    "reason_codes": [],
                }
            return None

    def _fake_with_mart(fn: Any, **_k: Any) -> Any:
        nonlocal mart_conn_calls
        mart_conn_calls += 1
        return fn(_RecordingCur())

    def _fake_identity(_cur: Any, iid: str) -> tuple[list[str], dict[str, str]]:
        nonlocal identity_resolve_calls
        identity_resolve_calls += 1
        uuid_str = "11111111-1111-1111-1111-111111111111"
        return [iid, uuid_str], {uuid_str: "issuer:cik:0000723125"}

    monkeypatch.setattr(entities_service, "get_strategy_run_page", _fake_page)
    monkeypatch.setattr(entities_service, "with_mart_readonly", _fake_with_mart)
    monkeypatch.setattr(entities_service, "resolve_issuer_identity_from_cursor", _fake_identity)

    ctx = auth_service.access_context_for(
        auth_service.PrincipalRecord(principal_id="p:1", tenant_id="t:1", principal_kind="member")
    )
    res = entities_service.get_entity_detail(ctx, issuer_id="issuer:cik:0000723125", database_url="postgresql://x")
    assert res.label == "MU · Micron"
    assert len(res.decisions) == 1
    assert res.decisions[0].issuer_id == "issuer:cik:0000723125"
    assert res.decisions[0].valuation_basis == "price_to_gross_profit"
    assert page_calls == 0
    assert mart_conn_calls == 1
    assert identity_resolve_calls == 1
    assert len(executed_sqls) == 6
    assert all("= any(%s::text[])" in sql for sql in executed_sqls)
    assert not any("in (select ei." in sql for sql in executed_sqls)
    assert not any("mart.entity_identity" in sql for sql in executed_sqls)
    assert "recorded_at" not in inspect.getsource(entities_service)


def test_company_profile_reader_resolves_legacy_id_without_lateral_entity_identity_join(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolve_calls = 0
    executed_sqls: list[str] = []

    class _RecordingCur:
        def __init__(self) -> None:
            self._last_sql = ""

        def execute(self, sql: str, _params: Any = None) -> None:
            self._last_sql = sql
            executed_sqls.append(sql)

        def fetchone(self) -> dict[str, Any] | None:
            if "from mart.strategy_decisions" in self._last_sql:
                return {
                    "issuer_id": "11111111-1111-1111-1111-111111111111",
                    "cutoff_at": "2026-06-30T00:00:00Z",
                    "tier": "high",
                    "valuation_gap": "0.15",
                    "current_price_to_sales": None,
                    "target_price_to_sales": None,
                    "current_price_to_gross_profit": "4.0",
                    "target_price_to_gross_profit": "4.6",
                    "outcome": "eligible",
                    "exclusion_reason": None,
                    "rank": 1,
                    "peg": "1.2",
                    "peg_rank": 1,
                    "peg_reason_codes": [],
                    "confidence": "0.9",
                    "reason_codes": [],
                }
            return None

        def fetchall(self) -> list[Any]:
            return []

    def _fake_identity(_cur: Any, iid: str) -> tuple[list[str], dict[str, str]]:
        nonlocal resolve_calls
        resolve_calls += 1
        uuid_str = "11111111-1111-1111-1111-111111111111"
        return [iid, uuid_str, "issuer:cik:0001045810"], {uuid_str: "issuer:cik:0001045810"}

    monkeypatch.setattr(entities_service, "with_mart_readonly", lambda fn, **_k: fn(_RecordingCur()))
    monkeypatch.setattr(entities_service, "resolve_issuer_identity_from_cursor", _fake_identity)

    profile = entities_service.get_company_profile_payload(issuer_id="NVDA", database_url="postgresql://x")
    assert resolve_calls == 1
    assert profile["issuer_id"] == "issuer:cik:0001045810"
    assert profile["tier"] == "high"
    assert len(executed_sqls) == 3
    assert all("= any(%s::text[])" in sql and "mart.entity_identity" not in sql for sql in executed_sqls)


def test_issuer_module_reader_resolves_candidates_once_per_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    from truealpha_runtime import issuer_modules_mart

    batch_calls = 0
    module_sqls: list[str] = []

    def _fake_batch(_db_url: str, ids: list[str] | tuple[str, ...]) -> dict[str, tuple[str, ...]]:
        nonlocal batch_calls
        batch_calls += 1
        return {str(i): (str(i), "uuid-1") for i in ids}

    def _fake_query_rows(_db_url: str, sql: str, params: Any = ()) -> list[dict[str, Any]]:
        module_sqls.append(sql)
        assert params == (["issuer:cik:0000723125", "uuid-1"],)
        return []

    monkeypatch.setattr(issuer_modules_mart, "resolve_issuer_id_candidates_batch", _fake_batch)
    monkeypatch.setattr(issuer_modules_mart, "_query_rows", _fake_query_rows)
    reader = issuer_modules_mart.PostgresIssuerModuleReader(database_url="postgresql://x")
    assert reader.supply_chain_exposure("issuer:cik:0000723125") is None
    assert reader.analyst_ratings("issuer:cik:0000723125") is None
    assert batch_calls == 1
    assert len(module_sqls) == 2
    assert all("= any(%s::text[])" in sql and "mart.entity_identity" not in sql for sql in module_sqls)
