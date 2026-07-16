# A0 Governed Research Access Boundary

Status: Accepted E2

This record freezes the stable Local/CI contract boundary accepted by `init.md`. Its DTOs
are exported from the `truealpha_contracts` package root for explicitly named consumers.

## Decision

TrueAlpha uses one server-derived `AccessContext` for browser sessions, delegated MCP
OAuth, and service identities. Client arguments never supply tenant, principal,
authority, entitlement, or publication-policy decisions. A trusted repository calls
`AuthorizationService` before mart SQL, private-row lookup, artifact retrieval, or
mutation and records the immutable decision and non-content audit event.

Private conversation and document locators live in the additive `app` schema. The
application role sets transaction-local tenant and principal coordinates after
authentication; forced PostgreSQL RLS then provides a second owner-isolation boundary.
Policy, grant, revocation, decision, and audit history is append-only. Revocation and
supersession add events instead of changing earlier records.

Materialized research remains in `mart` and is read only after a release-bound,
content-addressed `PublicationPolicySet` decision. Each member or service allow decision
records the exact active entitlement grant identities that matched a publication rule.
Policy rules are populated before an append-only seal record; once sealed, the set cannot
accept another rule under the same identity or content hash. Legacy decision writers may
continue writing their nullable pre-E2 shape during rollout, but only sealed E2 policy sets
can authorize the new typed resource shape.
`mart_readonly` limits database reach but does not replace application authorization.
Administrators may read policy-permitted materialized strategy/backtest results and
non-content audit metadata; they cannot read private conversation or document content
without a separately approved break-glass design.

Only an administrator action explicitly named by the immutable policy set may authorize
`SUBMIT_REGISTERED_REPLAY` for a registered replay definition. This access contract does
not persist or execute that request. A separate capability owns the append-only request
service and Dagster adapter; no Web, MCP, chat, or access-layer code invokes Qlib,
`BacktestDataGateway`, factors, or a Dagster launch API.

One `AccessAuditRecord` pairs an immutable decision with its deterministic, content-free
event. `AccessAuditRepository.append(record)` is the stable transactional port; the SQL
schema independently rejects missing policy sets and cross-tenant, cross-principal, or
policy-mismatched decision/grant associations.

## Computation Isolation

Access identity is a consumption concern. `AccessContext`, tenant, principal, role,
entitlement, and publication-policy fields do not enter factor inputs,
`BacktestDataGateway`, `DecisionSnapshot`, `ReplayEventStream`, or Qlib. Authorization
cannot change a historical factor or replay result; it only controls access to already
materialized artifacts and private application state.

## E2 Evidence Ceiling

E2 classifies the E1 findings, preserves the exact E1 corpus bytes, and adds a
content-hashed repair corpus for tiered entitlement, wrong-policy grant, administrator
replay-request authorization, and paired audit cases. It proves stable signatures,
deterministic pre-query decisions, additive migration compatibility, append-only bindings,
and two-tenant isolation in Local/CI. The accepted handoff authorizes only named consumer
batches and Local/CI environments.

The required `init.md` amendment exposed a generic delivery-toolkit defect: the blocked,
immutable Gate 0 v4 candidate was being recomputed from every future working tree. E2 does
not rewrite that candidate or its hash. A versioned Gate 0 v5 successor binds the amended
architecture, stable package export, and current validators while inheriting every v4
artifact, attestation, and blocker. The validator resolves the exact historical v4 tree
from Git and validates successor transitions independently. Isolated-copy negative tests
still prove byte drift and weakened blockers are rejected.

E2 does not activate authentication routes, an external identity provider, browser or MCP
traffic, content retention, sharing, a replay-request writer, replay execution, a release
binding, or Production readiness. Those claims require their own issues
and deployed evidence.

## Rollback

The implementation is not registered or selected by any runtime. Reverting the contract
and grants disables use; the additive `app` schema may remain dormant. Historical access,
grant, revocation, policy, and audit rows must never be updated or deleted during rollback.
