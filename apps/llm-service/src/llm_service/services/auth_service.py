"""Cookie + HS256 JWT authentication and principal resolution over `app` schema."""

from __future__ import annotations

import hmac
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Literal

import bcrypt
import jwt
from infra2_sdk.service import RateLimitWindow
from pydantic import BaseModel
from truealpha_runtime.access import AccessContext, AuthenticationMethod, PrincipalKind

from llm_service.config import settings
from llm_service.mcp_auth import current_mcp_principal_kind
from llm_service.services._db import with_app_runtime

SESSION_COOKIE_NAME = "truealpha_session"
MCP_SERVICE_PRINCIPAL_ID = "principal:llm-service-mcp"
MCP_OWNER_PRINCIPAL_ID = "principal:owner"
MCP_READONLY_PRINCIPAL_ID = "principal:mcp-readonly"
MCP_SERVICE_TENANT_ID = "tenant:truealpha"
MCP_DELEGATION_ID = "delegation:mcp-bearer-token"
MCP_CONTEXT_LIFETIME = timedelta(minutes=5)
_JWT_ALGORITHM = "HS256"
_DUMMY_BCRYPT_HASH = bcrypt.hashpw(b"timing-equalizer-secret", bcrypt.gensalt(rounds=10)).decode("ascii")


class PrincipalRecord(BaseModel):
    principal_id: str
    tenant_id: str
    principal_kind: Literal["member", "administrator", "service"]
    email: str | None = None


class SessionResponse(BaseModel):
    authenticated: bool
    principal_id: str | None = None
    tenant_id: str | None = None
    principal_kind: Literal["member", "administrator", "service"] | None = None


class LoginRequest(BaseModel):
    email: str
    password: str


class LoginResponse(BaseModel):
    ok: bool = True
    principal_id: str
    tenant_id: str
    principal_kind: Literal["member", "administrator", "service"]


class LogoutResponse(BaseModel):
    ok: bool = True


class LoginRateLimiter:
    """Per-key sliding-window attempt limiter backed by `infra2_sdk.service.RateLimitWindow`."""

    def __init__(self, max_attempts: int = 10, window_seconds: int = 300, *, now: Callable[[], float] | None = None):
        self._limit = max_attempts
        self._window_seconds = window_seconds
        self._clock = now or time.monotonic
        self._buckets: dict[str, RateLimitWindow] = {}
        self._lock = threading.Lock()

    def attempt(self, key: str) -> bool:
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = RateLimitWindow(self._limit, self._window_seconds, clock=self._clock)
                self._buckets[key] = bucket
        allowed, _ = bucket.acquire()
        return allowed

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


login_rate_limiter = LoginRateLimiter(10, 5 * 60)


class RateLimitExceededError(Exception):
    """Raised when login rate limit is exceeded."""


class InvalidCredentialsError(Exception):
    """Raised when email or password verification fails."""


class AuthConfigurationError(RuntimeError):
    """Raised when session signing key is missing."""


def resolve_signing_secret(secret_override: str | None = None) -> str:
    secret = (secret_override if secret_override is not None else settings.secret_key.get_secret_value()).strip()
    if not secret:
        if not settings.is_deployed:
            return "truealpha-local-dev-insecure-secret-key-do-not-use-in-prod"
        raise AuthConfigurationError("SECRET_KEY must be set")
    return secret


def is_secure_cookie() -> bool:
    return settings.is_deployed


def normalize_email(email: str) -> str:
    return email.strip().lower()


def verify_password(password: str, hashed_password: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed_password.encode("utf-8"))
    except ValueError:
        return False


def sign_session_token(
    *,
    principal_id: str,
    tenant_id: str,
    secret: str,
    expire_minutes: int | None = None,
    now: datetime | None = None,
) -> str:
    issued_at = now or datetime.now(UTC)
    ttl = expire_minutes if expire_minutes is not None else settings.access_token_expire_minutes
    payload = {
        "sub": principal_id,
        "tenant_id": tenant_id,
        "iat": int(issued_at.timestamp()),
        "exp": int((issued_at + timedelta(minutes=ttl)).timestamp()),
    }
    return jwt.encode(payload, secret, algorithm=_JWT_ALGORITHM)


def verify_session_token(token: str, *, secret: str) -> tuple[str, str] | None:
    """Validate HS256 JWT and return `(principal_id, tenant_id)` or None."""
    if not token or not secret:
        return None
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=[_JWT_ALGORITHM],
            options={"require": ["sub", "tenant_id", "exp"]},
        )
    except jwt.PyJWTError:
        return None
    sub = payload.get("sub")
    tenant_id = payload.get("tenant_id")
    if isinstance(sub, str) and sub and isinstance(tenant_id, str) and tenant_id:
        return (sub, tenant_id)
    return None


