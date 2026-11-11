"""Governed research access context contracts."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from truealpha_contracts.common import FrozenModel as StrictFrozenModel
from truealpha_contracts.common import require_aware, require_stable_and_immutable

_MUTABLE_TOKENS = frozenset({"latest", "current", "default", "head"})


class AuthenticationMethod(StrEnum):
    BROWSER_SESSION = "browser_session"
    DELEGATED_MCP = "delegated_mcp"
    DELEGATED_MCP_OAUTH = "delegated_mcp_oauth"
    SERVICE_IDENTITY = "service_identity"


class PrincipalKind(StrEnum):
    MEMBER = "member"
    ADMINISTRATOR = "administrator"
    SERVICE = "service"


class ActiveEntitlementGrant(StrictFrozenModel):
    grant_id: str
    entitlement_id: str
    publication_policy_set_id: str

    @field_validator("grant_id", "entitlement_id", "publication_policy_set_id")
    @classmethod
    def validate_coordinates(cls, value: str, info) -> str:
        return require_stable_and_immutable(value, info.field_name, mutable_tokens=_MUTABLE_TOKENS)


class AccessContext(StrictFrozenModel):
    context_id: str = Field(min_length=1)
    principal_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    authentication_method: AuthenticationMethod
    principal_kind: PrincipalKind
    issued_at: datetime
    expires_at: datetime
    active_entitlement_grants: tuple[ActiveEntitlementGrant, ...] = ()
    delegated_by_service_principal_id: str | None = None
    delegation_id: str | None = None

    @field_validator("issued_at", "expires_at")
    @classmethod
    def validate_datetimes(cls, value: datetime, info) -> datetime:
        return require_aware(value, info.field_name)

    @field_validator("active_entitlement_grants")
    @classmethod
    def validate_grants(cls, value: tuple[ActiveEntitlementGrant, ...]) -> tuple[ActiveEntitlementGrant, ...]:
        if len({g.grant_id for g in value}) != len(value):
            raise ValueError("active_entitlement_grants must not contain duplicate grant IDs")
        return tuple(sorted(value, key=lambda g: g.grant_id))

    @model_validator(mode="after")
    def validate_context(self) -> AccessContext:
        if self.expires_at <= self.issued_at:
            raise ValueError("expires_at must be after issued_at")
        delegated = self.authentication_method in {
            AuthenticationMethod.DELEGATED_MCP,
            AuthenticationMethod.DELEGATED_MCP_OAUTH,
        }
        if delegated != bool(self.delegation_id and self.delegated_by_service_principal_id):
            raise ValueError("delegated MCP contexts require delegation and service identities")
        return self


__all__ = [
    "AccessContext",
    "ActiveEntitlementGrant",
    "AuthenticationMethod",
    "PrincipalKind",
    "StrictFrozenModel",
]
