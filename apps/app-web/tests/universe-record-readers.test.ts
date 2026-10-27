/**
 * #1522: every reader of a universe reads the universe record. A key, id or lane name outside the
 * record fails one way and never falls back to the strategy universe (TOPT).
 *
 * Entry points: `loadRanking` (src/server/dashboard.ts), `handleCardExportGet` (the card export
 * route), and the role and lane lookups of src/contracts/universes.ts. The SQL bind parameters of the
 * mart readers are asserted in their own tests.
 *
 * Run standalone: `bun run tests/universe-record-readers.test.ts`.
 */

import { isCanaryUniverse } from "../src/contracts/serviceObjectives";
import type { AccessContext, StrategyRunDecision, StrategyRunReport } from "../src/contracts/strategyRun";
import {
  idPrefixLikeOfRole,
  idPrefixLikesOfRole,
  universeOfLane,
  universeWithRole,
} from "../src/contracts/universes";
import { handleCardExportGet } from "../src/server/cards/xhs-card";
import { loadRanking } from "../src/server/dashboard";
import type { DocumentsRepository } from "../src/server/documents";
import { StrategyRunReadAdapter } from "../src/server/mart/research-read";
import type { DecisionProvenance } from "../src/server/mart/strategy-run-repository";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

function throws(run: () => unknown): Error | null {
  try {
    run();
  } catch (error) {
    return error instanceof Error ? error : new Error(String(error));
  }
  return null;
}

const CONTEXT: AccessContext = {
  contextId: "ctx:universe-record",
  principalId: "principal:universe-record-owner",
  tenantId: "tenant:truealpha",
  sessionId: "session:universe-record",
  authenticationMethod: "password",
  issuedAt: "2026-10-27T00:00:00Z",
  expiresAt: "2026-10-27T01:00:00Z",
};

const CUTOFF = "2026-06-30T23:59:59Z";

function decision(issuerId: string, rank: number): StrategyRunDecision {
  return {
    issuer_id: issuerId,
    cutoff_at: CUTOFF,
    outcome: "selected",
    eligible: true,
    tier: "tech",
    capital_adjusted_labor_efficiency: "640000.00",
    current_price_to_sales: "6.10",
    target_price_to_sales: null,
    current_price_to_gross_profit: "12.50",
    target_price_to_gross_profit: "16.00",
    valuation_gap: "0.2800",
    confidence: "0.87",
    exclusion_reason: null,
    rank,
    target_weight: "0.05",
    peg: null,
    peg_rank: null,
    labor_efficiency_metric: "gppe_non_financial_v1",
  };
}

function provenance(universeId: string | null, universeVersion: string | null): DecisionProvenance {
  return {
    operating_period_end: "2025-12-31",
    revenue_period_end: "2025-12-31",
    shares_period_end: "2025-12-31",
    universe_id: universeId,
    universe_version: universeVersion,
    universe_sha256: "a".repeat(64),
    gppe_definition_sha256: "b".repeat(64),
    tier_definition_sha256: "c".repeat(64),
    peg_reason_codes: [],
  };
}

// The universe versions are real shapes: `universe_version` is no universe id and starts with no id prefix.
const report: StrategyRunReport & { provenance: ReadonlyMap<string, DecisionProvenance> } = {
  strategy_id: "large_model_value_v0",
  source: "mart",
  corpus_sha256: "f".repeat(64),
  decisions: [
    decision("issuer:in-topt", 1),
    decision("issuer:in-qqq", 2),
    decision("issuer:no-core-result", 3),
    decision("issuer:in-no-universe", 4),
  ],
  provenance: new Map([
    ["issuer:in-topt", provenance("universe:topt-us-2026-03-31", "topt-candidate-2026-03-31-v1")],
    ["issuer:in-qqq", provenance("universe:qqq-us-2026-06-30", "qqq-us-2026-06-30-v1")],
    ["issuer:in-no-universe", provenance("universe:nasdaq100-us-2026-06-30", "nasdaq100-us-2026-06-30-v1")],
  ]),
  golden_mismatches: [],
};

const adapter = new StrategyRunReadAdapter({ getLatest: async () => report }, () => {
  throw new Error("the ranking must not read the mart client");
});

function issuersOf(state: Awaited<ReturnType<typeof loadRanking>>): string {
  assert(state.kind === "ready", `expected ready, got ${state.kind}`);
  return state.data.rows.map((row) => row.issuerId).join(",");
}

