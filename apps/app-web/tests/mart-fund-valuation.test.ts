/**
 * loadFundValuation coverage arithmetic against a fake mart client (the
 * MartClientLike seam topt-gppe-repository established): the governed head
 * resolves pointer-first, and the three coverage masses answer exactly what
 * the rows say — valued ⊆ resolved ⊆ total, nothing assumed complete.
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
    total_weight_pct: "99.5",
    resolved_weight_pct: "90.0",
    valued_weight_pct: "60.0",
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
    total_weight_pct: "99.5",
    resolved_weight_pct: "90.0",
    valued_weight_pct: "60.0",
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
    total_weight_pct: "99.5",
    resolved_weight_pct: "90.0",
    valued_weight_pct: "60.0",
    current_ps: null,
    target_ps_midpoint: null,
    valuation_gap: null,
    tier: null,
    availability: null,
  },
];

function fakeRunner(headRows: Record<string, unknown>[], capture: { runParam?: unknown }) {
  return async <T>(fn: (client: MartClientLike) => Promise<T>): Promise<T> => {
    const client: MartClientLike = {
      query: async (sql: string, params?: readonly unknown[]) => {
        if (sql.includes("current_pointer_head")) return { rows: headRows };
        if (sql.includes("topt_capture_status")) return { rows: [] };
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
                  valued_weight_pct: null,
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
  assert(fund.lines.length === 3 && fund.lines[0].holdingName === "Valued Corp", "row order preserved");
}

{
  const capture: { runParam?: unknown } = {};
  const funds = await loadFundValuation(fakeRunner([], capture));
  assert(capture.runParam === "capture-run:none", "no governed run joins nothing instead of guessing one");
  assert(funds.length === 1 && funds[0].runId === null, "the absence of a run is reported, not hidden");
  assert(funds[0].valuedWeightPct === "0.00", "nothing is valued without a run");
  assert(funds[0].totalWeightPct === "99.50", "the filed mass still reports without a run");
}

console.log("mart fund-valuation coverage arithmetic passed");
