/**
 * Unit tests for Xiaohongshu card rendering (1080x1440 3:4 portrait) — see #372 / #44.
 *
 * Run standalone: `bun run tests/xhs-card.test.ts`.
 */

import {
  XHS_CARD_WIDTH,
  XHS_CARD_HEIGHT,
  CARD_SCHEMA_VERSION,
  CARD_TEMPLATE_VERSION,
  renderRankingCardSvg,
  buildRankingCardJson,
  type RankingCardData,
} from "../src/server/cards/xhs-card";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

const mockData: RankingCardData = {
  cutoffAt: "2026-03-31T22:15:00Z",
  totalMembers: 20,
  items: [
    {
      rank: 1,
      issuerId: "issuer:lei:5493001KJTIIGC8Y1R12",
      displayName: "Apple Inc.",
      tier: "1",
      currentPriceToSales: "7.85",
      valuationGap: "-0.24",
      peg: "1.45",
      confidence: "0.92",
    },
    {
      rank: 2,
      issuerId: "issuer:lei:5493006MHB84DD0ZWV18",
      displayName: "Microsoft Corp & Co <Special>",
      tier: "1",
      currentPriceToSales: "12.40",
      valuationGap: "0.15",
      peg: "2.10",
      confidence: "0.88",
    },
  ],
};

// 1. Dimensions contract
assert(XHS_CARD_WIDTH === 1080, "width must be exact 1080 px (3:4 portrait)");
assert(XHS_CARD_HEIGHT === 1440, "height must be exact 1440 px (3:4 portrait)");

// 2. SVG rendering
const svg = renderRankingCardSvg(mockData);
assert(svg.startsWith("<?xml"), "must start with xml header");
assert(svg.includes(`width="1080"`), "svg must declare width 1080");
assert(svg.includes(`height="1440"`), "svg must declare height 1440");
assert(svg.includes(`viewBox="0 0 1080 1440"`), "svg must declare viewBox 0 0 1080 1440");
assert(svg.includes("Valuation Rankings"), "must include title");
assert(svg.includes("Apple Inc."), "must include unescaped text");
assert(svg.includes("Microsoft Corp &amp; Co &lt;Special&gt;"), "must escape XML entities");
assert(svg.includes("2026-03-31T22:15:00Z"), "must include cutoff");
assert(svg.includes("RESEARCH HYPOTHESIS"), "must include research-risk disclaimer");
assert(svg.includes(CARD_TEMPLATE_VERSION), "must reference template version");

// 3. JSON building
const json = buildRankingCardJson(mockData);
assert(json.schema_version === CARD_SCHEMA_VERSION, "schema_version must match");
assert(json.card_kind === "ranking", "card_kind must be ranking");
assert(json.template_version === CARD_TEMPLATE_VERSION, "template_version must match");
assert(json.cutoff_at === "2026-03-31T22:15:00Z", "cutoff_at must match");
assert(json.subjects.length === 2, "subjects count must match");
assert(json.subjects[0].display_name === "Apple Inc.", "subject name must match");
assert(json.subjects[0].metrics.some((m) => m.key === "valuation_gap" && m.value === "-0.24"), "valuation_gap metric must match");

console.log("xhs-card.test.ts: all assertions passed");
