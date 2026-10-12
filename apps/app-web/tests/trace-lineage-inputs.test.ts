/**
 * #1241: the trace view model and the inputs table, without a database.
 *
 * Proves that the lineage of one `mart.topt_core_meta_info` row maps to the six inputs of a
 * decision for each listing of the issuer (value with currency, period, filed or knowable_at,
 * accession, source), that a field the lineage does not carry stays null and renders
 * "not recorded", that a headcount never takes the filing facts' source vintage, and that
 * `traceView` reads the lineage of the report's own strategy run and carries its `governed` flag.
 * The database half is `trace-lineage-mart.test.ts`.
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
import { GovernedRunNotice, TraceInputsTable } from "../src/components/trace-inputs-table";
import { loadTrace } from "../src/server/dashboard";
import {
  MartReadUnavailable,
  NOT_RECORDED,
  StrategyRunReadAdapter,
  TRACE_INPUT_SPECS,
  TRACE_LINEAGE_SQL,
  traceInputsFromLineage,
  type StrategyRunRepositoryLike,
  type TraceInput,
  type TraceListing,
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

const FILING = (accession: string, periodEnd: string, filed = "2026-02-01") => ({
  accession,
  document: null,
  filed,
  form: "10-K",
  fp: "FY",
  fy: 2025,
  period_end: periodEnd,
  statement_form: true,
});

const CLASS_A = "listing:xnas:goog";
const CLASS_B = "listing:xnas:googl";

/** The two items one listing contributes, as the view emits them. */
function listingItems(listingId: string, tag: string, shares: string, close: string) {
  return [
    {
      observation_id: `normalized-observation:fin-${tag}`,
      semantic_type: "financial-fact",
      source_vintage_id: `source-vintage:fin-${tag}`,
      knowable_at: "2026-02-03T00:00:00+00:00",
      vintage: {
        gross_profit: FILING("0000320193-26-000010", "2025-12-31"),
        total_assets: FILING("0000320193-26-000010", "2025-12-31"),
        revenue: FILING("0000320193-26-000011", "2025-09-30", "2026-01-15"),
        shares_outstanding: FILING("0000320193-26-000012", "2025-10-17", "2026-02-03"),
        // A headcount fact: its own pointer and time. It names no filing.
        headcount: {
          accession: null,
          evidence_ref: "annual report p. 4",
          filed: null,
          knowable_at: "2026-01-20T00:00:00+00:00",
          period_end: "2025-06-30",
          raw: "raw.fetches:7",
        },
      },
      inputs: {
        listing_id: listingId,
        currency: "USD",
        gross_profit: "210000000",
        total_assets: "200000000",
        headcount: "100",
        revenue: "100000000",
        shares_outstanding: shares,
      },
    },
    {
      observation_id: `normalized-observation:px-${tag}`,
      semantic_type: "market-price",
      source_vintage_id: `source-vintage:px-${tag}`,
      knowable_at: "2026-03-30T21:00:00+00:00",
      vintage: null,
      inputs: { listing_id: listingId, currency: "USD", close },
    },
  ];
}

/** An identity item carries a listing id and no input. The mapper ignores it. */
function identityItem(listingId: string, tag: string) {
  return {
    observation_id: `normalized-observation:id-${tag}`,
    semantic_type: "listing-identity",
    source_vintage_id: `source-vintage:id-${tag}`,
    knowable_at: "2026-01-01T00:00:00+00:00",
    vintage: null,
    inputs: { listing_id: listingId },
  };
}

function singleListingLineage() {
  return [...listingItems(CLASS_A, "a", "10000000", "40"), identityItem(CLASS_A, "a")];
}

function dualListingLineage() {
  return [
    ...listingItems(CLASS_B, "b", "6000000", "55.5"),
    ...listingItems(CLASS_A, "a", "10000000", "40"),
    identityItem(CLASS_A, "a"),
    identityItem(CLASS_B, "b"),
  ];
}

function inputOf(listing: TraceListing, key: string): TraceInput {
  const row = listing.inputs.find((candidate) => candidate.key === key);
  assert(row !== undefined, `no row for ${key}`);
  return row;
}

function listingOf(listings: readonly TraceListing[], listingId: string): TraceListing {
  const found = listings.find((candidate) => candidate.listingId === listingId);
  assert(found !== undefined, `no listing ${listingId}: ${listings.map((l) => l.listingId).join(",")}`);
  return found;
}

function throwsUnavailable(run: () => unknown, message: string): void {
  let thrown: unknown = null;
  try {
    run();
  } catch (error) {
    thrown = error;
  }
  assert(thrown instanceof MartReadUnavailable, message);
}

