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
    AccessAuditRecord,
    AccessAuditRepository,
    AccessContext,
    AccessDecisionKind,
    AccessDenialReason,
    AccessResource,
    AccessResourceType,
    ActiveEntitlementGrant,
    AuthenticationMethod,
    AuthorizationDecision,
    AuthorizationService,
    PrincipalKind,
    PublicationPolicySet,
    PublicationRule,
    authorize_access,
    build_access_audit_record,
)
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.execution import DecisionSnapshot, ReplayEventStream
from truealpha_contracts.ports import BacktestDataGateway
from truealpha_contracts.release import ReleaseManifest

BASE_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "governed_research_access.v1.json"
BASE_FIXTURE_SHA256 = "ac3186bcc8cf14c0b9d7c909ca0f3148618eade175c2b469022364d3fae2498b"
REPAIR_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "governed_research_access.v2.json"
REPAIR_FIXTURE_SHA256 = "eb7b08d3b125f25e3fc64add91f09e144ccb5be5489b24643103d2e69d8b9c86"
OBSERVED_AT = datetime.fromisoformat("2026-07-15T00:05:00+00:00")
POLICY_SET_ID = "publication-policy-set:research:v2"


def _fixture(path: Path = BASE_FIXTURE_PATH) -> dict[str, Any]:
    return json.loads(path.read_text())


def _grant(principal_id: str, entitlement_id: str) -> ActiveEntitlementGrant:
    return ActiveEntitlementGrant(
        grant_id=f"grant:{principal_id}:{entitlement_id}:001",
        entitlement_id=entitlement_id,
        publication_policy_set_id=POLICY_SET_ID,
    )


def _contexts(corpus: dict[str, Any]) -> dict[str, AccessContext | None]:
    principal_kinds = {
        principal["principal_id"]: PrincipalKind(principal["role"]) for principal in corpus["principals"]
    }
    contexts: dict[str, AccessContext | None] = {}
    for row in corpus["authentication_contexts"]:
        if row["state"] in {"missing", "invalid", "revoked", "forged"}:
            contexts[row["context_id"]] = None
            continue
        grants: tuple[ActiveEntitlementGrant, ...] = ()
        if principal_kinds[row["principal_id"]] is PrincipalKind.MEMBER:
            grants = (_grant(row["principal_id"], "entitlement:research:standard:v1"),)
        contexts[row["context_id"]] = AccessContext(
            context_id=row["context_id"],
            principal_id=row["principal_id"],
            tenant_id=row["tenant_id"],
            session_id=row["context_id"],
            authentication_method=AuthenticationMethod(row["method"]),
            principal_kind=principal_kinds[row["principal_id"]],
            issued_at=row["issued_at"],
            expires_at=row["expires_at"],
            active_entitlement_grants=grants,
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


def _policy(
    *,
    include_restricted: bool = False,
    administrator_actions: tuple[AccessAction, ...] | None = None,
) -> PublicationPolicySet:
    rules = [
        PublicationRule(
            publication_class_id="publication-class:standard:v1",
            eligible_entitlement_ids=(
                "entitlement:research:premium:v1",
                "entitlement:research:standard:v1",
            ),
        )
    ]
    if include_restricted:
        rules.append(
            PublicationRule(
                publication_class_id="publication-class:restricted:v1",
                eligible_entitlement_ids=("entitlement:research:premium:v1",),
            )
        )
    rules_tuple = tuple(rules)
    if administrator_actions is None:
        administrator_actions = (
            AccessAction.READ_AUDIT_METADATA,
            AccessAction.SUBMIT_REGISTERED_REPLAY,
        )
    content = {
        "publication_policy_set_id": POLICY_SET_ID,
        "release_manifest_id": "release-manifest:research:v1",
        "rules": [
            rule.model_dump(mode="json") for rule in sorted(rules_tuple, key=lambda item: item.publication_class_id)
        ],
        "administrator_actions": [
            action.value for action in sorted(administrator_actions, key=lambda item: item.value)
        ],
    }
    return PublicationPolicySet(
        publication_policy_set_id=POLICY_SET_ID,
        content_sha256=canonical_sha256(content),
        release_manifest_id="release-manifest:research:v1",
        rules=rules_tuple,
        administrator_actions=administrator_actions,
    )


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
        policy=_policy(),
        observed_at=datetime.fromisoformat(corpus["decision_time"].replace("Z", "+00:00")),
        authentication_failure=_authentication_failure(case["context_id"]),
    )


