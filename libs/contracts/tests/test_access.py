from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import truealpha_contracts
from pydantic import ValidationError
from truealpha_contracts.access import (
    AccessAction,
    AccessAuditEventKind,
    AccessContext,
    AccessDecisionKind,
    AccessDenialReason,
    AccessResource,
    AuthenticationMethod,
    AuthorizationDecision,
    PrincipalKind,
    PublicationPolicy,
    authorize_access,
)
from truealpha_contracts.execution import DecisionSnapshot, ReplayEventStream
from truealpha_contracts.ports import BacktestDataGateway
from truealpha_contracts.release import ReleaseManifest

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "governed_research_access.v1.json"


def _fixture() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text())


def _contexts(corpus: dict[str, Any]) -> dict[str, AccessContext | None]:
    principal_kinds = {
        principal["principal_id"]: PrincipalKind(principal["role"]) for principal in corpus["principals"]
    }
    contexts: dict[str, AccessContext | None] = {}
    for row in corpus["authentication_contexts"]:
        if row["state"] in {"missing", "invalid", "revoked", "forged"}:
            contexts[row["context_id"]] = None
            continue
        contexts[row["context_id"]] = AccessContext(
            context_id=row["context_id"],
            principal_id=row["principal_id"],
            tenant_id=row["tenant_id"],
            session_id=row["context_id"],
            authentication_method=AuthenticationMethod(row["method"]),
            principal_kind=principal_kinds[row["principal_id"]],
            issued_at=row["issued_at"],
            expires_at=row["expires_at"],
            delegated_by_service_principal_id=row.get("service_principal_id"),
            delegation_id=row.get("delegation_id"),
        )
    return contexts


def _resources(corpus: dict[str, Any]) -> dict[str, AccessResource]:
    return {
        row["resource_id"]: AccessResource(
            resource_id=row["resource_id"],
            resource_type=row["resource_type"],
            tenant_id=row["tenant_id"],
            owner_principal_id=row.get("owner_principal_id"),
            publication_class_id=row.get("publication_class_id"),
            content_private=row["content_private"],
        )
        for row in corpus["resources"]
    }


def _authentication_failure(context_id: str) -> AccessDenialReason | None:
    return {
        "auth:invalid-signature": AccessDenialReason.AUTHENTICATION_INVALID,
        "auth:revoked": AccessDenialReason.DELEGATION_REVOKED,
        "auth:forged-role-tier": AccessDenialReason.CLIENT_AUTHORITY_CLAIM_REJECTED,
    }.get(context_id)


@pytest.mark.parametrize("case_index", range(18))
def test_frozen_authorization_corpus(case_index: int) -> None:
    corpus = _fixture()
    case = corpus["authorization_cases"][case_index]
    observed_at = datetime.fromisoformat(corpus["decision_time"].replace("Z", "+00:00"))
    decision = authorize_access(
        context=_contexts(corpus)[case["context_id"]],
        action=AccessAction(case["action"]),
        resource=_resources(corpus)[case["resource_id"]],
        policy=PublicationPolicy(
            policy_id=corpus["policy_coordinates"]["publication_policy_id"],
            permitted_publication_class_ids=("publication-class:standard:v1",),
        ),
        observed_at=observed_at,
        authentication_failure=_authentication_failure(case["context_id"]),
    )

    assert decision.decision is AccessDecisionKind(case["expected_decision"])
    assert decision.query_permitted is bool(case["expected_query_count"])
    assert decision.audit_event is AccessAuditEventKind(case["expected_audit_event"])
    assert (decision.reason.value if decision.reason else None) == case.get("expected_reason")
    if decision.decision is AccessDecisionKind.DENY:
        assert case["expected_query_count"] == 0


def test_context_rejects_naive_time_and_client_authority_claims() -> None:
    valid = {
        "context_id": "auth:browser:alpha:test",
        "principal_id": "principal:alpha:alice",
        "tenant_id": "tenant:alpha",
        "session_id": "session:alpha:test",
        "authentication_method": "browser_session",
        "principal_kind": "member",
        "issued_at": "2026-07-15T00:00:00Z",
        "expires_at": "2026-07-15T01:00:00Z",
    }
    with pytest.raises(ValidationError, match="timezone-aware"):
        AccessContext.model_validate({**valid, "issued_at": "2026-07-15T00:00:00"})
    with pytest.raises(ValidationError, match="client_claimed_role"):
        AccessContext.model_validate({**valid, "client_claimed_role": "administrator"})
    with pytest.raises(ValidationError, match="client_claimed_tier"):
        AccessContext.model_validate({**valid, "client_claimed_tier": "internal"})


