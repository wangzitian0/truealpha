/**
 * #1521 step 6: the view of the key and run coverage table.
 *
 * `tests/admin-ledger-coverage-sql.test.ts` proves the numbers on a real Postgres. This test proves what the
 * view prints for them. It renders the real component with rows that cover each state of a cell:
 *   - a row with reasons, a key fetched twice and a run share;
 *   - a row with no reason and no run, whose cells must print "—" through `formatCell`;
 *   - the empty state, which names the window.
 * The view computes no number. A count that SQL returns is printed as it is, and the share is formatted as a percent.
 *
 * Run standalone: `bun run tests/admin-ledger-coverage-table.test.ts`.
 */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { LedgerCoverageTable } from "../src/app/admin/datahub/ledger-coverage";
import { LEDGER_COVERAGE_DAYS, type LedgerCoverageRow } from "../src/server/admin/datahub-stats";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

const render = (rows: LedgerCoverageRow[]): string =>
  renderToStaticMarkup(createElement(LedgerCoverageTable, { rows }));
const text = (html: string): string => html.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ");

const busy: LedgerCoverageRow = {
  day: "2026-10-27",
  source: "moomoo",
  endpoint: "get_research_analyst_consensus",
  calls: 30,
  no_key: 4,
  by_reason: { probe: 2, answer_refused: 1 },
  keys_twice: 3,
  run_share: "0.7500",
  calls_per_run: "7.5",
};
const quiet: LedgerCoverageRow = {
  day: "2026-10-26",
  source: "sec",
  endpoint: "companyfacts",
  calls: 5,
  no_key: 0,
  by_reason: {},
  keys_twice: 0,
  run_share: "0.0000",
  calls_per_run: null,
};

// --- rows: one table row per (day, source, endpoint), in the order given ---
{
  const html = render([busy, quiet]);
  const rows = html.match(/<tr[^>]*data-ledger-coverage-row="[^"]*"/g) ?? [];
  assert(rows.length === 2, `one row per (day, source, endpoint), got ${rows.length}`);
  assert(rows[0].includes("2026-10-27 moomoo get_research_analyst_consensus"), "the first row is the first input row");
  assert(html.indexOf("2026-10-27") < html.indexOf("2026-10-26"), "rows keep the order of the loader");
}

// --- cells: counts as they are, reasons by name, the share as a percent ---
{
  const words = text(render([busy]));
  assert(words.includes("moomoo") && words.includes("get_research_analyst_consensus"), "source and endpoint print");
  assert(words.includes("answer_refused 1, probe 2"), `reasons print by name in alphabetical order: ${words}`);
  assert(words.includes("75%"), `the share 0.7500 prints as 75%: ${words}`);
  assert(words.includes("7.5"), "calls per run prints as SQL returned it");
  const cells = [...render([busy]).matchAll(/<td[^>]*>([^<]*)<\/td>/g)].map((match) => match[1]);
  assert(cells.includes("30") && cells.includes("4") && cells.includes("3"), `calls, no key and keys twice: ${cells}`);
}

// --- a count above zero is amber; a count of zero is not ---
{
  const amber = (html: string): number => (html.match(/text-amber-400/g) ?? []).length;
  assert(amber(render([busy])) === 2, "no key 4 and keys twice 3 are amber");
  assert(amber(render([quiet])) === 0, "zero counts are not amber");
}

// --- empty cells print the em-dash of formatCell, never a blank or the word null ---
{
  const html = render([quiet]);
  const cells = [...html.matchAll(/<td[^>]*>([^<]*)<\/td>/g)].map((match) => match[1]);
  assert(cells.filter((cell) => cell === "—").length === 2, `reasons and calls per run print "—": ${cells}`);
  assert(!text(html).includes("null") && !text(html).includes("undefined"), "no raw null or undefined prints");
  assert(cells.includes("0%"), `a share of 0.0000 prints as 0%, not as a dash: ${cells}`);
}

// --- the empty state names the window ---
{
  const words = text(render([]));
  assert(words.includes(`last ${LEDGER_COVERAGE_DAYS} UTC days`), `the empty state names the window: ${words}`);
  assert(!render([]).includes("<table"), "no table for no rows");
}

console.log("admin-ledger-coverage-table: ok");
