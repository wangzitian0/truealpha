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
