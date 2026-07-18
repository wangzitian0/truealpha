/**
 * #368: credential lookup + login resolution.
 *
 * `resolveLogin` is pure orchestration over an injected `CredentialsRepository`
 * so it is unit-testable without Postgres (tests/auth-login.test.ts).
 * `PostgresCredentialsRepository` is the real adapter, used only from route
 * handlers — never imported into a client component.
 *
 * No self-serve registration exists anywhere in this module: rows in
 * `app.principal_credentials` are seeded by an administrator
 * (scripts/seed-principal-credential.ts), binding a credential to a
 * `(tenant, principal)` that already exists. This module only ever reads.
 */

import { verifyPassword } from "./security";

export interface CredentialRecord {
  principalId: string;
  tenantId: string;
  hashedPassword: string;
}

export interface CredentialsRepository {
  findByEmail(email: string): Promise<CredentialRecord | null>;
}

export type LoginOutcome =
  | { kind: "success"; principalId: string; tenantId: string }
  | { kind: "invalid_credentials" };

// A real bcrypt hash of an unguessable placeholder — never a real password.
// Compared against on an unknown email so a login attempt against a
// non-existent account costs the same bcrypt work as a real one, and so the
// response shape below is identical either way. Prevents both timing-based
// and response-shape-based email enumeration.
const DUMMY_BCRYPT_HASH = "$2a$12$CwTycUXWue0Thq9StjUM0uJ8m8p1EbSN4DDzTRAeixmwapDGO2LbG";

export async function resolveLogin(
  email: string,
  password: string,
  repo: CredentialsRepository,
): Promise<LoginOutcome> {
  const normalizedEmail = email.trim().toLowerCase();
  const record = await repo.findByEmail(normalizedEmail);
  const passwordMatches = await verifyPassword(password, record?.hashedPassword ?? DUMMY_BCRYPT_HASH);

  if (!record || !passwordMatches) {
    return { kind: "invalid_credentials" };
  }
  return { kind: "success", principalId: record.principalId, tenantId: record.tenantId };
}

/** Structural subset of `pg`'s `PoolClient`/`Pool` — accepting this instead
 * of a concrete `pg` type lets a caller pass in a client that has already
 * `SET ROLE app_runtime` (see `db.ts::withAppRuntime`) without this module
 * needing to know about connection/role management at all. */
export interface QueryExecutor {
  query<Row extends Record<string, unknown>>(text: string, params?: unknown[]): Promise<{ rows: Row[] }>;
}

/** Reads `app.principal_credentials` joined to `app.principals`. Must be
 * constructed with a client already scoped to `app_runtime` (migration
 * 0029). Server-only — does DB I/O, never import into a client component. */
export class PostgresCredentialsRepository implements CredentialsRepository {
  constructor(private readonly db: QueryExecutor) {}

  async findByEmail(email: string): Promise<CredentialRecord | null> {
    const result = await this.db.query<{ principal_id: string; tenant_id: string; hashed_password: string }>(
      `select c.principal_id, p.tenant_id, c.hashed_password
       from app.principal_credentials c
       join app.principals p on p.principal_id = c.principal_id
       where lower(c.email) = lower($1)`,
      [email],
    );
    const row = result.rows[0];
    if (!row) return null;
    return { principalId: row.principal_id, tenantId: row.tenant_id, hashedPassword: row.hashed_password };
  }
}
