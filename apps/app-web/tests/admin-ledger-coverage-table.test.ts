import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { LedgerCoverageTable, LEDGER_COVERAGE_DAYS, type LedgerCoverageRow } from "../src/app/admin/datahub/ledger-coverage";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

const render = (rows: LedgerCoverageRow[]): string => renderToStaticMarkup(createElement(LedgerCoverageTable, { rows }));
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

const html = render([busy, quiet]);
const rows = html.match(/<tr[^>]*data-ledger-coverage-row="[^"]*"/g) ?? [];
assert(rows.length === 2, `expected 2 rows, got ${rows.length}`);
assert(rows[0].includes("2026-10-27 moomoo get_research_analyst_consensus"), "first row must match");
assert(html.indexOf("2026-10-27") < html.indexOf("2026-10-26"), "rows keep loader order");

const busyHtml = render([busy]);
const busyWords = text(busyHtml);
assert(busyWords.includes("answer_refused 1, probe 2"), `reasons print alphabetically: ${busyWords}`);
assert(busyWords.includes("75%"), "run share formatted as 75%");
const busyCells = [...busyHtml.matchAll(/<td[^>]*>([^<]*)<\/td>/g)].map((m) => m[1]);
assert(busyCells.includes("30") && busyCells.includes("4") && busyCells.includes("3"), `cells missing counts: ${busyCells}`);
assert((busyHtml.match(/text-amber-400/g) ?? []).length === 2, "no_key and keys_twice are amber");

const quietHtml = render([quiet]);
const quietCells = [...quietHtml.matchAll(/<td[^>]*>([^<]*)<\/td>/g)].map((m) => m[1]);
assert(quietCells.filter((c) => c === "—").length === 2, `empty cells print em-dash: ${quietCells}`);
assert(!text(quietHtml).includes("null") && !text(quietHtml).includes("undefined"), "no raw null printed");
assert(quietCells.includes("0%"), "0.0000 prints as 0%");
assert((quietHtml.match(/text-amber-400/g) ?? []).length === 0, "zero counts are not amber");

assert(text(render([])).includes(`last ${LEDGER_COVERAGE_DAYS} UTC days`), "empty notice names window");
assert(!render([]).includes("<table"), "no table when empty");
console.log("admin-ledger-coverage-table: ok");
