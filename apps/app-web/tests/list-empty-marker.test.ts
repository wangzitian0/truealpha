/**
 * #1849: a list page that the walk opens shows its empty list with the notice that the walk reads.
 *
 * The walk waits on each list page for a detail link, the empty-list notice or the read-error
 * notice (`e2e/detail-link.mjs`). The library page and the backtest page showed "empty" as free
 * text, so the walk waited the full 15 s for a link that never came. This renders each list page
 * with an empty list and with one item. Empty: the page renders the notice and no detail link.
 * One item: the page renders the detail link and no notice.
 *
 * Run standalone: `bun run tests/list-empty-marker.test.ts`.
 */

import { renderToStaticMarkup } from "react-dom/server";
import { READ_EMPTY_SELECTOR } from "../e2e/detail-link.mjs";

const bunTestModuleId = "bun:test";
const { mock } = (await import(bunTestModuleId)) as {
  mock: { module: (id: string, factory: () => Record<string, unknown>) => void };
};

mock.module("@/server/auth/request-context", () => ({
  getServerPrincipal: async () => ({
    principalKind: "administrator",
    context: {
      contextId: "ctx:list-empty-test",
      principalId: "principal:test",
      tenantId: "tenant:truealpha",
      sessionId: "sess:test",
      authenticationMethod: "session",
      issuedAt: "2026-11-24T00:00:00Z",
      expiresAt: "2026-11-24T12:00:00Z",
    },
    requestOpts: { headers: { Cookie: "ta_session=test" } },
  }),
}));

// The library page holds client forms that call `useRouter`; static rendering mounts no app router.
const realNavigation = await import("next/navigation");
mock.module("next/navigation", () => ({ ...realNavigation, useRouter: () => ({ push() {}, replace() {}, refresh() {}, back() {}, forward() {}, prefetch() {} }) }));

const { __setApiFetch } = await import("../src/client/api");

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

const marker = READ_EMPTY_SELECTOR.match(/^\[(data-[a-z-]+)="([a-z]+)"\]$/);
assert(marker !== null, `READ_EMPTY_SELECTOR must be one attribute selector, got ${READ_EMPTY_SELECTOR}`);
const markerAttribute = `${marker[1]}="${marker[2]}"`;

const sampleRun = {
  run_id: "backtest:test:run:1849",
  strategy_name: "large_model_value_v0",
  status: "succeeded",
  start_date: "2024-04-01",
  end_date: "2024-04-02",
  cagr: 0.1234,
  sharpe: 1.56,
  max_drawdown: -0.045,
  price_basis: "unadjusted_open_explicit_splits",
  claim_ceiling: "price_return_no_dividends",
  definition_content_sha256: "d".repeat(64),
  benchmark_rule: "equal_weight_buy_and_hold_first_cutoff_issuers",
  benchmark_cagr: 0.0987,
  benchmark_max_dd: -0.05,
  metrics_payload: { limitations: ["Price return only without dividends"] },
  created_at: "2026-11-24T00:00:00Z",
};
const sampleDocument = { document_id: "doc:1849", created_at: "2026-11-24T00:00:00Z" };

type ListCase = {
  route: string;
  apiPath: string;
  hrefPrefix: string;
  load: () => Promise<{ default: (props: never) => Promise<unknown> }>;
  props: unknown;
  body: (items: "empty" | "one") => unknown;
};

// The two list pages that the walk opens with `findDetailLink` and that can be empty on a healthy deployment.
const cases: ListCase[] = [
  {
    route: "/research/backtest",
    apiPath: "/api/v1/strategy/backtests",
    hrefPrefix: "/research/backtest/",
    load: () => import("../src/app/research/backtest/page"),
    props: {},
    body: (items) => ({ runs: items === "one" ? [sampleRun] : [] }),
  },
  {
    route: "/research/library",
    apiPath: "/api/v1/library/documents",
    hrefPrefix: "/research/library/",
    load: () => import("../src/app/research/library/page"),
    props: { searchParams: Promise.resolve({}) },
    body: (items) => ({ documents: items === "one" ? [sampleDocument] : [] }),
  },
];

for (const listCase of cases) {
  const { default: Page } = await listCase.load();
  for (const items of ["empty", "one"] as const) {
    let served = 0;
    __setApiFetch(async (input) => {
      const url = String(input);
      if (url.includes(listCase.apiPath)) {
        served += 1;
        return new Response(JSON.stringify(listCase.body(items)), { status: 200, headers: { "Content-Type": "application/json" } });
      }
      return new Response(JSON.stringify({ detail: `unexpected read ${url}` }), { status: 500 });
    });
    const html = renderToStaticMarkup((await Page(listCase.props as never)) as never);
    assert(served === 1, `${listCase.route} read ${listCase.apiPath} ${served} times, expected 1`);
    const hasMarker = html.includes(markerAttribute);
    const hasLink = html.includes(`href="${listCase.hrefPrefix}`);
    if (items === "empty") {
      assert(hasMarker, `${listCase.route} with an empty list renders no ${markerAttribute} notice`);
      assert(!hasLink, `${listCase.route} with an empty list renders a ${listCase.hrefPrefix} link`);
    } else {
      assert(hasLink, `${listCase.route} with one item renders no ${listCase.hrefPrefix} link`);
      assert(!hasMarker, `${listCase.route} with one item renders the ${markerAttribute} notice`);
    }
  }
}

console.log(`ok   list-empty marker: ${cases.length} list pages render ${markerAttribute} when empty and a detail link when not`);
