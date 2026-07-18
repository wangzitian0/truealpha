/**
 * #368: derives `AccessContext` only from a verified session cookie —
 * replaces `getLocalAdminAccessContext()` on every production route.
 * Never accepts a client-supplied principal/tenant/role/entitlement field;
 * the only inputs are the signed cookie and a server-side DB lookup.
 */

import type { AccessContext } from "@/contracts/strategyRun";
import type { QueryExecutor } from "./credentials";
import { verifySessionToken } from "./security";

export interface ResolvedPrincipal {
  principalId: string;
  tenantId: string;
  principalKind: "member" | "administrator" | "service";
}

export interface PrincipalLookup {
  /** Resolves principal_kind + tenant_id for an already-authenticated
   * principal_id from `app.principals`. Returns `null` if the principal no
   * longer resolves (deleted, or never existed). */
  findPrincipal(principalId: string): Promise<ResolvedPrincipal | null>;
}

export interface SessionConfig {
  secret: Uint8Array;
}

/** Derives `AccessContext` only from a verified session cookie. Returns
 * `null` (deny) for a missing/invalid/expired token, a principal no longer
 * resolvable in `app.principals`, or a token whose `tenantId` claim
 * disagrees with the current `app.principals` row — the database is the
 * authority on tenant membership, not the JWT's own (possibly stale) claim.
 * This is identity derivation only; it makes no authorization decision —
 * every repository operation still re-authorizes through #229. */
export async function getSessionAccessContext(
  cookieValue: string | undefined,
  lookup: PrincipalLookup,
  config: SessionConfig,
): Promise<AccessContext | null> {
  if (!cookieValue) return null;

  const verified = await verifySessionToken(cookieValue, config.secret);
  if (!verified) return null;

  const principal = await lookup.findPrincipal(verified.sub);
  if (!principal) return null;
  if (principal.tenantId !== verified.tenantId) return null;

  return {
    contextId: `ctx:session:${verified.sub}:${verified.issuedAt.getTime()}`,
    principalId: principal.principalId,
    tenantId: principal.tenantId,
    sessionId: `session:${verified.sub}:${verified.issuedAt.getTime()}`,
    authenticationMethod: "password",
    issuedAt: verified.issuedAt.toISOString(),
    expiresAt: verified.expiresAt.toISOString(),
  };
}

/** Reads `principal_kind` + `tenant_id` from `app.principals`. Must be
 * constructed with a client already scoped to `app_runtime`. */
export class PostgresPrincipalLookup implements PrincipalLookup {
  constructor(private readonly db: QueryExecutor) {}

  async findPrincipal(principalId: string): Promise<ResolvedPrincipal | null> {
    const result = await this.db.query<{ principal_id: string; tenant_id: string; principal_kind: string }>(
      `select principal_id, tenant_id, principal_kind from app.principals where principal_id = $1`,
      [principalId],
    );
    const row = result.rows[0];
    if (!row) return null;
    if (row.principal_kind !== "member" && row.principal_kind !== "administrator" && row.principal_kind !== "service") {
      return null;
    }
    return { principalId: row.principal_id, tenantId: row.tenant_id, principalKind: row.principal_kind };
  }
}
