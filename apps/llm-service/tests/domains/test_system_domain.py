"""Domain 5 (`system`) OpenAPI probes and MCP mount boundary behavioral tests."""

from __future__ import annotations

import warnings

import pytest
from fastapi.testclient import TestClient
from llm_service import main as main_mod
from llm_service.main import app


def test_system_probes_and_openapi_surface(monkeypatch: pytest.MonkeyPatch) -> None:
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("error")
        app.openapi_schema = None
        schema = app.openapi()
    assert captured == []
    paths = schema["paths"]
    assert len(paths) == 25
    op_ids = [
        op["operationId"]
        for methods in paths.values()
        for method, op in methods.items()
        if method in {"get", "post", "put", "delete", "patch"} and isinstance(op, dict)
    ]
    assert len(op_ids) == 27 and len(op_ids) == len(set(op_ids))
    assert paths["/health"]["get"]["tags"] == ["system"]
    assert paths["/health"]["get"]["operationId"] == "system_health"
    assert paths["/livez"]["get"]["operationId"] == "system_livez"
    assert paths["/readyz"]["get"]["operationId"] == "system_readyz"

    monkeypatch.setattr(main_mod, "_data_engine_facts", lambda: ("unknown", "unknown", "unknown", "unknown", "unknown"))
    client = TestClient(app)
    assert client.get("/health").status_code == 200
    assert client.get("/livez").status_code == 200
    assert client.get("/readyz").status_code == 200
    mcp_redirect = client.get("/mcp", follow_redirects=False)
    assert mcp_redirect.status_code == 307 and mcp_redirect.headers["location"] == "/api/mcp/"
    assert client.get("/mcp/", follow_redirects=False).status_code == 401