// --- the mapper, one listing -------------------------------------------------------------------
{
  const listings = traceInputsFromLineage(singleListingLineage(), CLASS_A);
  assert(listings.length === 1, `one listing in, one set out: ${listings.length}`);
  const [only] = listings;
  assert(only.listingId === CLASS_A && only.isResultListing, "the set is labeled with its listing");
  assert(
    only.inputs.map((input) => input.key).join(",") === TRACE_INPUT_SPECS.map((spec) => spec.key).join(","),
    "the mapper returns the six inputs in the order of TRACE_INPUT_SPECS",
  );

  const grossProfit = inputOf(only, "gross_profit");
  assert(grossProfit.value === "210000000" && grossProfit.currency === "USD", `gross_profit: ${JSON.stringify(grossProfit)}`);
  assert(grossProfit.period === "2025-12-31", `gross_profit period: ${grossProfit.period}`);
  assert(grossProfit.accession === "0000320193-26-000010", `gross_profit accession: ${grossProfit.accession}`);
  // The filing date is the most exact time of this input; the observation time is the latest of all.
  assert(
    grossProfit.knowableAt === "2026-02-01" && grossProfit.knowableAtBasis === "filed",
    `gross_profit time: ${grossProfit.knowableAt} ${grossProfit.knowableAtBasis}`,
  );
  assert(
    grossProfit.source === "source-vintage:fin-a" && grossProfit.sourceKind === "source vintage",
    `gross_profit source: ${grossProfit.source}`,
  );

  const revenue = inputOf(only, "revenue");
  assert(revenue.accession === "0000320193-26-000011" && revenue.period === "2025-09-30", "revenue names its own filing");
  assert(revenue.knowableAt === "2026-01-15" && revenue.knowableAtBasis === "filed", "revenue names its own filing date");
  const shares = inputOf(only, "shares_outstanding");
  assert(shares.value === "10000000" && shares.period === "2025-10-17", "shares value and period");
  assert(shares.currency === null && !shares.monetary, "a share count is not a monetary value and has no currency");

  // Headcount: its own pointer and its own time. The filing facts' source vintage never labels it.
  const headcount = inputOf(only, "headcount");
  assert(headcount.value === "100" && headcount.period === "2025-06-30", "headcount value and period");
  assert(headcount.accession === null, "a headcount without a filing has no accession");
  assert(
    headcount.source === "raw.fetches:7" && headcount.sourceKind === "raw pointer",
    `headcount source: ${headcount.source} ${headcount.sourceKind}`,
  );
  assert((headcount.source as string | null) !== "source-vintage:fin-a", "the headcount must not carry the filing facts' source vintage");
  assert(
    headcount.knowableAt === "2026-01-20T00:00:00+00:00" && headcount.knowableAtBasis === "input",
    `headcount time: ${headcount.knowableAt} ${headcount.knowableAtBasis}`,
  );
  assert(!headcount.monetary && headcount.currency === null, "a headcount has no currency");

  // Price: the close with its currency, the price observation's own time and vintage, and no accession.
  const price = inputOf(only, "close");
  assert(price.value === "40" && price.currency === "USD", `price: ${price.value} ${price.currency}`);
  assert(
    price.knowableAt === "2026-03-30T21:00:00+00:00" && price.knowableAtBasis === "observation",
    `price time: ${price.knowableAt} ${price.knowableAtBasis}`,
  );
  assert(price.source === "source-vintage:px-a" && price.sourceKind === "source vintage", `price source: ${price.source}`);
  assert(price.accession === null && price.period === null, "a price has no accession and no period");
}

// --- the mapper, two listings (dual-class issuer) ----------------------------------------------
{
  const listings = traceInputsFromLineage(dualListingLineage(), CLASS_A);
  assert(listings.length === 2, `two listings in, two sets out: ${listings.length}`);
  assert(listings[0].listingId === CLASS_A && listings[0].isResultListing, "the listing the result names comes first");
  assert(listings[1].listingId === CLASS_B && !listings[1].isResultListing, "the other listing follows");

  const a = listingOf(listings, CLASS_A);
  const b = listingOf(listings, CLASS_B);
  assert(inputOf(a, "shares_outstanding").value === "10000000", "class A shares under class A");
  assert(inputOf(a, "close").value === "40", "class A close under class A");
  assert(inputOf(b, "shares_outstanding").value === "6000000", "class B shares under class B");
  assert(inputOf(b, "close").value === "55.5", "class B close under class B");
  assert(inputOf(a, "close").source === "source-vintage:px-a", "class A close names its own vintage");
  assert(inputOf(b, "close").source === "source-vintage:px-b", "class B close names its own vintage");
  assert(inputOf(b, "gross_profit").source === "source-vintage:fin-b", "class B gross profit names its own vintage");

  // The result may name the other listing. Then that listing comes first.
  assert(traceInputsFromLineage(dualListingLineage(), CLASS_B)[0].listingId === CLASS_B, "the result's listing leads");
  // The order of the lineage array does not decide which values go to which listing.
  const reversed = traceInputsFromLineage(dualListingLineage().reverse(), CLASS_A);
  assert(inputOf(listingOf(reversed, CLASS_B), "close").value === "55.5", "item order must not decide the owner");
}

