/**
 * #370/#371: research dashboard loaders + mart read adapter.
 *
 * Run standalone (`bun run tests/dashboard-read.test.ts`), not through Next.js. Proves
 * typed states (denied/ready/empty/unavailable/error/pagination), that a `null` context is
 * always denied before the adapter is touched (#371: callers pass an already-resolved
 * session `AccessContext`, this module never derives one itself), and that
 * ranking/comparison reproduce the MCP strategy-run fixture values exactly.
 *
 * Every fixture-value assertion injects `FixtureMartReadAdapter` explicitly: the loaders'
 * bare default is the real mart-backed adapter (#370 appended acceptance / #429 P3), so
 * relying on the default here would assert against whatever a local database contains.
 * The fixture and mart adapters share the same projection functions, so these fixture
 * assertions exercise exactly the projection code production runs on the mart.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";

import type { AccessContext, StrategyRunDecision, StrategyRunReport, StrategyRunUnavailable } from "../src/contracts/strategyRun";
import { loadComparison, loadOverview, loadRanking } from "../src/server/dashboard";
import { decisionAvailability, FixtureMartReadAdapter } from "../src/server/mart/research-read";
import { paginate } from "../src/server/mart/pagination";

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

const FIXTURE = JSON.parse(
  readFileSync(join(process.cwd(), "..", "..", "libs/contracts/src/truealpha_contracts/data/strategy_run_preview.v1.json"), "utf8"),
) as { corpus_sha256: string; decisions: Record<string, string | number | boolean | null>[] };

/** Tests-only adapter over the checked-in fixture bytes — never the loaders' default. */
const fixtureAdapter = () => new FixtureMartReadAdapter();

function emptyReport(): StrategyRunReport {
  return { strategy_id: "large_model_value_v0", source: "strategy_smoke_fixture", corpus_sha256: FIXTURE.corpus_sha256, decisions: [], golden_mismatches: [] };
}

function repositoryReturning(result: StrategyRunReport | StrategyRunUnavailable) {
  return { getLatest: (_id: string, _ctx: AccessContext) => result };
}

// --- denied: null context (no verified session), and the adapter must never be read ---
{
  let adapterTouched = false;
  const throwingAdapter = new FixtureMartReadAdapter({
    getLatest: () => {
      adapterTouched = true;
      throw new Error("must not be reached when denied");
    },
  });
  const outcome = await loadOverview(null, throwingAdapter);
  assert(outcome.kind === "denied", `expected denied, got ${outcome.kind}`);
  assert(!adapterTouched, "adapter must not be read before authorization");
}

// --- ready: verified context present, fixture-backed adapter injected, module availability from projection ---
{
  const outcome = await loadOverview(TEST_CONTEXT, fixtureAdapter());
  assert(outcome.kind === "ready", `expected ready, got ${outcome.kind}`);
  assert(outcome.data.modules.length === 7, `expected 7 modules, got ${outcome.data.modules.length}`);
  const gppe = outcome.data.modules.find((m) => m.module === 2);
  const tier = outcome.data.modules.find((m) => m.module === 7);
  const peg = outcome.data.modules.find((m) => m.module === 1);
  assert(gppe?.availability === "available", "module 2 (GPPE) should be materialized/available");
  assert(tier?.availability === "available", "module 7 (tier) should be materialized/available");
  assert(peg?.availability === "unavailable", "module 1 (PEG) is not materialized yet");
  assert(outcome.data.latestCutoff === "2026-06-30T23:59:59Z", `unexpected latest cutoff ${outcome.data.latestCutoff}`);
  // Run identity is surfaced so the page can be compared with the MCP strategy_run tool
  // output. The fixture has no mart run row: no run id, honest fixture source.
  assert(outcome.data.run.source === "strategy_smoke_fixture", `unexpected run source ${outcome.data.run.source}`);
  assert(outcome.data.run.strategyRunId === null, "fixture reports carry no strategy_run_id");
  assert(outcome.data.run.corpusSha256 === FIXTURE.corpus_sha256, "run identity must carry the report corpus hash");
}

