/**
 * loadFundValuation against a fake mart client (the MartClientLike seam
 * topt-gppe-repository established): the governed head resolves pointer-first, and the
 * fund-level aggregate is READ from mart.fund_virtual_company rather than computed here
 * (#727). The reader must report the factor's masses and its refusal verbatim — a fund
 * whose consolidation was refused shows no gap, and a fund with no consolidation row at
 * all shows an absent aggregate, never a zero.
 */

import { loadFundValuation } from "../src/server/mart/fund-valuation";
import type { MartClientLike } from "../src/server/mart/topt-gppe-repository";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

const LINES = [
  {
    fund_id: "etf:series:S1",
    fund_name: "Fund One",
    report_period: "2026-06-30",
    holding_name: "Valued Corp",
    ticker: "VAL",
    weight_pct: "60.0",
    current_ps: "10.5",
    target_ps_midpoint: "12.0",
    valuation_gap: "0.14",
    tier: "tier-1",
    availability: "available",
  },
  {
    fund_id: "etf:series:S1",
    fund_name: "Fund One",
    report_period: "2026-06-30",
    holding_name: "Resolved But Unvalued Corp",
    ticker: "RBU",
    weight_pct: "30.0",
    current_ps: null,
    target_ps_midpoint: null,
    valuation_gap: null,
    tier: null,
    availability: "unavailable",
  },
  {
    fund_id: "etf:series:S1",
    fund_name: "Fund One",
    report_period: "2026-06-30",
    holding_name: "Unresolved Corp",
    ticker: null,
    weight_pct: "9.5",
    current_ps: null,
    target_ps_midpoint: null,
    valuation_gap: null,
    tier: null,
    availability: null,
  },
];

/** What the tick's module-5 factor materialized for the governed run. */
const CONSOLIDATION = {
  fund_id: "etf:series:S1",
  weighted_gap: "0.14",
  valued_weight_pct: "60.0",
  availability_status: "available",
  source_evidence_status: "degraded",
  factor_validation_status: "not_evaluated",
  reason_codes: ["partial_valued_mass", "unresolved_holdings"],
};

function fakeRunner(
  headRows: Record<string, unknown>[],
  capture: { runParam?: unknown },
  consolidationRows: Record<string, unknown>[] = [CONSOLIDATION],
) {
  return async <T>(fn: (client: MartClientLike) => Promise<T>): Promise<T> => {
    const client: MartClientLike = {
      query: async (sql: string, params?: readonly unknown[]) => {
        if (sql.includes("current_pointer_head")) return { rows: headRows };
        if (sql.includes("topt_capture_status")) return { rows: [] };
        // Filed/resolved mass is the filing's, not the run's: answered whether or not a
        // governed run exists.
        if (sql.includes("fund_holdings_coverage")) {
          return { rows: [{ fund_id: "etf:series:S1", total_weight_pct: "99.5", resolved_weight_pct: "90.0" }] };
        }
        if (sql.includes("fund_virtual_company")) {
          // No governed run means no consolidation row for it — the same empty
          // result the real query returns for "capture-run:none".
          return { rows: params?.[0] === "capture-run:none" ? [] : consolidationRows };
        }
        if (sql.includes("fund_holdings_valuation")) {
          capture.runParam = params?.[0];
          // A non-existent run left-joins to nothing: valuation columns null,
          // exactly what the real SQL produces.
          const rows =
            params?.[0] === "capture-run:none"
              ? LINES.map((row) => ({
                  ...row,
                  current_ps: null,
                  target_ps_midpoint: null,
                  valuation_gap: null,
                  tier: null,
                  availability: null,
                }))
              : LINES;
          return { rows };
        }
        throw new Error(`unexpected SQL: ${sql.slice(0, 60)}`);
      },
    };
    return fn(client);
  };
}

{
  const capture: { runParam?: unknown } = {};
  const funds = await loadFundValuation(fakeRunner([{ run_id: "capture-run:abc" }], capture));
  assert(funds.length === 1, "one fund");
  const fund = funds[0];
  assert(capture.runParam === "capture-run:abc", "the governed head run parameterizes the join");
  assert(fund.runId === "capture-run:abc", "the run id is reported for provenance");
  assert(fund.totalWeightPct === "99.50", `total mass, got ${fund.totalWeightPct}`);
  assert(fund.resolvedWeightPct === "90.00", `resolved mass excludes null tickers, got ${fund.resolvedWeightPct}`);
  assert(fund.valuedWeightPct === "60.00", `valued mass counts only 'available', got ${fund.valuedWeightPct}`);
  assert(fund.weightedGap === "0.14", `weighted gap is read from the factor row, got ${fund.weightedGap}`);
  assert(fund.availabilityStatus === "available", "the consolidation's §8 availability is surfaced");
  assert(fund.sourceEvidenceStatus === "degraded", "partial valued mass reads as degraded evidence");
  assert(fund.factorValidationStatus === "not_evaluated", "module 5 has no sealed holdout verdict (#65)");
  assert(fund.reasonCodes.length === 2, "the factor's flags travel to the reader");
  assert(fund.lines.length === 3 && fund.lines[0].holdingName === "Valued Corp", "row order preserved");
}

{
  // A refused consolidation (coverage below the definition's floors) must not render as
  // a gap of zero: the aggregate is absent and the reason says why.
  const capture: { runParam?: unknown } = {};
  const funds = await loadFundValuation(
    fakeRunner([{ run_id: "capture-run:abc" }], capture, [
      {
        ...CONSOLIDATION,
        weighted_gap: null,
        valued_weight_pct: "3.0",
        availability_status: "unavailable",
        reason_codes: ["valued_weight_below_minimum"],
      },
    ]),
  );
  assert(funds[0].weightedGap === null, "a refused aggregate is absent, never 0.00");
  assert(funds[0].availabilityStatus === "unavailable", "the refusal is visible as unavailable");
  assert(funds[0].reasonCodes[0] === "valued_weight_below_minimum", "the refusing floor is named");
  assert(funds[0].valuedWeightPct === "3.00", "the coverage that caused the refusal is still reported");
}

{
  // A run whose tick predates #727 wrote no consolidation row. The lines still render;
  // the aggregate is absent rather than silently computed here.
  const capture: { runParam?: unknown } = {};
  const funds = await loadFundValuation(fakeRunner([{ run_id: "capture-run:abc" }], capture, []));
  assert(funds[0].weightedGap === null, "no consolidation row means no aggregate");
  assert(funds[0].totalWeightPct === "99.50", "the filed mass is the filing's, not the run's");
  assert(funds[0].valuedWeightPct === "0.00", "nothing is valued without a consolidation row");
  assert(funds[0].availabilityStatus === null, "an absent row carries no status dimensions");
  assert(funds[0].lines.length === 3, "the filed lines still render");
}

{
  const capture: { runParam?: unknown } = {};
  const funds = await loadFundValuation(fakeRunner([], capture));
  assert(capture.runParam === "capture-run:none", "no governed run joins nothing instead of guessing one");
  assert(funds.length === 1 && funds[0].runId === null, "the absence of a run is reported, not hidden");
  assert(funds[0].valuedWeightPct === "0.00", "nothing is valued without a run");
  assert(funds[0].weightedGap === null, "no weighted gap without a run — absent, not zero");
  assert(funds[0].totalWeightPct === "99.50", "the filed mass still reports without a run");
}

console.log("mart fund-valuation coverage arithmetic passed");