@pytest.mark.parametrize("case_index", range(18))
def test_e1_authorization_corpus_remains_compatible(case_index: int) -> None:
    corpus = _fixture()
    case = corpus["authorization_cases"][case_index]
    decision = _decision_for_case(corpus, case)

    assert decision.decision is AccessDecisionKind(case["expected_decision"])
    assert decision.query_permitted is bool(case["expected_query_count"])
    assert decision.audit_event is AccessAuditEventKind(case["expected_audit_event"])
    assert (decision.reason.value if decision.reason else None) == case.get("expected_reason")
    if decision.decision is AccessDecisionKind.DENY:
        assert not decision.entitlement_grant_ids


def test_e2_repair_corpus_pins_unchanged_e1_bytes() -> None:
    repair = _fixture(REPAIR_FIXTURE_PATH)
    assert sha256(BASE_FIXTURE_PATH.read_bytes()).hexdigest() == BASE_FIXTURE_SHA256
    assert sha256(REPAIR_FIXTURE_PATH.read_bytes()).hexdigest() == REPAIR_FIXTURE_SHA256
    assert repair["base_corpus"]["path"] == ("libs/contracts/tests/fixtures/governed_research_access.v1.json")
    assert repair["base_corpus"]["sha256"] == BASE_FIXTURE_SHA256
    assert {case["case_id"] for case in repair["repair_cases"]} == {
        "standard-grant-recorded-on-decision",
        "standard-grant-denied-restricted-class",
        "premium-grant-allows-restricted-class",
        "wrong-policy-grant-denied",
        "administrator-submits-registered-replay",
        "member-cannot-submit-registered-replay",
    }


def test_tiered_publication_rules_record_exact_matching_grants() -> None:
    standard = _grant("principal:alpha:alice", "entitlement:research:standard:v1")
    premium = _grant("principal:alpha:alice", "entitlement:research:premium:v1")
    base = _contexts(_fixture())["auth:browser:alpha:valid"]
    assert base is not None
    restricted = _resources(_fixture())["strategy-result:alpha:restricted-001"]

    standard_context = base.model_copy(update={"active_entitlement_grants": (standard,)})
    denied = authorize_access(
        context=standard_context,
        action=AccessAction.READ_MATERIALIZED_RESULT,
        resource=restricted,
        policy=_policy(include_restricted=True),
        observed_at=OBSERVED_AT,
    )
    assert denied.reason is AccessDenialReason.ENTITLEMENT_NOT_PERMITTED
    assert not denied.entitlement_grant_ids

    premium_context = base.model_copy(update={"active_entitlement_grants": (premium,)})
    allowed = authorize_access(
        context=premium_context,
        action=AccessAction.READ_MATERIALIZED_RESULT,
        resource=restricted,
        policy=_policy(include_restricted=True),
        observed_at=OBSERVED_AT,
    )
    assert allowed.decision is AccessDecisionKind.ALLOW
    assert allowed.entitlement_grant_ids == (premium.grant_id,)


def test_grant_bound_to_another_policy_set_fails_closed() -> None:
    base = _contexts(_fixture())["auth:browser:alpha:valid"]
    assert base is not None
    wrong = ActiveEntitlementGrant(
        grant_id="grant:alpha:wrong-policy:001",
        entitlement_id="entitlement:research:standard:v1",
        publication_policy_set_id="publication-policy-set:other:v1",
    )
    context = base.model_copy(update={"active_entitlement_grants": (wrong,)})
    decision = authorize_access(
        context=context,
        action=AccessAction.READ_MATERIALIZED_RESULT,
        resource=_resources(_fixture())["strategy-result:alpha:standard-001"],
        policy=_policy(),
        observed_at=OBSERVED_AT,
    )
    assert decision.reason is AccessDenialReason.ENTITLEMENT_NOT_PERMITTED


