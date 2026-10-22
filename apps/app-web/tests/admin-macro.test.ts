/**
 * #1255: the macro panel /admin/macro and its loader.
 *
 * The loader and the page are the real ones. The database client is a fake that answers the two
 * statements of the loader by the view they read and records every statement it receives. So the test
 * proves these things:
 *   - a member, a service and an anonymous visitor are denied before any query runs;
 *   - the loader assumes `mart_readonly`, reads `mart.` views only, and computes nothing;
 *   - each card of the page shows its own value and date, its own freshness against the cadence, its own
 *     source and its own fetch time, and the table shows the last 12 months cell by cell;
 *   - the freshness on the page is the one that the view states. A series that the view calls stale is
 *     stale on the page, whatever its date, and a null verdict is stale;
 *   - the unit of the series decides how a value reads. A yield reads as a percent. Any other unit reads
 *     as the exact value and the unit.
 *
 * The statements themselves run against a real Postgres in `tests/admin-macro-sql.test.ts`. A fake
 * cannot show that a column exists or that the window and the order are right.
 *
 * Run standalone: `bun run tests/admin-macro.test.ts`.
 */

import { renderToStaticMarkup } from "react-dom/server";

import { formatMacroValue, formatYieldPercent } from "../src/client/format";
import { loadMacroPanel } from "../src/server/admin/macro";
import { __setTestClient } from "../src/server/mart/db";

const bunTestModuleId = "bun:test";
const { mock } = (await import(bunTestModuleId)) as {
  mock: { module: (id: string, factory: () => Record<string, unknown>) => void };
};

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

function visibleText(html: string): string {
  return html.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim();
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

const pageModule = await import("../src/app/admin/macro/page");
const MacroPage = pageModule.default;

const THREE_MONTH = "us_treasury_bill_3_month";
const TWO_YEAR = "us_treasury_note_2_year";
const TEN_YEAR = "us_treasury_note_10_year";
const YIELD_UNIT = "annualized_decimal_fraction";

// The view orders the series by id, as text: 3_month, 10_year, 2_year.
const statusRows = [
  {
    series_id: THREE_MONTH,
    label: "U.S. Treasury 3-month par yield",
    unit: YIELD_UNIT,
    source_id: "us-treasury-par-yield",
    max_age_days: 5,
    observation_day: "2026-10-20",
    value: "0.0426",
    source: "us-treasury",
    fetched_at: new Date("2026-10-20T20:31:05Z"),
    age_days: 1,
    is_fresh: true,
  },
  {
    series_id: TEN_YEAR,
    label: "U.S. Treasury 10-year par yield",
    unit: YIELD_UNIT,
    source_id: "us-treasury-par-yield",
    max_age_days: 5,
    observation_day: null,
    value: null,
    source: null,
    fetched_at: null,
    age_days: null,
    is_fresh: false,
  },
  {
    series_id: TWO_YEAR,
    label: "U.S. Treasury 2-year par yield",
    unit: YIELD_UNIT,
    source_id: "us-treasury-par-yield",
    max_age_days: 5,
    observation_day: "2026-10-12",
    value: "0.0479",
    source: "us-treasury",
    fetched_at: new Date("2026-10-12T20:30:44Z"),
    age_days: 9,
    is_fresh: false,
  },
];
const monthRows = [
  { series_id: THREE_MONTH, observation_day: "2026-10-20", value: "0.0426" },
  { series_id: THREE_MONTH, observation_day: "2026-10-19", value: "0.0427" },
  { series_id: TWO_YEAR, observation_day: "2026-10-19", value: "0.0477" },
  { series_id: TWO_YEAR, observation_day: "2026-10-12", value: "0.0479" },
];

interface Recorded {
  sql: string;
}

/** Answers by the view that a statement reads, not by its text, so a rewritten statement is still answered. */
function fakeClient(answers: { status?: unknown[]; months?: unknown[]; failWith?: unknown }): { sent: Recorded[] } {
  const sent: Recorded[] = [];
  __setTestClient({
    query: async (sql: string) => {
      sent.push({ sql });
      if (sql === "set role mart_readonly" || sql === "reset role") return { rows: [] } as never;
      if (answers.failWith !== undefined) throw answers.failWith;
      if (/\bfrom mart\.macro_series_status\b/.test(sql)) return { rows: answers.status ?? [] } as never;
      if (/\bfrom mart\.macro_series_current\b/.test(sql)) return { rows: answers.months ?? [] } as never;
      throw new Error(`unexpected query: ${sql}`);
    },
  } as never);
  return { sent };
}

/** The markup of the card of one series. A card holds no nested article, so the first closing tag ends it. */
function cardOf(html: string, seriesId: string): string {
  const match = new RegExp(`<article[^>]*data-series-id="${seriesId}"[^>]*>[\\s\\S]*?</article>`).exec(html);
  assert(match !== null, `no card for ${seriesId}`);
  return match[0];
}

interface Cell {
  text: string;
  title: string | null;
}

/** The rows of the 12-month table, header first, as the text and the title of each cell. */
function tableOf(html: string): Cell[][] {
  const table = /<table[^>]*data-macro-series="months"[^>]*>[\s\S]*?<\/table>/.exec(html);
  assert(table !== null, "no 12-month table");
  const rows = table[0].match(/<tr[^>]*>[\s\S]*?<\/tr>/g) ?? [];
  return rows.map((row) =>
    (row.match(/<t[dh]([^>]*)>([\s\S]*?)<\/t[dh]>/g) ?? []).map((cell) => {
      const parts = /<t[dh]([^>]*)>([\s\S]*?)<\/t[dh]>/.exec(cell);
      assert(parts !== null, "cell");
      const title = /title="([^"]*)"/.exec(parts[1]);
      return { text: visibleText(parts[2]), title: title === null ? null : title[1] };
    }),
  );
}

