/**
 * #370: research dashboard loaders + mart read adapter.
 *
 * Run standalone (`bun run tests/dashboard-read.test.ts`), not through Next.js, so it
 * controls process.env directly without a live server. Proves typed states
 * (denied/ready/empty/unavailable/error/pagination), that reads derive AccessContext from
 * the server stand-in (never client input), and that ranking/comparison reproduce the MCP
 * strategy-run fixture values exactly.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";

import type { AccessContext, StrategyRunReport, StrategyRunUnavailable } from "../src/contracts/strategyRun";
import { loadComparison, loadOverview, loadRanking } from "../src/server/dashboard";
import { FixtureMartReadAdapter } from "../src/server/mart/research-read";

const ADMIN_ENV_VAR = "TRUEALPHA_LOCAL_ADMIN_PRINCIPAL_ID";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

const FIXTURE = JSON.parse(
  readFileSync(join(process.cwd(), "..", "..", "libs/contracts/src/truealpha_contracts/data/strategy_run_preview.v1.json"), "utf8"),
) as { corpus_sha256: string; decisions: Record<string, string | number | boolean | null>[] };

function emptyReport(): StrategyRunReport {
  return { strategy_id: "large_model_value_v0", source: "strategy_smoke_fixture", corpus_sha256: FIXTURE.corpus_sha256, decisions: [], golden_mismatches: [] };
}

function repositoryReturning(result: StrategyRunReport | StrategyRunUnavailable) {
  return { getLatest: (_id: string, _ctx: AccessContext) => result };
}

// --- denied: absent owner identity, and the adapter must never be read ---
{
  delete process.env[ADMIN_ENV_VAR];
  let adapterTouched = false;
  const throwingAdapter = new FixtureMartReadAdapter({
    getLatest: () => {
      adapterTouched = true;
      throw new Error("must not be reached when denied");
    },
  });
  const outcome = loadOverview(throwingAdapter);
  assert(outcome.kind === "denied", `expected denied, got ${outcome.kind}`);
  assert(!adapterTouched, "adapter must not be read before authorization");
}

// --- ready: owner present, real fixture-backed adapter, module availability from mart ---
{
  process.env[ADMIN_ENV_VAR] = "principal:test-owner";
  const outcome = loadOverview();
  assert(outcome.kind === "ready", `expected ready, got ${outcome.kind}`);
  assert(outcome.data.modules.length === 7, `expected 7 modules, got ${outcome.data.modules.length}`);
  const gppe = outcome.data.modules.find((m) => m.module === 2);
  const tier = outcome.data.modules.find((m) => m.module === 7);
  const peg = outcome.data.modules.find((m) => m.module === 1);
  assert(gppe?.availability === "available", "module 2 (GPPE) should be materialized/available");
  assert(tier?.availability === "available", "module 7 (tier) should be materialized/available");
  assert(peg?.availability === "unavailable", "module 1 (PEG) is not materialized yet");
  assert(outcome.data.latestCutoff === "2026-06-30T23:59:59Z", `unexpected latest cutoff ${outcome.data.latestCutoff}`);
}

// --- ranking reproduces the MCP fixture values exactly ---
{
  process.env[ADMIN_ENV_VAR] = "principal:test-owner";
  const outcome = loadRanking();
  assert(outcome.kind === "ready", `expected ready, got ${outcome.kind}`);
  const adm = outcome.data.rows.find((r) => r.issuerId === "issuer:adm");
  const expected = FIXTURE.decisions.find((d) => d.issuer_id === "issuer:adm" && d.cutoff_at === "2026-06-30T23:59:59Z");
  assert(adm !== undefined && expected !== undefined, "expected issuer:adm at 2026-06-30");
  assert(adm.valuationGap === expected.valuation_gap, `valuation gap not reproduced exactly: ${adm.valuationGap}`);
  assert(adm.currentPriceToSales === expected.current_price_to_sales, "P/S not reproduced exactly");
  assert(adm.confidence === expected.confidence, "confidence not reproduced exactly");
  assert(adm.rank === expected.rank, "rank not reproduced exactly");
  // First two rows are the ranked members in order.
  assert(outcome.data.rows[0].issuerId === "issuer:adm" && outcome.data.rows[0].rank === 1, "adm should rank 1");
  assert(outcome.data.rows[1].issuerId === "issuer:nice" && outcome.data.rows[1].rank === 2, "nice should rank 2");
  // Availability mapping is explicit for excluded/low-confidence subjects.
  const ddog = outcome.data.rows.find((r) => r.issuerId === "issuer:ddog");
  const jpm = outcome.data.rows.find((r) => r.issuerId === "issuer:jpm");
  assert(ddog?.availability === "low_confidence", "ddog below confidence floor should be low_confidence");
  assert(jpm?.availability === "excluded", "jpm financial exclusion should be excluded");
}

// --- comparison reproduces the labor-efficiency fixture value exactly ---
{
  process.env[ADMIN_ENV_VAR] = "principal:test-owner";
  const outcome = loadComparison();
  assert(outcome.kind === "ready", `expected ready, got ${outcome.kind}`);
  const adm = outcome.data.rows.find((r) => r.issuerId === "issuer:adm");
  const expected = FIXTURE.decisions.find((d) => d.issuer_id === "issuer:adm" && d.cutoff_at === "2026-06-30T23:59:59Z");
  assert(adm?.capitalAdjustedLaborEfficiency === expected?.capital_adjusted_labor_efficiency, "efficiency not reproduced exactly");
}

// --- pagination: bounded page size with a stable cursor ---
{
  process.env[ADMIN_ENV_VAR] = "principal:test-owner";
  const first = loadRanking({ limit: 2 });
  assert(first.kind === "ready", `expected ready, got ${first.kind}`);
  assert(first.data.rows.length === 2, `expected 2 rows, got ${first.data.rows.length}`);
  assert(first.data.page.total === 5, `expected 5 total, got ${first.data.page.total}`);
  assert(first.data.page.hasMore && first.data.page.nextCursor === "2", "expected more pages with cursor 2");
  const second = loadRanking({ limit: 2, cursor: first.data.page.nextCursor });
  assert(second.kind === "ready", `expected ready, got ${second.kind}`);
  assert(second.data.rows[0].issuerId !== first.data.rows[0].issuerId, "second page must differ from first");
}

// --- empty: a materialized run with no decisions ---
{
  process.env[ADMIN_ENV_VAR] = "principal:test-owner";
  const outcome = loadRanking({}, new FixtureMartReadAdapter(repositoryReturning(emptyReport())));
  assert(outcome.kind === "empty", `expected empty, got ${outcome.kind}`);
}

// --- unavailable: the read repository fails closed ---
{
  process.env[ADMIN_ENV_VAR] = "principal:test-owner";
  const unavailable: StrategyRunUnavailable = { strategy_id: "large_model_value_v0", reason: "fixture_missing" };
  const outcome = loadOverview(new FixtureMartReadAdapter(repositoryReturning(unavailable)));
  assert(outcome.kind === "unavailable", `expected unavailable, got ${outcome.kind}`);
  assert(outcome.reason === "fixture_missing", `unexpected reason ${outcome.reason}`);
}

// --- error: an unexpected read failure is caught, not propagated ---
{
  process.env[ADMIN_ENV_VAR] = "principal:test-owner";
  const boom = new FixtureMartReadAdapter({
    getLatest: () => {
      throw new Error("simulated read failure");
    },
  });
  const outcome = loadOverview(boom);
  assert(outcome.kind === "error", `expected error, got ${outcome.kind}`);
  assert(outcome.message.includes("simulated read failure"), `unexpected message ${outcome.message}`);
}

delete process.env[ADMIN_ENV_VAR];
console.log("#370 dashboard read loaders + adapter states passed");