def test_registered_replay_submission_is_policy_bound_and_administrator_only() -> None:
    contexts = _contexts(_fixture())
    replay = AccessResource(
        resource_id="registered-replay-definition:analyst-track-record:v1",
        resource_type=AccessResourceType.REGISTERED_REPLAY_DEFINITION,
        tenant_id="tenant:alpha",
        content_private=False,
    )
    administrator = authorize_access(
        context=contexts["auth:admin:valid"],
        action=AccessAction.SUBMIT_REGISTERED_REPLAY,
        resource=replay,
        policy=_policy(),
        observed_at=OBSERVED_AT,
    )
    member = authorize_access(
        context=contexts["auth:browser:alpha:valid"],
        action=AccessAction.SUBMIT_REGISTERED_REPLAY,
        resource=replay,
        policy=_policy(),
        observed_at=OBSERVED_AT,
    )
    policy_denied = authorize_access(
        context=contexts["auth:admin:valid"],
        action=AccessAction.SUBMIT_REGISTERED_REPLAY,
        resource=replay,
        policy=_policy(administrator_actions=()),
        observed_at=OBSERVED_AT,
    )
    assert administrator.decision is AccessDecisionKind.ALLOW
    assert member.reason is AccessDenialReason.ACTION_NOT_PERMITTED
    assert policy_denied.reason is AccessDenialReason.ACTION_NOT_PERMITTED


def test_audit_record_is_deterministic_content_free_and_tenant_consistent() -> None:
    decision = _decision_for_case(_fixture(), _fixture()["authorization_cases"][0])
    first = build_access_audit_record(decision)
    second = build_access_audit_record(decision)
    assert first == second
    assert first.event.tenant_id == decision.tenant_id
    assert first.event.principal_id == decision.principal_id
    assert first.event.event_kind is decision.audit_event
    assert set(type(first.event).model_fields) == {
        "audit_event_id",
        "decision_id",
        "tenant_id",
        "principal_id",
        "event_kind",
        "occurred_at",
    }
    with pytest.raises(ValidationError, match="tenant must match"):
        AccessAuditRecord(
            decision=decision,
            event=first.event.model_copy(update={"tenant_id": "tenant:beta"}),
        )


def test_context_rejects_naive_time_client_claims_and_duplicate_grants() -> None:
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
    grant = _grant("principal:alpha:alice", "entitlement:research:standard:v1")
    with pytest.raises(ValidationError, match="duplicate grant IDs"):
        AccessContext.model_validate({**valid, "active_entitlement_grants": (grant, grant)})


def test_resource_and_policy_shapes_fail_closed() -> None:
    with pytest.raises(ValidationError, match="private resource types require"):
        AccessResource(
            resource_id="strategy-result:alpha:restricted-001",
            resource_type="materialized_strategy_result",
            tenant_id="tenant:alpha",
            owner_principal_id="principal:alpha:alice",
            publication_class_id="publication-class:restricted:v1",
            content_private=True,
        )
    with pytest.raises(ValidationError, match="immutable version"):
        PublicationPolicySet(
            publication_policy_set_id="publication-policy-set:current",
            content_sha256=_policy().content_sha256,
            release_manifest_id="release-manifest:research:v1",
            rules=_policy().rules,
            administrator_actions=(),
        )


def test_private_resources_fail_closed_for_unsupported_actions() -> None:
    corpus = _fixture()
    context = _contexts(corpus)["auth:browser:alpha:valid"]
    assert context is not None
    resource = _resources(corpus)["document:alpha:private-001"]
    for action in set(AccessAction) - {AccessAction.READ_CONTENT}:
        decision = authorize_access(
            context=context,
            action=action,
            resource=resource,
            policy=_policy(),
            observed_at=OBSERVED_AT,
        )
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
        policy=_policy(),
        observed_at=context.issued_at.replace(year=context.issued_at.year - 1),
    )
    assert decision.reason is AccessDenialReason.AUTHENTICATION_NOT_YET_VALID
    assert decision.audit_event is AccessAuditEventKind.AUTHENTICATION_DENIED


def test_public_service_signatures_expose_complete_inputs() -> None:
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
    assert set(inspect.signature(AccessAuditRepository.append).parameters) == {"self", "record"}


def test_stable_package_root_exports_access_contract() -> None:
    for name in {
        "AccessContext",
        "ActiveEntitlementGrant",
        "PublicationPolicySet",
        "AuthorizationService",
        "AccessAuditRepository",
        "authorize_access",
        "build_access_audit_record",
    }:
        assert hasattr(truealpha_contracts, name)


def test_access_metadata_never_enters_computation_or_release_contracts() -> None:
    forbidden = {
        "access_context",
        "tenant_id",
        "principal_id",
        "role",
        "entitlement",
        "publication_policy_set_id",
    }
    assert forbidden.isdisjoint(DecisionSnapshot.model_fields)
    assert forbidden.isdisjoint(ReplayEventStream.model_fields)
    assert all(name not in str(BacktestDataGateway.load.__annotations__) for name in forbidden)
    assert all("access" not in field_name for field_name in ReleaseManifest.model_fields)
