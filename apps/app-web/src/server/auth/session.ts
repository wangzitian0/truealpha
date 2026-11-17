import type { AccessContext } from "@/contracts/strategyRun";
import type { QueryExecutor } from "./credentials";
import { verifySessionToken } from "./security";

export interface ResolvedPrincipal {
  principalId: string;
  tenantId: string;
  principalKind: "member" | "administrator" | "service";
}

export interface PrincipalLookup {
  findPrincipal(principalId: string): Promise<ResolvedPrincipal | null>;
}

export interface SessionConfig {
  secret: Uint8Array;
}

export interface SessionPrincipal {
  context: AccessContext;
  principalKind: ResolvedPrincipal["principalKind"];
}

export async function getSessionPrincipal(
  cookieValue: string | undefined,
  lookup: PrincipalLookup,
  config: SessionConfig,
): Promise<SessionPrincipal | null> {
  if (!cookieValue) return null;

  const verified = await verifySessionToken(cookieValue, config.secret);
  if (!verified) return null;

  const principal = await lookup.findPrincipal(verified.sub);
  if (!principal) return null;
  if (principal.tenantId !== verified.tenantId) return null;

  return {
    context: {
      contextId: `ctx:session:${verified.sub}:${verified.issuedAt.getTime()}`,
      principalId: principal.principalId,
      tenantId: principal.tenantId,
      sessionId: `session:${verified.sub}:${verified.issuedAt.getTime()}`,
      authenticationMethod: "password",
      issuedAt: verified.issuedAt.toISOString(),
      expiresAt: verified.expiresAt.toISOString(),
    },
    principalKind: principal.principalKind,
  };
}

export async function getSessionAccessContext(
  cookieValue: string | undefined,
  lookup: PrincipalLookup,
  config: SessionConfig,
): Promise<AccessContext | null> {
  const resolved = await getSessionPrincipal(cookieValue, lookup, config);
  return resolved ? resolved.context : null;
}

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
