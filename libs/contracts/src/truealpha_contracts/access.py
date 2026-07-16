"""Experimental governed research access contracts.

Identity and authority in these DTOs are server-derived. Consumers must authorize
before issuing mart SQL, looking up a private row, or retrieving an artifact.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.models import _require_aware

_STABLE_COORDINATE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-]*$")
_MUTABLE_TOKENS = frozenset({"latest", "current", "default", "head"})


def _stable_coordinate(value: str, field_name: str) -> str:
    if _STABLE_COORDINATE.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a stable coordinate")
    tokens = {token for token in re.split(r"[._:/@+\-]", value.lower()) if token}
    if tokens & _MUTABLE_TOKENS:
        raise ValueError(f"{field_name} must name an immutable version")
    return value


class StrictFrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AuthenticationMethod(StrEnum):
    BROWSER_SESSION = "browser_session"
    DELEGATED_MCP = "delegated_mcp"
    DELEGATED_MCP_OAUTH = "delegated_mcp_oauth"
    SERVICE_IDENTITY = "service_identity"


class PrincipalKind(StrEnum):
    MEMBER = "member"
    ADMINISTRATOR = "administrator"
    SERVICE = "service"


class AccessAction(StrEnum):
    READ_CONTENT = "read_content"
    READ_MATERIALIZED_RESULT = "read_materialized_result"
    READ_AUDIT_METADATA = "read_audit_metadata"
    CREATE_CONVERSATION = "create_conversation"
    CREATE_RESEARCH_GAP = "create_research_gap"
    TRIAGE_RESEARCH_GAP = "triage_research_gap"
    SUBMIT_REGISTERED_REPLAY = "submit_registered_replay"
    MANAGE_ENTITLEMENT = "manage_entitlement"


class AccessResourceType(StrEnum):
    PRIVATE_CONVERSATION = "private_conversation"
    PRIVATE_DOCUMENT = "private_document"
    MATERIALIZED_STRATEGY_RESULT = "materialized_strategy_result"
    MATERIALIZED_BACKTEST_RESULT = "materialized_backtest_result"
    ACCESS_AUDIT_METADATA = "access_audit_metadata"


class AccessDecisionKind(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class AccessAuditEventKind(StrEnum):
    ACCESS_ALLOWED = "access_allowed"
    ACCESS_DENIED = "access_denied"
    AUTHENTICATION_DENIED = "authentication_denied"


class AccessDenialReason(StrEnum):
    AUTHENTICATION_MISSING = "authentication_missing"
    AUTHENTICATION_INVALID = "authentication_invalid"
    AUTHENTICATION_NOT_YET_VALID = "authentication_not_yet_valid"
    AUTHENTICATION_EXPIRED = "authentication_expired"
    DELEGATION_REVOKED = "delegation_revoked"
    CLIENT_AUTHORITY_CLAIM_REJECTED = "client_authority_claim_rejected"
    TENANT_MISMATCH = "tenant_mismatch"
    OWNER_DELEGATION_REQUIRED = "owner_delegation_required"
    PRIVATE_CONTENT_OWNER_ONLY = "private_content_owner_only"
    PUBLICATION_CLASS_NOT_PERMITTED = "publication_class_not_permitted"
    ACTION_NOT_PERMITTED = "action_not_permitted"


class AccessContext(StrictFrozenModel):
    """Verified context built by trusted browser, MCP OAuth, or service middleware."""

    context_id: str = Field(min_length=1)
    principal_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    authentication_method: AuthenticationMethod
    principal_kind: PrincipalKind
    issued_at: datetime
    expires_at: datetime
    active_entitlement_grant_ids: tuple[str, ...] = ()
    delegated_by_service_principal_id: str | None = None
    delegation_id: str | None = None

    @field_validator("issued_at", "expires_at")
    @classmethod
    def validate_datetimes(cls, value: datetime, info) -> datetime:
        return _require_aware(value, info.field_name)

    @field_validator("active_entitlement_grant_ids")
    @classmethod
    def validate_grants(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("active_entitlement_grant_ids must not contain duplicates")
        return tuple(sorted(value))

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


class AccessResource(StrictFrozenModel):
    resource_id: str = Field(min_length=1)
    resource_type: AccessResourceType
    tenant_id: str = Field(min_length=1)
    owner_principal_id: str | None = Field(default=None, min_length=1)
    publication_class_id: str | None = None
    content_private: bool

    @field_validator("publication_class_id")
    @classmethod
    def validate_publication_class(cls, value: str | None) -> str | None:
        return _stable_coordinate(value, "publication_class_id") if value is not None else None

    @model_validator(mode="after")
    def validate_resource(self) -> AccessResource:
        private = self.resource_type in {
            AccessResourceType.PRIVATE_CONVERSATION,
            AccessResourceType.PRIVATE_DOCUMENT,
        }
        if private != self.content_private or private != (self.owner_principal_id is not None):
            raise ValueError(
                "private resource types require private content and an owner; non-private types forbid both"
            )
        materialized = self.resource_type in {
            AccessResourceType.MATERIALIZED_STRATEGY_RESULT,
            AccessResourceType.MATERIALIZED_BACKTEST_RESULT,
        }
        if materialized != bool(self.publication_class_id):
            raise ValueError("only materialized results require a publication class")
        return self


class PublicationPolicy(StrictFrozenModel):
    policy_id: str
    permitted_publication_class_ids: tuple[str, ...]

    @field_validator("policy_id")
    @classmethod
    def validate_policy_id(cls, value: str) -> str:
        return _stable_coordinate(value, "policy_id")

    @field_validator("permitted_publication_class_ids")
    @classmethod
    def validate_classes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or len(value) != len(set(value)):
            raise ValueError("permitted_publication_class_ids must be non-empty and unique")
        return tuple(sorted(_stable_coordinate(item, "publication_class_id") for item in value))


class AuthorizationDecision(StrictFrozenModel):
    decision_id: str
    decision: AccessDecisionKind
    reason: AccessDenialReason | None
    query_permitted: bool
    audit_event: AccessAuditEventKind
    principal_id: str | None
    tenant_id: str | None
    action: AccessAction
    resource_id: str
    policy_id: str
    decided_at: datetime

    @field_validator("decided_at")
    @classmethod
    def validate_decided_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "decided_at")

    @model_validator(mode="after")
    def validate_decision(self) -> AuthorizationDecision:
        allowed = self.decision is AccessDecisionKind.ALLOW
        if allowed != self.query_permitted:
            raise ValueError("query_permitted must match the authorization decision")
        if allowed != (self.reason is None):
            raise ValueError("allowed decisions have no denial reason; denied decisions require one")
        audit_allowed = self.audit_event is AccessAuditEventKind.ACCESS_ALLOWED
        if allowed != audit_allowed:
            raise ValueError("audit event must match the authorization decision")
        return self


def _decision(
    *,
    context: AccessContext | None,
    action: AccessAction,
    resource: AccessResource,
    policy: PublicationPolicy,
    observed_at: datetime,
    reason: AccessDenialReason | None,
    authentication_denial: bool = False,
) -> AuthorizationDecision:
    allowed = reason is None
    coordinates = {
        "principal_id": context.principal_id if context else None,
        "tenant_id": context.tenant_id if context else None,
        "action": action.value,
        "resource_id": resource.resource_id,
        "policy_id": policy.policy_id,
        "decided_at": observed_at.isoformat(),
        "decision": AccessDecisionKind.ALLOW.value if allowed else AccessDecisionKind.DENY.value,
        "reason": reason.value if reason else None,
    }
    return AuthorizationDecision(
        decision_id=f"access-decision:{canonical_sha256(coordinates)}",
        decision=AccessDecisionKind.ALLOW if allowed else AccessDecisionKind.DENY,
        reason=reason,
        query_permitted=allowed,
        audit_event=(
            AccessAuditEventKind.ACCESS_ALLOWED
            if allowed
            else AccessAuditEventKind.AUTHENTICATION_DENIED
            if authentication_denial
            else AccessAuditEventKind.ACCESS_DENIED
        ),
        principal_id=context.principal_id if context else None,
        tenant_id=context.tenant_id if context else None,
        action=action,
        resource_id=resource.resource_id,
        policy_id=policy.policy_id,
        decided_at=observed_at,
    )


def authorize_access(
    *,
    context: AccessContext | None,
    action: AccessAction,
    resource: AccessResource,
    policy: PublicationPolicy,
    observed_at: datetime,
    authentication_failure: AccessDenialReason | None = None,
    revoked_delegation_ids: frozenset[str] = frozenset(),
) -> AuthorizationDecision:
    """Authorize before any query or retrieval and return an auditable decision."""

    _require_aware(observed_at, "observed_at")
    if authentication_failure is not None:
        if context is not None:
            raise ValueError("an authentication failure cannot include a verified context")
        return _decision(
            context=None,
            action=action,
            resource=resource,
            policy=policy,
            observed_at=observed_at,
            reason=authentication_failure,
            authentication_denial=True,
        )
    if context is None:
        return _decision(
            context=None,
            action=action,
            resource=resource,
            policy=policy,
            observed_at=observed_at,
            reason=AccessDenialReason.AUTHENTICATION_MISSING,
            authentication_denial=True,
        )
    if observed_at < context.issued_at:
        return _decision(
            context=context,
            action=action,
            resource=resource,
            policy=policy,
            observed_at=observed_at,
            reason=AccessDenialReason.AUTHENTICATION_NOT_YET_VALID,
            authentication_denial=True,
        )
    if observed_at >= context.expires_at:
        return _decision(
            context=context,
            action=action,
            resource=resource,
            policy=policy,
            observed_at=observed_at,
            reason=AccessDenialReason.AUTHENTICATION_EXPIRED,
            authentication_denial=True,
        )
    if context.delegation_id and context.delegation_id in revoked_delegation_ids:
        return _decision(
            context=context,
            action=action,
            resource=resource,
            policy=policy,
            observed_at=observed_at,
            reason=AccessDenialReason.DELEGATION_REVOKED,
            authentication_denial=True,
        )

    reason: AccessDenialReason | None = None
    if resource.content_private:
        if action is not AccessAction.READ_CONTENT:
            reason = AccessDenialReason.ACTION_NOT_PERMITTED
        elif context.principal_kind is PrincipalKind.ADMINISTRATOR:
            reason = AccessDenialReason.PRIVATE_CONTENT_OWNER_ONLY
        elif context.principal_kind is PrincipalKind.SERVICE:
            reason = AccessDenialReason.OWNER_DELEGATION_REQUIRED
        elif context.tenant_id != resource.tenant_id:
            reason = AccessDenialReason.TENANT_MISMATCH
        elif context.principal_id != resource.owner_principal_id:
            reason = AccessDenialReason.PRIVATE_CONTENT_OWNER_ONLY
    elif resource.resource_type is AccessResourceType.ACCESS_AUDIT_METADATA:
        if action is not AccessAction.READ_AUDIT_METADATA or context.principal_kind is not PrincipalKind.ADMINISTRATOR:
            reason = AccessDenialReason.ACTION_NOT_PERMITTED
    elif resource.publication_class_id not in policy.permitted_publication_class_ids:
        reason = AccessDenialReason.PUBLICATION_CLASS_NOT_PERMITTED
    elif action is not AccessAction.READ_MATERIALIZED_RESULT:
        reason = AccessDenialReason.ACTION_NOT_PERMITTED

    return _decision(
        context=context,
        action=action,
        resource=resource,
        policy=policy,
        observed_at=observed_at,
        reason=reason,
    )


@runtime_checkable
class AuthorizationService(Protocol):
    def authorize(
        self,
        *,
        context: AccessContext | None,
        action: AccessAction,
        resource: AccessResource,
        policy: PublicationPolicy,
        observed_at: datetime,
        authentication_failure: AccessDenialReason | None = None,
        revoked_delegation_ids: frozenset[str] = frozenset(),
    ) -> AuthorizationDecision: ...
