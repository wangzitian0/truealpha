/**
 * #373: assertPositiveInteger — the one piece of documents.ts pure enough to
 * unit test without Postgres/S3. The repository's storage-backed behavior
 * (RLS isolation, owner-scoped composite FKs, append-only, single-redemption,
 * tombstone non-enumeration) is covered by db/tests/documents_contract.sql
 * (run via libs/contracts/tests/test_documents_db_contract.py) and live
 * verification, not here — same split as #396's conversations tests.
 *
 * Run standalone: `bun run tests/documents-validation.test.ts`.
 */

import { assertPositiveInteger } from "../src/server/documents";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

function run() {
  assert(assertPositiveInteger(10, "expiresInMinutes") === 10, "a positive integer must pass through");

  for (const bad of [0, -1, 1.5, Number.NaN, Number.POSITIVE_INFINITY]) {
    let threw = false;
    try {
      assertPositiveInteger(bad, "expiresInMinutes");
    } catch {
      threw = true;
    }
    assert(threw, `non-positive/non-integer input ${JSON.stringify(bad)} must throw`);
  }

  console.log("documents-validation.test.ts: all assertions passed");
}

run();
