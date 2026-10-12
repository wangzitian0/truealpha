/**
 * #1241: the trace view model and the inputs table, without a database.
 *
 * Proves that the lineage of one `mart.topt_core_meta_info` row maps to the six inputs of a
 * decision (value, period, knowable_at, accession, source vintage), that a field the lineage does
 * not carry stays null and renders "not recorded", and that `traceView` reads the lineage of the
 * report's own strategy run. The database half is `trace-lineage-mart.test.ts`.
 *
 * Run standalone: `bun run tests/trace-lineage-inputs.test.ts`.
 */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import type {
  AccessContext,
  StrategyRunDecision,
  StrategyRunReport,
} from "../src/contracts/strategyRun";
import { TraceInputsTable } from "../src/components/trace-inputs-table";
import { loadTrace } from "../src/server/dashboard";
import {
  MartReadUnavailable,
  NOT_RECORDED,
  StrategyRunReadAdapter,
  TRACE_INPUT_SPECS,
  TRACE_LINEAGE_SQL,
  traceInputsFromLineage,
  type StrategyRunRepositoryLike,
} from "../src/server/mart/research-read";
import type { MartClientLike } from "../src/server/mart/topt-gppe-repository";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

const CONTEXT: AccessContext = {
  contextId: "ctx:trace-lineage-inputs",
  principalId: "principal:test",
  tenantId: "tenant:test",
  sessionId: "session:test",
  authenticationMethod: "service_identity",
  issuedAt: "2026-10-12T00:00:00Z",
  expiresAt: "2026-10-12T01:00:00Z",
};

const FILING = (accession: string, periodEnd: string) => ({
  accession,
  document: null,
  filed: "2026-02-01",
  form: "10-K",
  fp: "FY",
  fy: 2025,
  period_end: periodEnd,
  statement_form: true,
});

/** One lineage as the view emits it: four items, one per semantic type. */
function lineage() {
  return [
    {
      observation_id: "normalized-observation:fin",
      semantic_type: "financial-fact",
      source_vintage_id: "source-vintage:fin",
      knowable_at: "2026-02-01T00:00:00+00:00",
      vintage: {
        gross_profit: FILING("0000320193-26-000010", "2025-12-31"),
        total_assets: FILING("0000320193-26-000010", "2025-12-31"),
        revenue: FILING("0000320193-26-000011", "2025-09-30"),
        shares_outstanding: FILING("0000320193-26-000012", "2025-10-17"),
        // A headcount whose evidence names no filing: the accession is null.
        headcount: { accession: null, period_end: "2025-06-30", raw: "raw.fetches:7" },
      },
      inputs: {
        gross_profit: "210000000",
        total_assets: "200000000",
        headcount: "100",
        revenue: "100000000",
        shares_outstanding: "10000000",
      },
    },
    {
      observation_id: "normalized-observation:px",
      semantic_type: "market-price",
      source_vintage_id: "source-vintage:px",
      knowable_at: "2026-03-30T21:00:00+00:00",
      vintage: null,
      inputs: { close: "40" },
    },
    {
      observation_id: "normalized-observation:id",
      semantic_type: "listing-identity",
      source_vintage_id: "source-vintage:id",
      knowable_at: "2026-01-01T00:00:00+00:00",
      vintage: null,
      inputs: {},
    },
  ];
}

function byKey<T extends { key: string }>(rows: readonly T[], key: string): T {
  const row = rows.find((candidate) => candidate.key === key);
  assert(row !== undefined, `no row for ${key}`);
  return row;
}

// --- the mapper -----------------------------------------------------------------------------
{
  const inputs = traceInputsFromLineage(lineage());
  assert(
    inputs.map((input) => input.key).join(",") === TRACE_INPUT_SPECS.map((spec) => spec.key).join(","),
    "the mapper must return the six inputs in the order of TRACE_INPUT_SPECS",
  );
  assert(inputs.length === 6, `expected 6 inputs, got ${inputs.length}`);

  const grossProfit = byKey(inputs, "gross_profit");
  assert(grossProfit.value === "210000000", `gross_profit value: ${grossProfit.value}`);
  assert(grossProfit.period === "2025-12-31", `gross_profit period: ${grossProfit.period}`);
  assert(grossProfit.accession === "0000320193-26-000010", `gross_profit accession: ${grossProfit.accession}`);
  assert(grossProfit.knowableAt === "2026-02-01T00:00:00+00:00", `gross_profit knowableAt: ${grossProfit.knowableAt}`);
  assert(grossProfit.sourceVintageId === "source-vintage:fin", `gross_profit vintage: ${grossProfit.sourceVintageId}`);

  const revenue = byKey(inputs, "revenue");
  assert(revenue.accession === "0000320193-26-000011" && revenue.period === "2025-09-30", "revenue names its own filing");
  const shares = byKey(inputs, "shares_outstanding");
  assert(shares.value === "10000000" && shares.period === "2025-10-17", "shares names its own period");

  // Headcount: a value and a period, but no filing. The accession is null, not invented.
  const headcount = byKey(inputs, "headcount");
  assert(headcount.value === "100" && headcount.period === "2025-06-30", "headcount value and period");
  assert(headcount.accession === null, "a headcount without a filing has no accession");

  // Price: the close, the price observation's own time and vintage, and no accession.
  const price = byKey(inputs, "close");
  assert(price.value === "40", `price value: ${price.value}`);
  assert(price.knowableAt === "2026-03-30T21:00:00+00:00", `price knowableAt: ${price.knowableAt}`);
  assert(price.sourceVintageId === "source-vintage:px", `price vintage: ${price.sourceVintageId}`);
  assert(price.accession === null && price.period === null, "a price has no accession and no period");
}