// --- the yield formatter cuts digits; it does not round and does not use binary floats ---
{
  const cases: [string, string][] = [
    ["0.0426", "4.26%"],
    ["0.05", "5.00%"],
    ["0.0001", "0.01%"],
    ["0.1234", "12.34%"],
    ["0", "0.00%"],
    ["1", "100.00%"],
    ["12", "1200.00%"],
    // Digits beyond the second decimal of the percent are cut. A rounding implementation fails the first two.
    ["0.00286", "0.28%"],
    ["0.00999", "0.99%"],
    ["0.04265678", "4.26%"],
    ["0.0426000", "4.26%"],
    // A binary float floors these to one hundredth less (0.0058 * 100 * 100 is 57.99999999999999).
    ["0.0058", "0.58%"],
    ["0.0029", "0.29%"],
    ["0.0115", "1.15%"],
    ["0.0007", "0.07%"],
    // A negative value keeps its sign. A value that cuts to zero has no sign.
    ["-0.0125", "-1.25%"],
    ["-0.0001", "-0.01%"],
    ["-0.00001", "0.00%"],
    ["-0", "0.00%"],
  ];
  for (const [value, expected] of cases) {
    const got = formatYieldPercent(value);
    assert(got === expected, `${value} must read ${expected}, got ${got}`);
  }
  for (const value of ["abc", "", " ", "1e-2", ".5", "5.", "+0.05", "NaN", "Infinity", "0.05abc", "5%", "0,05"]) {
    assert(formatYieldPercent(value) === null, `${JSON.stringify(value)} is not a plain decimal and must read null`);
  }
  assert(formatYieldPercent(null) === null && formatYieldPercent(undefined) === null, "null and undefined read null");
}

// --- the unit decides how a value reads ---
{
  assert(formatMacroValue(YIELD_UNIT, "0.0426") === "4.26%", "a yield reads as a percent");
  assert(formatMacroValue("index_points", "0.0426") === "0.0426 index_points", "another unit reads as value and unit");
  assert(formatMacroValue("index_points", "123.456") === "123.456 index_points", "the exact value stays");
  assert(formatMacroValue(YIELD_UNIT, "abc") === `abc ${YIELD_UNIT}`, "a yield that is not a decimal reads as it is");
  assert(formatMacroValue(YIELD_UNIT, null) === null, "no value reads null");
}

