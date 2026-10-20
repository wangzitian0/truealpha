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
import {
  SWEEP_RUN_SOURCE_PATTERN as CANONICAL_SWEEP_PATTERN,
  SWEEP_RUN_SOURCE_REGEX_SOURCE,
} from "../src/contracts/serviceObjectives";
import { PREVIEW_ISSUERS } from "../scripts/seed-e2e-fixture";

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
  "listing:xnas:t001", // a symbol with digits (the data-engine fixtures mint these)
  "listing:xhkg:0700", // an all-digit symbol, as non-US venues list them
  "018f9e6a-7b3e-7a1b-8c2d-9e0f1a2b3c4d", // UUID entity id (#877 PR-4)
];
for (const id of RAW_IDS) {
  const match = `Ranked first: ${id} with a gap of 12%`.match(ID_PATTERN);
  assert(match?.[0] === id, `walk-tree's raw-id guard misses ${id} (matched ${String(match?.[0])})`);
}

// #1322: all 5 golden replay issuers must have both UUID and LEI legacyId shapes caught by ID_PATTERN,
// and their resolved ticker/display labels must pass without tripping ID_PATTERN.
const previewEntries = Object.entries(PREVIEW_ISSUERS);
assert(previewEntries.length === 5, `expected 5 golden preview issuers, found ${previewEntries.length}`);
for (const [key, identity] of previewEntries) {
  assert(
    ID_PATTERN.test(identity.entityId),
    `preview issuer ${key} entityId ${identity.entityId} is not caught by ID_PATTERN`,
  );
  assert(
    ID_PATTERN.test(identity.legacyId),
    `preview issuer ${key} legacyId ${identity.legacyId} is not caught by ID_PATTERN`,
  );
  const resolvedLabel = `${identity.ticker} · ${identity.displayName}`;
  assert(
    !ID_PATTERN.test(resolvedLabel),
    `preview issuer ${key} resolved label "${resolvedLabel}" trips ID_PATTERN`,
  );
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

// #987 / #1322 Anti-puppet check against GREEN-WHILE-EMPTY: walk-tree must verify populated table rows
// and 5 resolved preview tickers on /research/strategy.
assert(source.includes("GREEN-WHILE-EMPTY"), "walk-tree.mjs does not check for GREEN-WHILE-EMPTY table states");
assert(source.includes("TA_DATA_STATE"), "walk-tree.mjs does not read TA_DATA_STATE");
assert(source.includes("PREVIEW_TICKERS"), "walk-tree.mjs does not verify the 5 golden preview tickers");
const navigationSource = readFileSync(new URL("../e2e/navigation.mjs", import.meta.url), "utf8");
assert(
  !source.includes("el.remove()") &&
    !source.includes("el.replaceWith(") &&
    source.includes('el.hasAttribute("data-evidence")') &&
    navigationSource.includes('[data-sign-out][data-hydrated="true"]'),
  "walk-tree.mjs must wait for client hydration in gotoStable and inspect visible text without mutating [data-evidence] DOM nodes so React hydration does not fail with #418",
);

// #1362 / #1363: SWEEP_RUN_SOURCE_PATTERN in walk-tree.mjs must match serviceObjectives.ts and catch timestamped sweep sources.
const sweepLiteral = source.match(/const SWEEP_RUN_SOURCE_PATTERN =\s*\/(.+)\/([a-z]*);/);
assert(sweepLiteral !== null, "e2e/walk-tree.mjs no longer declares SWEEP_RUN_SOURCE_PATTERN as a regex literal");
assert(
  sweepLiteral[1] === SWEEP_RUN_SOURCE_REGEX_SOURCE,
  `walk-tree.mjs SWEEP_RUN_SOURCE_PATTERN (${sweepLiteral[1]}) drifted from serviceObjectives.ts (${SWEEP_RUN_SOURCE_REGEX_SOURCE})`,
);
const SWEEP_RUN_SOURCE_PATTERN = new RegExp(sweepLiteral[1], sweepLiteral[2]);
for (const sweepName of ["yfinance-20260725T0527", "moomoo-fundamentals-20260903T2059"]) {
  assert(
    SWEEP_RUN_SOURCE_PATTERN.test(sweepName) && CANONICAL_SWEEP_PATTERN.test(sweepName),
    `SWEEP_RUN_SOURCE_PATTERN misses timestamped sweep source ${sweepName}`,
  );
}
for (const registeredSource of ["moomoo", "twelvedata", "sec-edgar", "yfinance"]) {
  assert(
    !SWEEP_RUN_SOURCE_PATTERN.test(registeredSource) && !CANONICAL_SWEEP_PATTERN.test(registeredSource),
    `SWEEP_RUN_SOURCE_PATTERN falsely flags registered source ${registeredSource}`,
  );
}

console.log(
  `walk-tree-id-pattern: ${RAW_IDS.length} id schemes caught, ${previewEntries.length} preview issuers verified, ${DISPLAYED.length} display strings pass, anti-empty & sweep-source checks verified`,
);