// ─── loadRanking: the entry point of the rankings page and the card export ──────
{
  // An empty value gives the universe with role "strategy".
  for (const value of [undefined, "", "  "]) {
    const state = await loadRanking(CONTEXT, { universe: value }, adapter);
    assert(
      issuersOf(state) === "issuer:in-topt",
      `an empty universe (${JSON.stringify(value)}) must give only the strategy universe rows, got ${issuersOf(state)}`,
    );
    assert(
      state.kind === "ready" && state.data.universe === universeWithRole("strategy"),
      "an empty universe must report the key of the strategy universe",
    );
  }

  // A record key gives only the rows of that universe.
  const qqq = await loadRanking(CONTEXT, { universe: "qqq" }, adapter);
  assert(issuersOf(qqq) === "issuer:in-qqq", `universe=qqq must give only QQQ rows, got ${issuersOf(qqq)}`);
  assert(qqq.kind === "ready" && qqq.data.universe === "qqq", "universe=qqq must report the key qqq");
  const upper = await loadRanking(CONTEXT, { universe: " QQQ " }, adapter);
  assert(issuersOf(upper) === "issuer:in-qqq", "a key matches without regard to case and padding");

  // A universe outside the record has no rows. The old code gave it the strategy universe rows.
  for (const outside of ["nasdaq100", "universe:topt-us-2026-03-31", "universe-list:qqq", "toptx", "qq"]) {
    const state = await loadRanking(CONTEXT, { universe: outside }, adapter);
    assert(
      state.kind === "empty",
      `universe=${outside} is outside the record and must give no rows, got ${state.kind}: ${state.kind === "ready" ? issuersOf(state) : ""}`,
    );
  }

  // A key of the record that holds no rows is empty. It is not another universe.
  const canary = await loadRanking(CONTEXT, { universe: "canary" }, adapter);
  assert(canary.kind === "empty", `universe=canary holds no rows and must be empty, got ${canary.kind}`);

  // A row without a universe id, or with an id outside the record, belongs to no universe.
  for (const key of ["topt", "qqq"]) {
    const state = await loadRanking(CONTEXT, { universe: key }, adapter);
    const issuers = issuersOf(state);
    assert(
      !issuers.includes("issuer:no-core-result") && !issuers.includes("issuer:in-no-universe"),
      `universe=${key} must not hold a row with no universe id or an id outside the record, got ${issuers}`,
    );
  }

  // The context is checked before the universe.
  assert((await loadRanking(null, { universe: "nasdaq100" }, adapter)).kind === "denied", "a null context is denied");
}

// ─── Card export: the same entry point, the same answer ─────────────────────────
{
  const documents = {} as DocumentsRepository;
  const deps = { resolvePrincipal: async () => ({ context: CONTEXT }), adapter, documents, displayNames: new Map() };
  const exportUrl = (universe: string) =>
    `http://localhost:3000/research/api/cards/export?kind=ranking&format=json&universe=${encodeURIComponent(universe)}`;

  const outside = await handleCardExportGet(new Request(exportUrl("nasdaq100")), deps);
  assert(outside.status === 404, `the card of a universe outside the record must answer 404, got ${outside.status}`);
  const outsideBody = (await outside.json()) as { read_state: string };
  assert(outsideBody.read_state === "empty", "the 404 must name the empty read state");

  const qqq = await handleCardExportGet(new Request(exportUrl("qqq")), deps);
  assert(qqq.status === 200, `the QQQ card must answer 200, got ${qqq.status}`);
  const qqqBody = (await qqq.json()) as { universe: string; subjects: { subject_id: string }[] };
  assert(
    qqqBody.universe === "qqq" && qqqBody.subjects.map((s) => s.subject_id).join(",") === "issuer:in-qqq",
    "the QQQ card must name qqq and hold only QQQ rows",
  );

  const defaulted = await handleCardExportGet(
    new Request("http://localhost:3000/research/api/cards/export?kind=ranking&format=json"),
    deps,
  );
  const defaultBody = (await defaulted.json()) as { universe: string };
  assert(defaultBody.universe === universeWithRole("strategy"), "a card without a universe names the strategy universe");
}

// ─── Role and lane lookups ──────────────────────────────────────────────────────
{
  assert(universeWithRole("strategy") === "topt", "the strategy role belongs to topt");
  assert(universeWithRole("fund_consolidation") === "qqq", "the fund-consolidation role belongs to qqq");
  assert(idPrefixLikeOfRole("strategy") === "universe:topt-us-%", "the strategy id pattern follows the record");
  assert(
    idPrefixLikesOfRole("sentinel").join(",") === "universe:canary-us-%",
    "the sentinel id patterns follow the record",
  );
  assert(universeOfLane("universe-list:qqq") === "qqq" && universeOfLane("topt") === "topt", "a lane name resolves to its key");
  const unknownLane = throws(() => universeOfLane("universe-list:nasdaq100"));
  assert(unknownLane !== null && unknownLane.message.includes("lane name of no universe"), "a lane name outside the record throws");

  assert(isCanaryUniverse("universe:canary-us-2026-10-19") === true, "a canary id belongs to a sentinel universe");
  assert(isCanaryUniverse("universe:topt-us-2026-03-31") === false, "a TOPT id belongs to no sentinel universe");
  const unknownId = throws(() => isCanaryUniverse("canary-topt3"));
  assert(unknownId !== null && unknownId.message.includes("starts with no universe id prefix"), "an id outside the record throws");
}

console.log("#1522 universe record readers passed");
