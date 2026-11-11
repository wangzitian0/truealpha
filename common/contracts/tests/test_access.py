from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from truealpha_contracts.access import (
    AccessContext,
    ActiveEntitlementGrant,
    AuthenticationMethod,
    PrincipalKind,
)


def _times() -> tuple[datetime, datetime]:
    issued = datetime(2026, 6, 30, 12, 0, tzinfo=UTC)
    return issued, issued + timedelta(hours=1)


def test_access_context_sorts_unique_grants_and_validates_delegation() -> None:
    issued, expires = _times()
    g2 = ActiveEntitlementGrant(
        grant_id="grant:two",
        entitlement_id="entitlement:research-pro:v1",
        publication_policy_set_id="pub-policy:v1",
    )
    g1 = ActiveEntitlementGrant(
        grant_id="grant:one",
        entitlement_id="entitlement:research-base:v1",
        publication_policy_set_id="pub-policy:v1",
    )
    ctx = AccessContext(
        context_id="ctx:1",
        principal_id="principal:member:1",
        tenant_id="tenant:alpha",
        session_id="session:1",
        authentication_method=AuthenticationMethod.DELEGATED_MCP_OAUTH,
        principal_kind=PrincipalKind.MEMBER,
        issued_at=issued,
        expires_at=expires,
        active_entitlement_grants=(g2, g1),
        delegated_by_service_principal_id="service:mcp-gateway",
        delegation_id="delegation:1",
    )
    assert [g.grant_id for g in ctx.active_entitlement_grants] == ["grant:one", "grant:two"]


def test_access_context_rejects_mutable_grant_coordinates_and_invalid_delegation() -> None:
    issued, expires = _times()
    with pytest.raises(ValidationError, match="mutable"):
        ActiveEntitlementGrant(
            grant_id="grant:latest",
            entitlement_id="entitlement:v1",
            publication_policy_set_id="policy:v1",
        )

    with pytest.raises(ValidationError, match="delegated MCP"):
        AccessContext(
            context_id="ctx:1",
            principal_id="principal:member:1",
            tenant_id="tenant:alpha",
            session_id="session:1",
            authentication_method=AuthenticationMethod.DELEGATED_MCP,
            principal_kind=PrincipalKind.MEMBER,
            issued_at=issued,
            expires_at=expires,
        )

    with pytest.raises(ValidationError, match="expires_at"):
        AccessContext(
            context_id="ctx:1",
            principal_id="principal:service:1",
            tenant_id="tenant:alpha",
            session_id="session:1",
            authentication_method=AuthenticationMethod.SERVICE_IDENTITY,
            principal_kind=PrincipalKind.SERVICE,
            issued_at=expires,
            expires_at=issued,
        )