// --- the time and the source of an input -------------------------------------------------------
{
  // No filing date: the observation time stands in, and the basis says so.
  const noFiled = singleListingLineage();
  delete (noFiled[0].vintage as Record<string, Record<string, unknown>>).gross_profit.filed;
  const grossProfit = inputOf(traceInputsFromLineage(noFiled, CLASS_A)[0], "gross_profit");
  assert(
    grossProfit.knowableAt === "2026-02-03T00:00:00+00:00" && grossProfit.knowableAtBasis === "observation",
    `observation fallback: ${grossProfit.knowableAt} ${grossProfit.knowableAtBasis}`,
  );

  // Headcount without a raw pointer: the evidence reference. Without both: not recorded. Never the vintage.
  const evidence = singleListingLineage();
  (evidence[0].vintage as Record<string, Record<string, unknown>>).headcount.raw = null;
  const viaEvidence = inputOf(traceInputsFromLineage(evidence, CLASS_A)[0], "headcount");
  assert(
    viaEvidence.source === "annual report p. 4" && viaEvidence.sourceKind === "evidence ref",
    `evidence fallback: ${viaEvidence.source} ${viaEvidence.sourceKind}`,
  );
  (evidence[0].vintage as Record<string, Record<string, unknown>>).headcount.evidence_ref = null;
  const bare = inputOf(traceInputsFromLineage(evidence, CLASS_A)[0], "headcount");
  assert(bare.source === null && bare.sourceKind === null, "a headcount with no pointer has no source");

  // No headcount vintage at all: the observation time stands in and the source is not recorded.
  const none = singleListingLineage();
  delete (none[0].vintage as Record<string, unknown>).headcount;
  const missing = inputOf(traceInputsFromLineage(none, CLASS_A)[0], "headcount");
  assert(missing.source === null && missing.knowableAtBasis === "observation", "no headcount vintage, no headcount source");
}

// --- absent fields stay null; nothing is invented ----------------------------------------------
{
  // No lineage items at all: one set, every field of every input null, under the result's listing.
  const empty = traceInputsFromLineage([], CLASS_A);
  assert(empty.length === 1 && empty[0].listingId === CLASS_A, "an empty lineage keeps the result's listing label");
  for (const input of empty[0].inputs) {
    assert(
      input.value === null &&
        input.currency === null &&
        input.period === null &&
        input.knowableAt === null &&
        input.knowableAtBasis === null &&
        input.accession === null &&
        input.source === null &&
        input.sourceKind === null,
      `${input.key}: an empty lineage must record nothing`,
    );
  }
  assert(traceInputsFromLineage([], null)[0].listingId === null, "no lineage and no result listing: no label");

  // A bank: gross_profit is JSON null in the payload. The key exists with null. Not recorded.
  const bank = singleListingLineage();
  (bank[0].inputs as Record<string, unknown>).gross_profit = null;
  delete (bank[0].vintage as Record<string, unknown>).gross_profit;
  const grossProfit = inputOf(traceInputsFromLineage(bank, CLASS_A)[0], "gross_profit");
  assert(grossProfit.value === null && grossProfit.period === null && grossProfit.accession === null, "a null input is not recorded");

  // The key is missing from `inputs` (an older view): value is null, not zero or empty text.
  const older = singleListingLineage();
  delete (older[0].inputs as Record<string, unknown>).total_assets;
  assert(inputOf(traceInputsFromLineage(older, CLASS_A)[0], "total_assets").value === null, "a missing key is not recorded");

  // An empty string is not a value.
  const blank = singleListingLineage();
  (blank[0].inputs as Record<string, unknown>).revenue = "";
  assert(inputOf(traceInputsFromLineage(blank, CLASS_A)[0], "revenue").value === null, "an empty string is not recorded");

  // A price with no currency in the payload: the value stays, the currency is null.
  const noCurrency = singleListingLineage();
  delete (noCurrency[1].inputs as Record<string, unknown>).currency;
  const price = inputOf(traceInputsFromLineage(noCurrency, CLASS_A)[0], "close");
  assert(price.value === "40" && price.currency === null, "a missing currency stays null");
}

