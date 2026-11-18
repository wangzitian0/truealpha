/**
 * #758 / #1358: Backtest list and detail pages render basis, ceiling, definition hash,
 * benchmark metadata, and percent-encoded dynamic route params from OpenAPI responses.
 */

import { renderToStaticMarkup } from "react-dom/server";

const bunTestModuleId = "bun:test";
const { mock } = (await import(bunTestModuleId)) as {
  mock: { module: (id: string, factory: () => Record<string, unknown>) => void };
};

mock.module("@/server/auth/request-context", () => ({
  getServerPrincipal: async () => ({
    principalKind: "administrator",
    context: {
      contextId: "ctx:backtest-test",
      principalId: "principal:test",
      tenantId: "tenant:truealpha",
      sessionId: "sess:test",
      authenticationMethod: "session",
      issuedAt: "2026-10-19T00:00:00Z",
      expiresAt: "2026-10-19T12:00:00Z",
    },
    requestOpts: { headers: { Cookie: "ta_session=test" } },
  }),
}));

const { __setApiFetch } = await import("../src/client/api");
const { default: BacktestListPage } = await import("../src/app/research/backtest/page");
const { default: BacktestDetailPage } = await import("../src/app/research/backtest/[id]/page");

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

const LIMITATIONS = [
  "Price return only without dividends",
  "Universe limited to covered issuers at cutoff",
];
const definitionSha = "d".repeat(64);
const runId = "backtest:test:run:758";

const sampleRun = {
  run_id: runId,
  strategy_name: "large_model_value_v0",
  status: "succeeded",
  start_date: "2024-04-01",
  end_date: "2024-04-02",
  cagr: 0.1234,
  sharpe: 1.56,
  max_drawdown: -0.045,
  price_basis: "unadjusted_open_explicit_splits",
  claim_ceiling: "price_return_no_dividends",
  definition_content_sha256: definitionSha,
  benchmark_rule: "equal_weight_buy_and_hold_first_cutoff_issuers",
  benchmark_cagr: 0.0987,
  benchmark_max_dd: -0.05,
  metrics_payload: {
    limitations: LIMITATIONS,
    benchmark: {
      rule: "equal_weight_buy_and_hold_first_cutoff_issuers",
      never_filled: ["T758E"],
    },
  },
  created_at: "2026-10-19T00:00:00Z",
};

const sampleValuations = [
  { valuation_date: "2024-04-01", cum_nav: 0.999, drawdown: 0, gross_exposure: 1, cash_weight: 0, benchmark_cum_nav: 0.998 },
  { valuation_date: "2024-04-02", cum_nav: 1.01, drawdown: 0, gross_exposure: 1, cash_weight: 0, benchmark_cum_nav: 1.004 },
];

__setApiFetch(async (input) => {
  const url = String(input);
  if (url.endsWith("/api/v1/strategy/backtests") || url.includes("/api/v1/strategy/backtests?")) {
    return new Response(JSON.stringify({ runs: [sampleRun] }), { status: 200, headers: { "Content-Type": "application/json" } });
  }
  if (url.includes(`/api/v1/strategy/backtests/${encodeURIComponent(runId)}`)) {
    return new Response(
      JSON.stringify({ run: sampleRun, valuations: sampleValuations }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    );
  }
  return new Response(JSON.stringify({ detail: `unmatched ${url}` }), { status: 404 });
});

try {
  const detail = renderToStaticMarkup(await BacktestDetailPage({ params: Promise.resolve({ id: runId }) }));
  assert(detail.includes('data-price-basis="unadjusted_open_explicit_splits"'), "detail shows price basis");
  assert(detail.includes("Dividends are not included"), "detail states claim ceiling in words");
  assert(detail.includes('data-claim-ceiling="price_return_no_dividends"'), "detail marks claim ceiling");
  assert(detail.includes(definitionSha), "detail shows definition hash");
  assert(detail.includes('data-benchmark-rule="equal_weight_buy_and_hold_first_cutoff_issuers"'), "detail shows benchmark rule");
  assert(detail.includes("CAGR 9.9%"), "detail formats benchmark CAGR");
  assert(detail.includes('data-benchmark-never-filled="T758E"'), "detail shows never-filled benchmark symbol");
  assert(!/vectorbt/i.test(detail), "detail does not mention VectorBT");
  for (const item of LIMITATIONS) assert(detail.includes(item), `detail lost limitation: ${item}`);

  const encoded = renderToStaticMarkup(
    await BacktestDetailPage({ params: Promise.resolve({ id: encodeURIComponent(runId) }) }),
  );
  assert(encoded.includes('data-price-basis="unadjusted_open_explicit_splits"'), "percent-encoded id decoded cleanly");

  const list = renderToStaticMarkup(await BacktestListPage());
  assert(list.includes(runId) && list.includes(`/research/backtest/${encodeURIComponent(runId)}`), "list links run id");
  assert(list.includes('data-claim-ceiling="price_return_no_dividends"'), "list shows claim ceiling");
  assert(!/vectorbt/i.test(list), "list does not mention VectorBT");
  assert(list.includes("Limitations (2)") && list.includes(LIMITATIONS[1]), "list summarizes limitations");
  console.log("backtest-page: basis, ceiling, definition and benchmark render on both pages — OK");
} finally {
  __setApiFetch(null);
}
