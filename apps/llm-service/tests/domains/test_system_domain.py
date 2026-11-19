"""Domain 5 (`system`) OpenAPI probes and MCP mount boundary behavioral tests."""

from __future__ import annotations

import warnings
from typing import Any

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


def test_system_config_uses_app_service_login_and_manifest_matches() -> None:
    import json
    from pathlib import Path

    from llm_service.config import Settings

    db_extra = Settings.model_fields["database_url"].json_schema_extra
    assert isinstance(db_extra, dict)
    composed = str(db_extra.get("composed_from", ""))
    assert composed.startswith("postgresql://app_service_login:{APP_SERVICE_DB_PASSWORD}@")
    assert "postgresql://postgres:" not in composed

    manifest = json.loads(
        (Path(__file__).resolve().parents[2] / "required-env.generated.json").read_text(encoding="utf-8")
    )
    fields_by_env = {f["env"]: f for f in manifest["fields"]}
    assert fields_by_env["DATABASE_URL"]["composed_from"] == composed
    assert fields_by_env["MIGRATIONS_DATABASE_URL"]["composed_from"].startswith("postgresql://postgres:")


def test_data_engine_facts_avoids_staging_and_does_not_swallow_bugs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import psycopg

    # 1. Unexpected non-psycopg bugs must surface instead of being silenced by `except Exception: pass`.
    monkeypatch.setattr(psycopg, "connect", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("unexpected_bug")))
    with pytest.raises(RuntimeError, match="unexpected_bug"):
        main_mod._data_engine_facts()

    # 2. DB connectivity error degrades `/health` honestly without leaking password in logs.
    monkeypatch.setattr(
        main_mod.settings,
        "database_url",
        "postgresql://app_service_login:SuperSecret99@db:5432/truealpha",
    )
    monkeypatch.setattr(
        psycopg,
        "connect",
        lambda *_a, **_k: (_ for _ in ()).throw(
            psycopg.OperationalError("failed: postgresql://app_service_login:SuperSecret99@db:5432/truealpha")
        ),
    )
    caplog.set_level("WARNING")
    facts = main_mod._data_engine_facts()
    assert facts == ("unknown", "unknown", "unknown", "unknown", "unknown")
    assert "SuperSecret99" not in caplog.text

    # 3. Normal query path never reads staging.*.
    executed_sql: list[str] = []

    class _FakeRows:
        def fetchone(self) -> None:
            return None

        def fetchall(self) -> list[Any]:
            return []

    class _FakeConn:
        def __enter__(self) -> _FakeConn:
            return self

        def __exit__(self, *_a: Any) -> bool:
            return False

        def execute(self, sql: str, *_a: Any) -> _FakeRows:
            executed_sql.append(sql)
            return _FakeRows()

        def rollback(self) -> None:
            return None

    monkeypatch.setattr(psycopg, "connect", lambda *_a, **_k: _FakeConn())
    main_mod._data_engine_facts()
    assert executed_sql and not any("staging." in q for q in executed_sql)
