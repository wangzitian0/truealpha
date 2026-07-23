/**
 * #470: keyset pagination against a real local Postgres (skips gracefully
 * without one, throws when armed — ci-web provides the database).
 *
 * The cursor used to round-trip `created_at` through a JS Date, truncating
 * Postgres microseconds to milliseconds: the replayed boundary sat strictly
 * below the real row, silently skipping every document in the boundary's
 * sub-millisecond window and dead-coding the documented `document_id`
 * tie-break. This seeds exactly those windows — two documents in the same
 * millisecond with different microseconds, two with byte-identical
 * timestamps — pages with limit 1, and requires every document exactly once
 * in order, plus an SQL-level proof that the cursor replays losslessly.
 *
 * Isolation: RLS scopes listDocuments to the (tenant, owner) GUCs, so a
 * unique per-run tenant sees only its own rows. Cleanup deletes the document
 * rows; the tenant/principal identity rows are append-only by design
 * (app.reject_mutation), so a persistent local database accumulates one
 * disposable random-suffixed pair per run — the same convention as
 * test_strategy_run_postgres. CI's database is ephemeral.
 * Run standalone: `bun run tests/documents-pagination.test.ts`.
 */

import { randomBytes } from "node:crypto";

import { Client } from "pg";

import { PostgresDocumentsRepository } from "../src/server/documents";
import type { AccessContext } from "../src/contracts/strategyRun";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

const REQUIRE_DB = Boolean(process.env.DATABASE_URL || process.env.TRUEALPHA_REQUIRE_RUNTIME);
process.env.DATABASE_URL ??= "postgresql://postgres:postgres@localhost:5432/truealpha";
const DATABASE_URL = process.env.DATABASE_URL;

async function reachable(): Promise<Client | null> {
  const client = new Client({ connectionString: DATABASE_URL, connectionTimeoutMillis: 3000 });
  try {
    await client.connect();
    return client;
  } catch (error) {
    await client.end().catch(() => {});
    if (REQUIRE_DB) throw new Error(`configured Postgres is unreachable: ${String(error)}`);
    console.log("documents-pagination: no local Postgres and TRUEALPHA_REQUIRE_RUNTIME unset — SKIP");
    return null;
  }
}

const admin = await reachable();
if (admin !== null) {
  const suffix = randomBytes(6).toString("hex");
  const tenantId = `tenant:test-pagination-${suffix}`;
  const principalId = `principal:test-pagination-${suffix}`;
  const doc = (name: string) => `document:test-pagination-${suffix}-${name}`;

  // Descending (created_at, document_id) order, so pages must come out as:
  //   late (.100999) → tie-z (.100455) → tie-a (.100455, smaller id) → early (.100000)
  // late→tie crosses a sub-millisecond boundary (the skipped-window case);
  // tie-z→tie-a is the equal-timestamp tie-break the comment documents.
  const SEED: Array<{ id: string; createdAt: string }> = [
    { id: doc("late"), createdAt: "2026-07-23T00:00:00.100999Z" },
    { id: doc("tie-z"), createdAt: "2026-07-23T00:00:00.100455Z" },
    { id: doc("tie-a"), createdAt: "2026-07-23T00:00:00.100455Z" },
    { id: doc("early"), createdAt: "2026-07-23T00:00:00.100000Z" },
  ];
  const EXPECTED_ORDER = [doc("late"), doc("tie-z"), doc("tie-a"), doc("early")];

  const CONTEXT: AccessContext = {
    contextId: "ctx:test-pagination",
    principalId,
    tenantId,
    sessionId: "session:test-pagination",
    authenticationMethod: "service_identity",
    issuedAt: "2026-07-23T00:00:00Z",
    expiresAt: "2026-07-23T01:00:00Z",
  };

  try {
    await admin.query("insert into app.tenants (tenant_id) values ($1)", [tenantId]);
    await admin.query("insert into app.principals (principal_id, tenant_id, principal_kind) values ($1, $2, 'member')", [
      principalId,
      tenantId,
    ]);
    for (const row of SEED) {
      await admin.query(
        `insert into app.research_documents (document_id, tenant_id, owner_principal_id, created_at)
         values ($1, $2, $3, $4::timestamptz)`,
        [row.id, tenantId, principalId, row.createdAt],
      );
    }

    const repository = new PostgresDocumentsRepository();
    const seen: string[] = [];
    let before: { createdAt: string; documentId: string } | null = null;
    let firstCursor: string | null = null;
    for (let page = 0; page < SEED.length + 1; page += 1) {
      const result = await repository.listDocuments(CONTEXT, { limit: 1, before });
      if (result.documents.length === 0) break;
      assert(result.documents.length === 1, "limit 1 must return one document per page");
      seen.push(result.documents[0].documentId);
      before = result.nextBefore;
      if (firstCursor === null && before !== null) firstCursor = before.createdAt;
      if (before === null) break;
    }
    assert(
      JSON.stringify(seen) === JSON.stringify(EXPECTED_ORDER),
      `every document exactly once, in (created_at, document_id) desc order:\n  got:      ${seen.join(", ")}\n  expected: ${EXPECTED_ORDER.join(", ")}`,
    );

    // Lossless replay: the first page's cursor must equal the boundary row's
    // stored timestamp EXACTLY at the SQL level — a millisecond-truncated
    // cursor compares strictly below it and this assertion goes red.
    assert(firstCursor !== null, "limit 1 over 4 rows must produce a cursor");
    const lossless = await admin.query<{ exact: boolean }>(
      "select created_at = $1::timestamptz as exact from app.research_documents where document_id = $2",
      [firstCursor, doc("late")],
    );
    assert(lossless.rows[0]?.exact === true, `cursor '${firstCursor}' does not replay to the stored microsecond value`);

    console.log("#470 documents keyset pagination passed");
  } finally {
    // Document rows are deletable; the tenant/principal identity rows are
    // append-only (app.reject_mutation) and intentionally left behind — see
    // the header note on the disposable-rows convention.
    await admin.query("delete from app.research_documents where tenant_id = $1", [tenantId]).catch(() => {});
    await admin.end().catch(() => {});
  }
}
