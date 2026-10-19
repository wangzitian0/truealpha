/**
 * #758: /research/backtest shows what a run can claim.
 *
 * Inserts one backtest run, as the nightly lane writes it, then renders the list page and the
 * detail page with React's server renderer over the real repository (mart_readonly). The pages
 * must show the price basis, the claim ceiling, the definition hash and the benchmark, and must
 * not name VectorBT: no vectorbt package runs the simulation.
 *
 * Run standalone: `bun run tests/backtest-page.test.ts`.
 */

import { randomBytes } from "node:crypto";

import { Client } from "pg";
import { renderToStaticMarkup } from "react-dom/server";

import BacktestListPage from "../src/app/research/backtest/page";
import BacktestDetailPage from "../src/app/research/backtest/[id]/page";
import { getBacktestRun, getBacktestValuations } from "../src/server/mart/backtest-repository";

import { requireDb } from "./helpers/db-guard";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

const dbUrl = requireDb("backtest-page", { write: true });
if (!dbUrl) {
  process.exit(0);
}
const DATABASE_URL: string = dbUrl;
process.env.DATABASE_URL = DATABASE_URL;

const hex64 = () => randomBytes(32).toString("hex");
const runId = `backtest-run:${hex64()}`;
const definitionSha = hex64();
const LIMITATIONS = [
  "Price return only. No dividend source exists, so dividends are not included.",
  "Each bar and each input uses its latest stored vintage. A later vendor restatement can change history.",
];

const admin = new Client({ connectionString: DATABASE_URL, connectionTimeoutMillis: 3000 });
await admin.connect();
try {
  await admin.query(
    `insert into mart.backtest_runs (
       run_id, strategy_key, strategy_version, universe_id, start_date, end_date, status,
       cagr_monthly, sharpe_daily, max_dd_daily, vol_daily, turnover_monthly, calmar_daily,
       metrics_payload, executed_at, definition_content_sha256, price_basis, claim_ceiling,
       benchmark_rule, benchmark_cagr, benchmark_max_dd
     ) values (
       $1, 'large_model_value_v0', '0.1.0', 'strategy-backtest-inputs:2-symbols:0123456789abcdef',
       '2024-04-01', '2024-10-11', 'succeeded', 0.1234, 1.1, 0.2, 0.25, 0.05, 0.6,
       $3::jsonb, now(), $2, 'unadjusted_open_explicit_splits', 'price_return_no_dividends',
       'equal_weight_buy_and_hold_first_cutoff_issuers', 0.0987, 0.15
     )`,
    [runId, definitionSha, JSON.stringify({ limitations: LIMITATIONS, benchmark: { never_filled: ["T758E"] } })],
  );
  await admin.query(
    `insert into mart.backtest_valuations (
       run_id, resolution, valuation_date, cum_nav, drawdown, gross_exposure, cash_weight, benchmark_cum_nav
     ) values ($1, '1D', '2024-04-01', 0.999, 0, 1, 0, 0.998), ($1, '1D', '2024-04-02', 1.01, 0, 1, 0, 1.004)`,
    [runId],
  );

  // The repository returns the new columns through mart_readonly.
  const run = await getBacktestRun(runId);
  assert(run !== null, "the inserted run must be readable");
  assert(run.price_basis === "unadjusted_open_explicit_splits", `price_basis: ${run.price_basis}`);
  assert(run.claim_ceiling === "price_return_no_dividends", `claim_ceiling: ${run.claim_ceiling}`);
  assert(run.definition_content_sha256 === definitionSha, "the definition hash round-trips");
  assert(run.benchmark_cagr === 0.0987, `benchmark_cagr: ${run.benchmark_cagr}`);
  const daily = await getBacktestValuations(runId, "1D");
  assert(daily.map((row) => row.benchmark_cum_nav).join(",") === "0.998,1.004", "each session carries the benchmark NAV");

  // The detail page shows the basis, the ceiling, the definition and the benchmark.
  const detail = renderToStaticMarkup(await BacktestDetailPage({ params: Promise.resolve({ id: runId }) }));
  assert(detail.includes('data-price-basis="unadjusted_open_explicit_splits"'), "the detail page lost the price basis");
  assert(detail.includes("Dividends are not included"), "the detail page must state the claim ceiling in words");
  assert(detail.includes('data-claim-ceiling="price_return_no_dividends"'), "the detail page lost the claim ceiling");
  assert(detail.includes(definitionSha), "the detail page lost the definition hash");
  assert(detail.includes('data-benchmark-rule="equal_weight_buy_and_hold_first_cutoff_issuers"'), "the detail page lost the benchmark");
  assert(detail.includes("CAGR 9.9%"), "the detail page must show the benchmark CAGR");
  assert(
    detail.includes("Equal weight over the issuers with inputs at the first cutoff") &&
      detail.includes("bought at its next open"),
    "the detail page must say which universe the benchmark holds and how a late symbol fills",
  );
  assert(!/vectorbt/i.test(detail), "the detail page names VectorBT");
  assert(
    detail.includes('data-benchmark-never-filled="T758E"') && detail.includes("held as cash for the whole run: T758E"),
    "the detail page must show the benchmark symbols that were never bought",
  );
  for (const item of LIMITATIONS) {
    assert(detail.includes(item), `the detail page lost the limitation: ${item}`);
  }

  // #1358 AC1: Next.js passes percent-encoded dynamic segment params when the id contains ':'.
  const encodedDetail = renderToStaticMarkup(
    await BacktestDetailPage({ params: Promise.resolve({ id: encodeURIComponent(runId) }) }),
  );
  assert(
    encodedDetail.includes('data-price-basis="unadjusted_open_explicit_splits"'),
    "BacktestDetailPage must decode percent-encoded run_id params without throwing notFound()",
  );

  // The list page shows the basis and the ceiling for each run, and no VectorBT claim.
  const list = renderToStaticMarkup(await BacktestListPage());
  assert(list.includes(runId), "the list page lost the inserted run");
  assert(
    list.includes(`/research/backtest/${encodeURIComponent(runId)}`),
    "the list page must percent-encode run_id in the detail link href",
  );
  assert(list.includes('data-claim-ceiling="price_return_no_dividends"'), "the list page lost the claim ceiling");
  assert(!/vectorbt/i.test(list), "the list page names VectorBT");
  assert(list.includes("Limitations (2)"), "the list page lost the count of limitations");
  assert(list.includes(LIMITATIONS[1]), "the list page lost a limitation");
  console.log("backtest-page: basis, ceiling, definition and benchmark render on both pages — OK");
} finally {
  await admin.query("delete from mart.backtest_runs where run_id = $1", [runId]).catch(() => {});
  await admin.end();
}
process.exit(0);
