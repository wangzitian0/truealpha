/**
 * #362: one pooled Postgres connection scoped down to the least-privilege
 * `mart_readonly` role (db/roles.sql) via `SET ROLE` before any query runs —
 * the App reads only the `mart` schema, never raw/staging, and the connecting
 * credential is never used directly. Mirrors `../auth/db.ts` `withAppRuntime`.
 * Server-only; never import into a client component.
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

/** Runs `fn` on a client that has assumed `mart_readonly` for this session.
 * Always resets the role and releases the client, even on error. */
export async function withMartReadonly<T>(fn: (client: PoolClient) => Promise<T>): Promise<T> {
  const client = await getPool().connect();
  try {
    await client.query("set role mart_readonly");
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
