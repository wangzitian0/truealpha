# A0 Governed Research Access Boundary

Status: Experimental E0

This record describes a provisional boundary below the stable architecture ceiling. It
does not amend `init.md`, and its DTOs are imported directly from
`truealpha_contracts.access` rather than the stable package root.

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

Materialized research remains in `mart` and is read only after a versioned publication
policy decision. `mart_readonly` limits database reach but does not replace application
authorization. Administrators may read permitted materialized strategy/backtest results
and non-content audit metadata; they cannot read private conversation or document content
without a separately approved break-glass design.

## Computation Isolation

Access identity is a consumption concern. `AccessContext`, tenant, principal, role,
entitlement, and publication-policy fields do not enter factor inputs,
`BacktestDataGateway`, `DecisionSnapshot`, `ReplayEventStream`, or Qlib. Authorization
cannot change a historical factor or replay result; it only controls access to already
materialized artifacts and private application state.

## E0 Evidence Ceiling

E0 proves immutable typed contracts, deterministic pre-query decisions, negative fixture
cases, additive schema shape, append-only enforcement, and two-tenant RLS isolation in
Local/CI. It does not activate authentication routes, an external identity provider,
browser or MCP access, content retention, sharing, replay submission, a release binding,
or a stable consumer handoff.

Before E2 can freeze a stable handoff, a separately valid candidate/version must integrate
the accepted boundary into authoritative `init.md` and the stable contracts export without
mutating the frozen Gate 0 v4 candidate in place. That freeze also requires E1 corpus
evidence and an independent review of the exact material change.

## Rollback

The implementation is not registered or selected by any runtime. Reverting the contract
and grants disables use; the additive `app` schema may remain dormant. Historical access,
grant, revocation, policy, and audit rows must never be updated or deleted during rollback.