def test_policy_and_decision_identities_are_immutable() -> None:
    with pytest.raises(ValidationError, match="immutable version"):
        PublicationPolicy(
            policy_id="publication-policy:current",
            permitted_publication_class_ids=("publication-class:standard:v1",),
        )
    decision = authorize_access(
        context=None,
        action=AccessAction.READ_CONTENT,
        resource=AccessResource(
            resource_id="document:alpha:private-001",
            resource_type="private_document",
            tenant_id="tenant:alpha",
            owner_principal_id="principal:alpha:alice",
            content_private=True,
        ),
        policy=PublicationPolicy(
            policy_id="publication-policy:research:v1",
            permitted_publication_class_ids=("publication-class:standard:v1",),
        ),
        observed_at=datetime.fromisoformat("2026-07-15T00:05:00+00:00"),
    )
    with pytest.raises(ValidationError, match="frozen"):
        decision.decision = AccessDecisionKind.ALLOW

    with pytest.raises(ValidationError, match="audit event must match"):
        AuthorizationDecision.model_validate(
            {
                **decision.model_dump(),
                "decision": AccessDecisionKind.DENY,
                "reason": AccessDenialReason.ACTION_NOT_PERMITTED,
                "query_permitted": False,
                "audit_event": AccessAuditEventKind.ACCESS_ALLOWED,
            }
        )


def test_private_resources_fail_closed_for_unsupported_actions() -> None:
    corpus = _fixture()
    context = _contexts(corpus)["auth:browser:alpha:valid"]
    assert context is not None
    resource = _resources(corpus)["document:alpha:private-001"]
    policy = PublicationPolicy(
        policy_id=corpus["policy_coordinates"]["publication_policy_id"],
        permitted_publication_class_ids=("publication-class:standard:v1",),
    )
    observed_at = datetime.fromisoformat(corpus["decision_time"].replace("Z", "+00:00"))

    for action in set(AccessAction) - {AccessAction.READ_CONTENT}:
        decision = authorize_access(
            context=context,
            action=action,
            resource=resource,
            policy=policy,
            observed_at=observed_at,
        )
        assert decision.decision is AccessDecisionKind.DENY
        assert decision.reason is AccessDenialReason.ACTION_NOT_PERMITTED
        assert not decision.query_permitted


def test_context_cannot_authorize_before_issuance() -> None:
    corpus = _fixture()
    context = _contexts(corpus)["auth:browser:alpha:valid"]
    assert context is not None
    decision = authorize_access(
        context=context,
        action=AccessAction.READ_CONTENT,
        resource=_resources(corpus)["document:alpha:private-001"],
        policy=PublicationPolicy(
            policy_id=corpus["policy_coordinates"]["publication_policy_id"],
            permitted_publication_class_ids=("publication-class:standard:v1",),
        ),
        observed_at=context.issued_at.replace(year=context.issued_at.year - 1),
    )

    assert decision.decision is AccessDecisionKind.DENY
    assert decision.reason is AccessDenialReason.AUTHENTICATION_NOT_YET_VALID
    assert decision.audit_event is AccessAuditEventKind.AUTHENTICATION_DENIED


def test_access_metadata_never_enters_computation_contracts() -> None:
    forbidden = {
        "access_context",
        "tenant_id",
        "principal_id",
        "role",
        "entitlement",
        "publication_policy_id",
    }
    assert forbidden.isdisjoint(DecisionSnapshot.model_fields)
    assert forbidden.isdisjoint(ReplayEventStream.model_fields)
    assert all(name not in str(BacktestDataGateway.load.__annotations__) for name in forbidden)


def test_experimental_access_contract_has_no_stable_or_release_binding() -> None:
    assert not hasattr(truealpha_contracts, "AccessContext")
    assert not hasattr(truealpha_contracts, "authorize_access")
    assert all("access" not in field_name for field_name in ReleaseManifest.model_fields)
