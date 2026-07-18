/**
 * #368: one pooled Postgres connection, scoped down to the least-privilege
 * `app_runtime` role (db/roles.sql) via `SET ROLE` before any query runs on
 * it — the connecting credential (the `postgres` superuser locally, a
 * scoped credential in Staging/Production) is never used directly for
 * app-schema reads/writes. Server-only; never import into a client
 * component.
 */

import { Pool, type PoolClient } from "pg";

let pool: Pool | null = null;

function getPool(): Pool {
  if (!pool) {
    const connectionString = process.env.DATABASE_URL;
    if (!connectionString) {
      throw new Error("DATABASE_URL is not set");
    }
    pool = new Pool({ connectionString });
  }
  return pool;
}

/** Runs `fn` on a client that has already assumed `app_runtime` for this
 * session. Always resets the role and releases the client back to the pool,
 * even on error. */
export async function withAppRuntime<T>(fn: (client: PoolClient) => Promise<T>): Promise<T> {
  const client = await getPool().connect();
  try {
    await client.query("set role app_runtime");
    return await fn(client);
  } finally {
    try {
      await client.query("reset role");
    } catch {
      // best-effort — the client is being released either way
    }
    client.release();
  }
}

export interface OwnerScope {
  tenantId: string;
  principalId: string;
}

/**
 * #396: runs `fn` inside a transaction with `app_runtime` assumed AND the
 * RLS GUCs (`truealpha.tenant_id`, `truealpha.principal_id`) set from an
 * already-verified `AccessContext` — never a client-supplied value. This is
 * the first caller of `app.conversations`/etc.'s row-level security
 * policies; every table those policies protect enforces owner isolation
 * through these two settings, not through application-layer filtering.
 *
 * `SET LOCAL` (unlike plain `SET`) is transaction-scoped, so this must run
 * inside an explicit `BEGIN`/`COMMIT` — it cannot use `withAppRuntime`'s
 * plain `SET ROLE`, which lives for the whole pooled connection lifetime
 * and would leak scope across requests if it also carried tenant/principal
 * state.
 */
export async function withOwnerScopedRuntime<T>(
  scope: OwnerScope,
  fn: (client: PoolClient) => Promise<T>,
): Promise<T> {
  const client = await getPool().connect();
  try {
    await client.query("begin");
    await client.query("set local role app_runtime");
    await client.query("select set_config('truealpha.tenant_id', $1, true)", [scope.tenantId]);
    await client.query("select set_config('truealpha.principal_id', $1, true)", [scope.principalId]);
    const result = await fn(client);
    await client.query("commit");
    return result;
  } catch (error) {
    await client.query("rollback").catch(() => {});
    throw error;
  } finally {
    client.release();
  }
}
