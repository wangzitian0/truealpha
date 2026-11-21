/**
 * #1707: an API read that fails renders the error notice that the route walk detects.
 *
 * `/api/v1/entities/holdings` returned 500 twice inside a staging_gate run, and the walk passed:
 * the page showed the error as plain text with HTTP 200. Each page that uses `loadAuthed` now
 * renders `ReadStateNotice` with kind `error`, which carries `data-read-state="error"`, and the walk
 * fails a route that shows it (`e2e/read-error.mjs`).
 *
 * Run standalone: `bun run tests/read-error-marker.test.ts`.
 */

import { renderToStaticMarkup } from "react-dom/server";
import { READ_ERROR_SELECTOR, readErrorProblems } from "../e2e/read-error.mjs";

const bunTestModuleId = "bun:test";
const { mock } = (await import(bunTestModuleId)) as {
  mock: { module: (id: string, factory: () => Record<string, unknown>) => void };
};

mock.module("@/server/auth/request-context", () => ({
  getServerPrincipal: async () => ({
    principalKind: "administrator",
    context: {
      contextId: "ctx:read-error-test",
      principalId: "principal:test",
      tenantId: "tenant:truealpha",
      sessionId: "sess:test",
      authenticationMethod: "session",
      issuedAt: "2026-11-21T00:00:00Z",
      expiresAt: "2026-11-21T12:00:00Z",
    },
    requestOpts: { headers: { Cookie: "ta_session=test" } },
  }),
}));

// Client components on these pages (forms, run triggers) call `useRouter`; static rendering mounts no app router.
const realNavigation = await import("next/navigation");
mock.module("next/navigation", () => ({ ...realNavigation, useRouter: () => ({ push() {}, replace() {}, refresh() {}, back() {}, forward() {}, prefetch() {} }) }));

const { __setApiFetch } = await import("../src/client/api");

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

const marker = READ_ERROR_SELECTOR.match(/^\[(data-[a-z-]+)="([a-z]+)"\]$/);
assert(marker !== null, `READ_ERROR_SELECTOR must be one attribute selector, got ${READ_ERROR_SELECTOR}`);
const markerAttribute = `${marker[1]}="${marker[2]}"`;

// Every route below reads one API path; the backend answers 500 for all of them.
__setApiFetch(async () => new Response(JSON.stringify({ detail: "statement timeout" }), { status: 500, statusText: "Internal Server Error" }));

const pages: Array<[string, () => Promise<{ default: (props: never) => Promise<unknown> }>, unknown]> = [
  ["/research/holdings", () => import("../src/app/research/holdings/page"), {}],
  ["/research/themes", () => import("../src/app/research/themes/page"), {}],
  ["/research/backtest", () => import("../src/app/research/backtest/page"), {}],
  ["/research/library", () => import("../src/app/research/library/page"), {}],
  ["/admin", () => import("../src/app/admin/page"), {}],
  ["/admin/datahub", () => import("../src/app/admin/datahub/page"), {}],
  ["/research/library/[id]", () => import("../src/app/research/library/[id]/page"), { params: Promise.resolve({ id: "doc:1" }) }],
  ["/research/backtest/[id]", () => import("../src/app/research/backtest/[id]/page"), { params: Promise.resolve({ id: "backtest:1" }) }],
];

for (const [route, load, props] of pages) {
  const { default: Page } = await load();
  const html = renderToStaticMarkup((await Page(props as never)) as never);
  assert(html.includes(markerAttribute), `${route} renders no ${markerAttribute} notice when its API read returns 500`);
}

// The walk turns each notice text into a route problem; no notice means no problem.
assert(readErrorProblems("/research/holdings", []).length === 0, "a page with no error notice must pass");
const problems = readErrorProblems("/research/holdings", ["  API 500:\n statement timeout "]);
assert(
  problems.length === 1 && problems[0] === "read error on /research/holdings: API 500: statement timeout",
  `unexpected walk problem text: ${JSON.stringify(problems)}`,
);

console.log(`ok   read-error marker: ${pages.length} routes render ${markerAttribute} on an API 500`);
