"""Shared FastAPI authentication dependencies for API v1 routes."""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status

from llm_service.services import auth_service
from llm_service.services.auth_service import SESSION_COOKIE_NAME, PrincipalRecord


def _extract_bearer(request: Request) -> str | None:
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        return header[7:].strip() or None
    return None


def get_optional_principal(request: Request) -> PrincipalRecord | None:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    bearer = _extract_bearer(request)
    return auth_service.resolve_principal_from_token(token or bearer, bearer_token=bearer)


def require_principal(
    principal: PrincipalRecord | None = Depends(get_optional_principal),
) -> PrincipalRecord:
    if principal is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required")
    return principal


def require_admin(principal: PrincipalRecord = Depends(require_principal)) -> PrincipalRecord:
    if principal.principal_kind != "administrator":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="administrator identity required")
    return principal