// --- absent fields stay null; nothing is invented --------------------------------------------
{
  // No lineage items at all: every field of every input is null.
  for (const input of traceInputsFromLineage([])) {
    assert(
      input.value === null &&
        input.period === null &&
        input.knowableAt === null &&
        input.accession === null &&
        input.sourceVintageId === null,
      `${input.key}: an empty lineage must record nothing`,
    );
  }

  // A bank: gross_profit is JSON null in the payload. The key exists with null. Not recorded.
  const bank = lineage();
  (bank[0].inputs as Record<string, unknown>).gross_profit = null;
  delete (bank[0].vintage as Record<string, unknown>).gross_profit;
  const grossProfit = byKey(traceInputsFromLineage(bank), "gross_profit");
  assert(grossProfit.value === null && grossProfit.period === null && grossProfit.accession === null, "a null input is not recorded");
  assert(grossProfit.knowableAt === "2026-02-01T00:00:00+00:00", "the item's knowable_at still travels with a null value");

  // The key is missing from `inputs` (an older view): value is null, not zero or empty text.
  const older = lineage();
  delete (older[0].inputs as Record<string, unknown>).total_assets;
  assert(byKey(traceInputsFromLineage(older), "total_assets").value === null, "a missing key is not recorded");

  // An empty string is not a value.
  const blank = lineage();
  (blank[0].inputs as Record<string, unknown>).revenue = "";
  assert(byKey(traceInputsFromLineage(blank), "revenue").value === null, "an empty string is not recorded");

  // `inputs.period_end` is the fallback for a period that the vintage does not carry.
  const fallback = lineage();
  delete (fallback[0].vintage as Record<string, unknown>).total_assets;
  (fallback[0].inputs as Record<string, unknown>).period_end = "2025-12-31";
  assert(byKey(traceInputsFromLineage(fallback), "total_assets").period === "2025-12-31", "inputs.period_end fallback");
}

// --- a lineage of the wrong shape fails; it is not coerced -----------------------------------
{
  for (const bad of [null, undefined, "x", { a: 1 }, [1], [null]]) {
    let thrown: unknown = null;
    try {
      traceInputsFromLineage(bad);
    } catch (error) {
      thrown = error;
    }
    assert(thrown instanceof MartReadUnavailable, `lineage ${JSON.stringify(bad)} must throw MartReadUnavailable`);
  }
  const numeric = lineage();
  (numeric[0].inputs as Record<string, unknown>).revenue = 100000000;
  let thrown: unknown = null;
  try {
    traceInputsFromLineage(numeric);
  } catch (error) {
    thrown = error;
  }
  assert(thrown instanceof MartReadUnavailable, "a numeric value must throw: values travel as base-10 strings");
}

// --- the page component prints "not recorded" for a null field -------------------------------
{
  const html = renderToStaticMarkup(
    createElement(TraceInputsTable, { inputs: traceInputsFromLineage(lineage()), lineageResultId: "topt-core-result:abc" }),
  );
  for (const expected of ["210000000", "2025-12-31", "0000320193-26-000010", "2026-02-01T00:00:00+00:00", "source-vintage:fin", "topt-core-result:abc"]) {
    assert(html.includes(expected), `the table must show ${expected}: ${html}`);
  }
  const headcountRow = html.slice(html.indexOf('data-input="headcount"'), html.indexOf('data-input="shares_outstanding"'));
  assert(
    headcountRow.includes(`data-field="accession" class="py-1 pr-4">${NOT_RECORDED}<`),
    `a headcount without a filing must print "${NOT_RECORDED}" as its accession: ${headcountRow}`,
  );

  const empty = renderToStaticMarkup(
    createElement(TraceInputsTable, { inputs: traceInputsFromLineage([]), lineageResultId: null }),
  );
  assert(empty.split(`>${NOT_RECORDED}<`).length - 1 === 6 * 5, `an empty lineage must print "${NOT_RECORDED}" in all 30 cells: ${empty}`);
  assert(!/>\s*(null|undefined|0)\s*</.test(empty), "no cell may print null, undefined or 0");
}

