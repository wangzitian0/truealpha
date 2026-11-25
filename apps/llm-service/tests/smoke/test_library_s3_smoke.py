"""Real RustFS S3 + Postgres RLS document lifecycle smoke test (#1803)."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest
from fastapi.testclient import TestClient
from infra2_sdk.runtime.s3 import create_s3_client, ensure_bucket, read_object_bytes
from llm_service.config import settings
from llm_service.main import app
from llm_service.mcp_server import build_mcp_server
from llm_service.services import library_service
from truealpha_runtime.testing import skip_or_fail

from ._smoke_support import (
    MEMBER_EMAIL,
    OWNER_A_PRINCIPAL_ID,
    OWNER_B_EMAIL,
    call_mcp_tool,
    smoke_app_service_db,
)

pytestmark = [pytest.mark.deps, pytest.mark.smoke]


def _ensure_smoke_s3_bucket() -> Any:
    s3_settings = settings.to_s3_settings()
    if not (s3_settings.endpoint_url or "").strip():
        skip_or_fail("S3_ENDPOINT is unset; CI runs the live S3 smoke test")
    client = create_s3_client(s3_settings)
    try:
        ensure_bucket(s3_settings, client=client, allow_create=True)
    except Exception as exc:  # noqa: BLE001
        skip_or_fail(f"no reachable object storage; CI runs the integration tests ({exc})")
    return client


def test_library_documents_s3_roundtrip_and_rls_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify real S3 upload/readback, Postgres RLS owner isolation, tombstone delete, and MCP document flow."""
    library_service._s3_client_cache = None
    try:
        with smoke_app_service_db(monkeypatch, "llm_s3_smoke") as ctx:
            s3_client = _ensure_smoke_s3_bucket()
            body_text = "Real RustFS S3 and Postgres RLS smoke document body."
            body_bytes = body_text.encode("utf-8")
            expected_sha = hashlib.sha256(body_bytes).hexdigest()

            with TestClient(app) as client_a, TestClient(app) as client_b:
                login_a = client_a.post("/api/v1/auth/login", json={"email": MEMBER_EMAIL, "password": ctx.password})
                assert login_a.status_code == 200, login_a.text
                login_b = client_b.post("/api/v1/auth/login", json={"email": OWNER_B_EMAIL, "password": ctx.password})
                assert login_b.status_code == 200, login_b.text

                # 1. Owner A uploads document -> S3 + Postgres revision
                created = client_a.post(
                    "/api/v1/library/documents",
                    json={
                        "source_artifact_id": "report:smoke-s3-1",
                        "content_type": "text/plain; charset=utf-8",
                        "content": body_text,
                    },
                )
                assert created.status_code == 201, created.text
                created_payload = created.json()
                document_id = created_payload["document"]["document_id"]
                revision = created_payload["revision"]
                assert revision["artifact_sha256"] == expected_sha
                assert revision["artifact_byte_length"] == len(body_bytes)

                # 2. Owner A can list and fetch content back from S3
                listed_a = client_a.get("/api/v1/library/documents").json()
                assert any(doc["document_id"] == document_id for doc in listed_a["documents"])
                detail_a = client_a.get(f"/api/v1/library/documents/{document_id}")
                assert detail_a.status_code == 200, detail_a.text
                assert detail_a.json()["latest_content"] == body_text

                # 3. Direct RustFS S3 readback matches stored bytes
                prefix = settings.s3_documents_prefix.strip("/") or "documents"
                object_key = f"{prefix}/{OWNER_A_PRINCIPAL_ID}/{expected_sha[:2]}/{expected_sha}"
                assert read_object_bytes(s3_client, bucket=settings.s3_bucket, key=object_key) == body_bytes

                # 4. Owner B cannot see or read Owner A's document (real Postgres RLS isolation)
                listed_b = client_b.get("/api/v1/library/documents").json()
                assert not any(doc["document_id"] == document_id for doc in listed_b["documents"])
                assert client_b.get(f"/api/v1/library/documents/{document_id}").status_code == 404

                # 5. Owner A deletes -> tombstoned in Postgres while immutable S3 blob persists
                deleted = client_a.delete(f"/api/v1/library/documents/{document_id}")
                assert deleted.status_code == 200, deleted.text
                assert deleted.json()["document_id"] == document_id
                listed_after = client_a.get("/api/v1/library/documents").json()
                assert not any(doc["document_id"] == document_id for doc in listed_after["documents"])
                assert client_a.get(f"/api/v1/library/documents/{document_id}").status_code == 404
                assert read_object_bytes(s3_client, bucket=settings.s3_bucket, key=object_key) == body_bytes

            # 6. MCP create + detail round-trip on real Postgres + S3
            mcp = build_mcp_server()
            mcp_content = "MCP real Postgres and S3 smoke content."
            mcp_created = call_mcp_tool(
                mcp,
                "library_document_create",
                {
                    "request": {
                        "source_artifact_id": "report:mcp-smoke-s3-1",
                        "content": mcp_content,
                    }
                },
            )
            mcp_doc_id = mcp_created["document"]["document_id"]
            mcp_detail = call_mcp_tool(mcp, "library_document_detail", {"request": {"document_id": mcp_doc_id}})
            assert mcp_detail["latest_content"] == mcp_content
    finally:
        library_service._s3_client_cache = None