// --- ranking reproduces the MCP fixture values exactly ---
{
  const outcome = await loadRanking(TEST_CONTEXT, {}, fixtureAdapter());
  assert(outcome.kind === "ready", `expected ready, got ${outcome.kind}`);
  const adm = outcome.data.rows.find((r) => r.issuerId === "issuer:adm");
  const expected = FIXTURE.decisions.find((d) => d.issuer_id === "issuer:adm" && d.cutoff_at === "2026-06-30T23:59:59Z");
  assert(adm !== undefined && expected !== undefined, "expected issuer:adm at 2026-06-30");
  assert(adm.valuationGap === expected.valuation_gap, `valuation gap not reproduced exactly: ${adm.valuationGap}`);
  assert(adm.currentPriceToSales === expected.current_price_to_sales, "P/S not reproduced exactly");
  assert(adm.confidence === expected.confidence, "confidence not reproduced exactly");
  assert(adm.rank === expected.rank, "rank not reproduced exactly");
  // The trace id prefix is the report's own source, never a hardcoded fixture literal
  // (#370 appended acceptance: a mart-backed row must not claim fixture provenance).
  assert(adm.traceId.startsWith("strategy_smoke_fixture:"), `unexpected trace id prefix: ${adm.traceId}`);
  // First two rows are the ranked members in order.
  assert(outcome.data.rows[0].issuerId === "issuer:adm" && outcome.data.rows[0].rank === 1, "adm should rank 1");
  assert(outcome.data.rows[1].issuerId === "issuer:nice" && outcome.data.rows[1].rank === 2, "nice should rank 2");
  // Availability mapping is explicit for low-confidence subjects.
  const ddog = outcome.data.rows.find((r) => r.issuerId === "issuer:ddog");
  assert(ddog?.availability === "low_confidence", "ddog below confidence floor should be low_confidence");
  // #381 removed financial-issuer special-casing: jpm is now a normal rejected-but-available
  // decision, not an exclusion. The shared fixture no longer has a naturally-occurring hard
  // exclusion, so that mapping branch is covered directly below instead of via this fixture.
  const jpm = outcome.data.rows.find((r) => r.issuerId === "issuer:jpm");
  assert(jpm?.availability === "available", "jpm should no longer be an exclusion after #381");
}

// --- trace ids honor a mart-sourced report's provenance (source is threaded, not hardcoded) ---
{
  const martSourced: StrategyRunReport = {
    ...emptyReport(),
    source: "mart",
    decisions: [
      {
        issuer_id: "issuer:adm",
        cutoff_at: "2026-06-30T23:59:59Z",
        outcome: "selected",
        eligible: true,
        tier: "large_model_native",
        capital_adjusted_labor_efficiency: null,
        current_price_to_sales: null,
        target_price_to_sales: null,
        valuation_gap: null,
        confidence: null,
        exclusion_reason: null,
        rank: 1,
        target_weight: null,
      },
    ],
  };
  const outcome = await loadRanking(TEST_CONTEXT, {}, new FixtureMartReadAdapter(repositoryReturning(martSourced)));
  assert(outcome.kind === "ready", `expected ready, got ${outcome.kind}`);
  assert(outcome.data.rows[0].traceId.startsWith("mart:"), `mart-sourced trace id must be mart-prefixed: ${outcome.data.rows[0].traceId}`);
}

// --- hard-excluded mapping is explicit, tested independently of the shared fixture ---
{
  const hardExcluded: StrategyRunDecision = {
    issuer_id: "issuer:test-excluded",
    cutoff_at: "2026-06-30T23:59:59Z",
    outcome: "excluded",
    eligible: false,
    tier: null,
    capital_adjusted_labor_efficiency: null,
    current_price_to_sales: null,
    target_price_to_sales: null,
    valuation_gap: null,
    confidence: null,
    exclusion_reason: "valuation_inputs_unavailable",
    rank: null,
    target_weight: null,
  };
  assert(decisionAvailability(hardExcluded) === "excluded", "non-confidence-floor exclusion should map to excluded");
  const lowConfidence: StrategyRunDecision = { ...hardExcluded, exclusion_reason: "below_confidence_floor" };
  assert(decisionAvailability(lowConfidence) === "low_confidence", "below_confidence_floor should map to low_confidence");
}