// --- a lineage of the wrong shape fails; it is not coerced -------------------------------------
{
  for (const bad of [null, undefined, "x", { a: 1 }, [1], [null]]) {
    throwsUnavailable(() => traceInputsFromLineage(bad), `lineage ${JSON.stringify(bad)} must throw MartReadUnavailable`);
  }
  const numeric = singleListingLineage();
  (numeric[0].inputs as Record<string, unknown>).revenue = 100000000;
  throwsUnavailable(() => traceInputsFromLineage(numeric, CLASS_A), "a numeric value must throw: values travel as base-10 strings");

  // An input item that names no listing cannot be given to a listing. It fails; it is not guessed.
  const unowned = singleListingLineage();
  delete (unowned[0].inputs as Record<string, unknown>).listing_id;
  throwsUnavailable(() => traceInputsFromLineage(unowned, CLASS_A), "a financial item with no listing_id must throw");
}

// --- the table prints currency, basis, listing labels and "not recorded" -----------------------
{
  const listings = traceInputsFromLineage(dualListingLineage(), CLASS_A);
  const html = renderToStaticMarkup(createElement(TraceInputsTable, { listings, lineageResultId: "topt-core-result:abc" }));
  for (const expected of [
    "210000000 USD",
    "40 USD",
    "55.5 USD",
    "2026-02-01 (filed)",
    "2026-01-20T00:00:00+00:00 (input knowable_at)",
    "2026-03-30T21:00:00+00:00 (observation knowable_at)",
    "0000320193-26-000010",
    "raw.fetches:7 (raw pointer)",
    "source-vintage:fin-a (source vintage)",
    "topt-core-result:abc",
    "Filed / knowable at",
  ]) {
    assert(html.includes(expected), `the table must show ${expected}: ${html}`);
  }
  // One block per listing, each labeled, and the shares sit under the right label.
  const blockA = html.slice(html.indexOf(`data-listing="${CLASS_A}"`), html.indexOf(`data-listing="${CLASS_B}"`));
  const blockB = html.slice(html.indexOf(`data-listing="${CLASS_B}"`));
  assert(blockA.includes("10000000") && !blockA.includes("6000000"), "class A block holds class A shares only");
  assert(blockB.includes("6000000") && !blockB.includes(">10000000<"), "class B block holds class B shares only");
  assert(blockA.includes("(listing named by the core result)") && !blockB.includes("(listing named by the core result)"), "only one listing is the result's");
  const headcountCell = blockA.slice(blockA.indexOf('data-input="headcount"'), blockA.indexOf('data-input="shares_outstanding"'));
  assert(
    headcountCell.includes(`data-field="accession" class="py-1 pr-4">${NOT_RECORDED}<`),
    `a headcount without a filing must print "${NOT_RECORDED}" as its accession: ${headcountCell}`,
  );

  const empty = renderToStaticMarkup(
    createElement(TraceInputsTable, { listings: traceInputsFromLineage([], null), lineageResultId: null }),
  );
  assert(empty.split(`>${NOT_RECORDED}<`).length - 1 === 6 * 5, `an empty lineage must print "${NOT_RECORDED}" in all 30 cells: ${empty}`);
  assert(!/>\s*(null|undefined|0)\s*</.test(empty), "no cell may print null, undefined or 0");

  // A monetary value without a currency prints the gap, not a guess.
  const noCurrency = singleListingLineage();
  delete (noCurrency[1].inputs as Record<string, unknown>).currency;
  const gap = renderToStaticMarkup(
    createElement(TraceInputsTable, { listings: traceInputsFromLineage(noCurrency, CLASS_A), lineageResultId: null }),
  );
  assert(gap.includes(`40 (currency ${NOT_RECORDED})`), `a price with no currency must say so: ${gap}`);
}

// --- the governed notice -----------------------------------------------------------------------
{
  assert(renderToStaticMarkup(createElement(GovernedRunNotice, { governed: true })) === "", "a governed run needs no note");
  const fallback = renderToStaticMarkup(createElement(GovernedRunNotice, { governed: false }));
  assert(fallback.includes("not the governed head") && fallback.includes("newest recorded run"), `fallback note: ${fallback}`);
  const unknown = renderToStaticMarkup(createElement(GovernedRunNotice, { governed: null }));
  assert(unknown.includes(NOT_RECORDED), `unknown governance is said to be not recorded: ${unknown}`);
}

