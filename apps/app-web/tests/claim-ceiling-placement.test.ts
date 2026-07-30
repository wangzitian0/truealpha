/**
 * #494 assertion (b) lint: the claim ceiling is a property of the page, not of
 * its data state.
 *
 * `e2e/walk-tree.mjs` asserts the banner renders on `/research/rankings` and
 * `/research/strategy`. That assertion is only meaningful if it holds in EVERY
 * data state — otherwise it passes on a database that happens to hold a
 * strategy run and fails on one that does not, which is exactly what a freshly
 * migrated CI database is (verified 2026-07-30: `loadStrategyRunPage` returns
 * `unavailable{no_runs_recorded}` there, and `/research/strategy` used to place
 * `<ClaimCeilingBanner />` after three early returns, so the banner vanished).
 *
 * This statically forbids the divergence: between the page component and its
 * banner there may be exactly ONE `return` — the outermost JSX return. Any
 * early exit above the banner makes the count 2+ and fails here, at the file
 * that caused it, instead of intermittently in the browser suite.
 *
 * `/research` (the overview) is deliberately NOT scanned: it renders strategy
 * output only when selections exist, and `claim-ceiling.tsx` scopes the ceiling
 * to "wherever strategy outputs render". These two pages always claim to be
 * strategy-output pages, so they always owe the ceiling.
 *
 * Run standalone: `bun run tests/claim-ceiling-placement.test.ts`.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

const PAGES = [
  "src/app/research/rankings/page.tsx",
  "src/app/research/strategy/page.tsx",
];

for (const relativePath of PAGES) {
  const rawSource = readFileSync(join(process.cwd(), relativePath), "utf8");

  // Same stripping order as tests/dashboard-boundary.test.ts: block comments
  // (which covers `{/* … */}` too), then string/template literals, then line
  // comments — so prose containing the word "return" cannot trip the scan.
  const code = rawSource
    .replace(/\/\*[\s\S]*?\*\//g, " ")
    .replace(/`[^`]*`/g, " ")
    .replace(/"[^"]*"/g, " ")
    .replace(/'[^']*'/g, " ")
    .replace(/\/\/[^\n]*/g, " ");

  const componentAt = code.indexOf("export default");
  assert(componentAt !== -1, `${relativePath}: no default export found`);

  const bannerAt = code.indexOf("<ClaimCeilingBanner");
  assert(
    bannerAt !== -1,
    `${relativePath}: renders no <ClaimCeilingBanner />. Every page e2e/walk-tree.mjs ` +
      `asserts the claim ceiling on must render it — see #494 assertion (b).`,
  );
  assert(
    bannerAt > componentAt,
    `${relativePath}: <ClaimCeilingBanner /> appears before the page component.`,
  );

  const returnsBefore = (code.slice(componentAt, bannerAt).match(/\breturn\b/g) ?? []).length;
  assert(
    returnsBefore === 1,
    `${relativePath}: ${returnsBefore} return statements sit between the page component and ` +
      `<ClaimCeilingBanner />, so at least one data state renders the page WITHOUT the claim ` +
      `ceiling. Exactly one (the outermost JSX return) is allowed: render every non-ready ` +
      `state inside the returned tree instead of exiting above the banner (#494 assertion b).`,
  );
}

console.log(`claim-ceiling placement: ${PAGES.length} strategy-output pages render the ceiling unconditionally`);