// --- traceView reads the lineage of the report's own strategy run ----------------------------
const ISSUER = "issuer:lei:TESTISSUER00000000001";
const CUTOFF = "2026-03-31T00:00:00Z";
const DECISION: StrategyRunDecision = {
  issuer_id: ISSUER,
  cutoff_at: CUTOFF,
  outcome: "selected",
  eligible: true,
  tier: "tech",
  capital_adjusted_labor_efficiency: "12.5",
  current_price_to_sales: "20",
  target_price_to_sales: "18.75",
  valuation_gap: "0.5",
  confidence: "0.85",
  exclusion_reason: null,
  rank: 1,
  target_weight: "1.0",
};

function report(strategyRunId: string | undefined): StrategyRunReport {
  return {
    strategy_id: "large_model_value_v0",
    source: "mart",
    corpus_sha256: "a".repeat(64),
    decisions: [DECISION],
    golden_mismatches: [],
    ...(strategyRunId === undefined ? {} : { strategy_run_id: strategyRunId }),
  };
}

function adapterWith(
  result: StrategyRunReport,
  rows: Record<string, unknown>[],
  calls: Array<{ sql: string; params: readonly unknown[] | undefined }>,
): StrategyRunReadAdapter {
  const repository: StrategyRunRepositoryLike = { getLatest: () => result };
  const client: MartClientLike = {
    query: async (sql, params) => {
      calls.push({ sql, params });
      return { rows };
    },
  };
  return new StrategyRunReadAdapter(repository, (fn) => fn(client));
}

{
  const calls: Array<{ sql: string; params: readonly unknown[] | undefined }> = [];
  const adapter = adapterWith(report("strategy-run:run1"), [{ result_id: "topt-core-result:r1", lineage: lineage() }], calls);
  const state = await loadTrace(CONTEXT, ISSUER, CUTOFF, adapter);
  assert(state.kind === "ready", `expected ready, got ${JSON.stringify(state)}`);
  assert(calls.length === 1, `expected one lineage query, got ${calls.length}`);
  assert(calls[0].sql === TRACE_LINEAGE_SQL, "traceView must run TRACE_LINEAGE_SQL");
  assert(
    JSON.stringify(calls[0].params) === JSON.stringify(["strategy-run:run1", ISSUER, CUTOFF]),
    `the query must bind the report's strategy run, the issuer and the cutoff: ${JSON.stringify(calls[0].params)}`,
  );
  assert(state.data.lineageResultId === "topt-core-result:r1", "the view model names the core result that supplied the lineage");
  assert(byKey(state.data.inputs, "gross_profit").accession === "0000320193-26-000010", "accession reaches the view model");
  assert(byKey(state.data.inputs, "close").value === "40", "price reaches the view model");
}

{
  // A decision with no core result: the query returns no row. Every input is not recorded.
  const calls: Array<{ sql: string; params: readonly unknown[] | undefined }> = [];
  const adapter = adapterWith(report("strategy-run:run1"), [], calls);
  const state = await loadTrace(CONTEXT, ISSUER, CUTOFF, adapter);
  assert(state.kind === "ready", `expected ready, got ${JSON.stringify(state)}`);
  assert(state.data.lineageResultId === null, "no core result, no lineage result id");
  assert(state.data.inputs.length === 6 && state.data.inputs.every((input) => input.value === null && input.accession === null), "no row means nothing recorded");
}

{
  // A report with no strategy run id (the checked-in fixture): no query runs.
  const calls: Array<{ sql: string; params: readonly unknown[] | undefined }> = [];
  const adapter = adapterWith(report(undefined), [{ result_id: "topt-core-result:x", lineage: lineage() }], calls);
  const state = await loadTrace(CONTEXT, ISSUER, CUTOFF, adapter);
  assert(state.kind === "ready" && calls.length === 0, "a fixture report must not query the lineage");
  assert(state.data.inputs.every((input) => input.value === null), "a fixture report records no input");
}

{
  // A lineage of the wrong shape surfaces as an unavailable state, not as an empty table.
  const calls: Array<{ sql: string; params: readonly unknown[] | undefined }> = [];
  const adapter = adapterWith(report("strategy-run:run1"), [{ result_id: "topt-core-result:r1", lineage: { not: "an array" } }], calls);
  const state = await loadTrace(CONTEXT, ISSUER, CUTOFF, adapter);
  assert(state.kind === "unavailable", `a malformed lineage must be unavailable, got ${state.kind}`);
}

console.log("#1241 trace lineage inputs (no database) passed");
