/**
 * #1241: e2e/walk-tree.mjs reads the cells of the trace inputs table. It compares each cell with the text
 * that the page prints for a field that the lineage does not carry. It also lists the cells of one input row.
 * The walk cannot import the app code, because it drives a browser at import time. So it keeps its own
 * copy of both facts. This test fails when a copy differs from the component.
 *
 * A copy of the text that drifted would count every "not recorded" cell as a recorded value. Then the
 * walk would pass on an empty table. A copy of the cell list that drifted would skip a cell.
 *
 * Run standalone: `bun run tests/walk-tree-trace-inputs.test.ts`.
 */

import { readFileSync } from "node:fs";

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { TraceInputsTable } from "../src/components/trace-inputs-table";
import { NOT_RECORDED, traceInputsFromLineage } from "../src/server/mart/research-read";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

const source = readFileSync(new URL("../e2e/walk-tree.mjs", import.meta.url), "utf8");

const declaredText = source.match(/const NOT_RECORDED = "([^"]+)";/);
assert(declaredText !== null, "e2e/walk-tree.mjs no longer declares NOT_RECORDED as a string literal");
assert(
  declaredText[1] === NOT_RECORDED,
  `the walk compares cells with "${declaredText[1]}", but the page prints "${NOT_RECORDED}"`,
);

const declaredCells = source.match(/const TRACE_FIELDS = \[([^\]]*)\];/);
assert(declaredCells !== null, "e2e/walk-tree.mjs no longer declares TRACE_FIELDS as an array literal");
const walkFields = Array.from(declaredCells[1].matchAll(/"([a-z_]+)"/g), (match) => match[1]);

// A lineage with no item shows the six inputs as not recorded. Every input row prints its five cells.
const html = renderToStaticMarkup(
  createElement(TraceInputsTable, { listings: traceInputsFromLineage([], null), lineageResultId: null }),
);
const pageFields = Array.from(new Set(Array.from(html.matchAll(/data-field="([a-z_]+)"/g), (match) => match[1])));
assert(pageFields.length > 0, "TraceInputsTable renders no data-field cell, so this test compares nothing");
assert(
  JSON.stringify([...walkFields].sort()) === JSON.stringify([...pageFields].sort()),
  `the walk reads the cells [${walkFields.join(", ")}], but the table renders [${pageFields.join(", ")}]`,
);

const valueCells = Array.from(html.matchAll(/<td data-field="value"[^>]*>([^<]*)<\/td>/g), (match) => match[1]);
assert(valueCells.length > 0, "the table renders no value cell");
assert(
  valueCells.every((text) => text === NOT_RECORDED),
  `a value that the lineage does not carry prints [${valueCells.join(" | ")}] and not "${NOT_RECORDED}"`,
);

console.log(
  `walk-tree-trace-inputs: the walk reads ${walkFields.length} cells that the table renders, and "${NOT_RECORDED}" matches the page`,
);
