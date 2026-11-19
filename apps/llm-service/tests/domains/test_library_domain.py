"""Domain 4 (`library`) HTTP + MCP behavioral tests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from llm_service.main import app
from llm_service.mcp_server import build_mcp_server
from llm_service.services import library_service
from truealpha_runtime.research_report_mart import MartResearchReadRepository

from ._helpers import auth_client, load_fixture_strategy_repo


def test_library_endpoints_require_authentication() -> None:
    client = TestClient(app)
    assert client.get("/api/v1/library/reports").status_code == 401
    assert client.post("/api/v1/library/cards/export", json={}).status_code == 401
    assert client.get("/api/v1/library/documents").status_code == 401
    assert (
        client.post("/api/v1/library/documents", json={"source_artifact_id": "n:1", "content": "x"}).status_code == 401
    )
    assert client.get("/api/v1/library/documents/document:1").status_code == 401
    assert client.delete("/api/v1/library/documents/document:1").status_code == 401


@pytest.mark.anyio
async def test_library_http_and_mcp_usecases(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = library_service.DocumentDto(
        document_id="document:1",
        tenant_id="tenant:truealpha",
        owner_principal_id="principal:member",
        created_at="2026-07-01T00:00:00Z",
    )
    rev = library_service.DocumentRevisionDto(
        revision_id="revision:1",
        document_id="document:1",
        tenant_id="tenant:truealpha",
        owner_principal_id="principal:member",
        source_artifact_id="note:1",
        artifact_sha256="a" * 64,
        artifact_byte_length=5,
        artifact_content_type="text/plain; charset=utf-8",
        created_at="2026-07-01T00:00:00Z",
    )
    monkeypatch.setattr(
        "llm_service.api.v1.library.build_library_report",
        lambda *_a, **_k: library_service.LibraryReportResponse(
            report_id="report:1",
            schema_version="v1",
            format="json",
            report={"report_id": "report:1"},
        ),
    )
    monkeypatch.setattr(
        "llm_service.api.v1.library.export_or_render_card",
        lambda *_a, **_k: library_service.CardExportResponse(
            card_id="card:1",
            card_kind="ranking",
            render_mode="cold_verdict_html",
            document_id="document:1",
            rendered_html="<!DOCTYPE html><html></html>",
            card={"card_id": "card:1"},
        ),
    )
    monkeypatch.setattr(
        "llm_service.api.v1.library.list_documents",
        lambda *_a, **_k: library_service.DocumentListResponse(documents=[doc]),
    )
    monkeypatch.setattr(
        "llm_service.api.v1.library.create_document",
        lambda *_a, **_k: library_service.CreateDocumentResponse(document=doc, revision=rev),
    )
    monkeypatch.setattr(
        "llm_service.api.v1.library.get_document_detail",
        lambda *_a, document_id, **_k: (
            library_service.DocumentDetailResponse(document=doc, revisions=[rev], latest_content="hello")
            if document_id == "document:1"
            else None
        ),
    )
    monkeypatch.setattr(
        "llm_service.api.v1.library.delete_document",
        lambda *_a, document_id, **_k: (
            library_service.DeleteDocumentResponse(
                tombstone_id="tomb:1", document_id=document_id, created_at="2026-07-01T00:00:00Z"
            )
            if document_id == "document:1"
            else None
        ),
    )

    client, headers = auth_client(monkeypatch)
    rep = client.get("/api/v1/library/reports", headers=headers)
    exp = client.post("/api/v1/library/cards/export", json={"card_kind": "ranking"}, headers=headers)
    lst = client.get("/api/v1/library/documents", headers=headers)
    crt = client.post(
        "/api/v1/library/documents", json={"source_artifact_id": "note:1", "content": "hello"}, headers=headers
    )
    det = client.get("/api/v1/library/documents/document:1", headers=headers)
    det_404 = client.get("/api/v1/library/documents/document:missing", headers=headers)
    dlt = client.delete("/api/v1/library/documents/document:1", headers=headers)
    dlt_404 = client.delete("/api/v1/library/documents/document:missing", headers=headers)

    assert rep.status_code == 200 and rep.json()["report_id"] == "report:1"
    assert exp.status_code == 200 and exp.json()["card_id"] == "card:1"
    assert lst.status_code == 200 and lst.json()["documents"][0]["document_id"] == "document:1"
    assert crt.status_code == 201 and crt.json()["document"]["document_id"] == "document:1"
    assert det.status_code == 200 and det.json()["latest_content"] == "hello"
    assert det_404.status_code == 404
    assert dlt.status_code == 200 and dlt.json()["tombstone_id"] == "tomb:1"
    assert dlt_404.status_code == 404

    class _FakeModules:
        def supply_chain_exposure(self, **_k: Any) -> None:
            return None

        def analyst_ratings(self, **_k: Any) -> None:
            return None

    fake_repo = MartResearchReadRepository(
        strategy_repository=load_fixture_strategy_repo(), issuer_modules=_FakeModules()
    )
    import llm_service.mcp_server as mcp_mod

    monkeypatch.setattr(
        mcp_mod, "list_documents", lambda *_a, **_k: library_service.DocumentListResponse(documents=[doc])
    )
    monkeypatch.setattr(
        mcp_mod, "create_document", lambda *_a, **_k: library_service.CreateDocumentResponse(document=doc, revision=rev)
    )
    monkeypatch.setattr(
        mcp_mod,
        "get_document_detail",
        lambda *_a, **_k: library_service.DocumentDetailResponse(document=doc, revisions=[rev], latest_content="hello"),
    )
    monkeypatch.setattr(library_service, "_lookup_governed_card_artifact", lambda **_k: None)

    server = build_mcp_server(research_repository=fake_repo)
    cutoff = datetime(2026, 6, 30, 23, 59, 59, tzinfo=UTC).isoformat()
    _, r_mcp = await server.call_tool(
        "library_report",
        {"request": {"report_kind": "company", "target_entity_ids": ["issuer:nvda"], "cutoff_at": cutoff}},
    )  # type: ignore[misc]
    _, c_mcp = await server.call_tool(
        "library_card_export",
        {
            "request": {
                "report_kind": "company",
                "target_entity_ids": ["issuer:nvda"],
                "cutoff_at": cutoff,
                "card_kind": "company",
            }
        },
    )  # type: ignore[misc]
    _, l_mcp = await server.call_tool("library_documents_list", {})  # type: ignore[misc]
    _, dc_mcp = await server.call_tool(
        "library_document_create", {"request": {"source_artifact_id": "note:1", "content": "hello"}}
    )  # type: ignore[misc]
    _, dd_mcp = await server.call_tool("library_document_detail", {"request": {"document_id": "document:1"}})  # type: ignore[misc]

    assert r_mcp["report_id"].startswith("report:")
    assert c_mcp["card_id"].startswith("card:")
    assert l_mcp["documents"][0]["document_id"] == "document:1"
    assert dc_mcp["document"]["document_id"] == "document:1"
    assert dd_mcp["result"]["latest_content"] == "hello"


def test_library_export_persist_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeModules:
        def supply_chain_exposure(self, **_k: Any) -> None:
            return None

        def analyst_ratings(self, **_k: Any) -> None:
            return None

    fake_repo = MartResearchReadRepository(
        strategy_repository=load_fixture_strategy_repo(), issuer_modules=_FakeModules()
    )
    monkeypatch.setattr(library_service, "_lookup_governed_card_artifact", lambda **_k: None)

    def _fail_create(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("s3_down")

    monkeypatch.setattr(library_service, "create_document", _fail_create)

    from llm_service.services.auth_service import PrincipalRecord, access_context_for

    ctx = access_context_for(PrincipalRecord(principal_id="principal:1", tenant_id="tenant:1", principal_kind="member"))
    with pytest.raises(RuntimeError, match="s3_down"):
        library_service.export_or_render_card(
            ctx,
            library_service.CardExportRequest(
                card_kind=library_service.CardKind.COMPANY,
                issuer_id="issuer:nvda",
                cutoff="2026-06-30T23:59:59Z",
                format="html",
                persist=True,
            ),
            research_repo=fake_repo,
        )


def test_library_report_requires_governed_cutoff_without_wall_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    from truealpha_runtime.strategy_run import StrategyRunUnavailable

    class _UnavailableStrategyRepo:
        def get_latest(self, *, strategy_id: str, context: Any) -> StrategyRunUnavailable:
            return StrategyRunUnavailable(strategy_id=strategy_id, reason="no_runs_recorded")

    class _NoModules:
        def supply_chain_exposure(self, **_k: Any) -> None:
            return None

        def analyst_ratings(self, **_k: Any) -> None:
            return None

    unavailable_repo = MartResearchReadRepository(
        strategy_repository=_UnavailableStrategyRepo(), issuer_modules=_NoModules()
    )
    from llm_service.services.auth_service import PrincipalRecord, access_context_for

    ctx = access_context_for(PrincipalRecord(principal_id="principal:1", tenant_id="tenant:1", principal_kind="member"))
    with pytest.raises(ValueError, match="cutoff"):
        library_service.build_library_report(ctx, cutoff_at=None, research_repo=unavailable_repo)

    # Same ValueError surfaces as HTTP 422 on GET /api/v1/library/reports.
    client, headers = auth_client(monkeypatch)
    monkeypatch.setattr(
        "llm_service.api.v1.library.build_library_report",
        lambda *_a, **_k: (_ for _ in ()).throw(ValueError("governed cutoff_at is unavailable")),
    )
    bad = client.get("/api/v1/library/reports", headers=headers)
    assert bad.status_code == 422

    # When strategy decisions exist, cutoff_at=None resolves deterministically from the governed run (not now()).
    fixture_repo = MartResearchReadRepository(
        strategy_repository=load_fixture_strategy_repo(), issuer_modules=_NoModules()
    )
    resp = library_service.build_library_report(
        ctx,
        report_kind=library_service.ResearchReportKind.COMPANY,
        target_entity_ids=("issuer:nvda",),
        cutoff_at=None,
        research_repo=fixture_repo,
    )
    assert resp.report is not None and resp.report["cutoff_at"].startswith("2026-")


def test_library_get_s3_bytes_distinguishes_not_found_from_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    class _NotFoundError(Exception):
        response = {"Error": {"Code": "NoSuchKey"}}

    monkeypatch.setattr(library_service, "create_s3_client", lambda *_a, **_k: object())
    monkeypatch.setattr(
        library_service,
        "read_object_bytes",
        lambda *_a, **_k: (_ for _ in ()).throw(_NotFoundError("missing")),
    )
    assert library_service._get_s3_bytes("documents/p/missing") is None

    monkeypatch.setattr(
        library_service,
        "read_object_bytes",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("s3_network_timeout")),
    )
    with pytest.raises(RuntimeError, match="s3_network_timeout"):
        library_service._get_s3_bytes("documents/p/broken")


def test_lookup_governed_card_artifact_does_not_swallow_db_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    import psycopg

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(library_service, "with_mart_readonly", _boom)
    with pytest.raises(psycopg.OperationalError, match="connection refused"):
        library_service._lookup_governed_card_artifact(
            card_kind="ranking",
            subject_key="theme:large_model_value",
            convention="ttm",
            universe="topt",
            database_url="postgresql://unused",
        )

    captured_sql: list[str] = []

    class _FakeCur:
        def execute(self, sql: str, _params: Any) -> None:
            captured_sql.append(sql)

        def fetchone(self) -> None:
            return None

    monkeypatch.setattr(library_service, "with_mart_readonly", lambda fn, **_k: fn(_FakeCur()))
    library_service._lookup_governed_card_artifact(
        card_kind="ranking",
        subject_key="theme:large_model_value",
        convention="ttm",
        universe="topt",
        cutoff_at=datetime(2026, 6, 30, tzinfo=UTC),
        database_url="postgresql://unused",
    )
    assert len(captured_sql) == 1
    assert "join mart.served_head" in captured_sql[0]
    assert "recorded_at desc" not in captured_sql[0]
