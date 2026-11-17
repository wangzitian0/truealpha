/**
 * #1235: StrategyDefinitionLine marks reviewed, unreviewed, provisional, and missing definitions.
 */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { StrategyDefinitionLine } from "../src/components/strategy-definition";
import type { StrategyRunDefinition } from "../src/contracts/strategyDefinition";
import { STRATEGY_DEFINITION_REGISTRY } from "../src/contracts/strategyDefinitions.generated";
import { SYNTHETIC_PROVISIONAL_DEFINITION } from "./helpers/synthetic-provisional-definition.generated";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

const reviewedEntry = STRATEGY_DEFINITION_REGISTRY.find((entry) => !entry.provisional_semantics);
assert(reviewedEntry !== undefined, "registry must contain at least one reviewed entry");
assert(
  SYNTHETIC_PROVISIONAL_DEFINITION.provisional_semantics &&
    !STRATEGY_DEFINITION_REGISTRY.some((entry) => entry.content_sha256 === SYNTHETIC_PROVISIONAL_DEFINITION.content_sha256),
  "synthetic provisional definition must stay absent from deployed registry",
);

const CASES: ReadonlyArray<{ name: string; definition: StrategyRunDefinition | undefined; state: string; text: string }> = [
  {
    name: "reviewed",
    definition: {
      version: reviewedEntry.version,
      content_sha256: reviewedEntry.content_sha256,
      review_state: "reviewed",
    },
    state: "reviewed",
    text: `Definition ${reviewedEntry.version} · reviewed`,
  },
  {
    name: "unreviewed 0.1.0",
    definition: { version: "0.1.0", content_sha256: "e".repeat(64), review_state: "unknown" },
    state: "unknown",
    text: "Definition 0.1.0 · not reviewed (not in the registry)",
  },
  {
    name: "provisional",
    definition: {
      version: SYNTHETIC_PROVISIONAL_DEFINITION.version,
      content_sha256: SYNTHETIC_PROVISIONAL_DEFINITION.content_sha256,
      review_state: "provisional",
    },
    state: "provisional",
    text: `Definition ${SYNTHETIC_PROVISIONAL_DEFINITION.version} · PROVISIONAL: bands not reviewed`,
  },
  {
    name: "missing",
    definition: undefined,
    state: "unknown",
    text: "Definition unknown · not reviewed",
  },
];

for (const tc of CASES) {
  const html = renderToStaticMarkup(createElement(StrategyDefinitionLine, { definition: tc.definition }));
  const text = html.replace(/<[^>]*>/g, "");
  assert(text === tc.text, `${tc.name}: expected "${tc.text}", got "${text}"`);
  assert(html.includes(`data-review-state="${tc.state}"`), `${tc.name}: missing data-review-state="${tc.state}"`);
  assert(text.includes("PROVISIONAL") === (tc.state === "provisional"), `${tc.name}: PROVISIONAL marker mismatch`);
}
console.log(`#1235 strategy definition marker: ${CASES.length} definition states passed`);
