from __future__ import annotations

import inspect
import json
from datetime import datetime
from hashlib import sha256
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
    AuthorizationService,
    PrincipalKind,
    PublicationPolicy,
    authorize_access,
)
from truealpha_contracts.execution import DecisionSnapshot, ReplayEventStream
from truealpha_contracts.ports import BacktestDataGateway
from truealpha_contracts.release import ReleaseManifest

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "governed_research_access.v1.json"
FIXTURE_SHA256 = "ac3186bcc8cf14c0b9d7c909ca0f3148618eade175c2b469022364d3fae2498b"


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


def _decision_for_case(corpus: dict[str, Any], case: dict[str, Any]) -> AuthorizationDecision:
    return authorize_access(
        context=_contexts(corpus)[case["context_id"]],
        action=AccessAction(case["action"]),
        resource=_resources(corpus)[case["resource_id"]],
        policy=PublicationPolicy(
            policy_id=corpus["policy_coordinates"]["publication_policy_id"],
            permitted_publication_class_ids=("publication-class:standard:v1",),
        ),
        observed_at=datetime.fromisoformat(corpus["decision_time"].replace("Z", "+00:00")),
        authentication_failure=_authentication_failure(case["context_id"]),
    )


@pytest.mark.parametrize("case_index", range(18))
def test_frozen_authorization_corpus(case_index: int) -> None:
    corpus = _fixture()
    case = corpus["authorization_cases"][case_index]
    decision = _decision_for_case(corpus, case)

    assert decision.decision is AccessDecisionKind(case["expected_decision"])
    assert decision.query_permitted is bool(case["expected_query_count"])
    assert decision.audit_event is AccessAuditEventKind(case["expected_audit_event"])
    assert (decision.reason.value if decision.reason else None) == case.get("expected_reason")
    if decision.decision is AccessDecisionKind.DENY:
        assert case["expected_query_count"] == 0


def test_frozen_tiny_corpus_identity_and_strata() -> None:
    corpus = _fixture()
    assert sha256(FIXTURE_PATH.read_bytes()).hexdigest() == FIXTURE_SHA256
    assert len(corpus["tenants"]) == 2
    assert len(corpus["authentication_contexts"]) == 10
    assert len(corpus["resources"]) == 8
    assert len(corpus["authorization_cases"]) == 18
    assert len(corpus["append_only_event_cases"]) == 3
    assert {row["state"] for row in corpus["authentication_contexts"]} >= {
        "valid",
        "missing",
        "invalid",
        "expired",
        "revoked",
        "forged",
    }
    case_ids = [row["case_id"] for row in corpus["authorization_cases"]]
    assert len(case_ids) == len(set(case_ids))


def test_frozen_decisions_replay_deterministically() -> None:
    corpus = _fixture()
    first = [_decision_for_case(corpus, case) for case in corpus["authorization_cases"]]
    replay = [_decision_for_case(corpus, case) for case in corpus["authorization_cases"]]
    assert replay == first
    assert len({decision.decision_id for decision in first}) == len(first)


def test_denials_are_fail_closed_before_retrieval() -> None:
    corpus = _fixture()
    retrieval_count = 0
    for case in corpus["authorization_cases"]:
        decision = _decision_for_case(corpus, case)
        if decision.query_permitted:
            retrieval_count += 1
        assert int(decision.query_permitted) == case["expected_query_count"]
    assert retrieval_count == sum(case["expected_query_count"] for case in corpus["authorization_cases"])


def test_frozen_append_only_histories_preserve_every_event() -> None:
    histories = _fixture()["append_only_event_cases"]
    event_ids: list[str] = []
    for history in histories:
        recorded_at = [datetime.fromisoformat(row["recorded_at"].replace("Z", "+00:00")) for row in history["events"]]
        assert recorded_at == sorted(recorded_at)
        event_ids.extend(row["event_id"] for row in history["events"])
    assert len(event_ids) == len(set(event_ids))

    audit_history = next(row for row in histories if row["case_id"] == "allowed-and-denied-audit-history")
    assert {event["event_type"] for event in audit_history["events"]} == {"access_allowed", "access_denied"}
    assert all(
        forbidden not in event
        for event in audit_history["events"]
        for forbidden in ("content", "body", "payload", "document_text", "conversation_text")
    )


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


@pytest.mark.parametrize(
    "resource",
    [
        {
            "resource_id": "strategy-result:alpha:restricted-001",
            "resource_type": "materialized_strategy_result",
            "tenant_id": "tenant:alpha",
            "owner_principal_id": "principal:alpha:alice",
            "publication_class_id": "publication-class:restricted:v1",
            "content_private": True,
        },
        {
            "resource_id": "document:alpha:public-001",
            "resource_type": "private_document",
            "tenant_id": "tenant:alpha",
            "content_private": False,
        },
        {
            "resource_id": "audit:alpha:private-001",
            "resource_type": "access_audit_metadata",
            "tenant_id": "tenant:alpha",
            "owner_principal_id": "principal:alpha:alice",
            "content_private": True,
        },
    ],
)
def test_resource_type_rejects_contradictory_privacy_shape(resource: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="private resource types require"):
        AccessResource.model_validate(resource)


def test_non_private_resource_rejects_empty_owner_field() -> None:
    with pytest.raises(ValidationError, match="at least 1 character"):
        AccessResource(
            resource_id="strategy-result:alpha:standard-001",
            resource_type="materialized_strategy_result",
            tenant_id="tenant:alpha",
            owner_principal_id="",
            publication_class_id="publication-class:standard:v1",
            content_private=False,
        )


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


def test_authorization_service_signature_exposes_all_decision_inputs() -> None:
    assert set(inspect.signature(AuthorizationService.authorize).parameters) == {
        "self",
        "context",
        "action",
        "resource",
        "policy",
        "observed_at",
        "authentication_failure",
        "revoked_delegation_ids",
    }


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
