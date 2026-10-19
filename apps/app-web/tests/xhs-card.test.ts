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
  renderRankingCardPng,
  buildRankingCardJson,
  renderEntityDeepDiveCardSvg,
  renderEntityDeepDiveCardPng,
  buildEntityCardJson,
  entityCardThemes,
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
      currentMultiple: "11.03",
      valuationBasis: "price_to_gross_profit",
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
      currentMultiple: "17.21",
      valuationBasis: "price_to_gross_profit",
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
// #1235: the card names the multiple the gap divides. Under definition 1.0.0 that is P/GP, never P/S.
assert(svg.includes(">P/GP</text>"), "ranking column must be headed by the valuation basis");
assert(svg.includes("VALUATION GAP (P/GP)"), "ranking gap must name its basis");
assert(svg.includes(">11.03</text>"), "ranking row must show the P/GP multiple");
assert(!svg.includes(">7.85</text>"), "ranking row must not show P/S under a P/GP heading");
assert(json.subjects[0].valuation_basis === "price_to_gross_profit", "subject must name its basis");
assert(
  json.subjects[0].metrics.some((m) => m.key === "current_price_to_gross_profit" && m.value === "11.03"),
  "current_price_to_gross_profit metric must match",
);
assert(
  json.subjects[0].metrics.some((m) => m.key === "current_price_to_sales" && m.value === "7.85"),
  "current_price_to_sales stays recorded",
);
const psCard = renderRankingCardSvg({
  ...mockData,
  items: mockData.items.map((item) => ({ ...item, currentMultiple: item.currentPriceToSales, valuationBasis: "price_to_sales" as const })),
});
assert(psCard.includes(">P/S</text>") && psCard.includes("VALUATION GAP (P/S)"), "a 0.1.0 run keeps its P/S label");

// 4. Entity Deep Dive mock data
const mockEntityData: EntityCardData = {
  issuerId: "issuer:lei:5493001KJTIIGC8Y1R12",
  displayName: "NVIDIA Corp & Co <Special>",
  cutoffAt: "2026-06-30T23:59:59Z",
  tier: "large_model_native",
  currentPriceToSales: "25.40",
  currentMultiple: "36.20",
  valuationBasis: "price_to_gross_profit",
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
assert(entityJson.metrics.current_price_to_gross_profit === "36.20", "entity P/GP metric must match");
assert(entityJson.metrics.valuation_basis === "price_to_gross_profit", "entity card must name its basis");
assert(entitySvg.includes("Current P/GP") && entitySvg.includes("Valuation Gap (P/GP)"), "entity svg must label P/GP");
assert(!entitySvg.includes("Current P/S"), "entity svg must not label P/GP as P/S");
assert(entityJson.metrics.peg === "1.85", "metric peg must match");
assert(entityJson.themes.length === 2, "themes count must match");
assert(entityJson.themes[0].theme === "AI Compute & Hardware <Core>", "theme name must match");
assert(entityJson.themes[0].theme_share === "0.85", "theme share must match");

// 7. A fill row (an issuer without a segment partition) is not a theme of the card (#1117)
const cardThemes = entityCardThemes([
  { theme: "AI Compute", themeShare: "0.85", unvisitedReason: null },
  { theme: "Automotive Edge", themeShare: null, unvisitedReason: "no_segment_partition" },
  { theme: "Refused Theme", themeShare: null, unvisitedReason: null },
]);
assert(cardThemes.length === 2, "the card drops the fill and keeps a refused real row");
assert(cardThemes[0].theme === "AI Compute" && cardThemes[0].themeShare === "0.85", "a judged theme passes through");
assert(cardThemes[1].theme === "Refused Theme" && cardThemes[1].themeShare === null, "a refused real row stays");
assert(!("unvisitedReason" in cardThemes[0]), "the card data carries no reason field");

// 8. 1080x1440 PNG rendering (#1360 AC4)
const rankingPng = await renderRankingCardPng(mockData);
assert(
  rankingPng.subarray(0, 8).toString("hex") === "89504e470d0a1a0a",
  "ranking PNG must start with PNG magic bytes",
);
assert(rankingPng.subarray(12, 16).toString("ascii") === "IHDR", "ranking PNG must contain IHDR chunk");
assert(rankingPng.readUInt32BE(16) === XHS_CARD_WIDTH, "ranking PNG width must be 1080");
assert(rankingPng.readUInt32BE(20) === XHS_CARD_HEIGHT, "ranking PNG height must be 1440");

const entityPng = await renderEntityDeepDiveCardPng(mockEntityData);
assert(
  entityPng.subarray(0, 8).toString("hex") === "89504e470d0a1a0a",
  "entity PNG must start with PNG magic bytes",
);
assert(entityPng.subarray(12, 16).toString("ascii") === "IHDR", "entity PNG must contain IHDR chunk");
assert(entityPng.readUInt32BE(16) === XHS_CARD_WIDTH, "entity PNG width must be 1080");
assert(entityPng.readUInt32BE(20) === XHS_CARD_HEIGHT, "entity PNG height must be 1440");

console.log("xhs-card.test.ts: all assertions passed");
