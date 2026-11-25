"""Domain 6 (`auth`) HTTP + MCP BearerTokenGate behavioral tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
import pytest
from fastapi.testclient import TestClient
from llm_service import main as main_mod
from llm_service.config import Settings
from llm_service.main import app
from llm_service.services import _db, auth_service
from pydantic import SecretStr
from truealpha_runtime.access import AuthenticationMethod, PrincipalKind

pytestmark = pytest.mark.gate


def test_auth_login_session_logout_and_rate_limiter(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [100.0]
    limiter = auth_service.LoginRateLimiter(2, 60, now=lambda: clock[0])
    assert limiter.attempt("1.2.3.4") is True
    clock[0] += 30.0
    assert limiter.attempt("1.2.3.4") is True
    assert limiter.attempt("1.2.3.4") is False
    # Sliding window: first attempt at 100s expires at 161s, second at 130s remains counted.
    clock[0] = 161.0
    assert limiter.attempt("1.2.3.4") is True
    assert limiter.attempt("1.2.3.4") is False

    for env in ("dev", "local_dev", "test", "ci"):
        monkeypatch.setattr(
            auth_service, "settings", Settings(_env_file=None, environment=env, secret_key=SecretStr(""))
        )
        assert auth_service.resolve_signing_secret()
    for env in ("preview", "staging", "production"):
        monkeypatch.setattr(
            auth_service, "settings", Settings(_env_file=None, environment=env, secret_key=SecretStr(""))
        )
        with pytest.raises(auth_service.AuthConfigurationError):
            auth_service.resolve_signing_secret()

    test_settings = Settings(
        _env_file=None,
        environment="staging",
        secret_key=SecretStr("unit-test-signing-key-32-chars-minimum"),
    )
    monkeypatch.setattr(auth_service, "settings", test_settings)
    pw_hash = bcrypt.hashpw(b"CorrectHorseBatteryStaple", bcrypt.gensalt(rounds=4)).decode("ascii")

    monkeypatch.setattr(
        auth_service,
        "_lookup_credential_by_email",
        lambda email: ("principal:1", "tenant:1", "member", pw_hash) if email == "alice@example.com" else None,
    )
    monkeypatch.setattr(
        auth_service,
        "_lookup_principal",
        lambda pid, tid: (
            auth_service.PrincipalRecord(principal_id=pid, tenant_id=tid, principal_kind="member")
            if pid == "principal:1"
            else None
        ),
    )
    monkeypatch.setattr(auth_service, "login_rate_limiter", auth_service.LoginRateLimiter(3, 60))

    client = TestClient(app, base_url="https://testserver")
    assert client.get("/api/v1/auth/session").json() == {
        "authenticated": False,
        "principal_id": None,
        "tenant_id": None,
        "principal_kind": None,
    }
    bad = client.post("/api/v1/auth/login", json={"email": "alice@example.com", "password": "wrong"})
    assert bad.status_code == 401

    ok = client.post(
        "/api/v1/auth/login", json={"email": "  ALICE@Example.com ", "password": "CorrectHorseBatteryStaple"}
    )
    assert ok.status_code == 200 and ok.json()["principal_id"] == "principal:1"
    assert auth_service.SESSION_COOKIE_NAME in ok.cookies

    sess = client.get("/api/v1/auth/session")
    assert sess.status_code == 200
    assert sess.json() == {
        "authenticated": True,
        "principal_id": "principal:1",
        "tenant_id": "tenant:1",
        "principal_kind": "member",
    }

    out = client.post("/api/v1/auth/logout")
    assert out.status_code == 200 and out.json() == {"ok": True}

    expired = auth_service.sign_session_token(
        principal_id="principal:1",
        tenant_id="tenant:1",
        secret=auth_service.resolve_signing_secret(),
        expire_minutes=-1,
        now=datetime.now(UTC) - timedelta(minutes=5),
    )
    assert auth_service.verify_session_token(expired, secret=auth_service.resolve_signing_secret()) is None


def test_mcp_access_context_and_reenterable_lifespan(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = auth_service.resolve_mcp_access_context()
    assert ctx.authentication_method is AuthenticationMethod.DELEGATED_MCP
    assert ctx.principal_kind is PrincipalKind.ADMINISTRATOR
    assert ctx.principal_id == "principal:owner"
    assert ctx.delegated_by_service_principal_id == "principal:llm-service-mcp"

    monkeypatch.setattr(main_mod, "refuse_to_boot_on_a_missing_environment", lambda: None)
    monkeypatch.setattr(main_mod.settings, "mcp_bearer_token", SecretStr("mcp-lifespan-secret"))
    headers = {"Accept": "application/json, text/event-stream", "Authorization": "Bearer mcp-lifespan-secret"}
    init_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "auth-domain-test", "version": "1"},
        },
    }
    for _ in range(2):
        with TestClient(app, client=("127.0.0.1", 50000)) as client:
            unauth = client.post("/mcp/", headers={"Accept": "application/json, text/event-stream"}, json=init_body)
            assert unauth.status_code == 401 and unauth.headers["www-authenticate"].startswith("Bearer ")
            ok = client.post("/mcp/", headers=headers, json=init_body)
            assert ok.status_code == 200 and ok.headers.get("mcp-session-id")


def test_db_statement_timeout_and_error_redaction(monkeypatch: pytest.MonkeyPatch) -> None:
    executed: list[tuple[str, Any]] = []

    class _Cur:
        def execute(self, sql: str, params: Any = None) -> None:
            executed.append((sql, params))

        def __enter__(self) -> _Cur:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    class _Conn:
        def cursor(self) -> _Cur:
            return _Cur()

        def __enter__(self) -> _Conn:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(_db.psycopg, "connect", lambda *_a, **_k: _Conn())
    _db.with_app_runtime(lambda _cur: None)
    assert ("select set_config('statement_timeout', %s, true)", ("5s",)) in executed

    from truealpha_runtime.checks import redact_postgres_exception

    redacted = redact_postgres_exception(
        RuntimeError("connection failed: postgresql://app_service_login:SuperSecretPass123@db:5432/truealpha"),
        database_url="postgresql://app_service_login:SuperSecretPass123@db:5432/truealpha",
    )
    assert "SuperSecretPass123" not in redacted
