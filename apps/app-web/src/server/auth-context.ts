/**
 * Server-only, Local/CI-only administrator identity stand-in — see #349.
 *
 * `apps/app-web` has no real session-issuance mechanism yet (no NextAuth or
 * equivalent, and #229's `authorize_access` is a pure decision function over
 * an already-constructed AccessContext — nothing derives one from an HTTP
 * request today). Building real login is separate, security-sensitive work
 * tracked as follow-up, not this issue's scope.
 *
 * This module never accepts a client-supplied identity: the only input is a
 * server environment variable. Never import this from a client component.
 */

import type { AccessContext } from "@/contracts/strategyRun";

const ADMIN_PRINCIPAL_ENV_VAR = "TRUEALPHA_LOCAL_ADMIN_PRINCIPAL_ID";
const CONTEXT_LIFETIME_MS = 5 * 60 * 1000;

export type { AccessContext };

/**
 * Returns a fresh, short-lived Local/CI administrator `AccessContext`, or
 * `null` if `TRUEALPHA_LOCAL_ADMIN_PRINCIPAL_ID` is unset — the route must
 * treat `null` as denied. This never reads a cookie, header, or query
 * parameter, so a request cannot forge or influence the resulting identity.
 */
export function getLocalAdminAccessContext(): AccessContext | null {
  const principalId = process.env[ADMIN_PRINCIPAL_ENV_VAR];
  if (!principalId) return null;

  const issuedAt = new Date();
  const expiresAt = new Date(issuedAt.getTime() + CONTEXT_LIFETIME_MS);
  return {
    contextId: `ctx:local-admin:${issuedAt.getTime()}`,
    principalId,
    tenantId: "tenant:truealpha",
    sessionId: `session:local-admin:${issuedAt.getTime()}`,
    authenticationMethod: "service_identity",
    issuedAt: issuedAt.toISOString(),
    expiresAt: expiresAt.toISOString(),
  };
}
