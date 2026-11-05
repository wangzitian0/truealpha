/**
 * Unit tests for Xiaohongshu HTML card rendering & fact-preservation guard (1080x1440 3:4 portrait) — see #372 / #44.
 *
 * Run standalone: `bun run tests/xhs-card.test.ts`.
 */

import {
  XHS_CARD_WIDTH,
  XHS_CARD_HEIGHT,
  CARD_SCHEMA_VERSION,
  CARD_TEMPLATE_VERSION,
  XHS_CARD_PROMPT_VERSION,
  renderRankingCardHtml,
  buildRankingCardJson,
  renderEntityDeepDiveCardHtml,
  buildEntityCardJson,
  verifyCardHtmlDocument,
  entityCardThemes,
  type RankingCardData,
  type EntityCardData,
} from "../src/server/cards/xhs-card";
import { universeWithRole } from "../src/contracts/universes";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

const mockData: RankingCardData = {
  cutoffAt: "2026-03-31T22:15:00Z",
  totalMembers: 20,
  universe: universeWithRole("strategy"),
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

// 2. Ranking HTML rendering & deterministic fact-preservation verification
const rankingHtml = renderRankingCardHtml(mockData);
assert(rankingHtml.startsWith("<!DOCTYPE html>"), "must start with DOCTYPE html");
assert(rankingHtml.includes("1080px") && rankingHtml.includes("1440px"), "html must declare 1080x1440px canvas");
assert(rankingHtml.includes("Valuation Rankings"), "must include title");
assert(rankingHtml.includes("Apple Inc."), "must include unescaped text");
assert(rankingHtml.includes("Microsoft Corp &amp; Co &lt;Special&gt;"), "must escape HTML entities");
assert(rankingHtml.includes("2026-03-31T22:15:00Z"), "must include cutoff");
assert(rankingHtml.includes("RESEARCH HYPOTHESIS"), "must include research-risk disclaimer");
assert(rankingHtml.includes(CARD_TEMPLATE_VERSION), "must reference template version");
assert(rankingHtml.includes(XHS_CARD_PROMPT_VERSION), "must reference prompt version");

const rankingVerification = verifyCardHtmlDocument(rankingHtml, [
  "2026-03-31T22:15:00Z",
  "Apple Inc.",
  "Microsoft Corp & Co <Special>",
  "11.03",
  "-0.24",
  "1.45",
  "0.92",
]);
assert(
  rankingVerification.ok,
  `ranking HTML must pass fact-preservation verifier: ${rankingVerification.violations.join(", ")}`,
);

// Mutation check: dropping a metric or injecting a script must fail verification
const mutatedHtml = rankingHtml.replace("11.03", "99.99");
const mutatedVerification = verifyCardHtmlDocument(mutatedHtml, ["11.03"]);
assert(!mutatedVerification.ok && mutatedVerification.violations.includes("missing_fact:11.03"), "verifier must catch mutated metric value");

const scriptInjectedHtml = rankingHtml.replace("</body>", "<script>alert(1)</script></body>");
const scriptVerification = verifyCardHtmlDocument(scriptInjectedHtml, []);
assert(!scriptVerification.ok && scriptVerification.violations.includes("forbidden_script_or_event_handler"), "verifier must reject script injection");

const onclickInjectedHtml = rankingHtml.replace("</body>", '<div onmouseover="alert(1)">x</div></body>');
const onclickVerification = verifyCardHtmlDocument(onclickInjectedHtml, []);
assert(!onclickVerification.ok && onclickVerification.violations.includes("forbidden_script_or_event_handler"), "verifier must reject arbitrary on*= event handler attributes");

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
assert(rankingHtml.includes(">P/GP</th>"), "ranking column must be headed by the valuation basis");
assert(rankingHtml.includes("VALUATION GAP (P/GP)"), "ranking gap must name its basis");
assert(rankingHtml.includes(">11.03</td>"), "ranking row must show the P/GP multiple");
assert(!rankingHtml.includes(">7.85</td>"), "ranking row must not show P/S under a P/GP heading");
assert(json.subjects[0].valuation_basis === "price_to_gross_profit", "subject must name its basis");
assert(
  json.subjects[0].metrics.some((m) => m.key === "current_price_to_gross_profit" && m.value === "11.03"),
  "current_price_to_gross_profit metric must match",
);
assert(
  json.subjects[0].metrics.some((m) => m.key === "current_price_to_sales" && m.value === "7.85"),
  "current_price_to_sales stays recorded",
);
const psCard = renderRankingCardHtml({
  ...mockData,
  items: mockData.items.map((item) => ({
    ...item,
    currentMultiple: item.currentPriceToSales,
    valuationBasis: "price_to_sales" as const,
  })),
});
assert(psCard.includes(">P/S</th>") && psCard.includes("VALUATION GAP (P/S)"), "a 0.1.0 run keeps its P/S label");

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

// 5. Entity HTML rendering & fact-preservation verification
const entityHtml = renderEntityDeepDiveCardHtml(mockEntityData);
assert(entityHtml.startsWith("<!DOCTYPE html>"), "entity html must start with DOCTYPE html");
assert(entityHtml.includes("1080px") && entityHtml.includes("1440px"), "entity html must declare 1080x1440px");
assert(entityHtml.includes("TrueAlpha Deep Dive"), "entity html must include title");
assert(entityHtml.includes("NVIDIA Corp &amp; Co &lt;Special&gt;"), "entity html must escape HTML entities in displayName");
assert(entityHtml.includes("AI Compute &amp; Hardware &lt;Core&gt;"), "entity html must escape HTML entities in theme");
assert(entityHtml.includes("2026-06-30T23:59:59Z"), "entity html must include cutoff");
assert(entityHtml.includes("Large Model Native"), "entity html must format tier label");
assert(entityHtml.includes("+0.42"), "entity html must format signed valuation gap");
assert(entityHtml.includes("1.85"), "entity html must include formatted PEG");
assert(entityHtml.includes("Historical CAGR"), "entity html must include Historical CAGR note");
assert(entityHtml.includes("non_financial"), "entity html must include operating branch");
assert(entityHtml.includes("85%"), "entity html must include theme share percent");
assert(entityHtml.includes("RESEARCH HYPOTHESIS"), "entity html must include research-risk disclaimer");
assert(entityHtml.includes(CARD_TEMPLATE_VERSION), "entity html must reference template version");

const entityVerification = verifyCardHtmlDocument(entityHtml, [
  "issuer:lei:5493001KJTIIGC8Y1R12",
  "NVIDIA Corp & Co <Special>",
  "2026-06-30T23:59:59Z",
  "36.20",
  "+0.42",
  "1.85",
  "0.95",
]);
assert(
  entityVerification.ok,
  `entity HTML must pass fact-preservation verifier: ${entityVerification.violations.join(", ")}`,
);

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
assert(entityHtml.includes("Current P/GP") && entityHtml.includes("Valuation Gap (P/GP)"), "entity html must label P/GP");
assert(!entityHtml.includes("Current P/S"), "entity html must not label P/GP as P/S");
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

console.log("xhs-card.test.ts: all assertions passed");
