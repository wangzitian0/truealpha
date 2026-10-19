/**
 * #494 / #1363: loadQualityFunnel outcomes — deny-before-query for anonymous and
 * non-administrator principals, unavailable when GPPE has no accepted run, and
 * L1–L5 shaping with L5 scoped to the governed TOPT head in mart.served_head
 * (never picking canary-topt3 alphabetically).
 *
 * Run standalone: `bun run tests/admin-funnel.test.ts`.
 */

import type { AccessContext } from "../src/contracts/strategyRun";
import type { ToptGppeReport } from "../src/contracts/toptGppe";
import { loadQualityFunnel } from "../src/server/admin/funnel";

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

// --- denied: anonymous and member principals never touch repositories or DB ---
{
  let touched = false;
  const failingDeps = {
    gppeRepo: {
      latest: async () => {
        touched = true;
        throw new Error("must not query before authorization");
      },
    },
  };
  const anon = await loadQualityFunnel(null, failingDeps);
  const member = await loadQualityFunnel(
    { context: TEST_CONTEXT, principalKind: "member" },
    failingDeps,
  );
  assert(
    anon.kind === "denied" && member.kind === "denied",
    "non-administrators must be denied before any repository call",
  );
  assert(!touched, "no repository call may run before authorization");
}

// --- unavailable: when GPPE has no accepted run, funnel degrades to unavailable ---
{
  const outcome = await loadQualityFunnel(
    { context: TEST_CONTEXT, principalKind: "administrator" },
    {
      gppeRepo: {
        latest: async () => ({ reason: "no accepted production TOPT run" }),
      },
    },
  );
  assert(
    outcome.kind === "unavailable" && outcome.reason === "no accepted production TOPT run",
    "missing GPPE run yields unavailable outcome with reason",
  );
}

// --- ready: L5 queries the governed TOPT head and reads freshness/limit_hours from mart.served_head ---
{
  const runId = "capture-run:" + "f".repeat(64);
  const gppeReport: ToptGppeReport = {
    run_id: runId,
    requested_count: 84,
    available_count: 18,
    cells: [
      { listing_id: "listing:xnas:aapl", availability: "available", gppe: "1500000", confidence: "0.95" },
      { listing_id: "listing:xnas:msft", availability: "unavailable", gppe: null, confidence: null },
    ],
    quality: {
      available_count: 82,
      requested_count: 84,
      availability: 0.9762,
      freshness: 1.0,
      independent_reconciliation: 0.95,
    },
  };

  const capturedQueries: { sql: string; params: readonly unknown[] | undefined }[] = [];

  const outcome = await loadQualityFunnel(
    { context: TEST_CONTEXT, principalKind: "administrator" },
    {
      gppeRepo: {
        latest: async () => gppeReport,
      },
      strategyRepo: {
        getLatest: async () => ({
          decisions: [
            { outcome: "selected", rank: 1 },
            { outcome: "rejected", rank: 2 },
            { outcome: "excluded", rank: null },
          ],
        } as never),
      },
      runWithMartClient: async (fn) =>
        fn({
          query: async (sql: string, params?: readonly unknown[]) => {
            capturedQueries.push({ sql, params });
            if (sql.includes("strategy_input_coverage")) {
              return { rows: [{ total: 20, complete: 18 }] };
            }
            if (sql.includes("from mart.served_head")) {
              return {
                rows: [
                  {
                    environment: "production",
                    universe_id: "universe:topt-us-2026-03-31",
                    age: 36.0,
                    freshness: "stale",
                    limit_hours: 192.0,
                    availability: "available",
                  },
                ],
              };
            }
            return { rows: [] };
          },
        }),
    },
  );

  assert(outcome.kind === "ready", `expected ready, got ${outcome.kind}`);
  assert(outcome.data.runId === runId, "runId echoes the governed TOPT run");
  const l1 = outcome.data.layers.find((l) => l.key === "L1");
  const l2 = outcome.data.layers.find((l) => l.key === "L2");
  const l3 = outcome.data.layers.find((l) => l.key === "L3");
  const l4 = outcome.data.layers.find((l) => l.key === "L4");
  const l5 = outcome.data.layers.find((l) => l.key === "L5");
  assert(l1 !== undefined && l1.headline === "82 / 84 cells" && l1.status === "warn", "L1 shapes capture cells");
  assert(l2 !== undefined && l2.headline === "18 / 20 issuers input-complete" && l2.status === "warn", "L2 shapes coverage");
  assert(l3 !== undefined && l3.headline === "GPPE 1 / 2 listings" && l3.status === "crit", "L3 shapes GPPE ratio");
  assert(l4 !== undefined && l4.headline === "3 evaluated → 2 ranked → 1 selected" && l4.status === "ok", "L4 shapes strategy waterfall");
  assert(
    l5 !== undefined &&
      l5.headline === "pointer 36.0h old" &&
      l5.status === "warn" &&
      l5.ratio !== null &&
      Math.abs(l5.ratio - (1 - 36 / 192)) < 1e-6,
    "L5 derives status and ratio from mart.served_head freshness and limit_hours (#1363)",
  );

  const ptrQuery = capturedQueries.find((q) => q.sql.includes("from mart.served_head"));
  assert(
    ptrQuery !== undefined &&
      ptrQuery.sql.includes("environment, universe_id, age_hours as age") &&
      ptrQuery.sql.includes("coalesce(run_id, head_run_id) = $1") &&
      ptrQuery.sql.includes("universe_id like 'universe:topt-%'") &&
      ptrQuery.params?.[0] === runId,
    "L5 pointer query scopes to the governed TOPT run/universe instead of picking canary-topt3 alphabetically (#1363)",
  );

  // Unscheduled staging head with raw freshness='stale' resolves to 'manual' -> status='ok' via resolveHeadFreshness
  const manualStagingOutcome = await loadQualityFunnel(
    { context: TEST_CONTEXT, principalKind: "administrator" },
    {
      gppeRepo: { latest: async () => gppeReport },
      strategyRepo: { getLatest: async () => ({ decisions: [] } as never) },
      runWithMartClient: async (fn) =>
        fn({
          query: async (sql: string) => {
            if (sql.includes("from mart.served_head")) {
              return {
                rows: [
                  {
                    environment: "staging",
                    universe_id: "universe:qqq-us-t5",
                    age: 120.0,
                    freshness: "stale",
                    limit_hours: 72.0,
                    availability: "available",
                  },
                ],
              };
            }
            return { rows: [{ total: 0, complete: 0 }] };
          },
        }),
    },
  );
  assert(manualStagingOutcome.kind === "ready", "manual staging head returns ready");
  const manualL5 = manualStagingOutcome.data.layers.find((l) => l.key === "L5");
  assert(
    manualL5 !== undefined && manualL5.status === "ok",
    "unscheduled staging head with raw freshness='stale' resolves to 'manual' and grades L5 status 'ok'",
  );
}

console.log("#494 / #1363 admin-funnel loader outcomes passed");
