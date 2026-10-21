/**
 * #1255: the macro panel /admin/macro and its loader.
 *
 * The loader and the page are the real ones. The database client is a fake that answers the two
 * statements of the loader and records every statement it receives. So the test proves four things:
 *   - a member and an anonymous visitor are denied before any query runs;
 *   - the loader assumes `mart_readonly`, reads `mart.` relations only, and computes nothing;
 *   - the page shows, per series, the latest value and date, the freshness against the cadence, the
 *     source and the fetch time, and a table of the last 12 months;
 *   - the freshness on the page is the one that the view states. A series that the view calls stale is
 *     stale on the page, whatever its date.
 *
 * Run standalone: `bun run tests/admin-macro.test.ts`.
 */

import { renderToStaticMarkup } from "react-dom/server";

import { formatYieldPercent } from "../src/client/format";
import { loadMacroPanel, MONTHS_SQL, STATUS_SQL } from "../src/server/admin/macro";
import { __setTestClient } from "../src/server/mart/db";

const bunTestModuleId = "bun:test";
const { mock } = (await import(bunTestModuleId)) as {
  mock: { module: (id: string, factory: () => Record<string, unknown>) => void };
};

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

function visibleText(html: string): string {
  return html.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ");
}

const ADMIN = {
  principalKind: "administrator" as const,
  context: {
    contextId: "ctx:macro",
    principalId: "principal:e2e:admin",
    tenantId: "tenant:truealpha",
    sessionId: "sess:macro",
    authenticationMethod: "session" as const,
    issuedAt: "2026-10-21T00:00:00Z",
    expiresAt: "2026-10-21T12:00:00Z",
  },
};
let principal: unknown = ADMIN;
mock.module("@/server/auth/request-context", () => ({
  getServerPrincipal: async () => principal,
}));

const { default: MacroPage } = await import("../src/app/admin/macro/page");

const THREE_MONTH = "us_treasury_bill_3_month";
const TWO_YEAR = "us_treasury_note_2_year";
const TEN_YEAR = "us_treasury_note_10_year";

const statusRows = [
  {
    series_id: THREE_MONTH,
    label: "U.S. Treasury 3-month par yield",
    unit: "annualized_decimal_fraction",
    source_id: "us-treasury-par-yield",
    max_age_days: 5,
    observation_date: "2026-10-20",
    value: "0.0426",
    source: "us-treasury",
    fetched_at: new Date("2026-10-20T20:31:05Z"),
    age_days: 1,
    is_fresh: true,
  },
  {
    series_id: TWO_YEAR,
    label: "U.S. Treasury 2-year par yield",
    unit: "annualized_decimal_fraction",
    source_id: "us-treasury-par-yield",
    max_age_days: 5,
    observation_date: "2026-10-12",
    value: "0.0479",
    source: "us-treasury",
    fetched_at: new Date("2026-10-12T20:30:44Z"),
    age_days: 9,
    is_fresh: false,
  },
  {
    series_id: TEN_YEAR,
    label: "U.S. Treasury 10-year par yield",
    unit: "annualized_decimal_fraction",
    source_id: "us-treasury-par-yield",
    max_age_days: 5,
    observation_date: null,
    value: null,
    source: null,
    fetched_at: null,
    age_days: null,
    is_fresh: false,
  },
];
const monthRows = [
  { series_id: THREE_MONTH, observation_date: "2026-10-20", value: "0.0426" },
  { series_id: THREE_MONTH, observation_date: "2026-10-19", value: "0.0427" },
  { series_id: TWO_YEAR, observation_date: "2026-10-19", value: "0.0477" },
  { series_id: TWO_YEAR, observation_date: "2026-10-12", value: "0.0479" },
];

interface Recorded {
  sql: string;
}

function fakeClient(answers: { status?: unknown[]; months?: unknown[]; failWith?: unknown }): { sent: Recorded[] } {
  const sent: Recorded[] = [];
  __setTestClient({
    query: async (sql: string) => {
      sent.push({ sql });
      if (sql === "set role mart_readonly" || sql === "reset role") return { rows: [] } as never;
      if (answers.failWith !== undefined) throw answers.failWith;
      if (sql === STATUS_SQL) return { rows: answers.status ?? [] } as never;
      if (sql === MONTHS_SQL) return { rows: answers.months ?? [] } as never;
      throw new Error(`unexpected query: ${sql}`);
    },
  } as never);
  return { sent };
}

// --- the formatter is exact on the digits ---
{
  assert(formatYieldPercent("0.0426") === "4.26%", "0.0426 must read 4.26%");
  assert(formatYieldPercent("0.05") === "5.00%", "0.05 must read 5.00%");
  assert(formatYieldPercent("0.0001") === "0.01%", "0.0001 must read 0.01%");
  assert(formatYieldPercent("0.1234") === "12.34%", "0.1234 must read 12.34%");
  assert(formatYieldPercent("abc") === null && formatYieldPercent(null) === null, "a non-decimal reads null");
}

// --- denied: anonymous and member never reach the client ---
{
  const { sent } = fakeClient({ status: statusRows, months: monthRows });
  const anonymous = await loadMacroPanel(null);
  const member = await loadMacroPanel({ principalKind: "member" });
  assert(anonymous.kind === "denied" && member.kind === "denied", "non-administrators must be denied");
  assert(sent.length === 0, "no query may run before the deny decision");
  __setTestClient(null);
}

