/**
 * #362: one pooled Postgres connection scoped down to the least-privilege
 * `mart_readonly` role (db/roles.sql) via `SET ROLE` before any query runs —
 * the App reads only the `mart` schema, never raw/staging, and the connecting
 * credential is never used directly. Mirrors `../auth/db.ts` `withAppRuntime`.
 * Server-only; never import into a client component.
 */

import type { PoolClient } from "pg";
import { withRoleClient } from "../auth/db";

let testClientOverride: Pick<PoolClient, "query"> | null = null;

/** Test seam (mirrors documents/object-store's __setTestClient): lets a test
 * lend its own transaction-scoped client so conformance runs see uncommitted
 * seed rows and roll everything back — nothing is ever committed to a shared
 * database (#469). Never set outside tests. */
export function __setTestClient(overrideClient: Pick<PoolClient, "query"> | null): void {
  testClientOverride = overrideClient;
}

/** Runs `fn` on a client that has assumed `mart_readonly` for this session.
 * Always resets the role and releases the client, even on error. */
export async function withMartReadonly<T>(fn: (client: PoolClient) => Promise<T>): Promise<T> {
  if (testClientOverride) {
    const injected = testClientOverride as PoolClient;
    try {
      await injected.query("set role mart_readonly");
      return await fn(injected);
    } finally {
      // reset even inside the test's transaction; the test owns the client's
      // lifetime, so no release here.
      await injected.query("reset role").catch(() => {});
    }
  }
  return withRoleClient("mart_readonly", fn);
}

