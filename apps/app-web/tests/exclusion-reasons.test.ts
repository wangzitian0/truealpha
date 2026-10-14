/**
 * #494 acceptance 3: the exclusion-reason translation table is complete
 * against the contracts enum — the reason strings the deployed strategy can
 * emit are read from the PYTHON contract source (single source of truth,
 * libs/contracts strategy.ExclusionReason) so a new enum member fails THIS
 * test instead of rendering a raw code on the page.
 *
 * Run standalone: `bun run tests/exclusion-reasons.test.ts`.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import {
  EXCLUSION_REASON_LABEL,
  exclusionLabel,
  summarizeUniverseUsability,
} from "../src/contracts/exclusion-reasons";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

const contractSource = readFileSync(
  join(process.cwd(), "..", "..", "libs", "contracts", "src", "truealpha_contracts", "strategy.py"),
  "utf8",
);
const enumBlock = contractSource.split("class ExclusionReason(StrEnum):")[1]?.split("\n\n\n")[0];
assert(enumBlock, "ExclusionReason enum not found in contracts source");
const reasons = [...enumBlock.matchAll(/=\s*"([a-z_]+)"/g)].map((m) => m[1]);
assert(reasons.length >= 12, `expected the full enum, found ${reasons.length}`);

for (const reason of reasons) {
  assert(
    typeof EXCLUSION_REASON_LABEL[reason] === "string" && EXCLUSION_REASON_LABEL[reason].length > 0,
    `ExclusionReason.${reason} has no human-readable entry in EXCLUSION_REASON_LABEL`,
  );
}

assert(exclusionLabel(null) === null, "null passes through");
assert(exclusionLabel("unknown_future_reason") === "unknown_future_reason", "unknown codes fall back to raw");

// #1265 item 3: verify the 20-issuer TOPT universe partitions into 17 usable + 3 excluded
// (BRK.B, V, XOM) with their specific exclusion reasons and human-readable explanations.
const sampleDecisions = [
  ...Array.from({ length: 5 }, (_, idx) => ({
    issuer_id: `issuer:selected:${idx + 1}`,
    outcome: "selected",
    eligible: true,
    exclusion_reason: null,
  })),
  ...Array.from({ length: 12 }, (_, idx) => ({
    issuer_id: `issuer:ranked:${idx + 1}`,
    outcome: "ranked_beyond_selection_count",
    eligible: true,
    exclusion_reason: null,
  })),
  {
    issuer_id: "BRK.B",
    outcome: "excluded",
    eligible: false,
    exclusion_reason: "missing_gross_profit_fact",
  },
  {
    issuer_id: "V",
    outcome: "excluded",
    eligible: false,
    exclusion_reason: "missing_market_value_input",
  },
  {
    issuer_id: "XOM",
    outcome: "excluded",
    eligible: false,
    exclusion_reason: "missing_gross_profit_fact",
  },
];

const summary = summarizeUniverseUsability(sampleDecisions);
assert(summary.evaluatedCount === 20, `expected 20 evaluated, got ${summary.evaluatedCount}`);
assert(summary.usableCount === 17, `expected 17 usable, got ${summary.usableCount}`);
assert(summary.selectedCount === 5, `expected 5 selected, got ${summary.selectedCount}`);
assert(summary.excludedCount === 3, `expected 3 excluded, got ${summary.excludedCount}`);
assert(
  summary.excluded.map((e) => `${e.issuer_id}:${e.exclusion_reason}`).join(",") ===
    "BRK.B:missing_gross_profit_fact,V:missing_market_value_input,XOM:missing_gross_profit_fact",
  "excluded issuers and reasons must match BRK.B, V, XOM",
);
assert(
  summary.excluded[1]?.label.includes("dimensional XBRL axes"),
  "V's missing_market_value_input label must explain dimensional multi-class share counts",
);
assert(
  summary.excluded[2]?.label.includes("cost of revenue or insurance claims"),
  "XOM/BRK.B missing_gross_profit_fact label must explain untagged cost of revenue / insurance claims",
);

const todayPageSource = readFileSync(
  join(process.cwd(), "src", "app", "research", "page.tsx"),
  "utf8",
);
assert(
  todayPageSource.includes("summarizeUniverseUsability") &&
    todayPageSource.includes("usability.usableCount") &&
    todayPageSource.includes("excluded-issuers-disclosure"),
  "Today page (/research) must render usability.usableCount and excluded-issuers-disclosure (#1265 item 3)",
);

console.log(`#494 + #1265 exclusion-reason table & usability summary verified (${reasons.length} enum members)`);