def _lookup_credential_by_email(email_norm: str) -> tuple[str, str, str, str] | None:
    def _q(cur):
        cur.execute(
            """
            select pc.principal_id, p.tenant_id, p.principal_kind, pc.hashed_password
            from app.principal_credentials pc
            join app.principals p on p.principal_id = pc.principal_id
            where lower(pc.email) = %s
            limit 1
            """,
            (email_norm,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return (
            str(row["principal_id"]),
            str(row["tenant_id"]),
            str(row["principal_kind"]),
            str(row["hashed_password"]),
        )

    return with_app_runtime(_q)


def _lookup_principal(principal_id: str, tenant_id: str) -> PrincipalRecord | None:
    def _q(cur):
        cur.execute(
            """
            select principal_id, tenant_id, principal_kind
            from app.principals
            where principal_id = %s and tenant_id = %s
            limit 1
            """,
            (principal_id, tenant_id),
        )
        row = cur.fetchone()
        if not row:
            return None
        kind = str(row["principal_kind"])
        if kind not in {"member", "administrator", "service"}:
            return None
        return PrincipalRecord(
            principal_id=str(row["principal_id"]),
            tenant_id=str(row["tenant_id"]),
            principal_kind=kind,  # type: ignore[arg-type]
        )

    return with_app_runtime(_q)


def authenticate_login(
    *,
    email: str,
    password: str,
    client_ip: str,
    limiter: LoginRateLimiter | None = None,
) -> tuple[PrincipalRecord, str]:
    """Authenticate credentials and return `(PrincipalRecord, jwt_token)`."""
    active_limiter = limiter or login_rate_limiter
    if not active_limiter.attempt(client_ip or "unknown"):
        raise RateLimitExceededError("too_many_attempts")
    email_norm = normalize_email(email)
    if not email_norm or not password:
        verify_password("dummy-password", _DUMMY_BCRYPT_HASH)
        raise InvalidCredentialsError("invalid_credentials")
    row = _lookup_credential_by_email(email_norm)
    if row is None:
        # Equalize bcrypt latency even when the email is unknown.
        verify_password(password, _DUMMY_BCRYPT_HASH)
        raise InvalidCredentialsError("invalid_credentials")
    principal_id, tenant_id, kind, hashed = row
    if not verify_password(password, hashed) or kind not in {"member", "administrator", "service"}:
        raise InvalidCredentialsError("invalid_credentials")
    record = PrincipalRecord(
        principal_id=principal_id,
        tenant_id=tenant_id,
        principal_kind=kind,  # type: ignore[arg-type]
        email=email_norm,
    )
    secret = resolve_signing_secret()
    token = sign_session_token(principal_id=principal_id, tenant_id=tenant_id, secret=secret)
    return record, token


def resolve_principal_from_token(token: str | None, *, bearer_token: str | None = None) -> PrincipalRecord | None:
    """Resolve active principal from session cookie JWT or static MCP service bearer tokens."""
    configured_bearer = settings.mcp_bearer_token.get_secret_value() if settings.mcp_bearer_token else ""
    if bearer_token and configured_bearer and hmac.compare_digest(bearer_token, configured_bearer):
        return PrincipalRecord(
            principal_id=MCP_OWNER_PRINCIPAL_ID,
            tenant_id=MCP_SERVICE_TENANT_ID,
            principal_kind="administrator",
        )
    configured_readonly = settings.mcp_readonly_token.get_secret_value() if settings.mcp_readonly_token else ""
    if bearer_token and configured_readonly and hmac.compare_digest(bearer_token, configured_readonly):
        return PrincipalRecord(
            principal_id=MCP_READONLY_PRINCIPAL_ID,
            tenant_id=MCP_SERVICE_TENANT_ID,
            principal_kind="member",
        )
    if not token:
        return None
    try:
        secret = resolve_signing_secret()
    except AuthConfigurationError:
        return None
    claims = verify_session_token(token, secret=secret)
    if claims is None:
        return None
    principal_id, tenant_id = claims
    return _lookup_principal(principal_id, tenant_id)


def access_context_for(principal: PrincipalRecord) -> AccessContext:
    kind_map = {
        "member": PrincipalKind.MEMBER,
        "administrator": PrincipalKind.ADMINISTRATOR,
        "service": PrincipalKind.SERVICE,
    }
    now = datetime.now(UTC)
    return AccessContext(
        context_id=f"ctx:web:{uuid.uuid4().hex}",
        principal_id=principal.principal_id,
        tenant_id=principal.tenant_id,
        session_id=f"sess:web:{uuid.uuid4().hex}",
        principal_kind=kind_map[principal.principal_kind],
        authentication_method=AuthenticationMethod.BROWSER_SESSION,
        issued_at=now,
        expires_at=now + timedelta(minutes=settings.access_token_expire_minutes),
    )


def resolve_mcp_access_context() -> AccessContext:
    """Build the delegated AccessContext honoring the bound MCP bearer tier (Q03)."""
    issued_at = datetime.now(UTC)
    stamp = issued_at.timestamp()
    kind = current_mcp_principal_kind() or PrincipalKind.ADMINISTRATOR
    is_admin = kind is PrincipalKind.ADMINISTRATOR
    principal_id = MCP_OWNER_PRINCIPAL_ID if is_admin else MCP_READONLY_PRINCIPAL_ID
    ctx_prefix = "mcp-owner" if is_admin else "mcp-readonly"
    return AccessContext(
        context_id=f"ctx:{ctx_prefix}:{stamp}",
        principal_id=principal_id,
        tenant_id=MCP_SERVICE_TENANT_ID,
        session_id=f"session:{ctx_prefix}:{stamp}",
        authentication_method=AuthenticationMethod.DELEGATED_MCP,
        principal_kind=kind,
        issued_at=issued_at,
        expires_at=issued_at + MCP_CONTEXT_LIFETIME,
        delegated_by_service_principal_id=MCP_SERVICE_PRINCIPAL_ID,
        delegation_id=MCP_DELEGATION_ID,
    )
