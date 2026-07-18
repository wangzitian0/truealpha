/**
 * #396: assertNonEmptyText — the one piece of conversations.ts pure enough
 * to unit test without Postgres. The repository's DB-backed behavior (RLS
 * isolation, append-only, single-redemption) is covered by
 * db/tests/conversations_contract.sql (run via
 * libs/contracts/tests/test_conversations_db_contract.py) and live
 * verification, not here — same split as #368/#371's Postgres-touching
 * adapters.
 *
 * Run standalone: `bun run tests/conversations-validation.test.ts`.
 */

import { assertNonEmptyText } from "../src/server/conversations";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

function run() {
  assert(assertNonEmptyText("hello", "content") === "hello", "a non-empty string must pass through");
  assert(assertNonEmptyText("  padded  ", "content") === "padded", "surrounding whitespace must be trimmed");

  for (const bad of ["", "   ", "\n\t"]) {
    let threw = false;
    try {
      assertNonEmptyText(bad, "content");
    } catch {
      threw = true;
    }
    assert(threw, `empty/whitespace-only input ${JSON.stringify(bad)} must throw`);
  }

  console.log("conversations-validation.test.ts: all assertions passed");
}

run();
