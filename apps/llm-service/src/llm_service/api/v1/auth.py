"""Authentication OpenAPI 3.1 endpoints (`/api/v1/auth/*`)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from llm_service.api.v1._deps import get_optional_principal
from llm_service.config import settings
from llm_service.services.auth_service import (
    SESSION_COOKIE_NAME,
    InvalidCredentialsError,
    LoginRequest,
    LoginResponse,
    LogoutResponse,
    PrincipalRecord,
    RateLimitExceededError,
    SessionResponse,
    authenticate_login,
    is_secure_cookie,
)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for") or ""
    if forwarded:
        return forwarded.split(",")[0].strip() or "unknown"
    return request.client.host if request.client else "unknown"


@router.post("/login", response_model=LoginResponse, operation_id="auth_login")
def login(payload: LoginRequest, request: Request, response: Response) -> LoginResponse:
    try:
        principal, token = authenticate_login(
            email=payload.email,
            password=payload.password,
            client_ip=_client_ip(request),
        )
    except RateLimitExceededError:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="too_many_attempts") from None
    except InvalidCredentialsError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_credentials") from None

    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        secure=is_secure_cookie(),
        path="/",
        max_age=settings.access_token_expire_minutes * 60,
    )
    return LoginResponse(
        ok=True,
        principal_id=principal.principal_id,
        tenant_id=principal.tenant_id,
        principal_kind=principal.principal_kind,
    )


@router.post("/logout", response_model=LogoutResponse, operation_id="auth_logout")
def logout(response: Response) -> LogoutResponse:
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        httponly=True,
        samesite="lax",
        secure=is_secure_cookie(),
    )
    return LogoutResponse(ok=True)


@router.get("/session", response_model=SessionResponse, operation_id="auth_session")
def session(principal: PrincipalRecord | None = Depends(get_optional_principal)) -> SessionResponse:
    if principal is None:
        return SessionResponse(authenticated=False)
    return SessionResponse(
        authenticated=True,
        principal_id=principal.principal_id,
        tenant_id=principal.tenant_id,
        principal_kind=principal.principal_kind,
    )