// --- comparison reproduces the labor-efficiency fixture value exactly ---
{
  const outcome = await loadComparison(TEST_CONTEXT, {}, fixtureAdapter());
  assert(outcome.kind === "ready", `expected ready, got ${outcome.kind}`);
  const adm = outcome.data.rows.find((r) => r.issuerId === "issuer:adm");
  const expected = FIXTURE.decisions.find((d) => d.issuer_id === "issuer:adm" && d.cutoff_at === "2026-06-30T23:59:59Z");
  assert(adm?.capitalAdjustedLaborEfficiency === expected?.capital_adjusted_labor_efficiency, "efficiency not reproduced exactly");
}

// --- pagination: bounded page size with a stable cursor ---
{
  const first = await loadRanking(TEST_CONTEXT, { limit: 2 }, fixtureAdapter());
  assert(first.kind === "ready", `expected ready, got ${first.kind}`);
  assert(first.data.rows.length === 2, `expected 2 rows, got ${first.data.rows.length}`);
  assert(first.data.page.total === 5, `expected 5 total, got ${first.data.page.total}`);
  assert(first.data.page.hasMore && first.data.page.nextCursor === "2", "expected more pages with cursor 2");
  const second = await loadRanking(TEST_CONTEXT, { limit: 2, cursor: first.data.page.nextCursor }, fixtureAdapter());
  assert(second.kind === "ready", `expected ready, got ${second.kind}`);
  assert(second.data.rows[0].issuerId !== first.data.rows[0].issuerId, "second page must differ from first");
}

// --- pagination: malformed/out-of-range cursors reset to page 1, never silently offset
// or return a blank page (Copilot review on #387: Number.parseInt("2oops", 10) === 2) ---
{
  const items = ["a", "b", "c", "d", "e"];
  const garbage = paginate(items, "2oops");
  assert(garbage.items[0] === "a", `garbage cursor must reset to page 1, got ${garbage.items[0]}`);
  const outOfRange = paginate(items, "999");
  assert(outOfRange.items[0] === "a", `out-of-range cursor must reset to page 1, got ${outOfRange.items[0]}`);
  const valid = paginate(items, "2");
  assert(valid.items[0] === "c", `valid cursor should offset normally, got ${valid.items[0]}`);
}

// --- empty: a materialized run with no decisions ---
{
  const outcome = await loadRanking(TEST_CONTEXT, {}, new FixtureMartReadAdapter(repositoryReturning(emptyReport())));
  assert(outcome.kind === "empty", `expected empty, got ${outcome.kind}`);
}

// --- unavailable: the read repository fails closed (same ReadState path the mart
// repository takes when the database is empty or unreachable) ---
{
  for (const reason of ["fixture_missing", "no_runs_recorded", "database_unavailable"] as const) {
    const unavailable: StrategyRunUnavailable = { strategy_id: "large_model_value_v0", reason };
    const outcome = await loadOverview(TEST_CONTEXT, new FixtureMartReadAdapter(repositoryReturning(unavailable)));
    assert(outcome.kind === "unavailable", `expected unavailable, got ${outcome.kind}`);
    assert(outcome.reason === reason, `unexpected reason ${outcome.reason}`);
  }
}

// --- error: an unexpected read failure is caught, not propagated ---
{
  const boom = new FixtureMartReadAdapter({
    getLatest: () => {
      throw new Error("simulated read failure");
    },
  });
  const outcome = await loadOverview(TEST_CONTEXT, boom);
  assert(outcome.kind === "error", `expected error, got ${outcome.kind}`);
  assert(outcome.message.includes("simulated read failure"), `unexpected message ${outcome.message}`);
}

console.log("#370/#371 dashboard read loaders + adapter states passed");