// --- ready: the role, the statements and the shapes ---
{
  const { sent } = fakeClient({ status: statusRows, months: monthRows });
  const outcome = await loadMacroPanel({ principalKind: "administrator" });
  assert(outcome.kind === "ready", `the loader must be ready: ${JSON.stringify(outcome)}`);
  assert(sent[0].sql === "set role mart_readonly" && sent[sent.length - 1].sql === "reset role", "mart_readonly scope");
  const statements = sent.map((item) => item.sql).filter((sql) => sql !== "set role mart_readonly" && sql !== "reset role");
  assert(statements.length === 2 && statements[0] === STATUS_SQL && statements[1] === MONTHS_SQL, "two statements");
  for (const sql of statements) {
    assert(/\bfrom mart\.macro_series_(status|current)\b/.test(sql), `reads a mart view only: ${sql}`);
    assert(!/\b(raw|staging)\./.test(sql), `reads no raw or staging relation: ${sql}`);
    assert(!/\b(sum|avg|count|min|max)\s*\(/i.test(sql), `computes no aggregate: ${sql}`);
  }
  const { series, months } = outcome.data;
  assert(series.length === 3 && series[0].seriesId === THREE_MONTH, "three series, in the view order");
  assert(series[0].latest?.value === "0.0426" && series[0].latest.fetchedAt === "2026-10-20T20:31:05.000Z", "latest");
  assert(series[0].isFresh && !series[1].isFresh && !series[2].isFresh, "freshness is the view's");
  assert(series[2].latest === null, "a series without an observation has no latest");
  assert(months.length === 4 && months[0].observationDate === "2026-10-20", "the months keep the view order");
  __setTestClient(null);
}

// --- empty, unavailable and error ---
{
  fakeClient({ status: [], months: [] });
  assert((await loadMacroPanel({ principalKind: "administrator" })).kind === "empty", "no catalog row is empty");
  __setTestClient(null);

  fakeClient({ failWith: Object.assign(new Error('relation "mart.macro_series_status" does not exist'), { code: "42P01" }) });
  const missing = await loadMacroPanel({ principalKind: "administrator" });
  assert(missing.kind === "unavailable", `a database before the migration is unavailable: ${JSON.stringify(missing)}`);
  __setTestClient(null);

  fakeClient({ failWith: new Error("connection refused") });
  const failed = await loadMacroPanel({ principalKind: "administrator" });
  assert(failed.kind === "error" && failed.message === "connection refused", "any other failure is an error");
  __setTestClient(null);
}

// --- the page ---
{
  fakeClient({ status: statusRows, months: monthRows });
  principal = ADMIN;
  const html = renderToStaticMarkup(await MacroPage());
  const text = visibleText(html);

  assert(text.includes("Macro base data"), "heading");
  for (const id of [THREE_MONTH, TWO_YEAR, TEN_YEAR]) {
    assert(html.includes(`data-series-id="${id}"`), `a card for ${id}`);
  }
  // the latest value and date, with the exact string in a title
  assert(html.includes(">4.26%<") && html.includes('title="0.0426"'), "3-month latest value and its exact title");
  assert(text.includes("observed 2026-10-20"), "latest date");
  // the freshness against the cadence
  assert(text.includes("fresh · 1 day old, limit 5"), `fresh badge: ${text}`);
  assert(text.includes("stale · 9 days old, limit 5"), "stale badge states age and limit");
  assert(text.includes("stale · no observation yet"), "a series without a row is stale and says why");
  assert(html.includes('data-series-id="us_treasury_note_2_year"') && /data-fresh="false"[^>]*>\s*<h2/.test(html), "stale mark");
  // the source and the fetch time
  assert(text.includes("Source us-treasury"), "source");
  assert(text.includes("Fetched 2026-10-20 20:31 UTC") && text.includes("Fetched 2026-10-12 20:30 UTC"), "fetch times");
  // the 12-month table: one row per date, a column per series, an em-dash for an absent cell
  const rows = html.match(/<tr class="border-t border-border">/g) ?? [];
  assert(rows.length === 3, `one row per observation date (3): ${rows.length}`);
  assert(text.includes("2026-10-19 4.27% 4.77% —"), `row of 2026-10-19: ${text}`);
  assert(text.includes("2026-10-12 — 4.79% —"), "row of 2026-10-12");
  __setTestClient(null);
}

// --- the page for a member and for no data ---
{
  principal = { ...ADMIN, principalKind: "member" };
  fakeClient({ status: statusRows, months: monthRows });
  const denied = visibleText(renderToStaticMarkup(await MacroPage()));
  assert(denied.includes("Access denied"), "a member sees the denied notice");
  assert(!denied.includes("4.26%"), "a member sees no value");
  __setTestClient(null);

  principal = ADMIN;
  fakeClient({ status: [], months: [] });
  const empty = visibleText(renderToStaticMarkup(await MacroPage()));
  assert(empty.includes("No macro series is registered"), "an empty database says so");
  __setTestClient(null);
}

console.log("#1255 admin macro panel passed");
