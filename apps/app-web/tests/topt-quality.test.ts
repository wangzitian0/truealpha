/**
 * #433: TOPT GPPE / quality-report loader — typed states with an injected fake
 * repository (no live Postgres). The real repository is proven against a seeded
 * database in tests/topt-gppe-repository.test.ts.
 *
 * Run standalone: `bun run tests/topt-quality.test.ts`.
 */

import type { AccessContext } from "../src/contracts/strategyRun";
import type { ToptGppeReport, ToptGppeUnavailable } from "../src/contracts/toptGppe";
import { loadToptQuality, type ToptGppeRepositoryLike } from "../src/server/topt-quality";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

const TEST_CONTEXT: AccessContext = {
  contextId: "ctx:test",
  principalId: "principal:test-owner",
  tenantId: "tenant:truealpha",
  sessionId: "session:test",
  authenticationMethod: "password",
  issuedAt: "2026-01-01T00:00:00Z",
  expiresAt: "2026-01-01T01:00:00Z",
};

function repositoryReturning(result: ToptGppeReport | ToptGppeUnavailable): ToptGppeRepositoryLike {
  return { latest: async (_limit?: number) => result };
}

// --- denied: null context, and the repository must never be called ---
{
  let repositoryCalled = false;
  const outcome = await loadToptQuality(null, {
    latest: async () => {
      repositoryCalled = true;
      throw new Error("must not be reached when denied");
    },
  });
  assert(outcome.kind === "denied", `expected denied, got ${outcome.kind}`);
  assert(!repositoryCalled, "repository must not be called before authorization");
}

// --- ready: verified context, repository returns a report ---
{
  const report: ToptGppeReport = {
    run_id: "capture-run:" + "a".repeat(64),
    requested_count: 84,
    available_count: 1,
    cells: [
      { listing_id: "listing:aaa", availability: "available", gppe: "1500000.00", confidence: "0.90" },
      { listing_id: "listing:bbb", availability: "unavailable", gppe: null, confidence: null },
    ],
    quality: { independent_reconciliation: "0.25" },
  };
  const outcome = await loadToptQuality(TEST_CONTEXT, repositoryReturning(report));
  assert(outcome.kind === "ready", `expected ready, got ${outcome.kind}`);
  assert(outcome.data.run_id === report.run_id, "run_id must round-trip");
  assert(outcome.data.cells.length === 2, `expected 2 cells, got ${outcome.data.cells.length}`);
  assert(outcome.data.available_count === 1, "available_count must round-trip");
}

// --- unavailable: repository reports no accepted run ---
{
  const outcome = await loadToptQuality(TEST_CONTEXT, repositoryReturning({ reason: "no accepted (quality-reported) production TOPT run" }));
  assert(outcome.kind === "unavailable", `expected unavailable, got ${outcome.kind}`);
  assert(outcome.reason.includes("no accepted"), `unexpected reason: ${outcome.reason}`);
}

// --- error: repository throws, loader must not propagate the exception ---
{
  const outcome = await loadToptQuality(TEST_CONTEXT, {
    latest: async () => {
      throw new Error("simulated mart read failure");
    },
  });
  assert(outcome.kind === "error", `expected error, got ${outcome.kind}`);
  assert(outcome.message.includes("simulated mart read failure"), `unexpected message: ${outcome.message}`);
}

console.log("#433 topt-quality loader outcomes passed");