// --- traceView reads the lineage of the report's own strategy run ------------------------------
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

function report(strategyRunId: string | undefined, governed?: boolean): StrategyRunReport {
  return {
    strategy_id: "large_model_value_v0",
    source: "mart",
    corpus_sha256: "a".repeat(64),
    decisions: [DECISION],
    golden_mismatches: [],
    ...(strategyRunId === undefined ? {} : { strategy_run_id: strategyRunId }),
    ...(governed === undefined ? {} : { governed }),
  };
}

type Call = { sql: string; params: readonly unknown[] | undefined };

function adapterWith(result: StrategyRunReport, rows: Record<string, unknown>[], calls: Call[]): StrategyRunReadAdapter {
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
  const calls: Call[] = [];
  const row = { result_id: "topt-core-result:r1", listing_id: CLASS_A, lineage: dualListingLineage() };
  const state = await loadTrace(CONTEXT, ISSUER, CUTOFF, adapterWith(report("strategy-run:run1", true), [row], calls));
  assert(state.kind === "ready", `expected ready, got ${JSON.stringify(state)}`);
  assert(calls.length === 1, `expected one lineage query, got ${calls.length}`);
  assert(calls[0].sql === TRACE_LINEAGE_SQL, "traceView must run TRACE_LINEAGE_SQL");
  assert(
    JSON.stringify(calls[0].params) === JSON.stringify(["strategy-run:run1", ISSUER, CUTOFF]),
    `the query must bind the report's strategy run, the issuer and the cutoff: ${JSON.stringify(calls[0].params)}`,
  );
  assert(state.data.lineageResultId === "topt-core-result:r1", "the view model names the core result that supplied the lineage");
  assert(state.data.governed === true, `a governed report stays governed: ${state.data.governed}`);
  assert(state.data.listings.length === 2 && state.data.listings[0].listingId === CLASS_A, "the result's listing leads");
  assert(inputOf(listingOf(state.data.listings, CLASS_B), "close").value === "55.5", "class B close reaches the view model");
  assert(inputOf(listingOf(state.data.listings, CLASS_A), "gross_profit").accession === "0000320193-26-000010", "accession reaches the view model");
}

{
  // The report says the run is not the governed head. The view model says so too.
  const row = { result_id: "topt-core-result:r1", listing_id: CLASS_A, lineage: singleListingLineage() };
  const state = await loadTrace(CONTEXT, ISSUER, CUTOFF, adapterWith(report("strategy-run:run1", false), [row], []));
  assert(state.kind === "ready" && state.data.governed === false, "a fallback run must not read as governed");
}

{
  // A decision with no core result: the query returns no row. Every input is not recorded.
  const state = await loadTrace(CONTEXT, ISSUER, CUTOFF, adapterWith(report("strategy-run:run1", true), [], []));
  assert(state.kind === "ready", `expected ready, got ${JSON.stringify(state)}`);
  assert(state.data.lineageResultId === null, "no core result, no lineage result id");
  assert(
    state.data.listings.length === 1 &&
      state.data.listings[0].inputs.length === 6 &&
      state.data.listings[0].inputs.every((input) => input.value === null && input.accession === null),
    "no row means nothing recorded",
  );
}

{
  // A report with no strategy run id (the checked-in fixture): no query runs, and governance is unknown.
  const calls: Call[] = [];
  const row = { result_id: "topt-core-result:x", listing_id: CLASS_A, lineage: singleListingLineage() };
  const state = await loadTrace(CONTEXT, ISSUER, CUTOFF, adapterWith(report(undefined), [row], calls));
  assert(state.kind === "ready" && calls.length === 0, "a fixture report must not query the lineage");
  assert(state.data.governed === null, "a report that does not say is governed:null");
  assert(state.data.listings[0].inputs.every((input) => input.value === null), "a fixture report records no input");
}

{
  // A lineage of the wrong shape surfaces as an unavailable state, not as an empty table.
  const row = { result_id: "topt-core-result:r1", listing_id: CLASS_A, lineage: { not: "an array" } };
  const state = await loadTrace(CONTEXT, ISSUER, CUTOFF, adapterWith(report("strategy-run:run1", true), [row], []));
  assert(state.kind === "unavailable", `a malformed lineage must be unavailable, got ${state.kind}`);
}

console.log("#1241 trace lineage inputs (no database) passed");