// --- denied: anonymous, member and service never reach the client ---
{
  const { sent } = fakeClient({ status: statusRows, months: monthRows });
  const anonymous = await loadMacroPanel(null);
  const member = await loadMacroPanel({ principalKind: "member" });
  const service = await loadMacroPanel({ principalKind: "service" });
  assert(
    anonymous.kind === "denied" && member.kind === "denied" && service.kind === "denied",
    "only an administrator may read the panel",
  );
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
  assert(statements.length === 2, `two statements, got ${statements.length}`);
  assert(/\bfrom mart\.macro_series_status\b/.test(statements[0]), `the first statement reads the status view: ${statements[0]}`);
  assert(/\bfrom mart\.macro_series_current\b/.test(statements[1]), `the second statement reads the current view: ${statements[1]}`);
  for (const sql of statements) {
    assert(!/\b(raw|staging)\./.test(sql), `reads no raw or staging relation: ${sql}`);
    assert(!/\bfrom\s+(?!mart\.)/i.test(sql), `reads a mart relation only: ${sql}`);
    assert(!/\b(sum|avg|count|min|max)\s*\(/i.test(sql), `computes no aggregate: ${sql}`);
  }
  const { series, months } = outcome.data;
  assert(
    series.map((item) => item.seriesId).join() === [THREE_MONTH, TEN_YEAR, TWO_YEAR].join(),
    "three series, in the view order",
  );
  assert(series[0].latest?.value === "0.0426" && series[0].latest.fetchedAt === "2026-10-20T20:31:05.000Z", "latest");
  assert(series[0].latest?.observationDate === "2026-10-20", "the date is the text of the view");
  assert(series[0].isFresh && !series[1].isFresh && !series[2].isFresh, "freshness is the view's");
  assert(series[1].latest === null, "a series without an observation has no latest");
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

// --- the page: each card on its own ---
{
  fakeClient({ status: statusRows, months: monthRows });
  principal = ADMIN;
  const html = renderToStaticMarkup(await MacroPage());
  const text = visibleText(html);
  assert(text.includes("Macro base data"), "heading");
  assert(pageModule.dynamic === "force-dynamic", "the page reads the database on every request");

  const cards = html.match(/<article[^>]*data-series-id="/g) ?? [];
  assert(cards.length === 3, `one card for each series (3): ${cards.length}`);

  const fresh = cardOf(html, THREE_MONTH);
  const freshText = visibleText(fresh);
  assert(fresh.includes('data-fresh="true"') && fresh.includes('data-freshness="fresh"'), "3-month is marked fresh");
  assert(fresh.includes(">4.26%<") && fresh.includes('title="0.0426"'), `3-month value and exact title: ${fresh}`);
  assert(freshText.includes("observed 2026-10-20"), "3-month date");
  assert(freshText.includes("fresh · 1 day old, limit 5"), `3-month badge: ${freshText}`);
  assert(freshText.includes("Source us-treasury"), "3-month source");
  assert(freshText.includes("Fetched 2026-10-20 20:31 UTC"), "3-month fetch time");

  const stale = cardOf(html, TWO_YEAR);
  const staleText = visibleText(stale);
  assert(stale.includes('data-fresh="false"') && stale.includes('data-freshness="stale"'), "2-year is marked stale");
  assert(!stale.includes('data-fresh="true"') && !stale.includes('data-freshness="fresh"'), "2-year is not marked fresh");
  assert(stale.includes(">4.79%<") && stale.includes('title="0.0479"'), `2-year value and exact title: ${stale}`);
  assert(staleText.includes("observed 2026-10-12"), "2-year date");
  assert(staleText.includes("stale · 9 days old, limit 5"), `2-year badge states age and limit: ${staleText}`);
  assert(staleText.includes("Source us-treasury") && staleText.includes("Fetched 2026-10-12 20:30 UTC"), "2-year source and fetch time");

  const empty = cardOf(html, TEN_YEAR);
  const emptyText = visibleText(empty);
  assert(empty.includes('data-fresh="false"') && empty.includes('data-freshness="stale"'), "10-year is marked stale");
  assert(emptyText.includes("stale · no observation yet"), `10-year says why: ${emptyText}`);
  assert(emptyText.includes("no observation") && emptyText.includes("Source —") && emptyText.includes("Fetched —"), emptyText);
  assert(empty.includes(">—<") && !empty.includes("title="), `10-year has no value and no title: ${empty}`);

  // the 12-month table: one row per date, a column per series in the order of the cards
  const table = tableOf(html);
  const rowText = table.map((row) => row.map((cell) => cell.text));
  assert(
    JSON.stringify(rowText) ===
      JSON.stringify([
        ["Date", "U.S. Treasury 3-month par yield", "U.S. Treasury 10-year par yield", "U.S. Treasury 2-year par yield"],
        ["2026-10-20", "4.26%", "—", "—"],
        ["2026-10-19", "4.27%", "—", "4.77%"],
        ["2026-10-12", "—", "—", "4.79%"],
      ]),
    `12-month table: ${JSON.stringify(rowText)}`,
  );
  assert(table[2][1].title === "0.0427" && table[2][3].title === "0.0477", "a value cell holds the exact value in its title");
  assert(table[1][2].title === null, "an absent cell has no title");
  __setTestClient(null);
}

// --- the page: a null verdict is stale, and another unit reads as it is ---
{
  const other = [
    {
      ...statusRows[0],
      series_id: "us_cpi_index",
      label: "CPI index",
      unit: "index_points",
      value: "312.332",
      is_fresh: null,
    },
  ];
  fakeClient({
    status: other,
    months: [{ series_id: "us_cpi_index", observation_day: "2026-10-20", value: "312.332" }],
  });
  principal = ADMIN;
  const html = renderToStaticMarkup(await MacroPage());
  const card = cardOf(html, "us_cpi_index");
  assert(card.includes('data-fresh="false"') && card.includes('data-freshness="stale"'), `a null verdict is stale: ${card}`);
  assert(visibleText(card).includes("stale · 1 day old, limit 5"), "a null verdict reads stale");
  assert(card.includes(">312.332 index_points<") && !card.includes("%"), `an index reads as value and unit: ${card}`);
  assert(tableOf(html)[1][1].text === "312.332 index_points", "the table reads the unit too");
  __setTestClient(null);
}

// --- the page for a member, a service and for no data ---
{
  for (const kind of ["member", "service"] as const) {
    principal = { ...ADMIN, principalKind: kind };
    fakeClient({ status: statusRows, months: monthRows });
    const denied = visibleText(renderToStaticMarkup(await MacroPage()));
    assert(denied.includes("Access denied"), `a ${kind} sees the denied notice`);
    assert(!denied.includes("4.26%"), `a ${kind} sees no value`);
    __setTestClient(null);
  }

  principal = ADMIN;
  fakeClient({ status: [], months: [] });
  const empty = visibleText(renderToStaticMarkup(await MacroPage()));
  assert(empty.includes("No macro series is registered"), "an empty database says so");
  __setTestClient(null);
}

console.log("#1255 admin macro panel passed");
