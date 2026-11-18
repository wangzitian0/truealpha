"""Domain 6 (`auth`) HTTP + MCP BearerTokenGate behavioral tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import bcrypt
import pytest
from fastapi.testclient import TestClient
from llm_service.config import Settings
from llm_service.main import app
from llm_service.services import auth_service
from pydantic import SecretStr


def test_auth_login_session_logout_and_rate_limiter(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [100.0]
    limiter = auth_service.FixedWindowRateLimiter(2, 60, now=lambda: clock[0])
    assert limiter.attempt("1.2.3.4") is True
    assert limiter.attempt("1.2.3.4") is True
    assert limiter.attempt("1.2.3.4") is False
    clock[0] += 61.0
    assert limiter.attempt("1.2.3.4") is True

    test_settings = Settings(
        _env_file=None,
        app_env="staging",
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
    monkeypatch.setattr(auth_service, "login_rate_limiter", auth_service.FixedWindowRateLimiter(3, 60))

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
