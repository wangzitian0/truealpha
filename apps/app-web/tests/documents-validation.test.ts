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

import { assertNonEmptyText, assertPositiveInteger, assertStableId, parseBeforeCursor } from "../src/server/documents";

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

  assert(parseBeforeCursor(undefined) === null, "an absent cursor must parse to null");
  const parsed = parseBeforeCursor({ createdAt: "2026-07-18T00:00:00.000Z", documentId: "document:1" });
  assert(
    parsed !== null && parsed.createdAt.toISOString() === "2026-07-18T00:00:00.000Z" && parsed.documentId === "document:1",
    "a valid cursor must parse",
  );

  for (const bad of [
    { createdAt: "not-a-date", documentId: "document:1" },
    { createdAt: "", documentId: "document:1" },
    { createdAt: "2026-13-99", documentId: "document:1" },
    { createdAt: "2026-07-18T00:00:00.000Z", documentId: "" },
    { createdAt: "2026-07-18T00:00:00.000Z", documentId: "not stable" },
  ]) {
    let threw = false;
    try {
      parseBeforeCursor(bad);
    } catch {
      threw = true;
    }
    assert(threw, `malformed cursor ${JSON.stringify(bad)} must throw`);
  }

  assert(assertNonEmptyText("hello", "sourceArtifactId") === "hello", "a non-empty string must pass through");
  assert(assertNonEmptyText("  padded  ", "sourceArtifactId") === "padded", "surrounding whitespace must be trimmed");

  for (const bad of ["", "   ", "\n\t"]) {
    let threw = false;
    try {
      assertNonEmptyText(bad, "sourceArtifactId");
    } catch {
      threw = true;
    }
    assert(threw, `empty/whitespace-only input ${JSON.stringify(bad)} must throw`);
  }

  assert(assertStableId("report:abc123", "sourceArtifactId") === "report:abc123", "a stable id must pass through");

  for (const bad of [
    "not stable",
    "!leading-bang",
    " leading-space",
    "report:latest",
    "report:current",
    "card:default",
    "report:head",
  ]) {
    let threw = false;
    try {
      assertStableId(bad, "sourceArtifactId");
    } catch {
      threw = true;
    }
    assert(threw, `unstable id ${JSON.stringify(bad)} must throw`);
  }

  console.log("documents-validation.test.ts: all assertions passed");
}

run();
