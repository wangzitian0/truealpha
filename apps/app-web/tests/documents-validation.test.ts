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

import {
  assertNonEmptyText,
  assertPositiveInteger,
  assertStableId,
  clampListLimit,
  parseBeforeCursor,
  parseByteLength,
} from "../src/server/documents";

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
  assert(parseBeforeCursor(null) === null, "a null cursor must parse to null");
  // The parsed cursor keeps the ORIGINAL string — a Date round-trip truncated
  // Postgres microseconds to milliseconds and silently skipped rows (#470).
  const parsed = parseBeforeCursor({ createdAt: "2026-07-18T00:00:00.123456Z", documentId: "document:1" });
  assert(
    parsed !== null && parsed.createdAt === "2026-07-18T00:00:00.123456Z" && parsed.documentId === "document:1",
    "a valid cursor must parse and keep its microsecond precision verbatim",
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

  assert(clampListLimit(undefined) === 50, "an absent limit must default to 50");
  assert(clampListLimit(10) === 10, "an in-range limit must pass through");
  assert(clampListLimit(0) === 1, "a too-small limit must clamp up to 1");
  assert(clampListLimit(1000) === 200, "a too-large limit must clamp down to 200");

  for (const bad of [Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY, 1.5]) {
    let threw = false;
    try {
      clampListLimit(bad);
    } catch {
      threw = true;
    }
    assert(threw, `non-finite/non-integer limit ${JSON.stringify(bad)} must throw`);
  }

  assert(parseByteLength("1024") === 1024, "a valid byte length string must parse");
  assert(parseByteLength("0") === 0, "zero is a valid byte length");

  for (const bad of ["not-a-number", "-1", "1.5", "NaN", "Infinity", String(Number.MAX_SAFE_INTEGER + 1)]) {
    let threw = false;
    try {
      parseByteLength(bad);
    } catch {
      threw = true;
    }
    assert(threw, `invalid byte length string ${JSON.stringify(bad)} must throw`);
  }

  console.log("documents-validation.test.ts: all assertions passed");
}

run();
