/**
 * #877 H5: e2e/walk-tree.mjs fails a research first screen that shows a raw entity
 * id as visible text. Its pattern knew TOPT's schemes only (`issuer:lei:`, listing
 * ids), so a raw plane issuer (`issuer:cik:…`) or instrument (`security:figi:…`)
 * passed, and TOPT's own ids would stop being caught the day TOPT resolves to
 * CIK/FIGI (#877 PR-4).
 *
 * The walk script cannot be imported (it drives a browser at import time), so
 * this reads its pattern from the source and exercises it on the id shapes the
 * warehouse actually mints.
 *
 * Run standalone: `bun run tests/walk-tree-id-pattern.test.ts`.
 */

import { readFileSync } from "node:fs";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

const source = readFileSync(new URL("../e2e/walk-tree.mjs", import.meta.url), "utf8");
// [1] the pattern body, [2] its flags.
const literal = source.match(/const ID_PATTERN =\s*\/(.+)\/([a-z]*);/);
assert(literal !== null, "e2e/walk-tree.mjs no longer declares ID_PATTERN as a regex literal");
const ID_PATTERN = new RegExp(literal[1], literal[2]);

// One id per scheme, in the shapes staging and production carry (2026-09-16).
const RAW_IDS = [
  "issuer:lei:HWUPKR0MPOU8FGXBT394", // TOPT issuer (N-PORT LEI)
  "issuer:cik:0000320193", // plane issuer (SEC CIK, zero-padded)
  "security:cusip:037833100", // TOPT instrument
  "security:figi:bbg001s5n8v8", // plane instrument, as the universe corpus mints it (lower case)
  "security:figi:BBG001S5N8V8", // the same FIGI as OpenFIGI prints it
  "listing:xnas:aapl",
  "listing:xnys:brk.b",
];
for (const id of RAW_IDS) {
  const match = `Ranked first: ${id} with a gap of 12%`.match(ID_PATTERN);
  assert(match?.[0] === id, `walk-tree's raw-id guard misses ${id} (matched ${String(match?.[0])})`);
}

// What a first screen legitimately shows must not trip it.
const DISPLAYED = [
  "AAPL · Apple Inc.",
  "issuer 12 of 20",
  "CIK 320193",
  "FIGI coverage 101/101",
  "listing count 21",
];
for (const text of DISPLAYED) {
  assert(!ID_PATTERN.test(text), `walk-tree's raw-id guard flags ordinary text: ${text}`);
}

console.log(`walk-tree-id-pattern: ${RAW_IDS.length} id schemes caught, ${DISPLAYED.length} display strings pass`);
