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
  renderEntityDeepDiveCardSvg,
  buildEntityCardJson,
  type RankingCardData,
  type EntityCardData,
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

// 2. Ranking SVG rendering
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

// 3. Ranking JSON building
const json = buildRankingCardJson(mockData);
assert(json.schema_version === CARD_SCHEMA_VERSION, "schema_version must match");
assert(json.card_kind === "ranking", "card_kind must be ranking");
assert(json.template_version === CARD_TEMPLATE_VERSION, "template_version must match");
assert(json.cutoff_at === "2026-03-31T22:15:00Z", "cutoff_at must match");
assert(json.subjects.length === 2, "subjects count must match");
assert(json.subjects[0].display_name === "Apple Inc.", "subject name must match");
assert(json.subjects[0].metrics.some((m) => m.key === "valuation_gap" && m.value === "-0.24"), "valuation_gap metric must match");

// 4. Entity Deep Dive mock data
const mockEntityData: EntityCardData = {
  issuerId: "issuer:lei:5493001KJTIIGC8Y1R12",
  displayName: "NVIDIA Corp & Co <Special>",
  cutoffAt: "2026-06-30T23:59:59Z",
  tier: "large_model_native",
  currentPriceToSales: "25.40",
  valuationGap: "0.42",
  peg: "1.85",
  gppe: "1250000.50",
  operatingBranch: "non_financial",
  themes: [
    { theme: "AI Compute & Hardware <Core>", themeShare: "0.85" },
    { theme: "Automotive Edge", themeShare: "0.15" },
  ],
  confidence: "0.95",
};

// 5. Entity SVG rendering
const entitySvg = renderEntityDeepDiveCardSvg(mockEntityData);
assert(entitySvg.startsWith("<?xml"), "entity svg must start with xml header");
assert(entitySvg.includes(`width="1080"`), "entity svg must declare width 1080");
assert(entitySvg.includes(`height="1440"`), "entity svg must declare height 1440");
assert(entitySvg.includes(`viewBox="0 0 1080 1440"`), "entity svg must declare viewBox 0 0 1080 1440");
assert(entitySvg.includes("TrueAlpha Deep Dive"), "entity svg must include title");
assert(entitySvg.includes("NVIDIA Corp &amp; Co &lt;Special&gt;"), "entity svg must escape XML entities in displayName");
assert(entitySvg.includes("AI Compute &amp; Hardware &lt;Core&gt;"), "entity svg must escape XML entities in theme");
assert(entitySvg.includes("2026-06-30T23:59:59Z"), "entity svg must include cutoff");
assert(entitySvg.includes("Large Model Native"), "entity svg must format tier label");
assert(entitySvg.includes("+0.42"), "entity svg must format signed valuation gap");
assert(entitySvg.includes("1.85"), "entity svg must include formatted PEG");
assert(entitySvg.includes("Historical CAGR"), "entity svg must include Historical CAGR note");
assert(entitySvg.includes("non_financial"), "entity svg must include operating branch");
assert(entitySvg.includes("85%"), "entity svg must include theme share percent");
assert(entitySvg.includes("RESEARCH HYPOTHESIS"), "entity svg must include research-risk disclaimer");
assert(entitySvg.includes(CARD_TEMPLATE_VERSION), "entity svg must reference template version");

// 6. Entity JSON building
const entityJson = buildEntityCardJson(mockEntityData);
assert(entityJson.schema_version === CARD_SCHEMA_VERSION, "entity schema_version must match");
assert(entityJson.card_kind === "entity", "card_kind must be entity");
assert(entityJson.template_version === CARD_TEMPLATE_VERSION, "entity template_version must match");
assert(entityJson.title === "Entity Deep Dive", "entity title must match");
assert(entityJson.issuer_id === "issuer:lei:5493001KJTIIGC8Y1R12", "issuer_id must match");
assert(entityJson.display_name === "NVIDIA Corp & Co <Special>", "display_name must match");
assert(entityJson.cutoff_at === "2026-06-30T23:59:59Z", "cutoff_at must match");
assert(entityJson.metrics.tier === "large_model_native", "metric tier must match");
assert(entityJson.metrics.valuation_gap === "0.42", "metric valuation_gap must match");
assert(entityJson.metrics.peg === "1.85", "metric peg must match");
assert(entityJson.themes.length === 2, "themes count must match");
assert(entityJson.themes[0].theme === "AI Compute & Hardware <Core>", "theme name must match");
assert(entityJson.themes[0].theme_share === "0.85", "theme share must match");

console.log("xhs-card.test.ts: all assertions passed");
