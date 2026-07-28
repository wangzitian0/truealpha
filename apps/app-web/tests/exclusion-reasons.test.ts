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
import { EXCLUSION_REASON_LABEL, exclusionLabel } from "../src/contracts/exclusion-reasons";

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

console.log(`#494 exclusion-reason table complete: ${reasons.length}/${reasons.length} enum members labeled`);
