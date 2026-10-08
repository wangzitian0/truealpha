/**
 * Xiaohongshu Card Renderer (1080x1440 3:4 portrait) — see #372 / #44.
 *
 * Provides deterministic SVG and JSON card generation for research rankings,
 * embedding immutable cutoff, governed run lineage, and strict research-risk attribution.
 */

import {
  formatPercentFromFraction,
  formatRatio,
  formatSignedRatio,
  formatUsdMagnitude,
} from "@/client/format";

export const XHS_CARD_WIDTH = 1080;
export const XHS_CARD_HEIGHT = 1440;
export const CARD_TEMPLATE_VERSION = "card_template.v1";
export const CARD_SCHEMA_VERSION = "research_card.v1";

export interface RankingCardItem {
  rank: number | null;
  issuerId: string;
  displayName: string;
  tier: string | null;
  currentPriceToSales: string | null;
  valuationGap: string | null;
  peg: string | null;
  confidence: string | null;
}

export interface RankingCardData {
  cutoffAt: string;
  totalMembers: number;
  items: RankingCardItem[];
}

function escapeXml(unsafe: string): string {
  return unsafe
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&apos;");
}

export function buildRankingCardJson(data: RankingCardData) {
  return {
    schema_version: CARD_SCHEMA_VERSION,
    card_kind: "ranking",
    template_version: CARD_TEMPLATE_VERSION,
    title: "Valuation Rankings",
    cutoff_at: data.cutoffAt,
    generated_at: new Date().toISOString(),
    research_risk_note:
      "Ranking reflects one versioned strategy run at one cutoff; historical rank is not a forward-looking guarantee.",
    source_attribution:
      "TrueAlpha research — traceable to a materialized factor output (mart.governed_strategy_run).",
    subjects: data.items.map((item) => ({
      subject_id: item.issuerId,
      display_name: item.displayName,
      rank: item.rank,
      tier: item.tier ?? "—",
      metrics: [
        { key: "current_price_to_sales", value: item.currentPriceToSales },
        { key: "valuation_gap", value: item.valuationGap },
        { key: "peg", value: item.peg },
        { key: "confidence", value: item.confidence },
      ],
    })),
  };
}

export function renderRankingCardSvg(data: RankingCardData): string {
  const displayItems = data.items.slice(0, 7); // Best fit for 1080x1440 layout
  const startY = 360;
  const rowHeight = 110;

  const rowsSvg = displayItems
    .map((item, idx) => {
      const y = startY + idx * rowHeight;
      const rankStr = item.rank !== null ? `#${item.rank}` : "—";
      const gapVal = Number(item.valuationGap);
      const gapColor =
        isNaN(gapVal) || gapVal === 0 ? "#94A3B8" : gapVal > 0 ? "#10B981" : "#F43F5E";
      const formattedGap = formatSignedRatio(item.valuationGap) ?? "—";
      const formattedPs = formatRatio(item.currentPriceToSales) ?? "—";
      const formattedPeg = formatRatio(item.peg) ?? "—";
      const formattedConf = formatRatio(item.confidence) ?? "—";

      return `
    <!-- Row ${idx + 1} -->
    <g transform="translate(60, ${y})">
      <rect width="960" height="96" rx="16" fill="#131B2E" stroke="#1E293B" stroke-width="1.5"/>
      <!-- Rank Badge -->
      <circle cx="56" cy="48" r="28" fill="${idx < 3 ? "#1E293B" : "#0F172A"}" stroke="${idx === 0 ? "#F59E0B" : idx === 1 ? "#94A3B8" : idx === 2 ? "#D97706" : "#334155"}" stroke-width="2"/>
      <text x="56" y="55" font-size="20" font-weight="700" fill="${idx === 0 ? "#F59E0B" : idx === 1 ? "#F1F5F9" : idx === 2 ? "#F59E0B" : "#94A3B8"}" text-anchor="middle">${escapeXml(rankStr)}</text>
      
      <!-- Issuer Name & ID -->
      <text x="110" y="44" font-size="24" font-weight="600" fill="#F8FAFC">${escapeXml(item.displayName)}</text>
      <text x="110" y="68" font-size="16" font-family="monospace" fill="#64748B">${escapeXml(item.issuerId)}</text>

      <!-- Tier Badge -->
      <rect x="360" y="32" width="70" height="32" rx="8" fill="#1E293B" />
      <text x="395" y="53" font-size="14" font-weight="500" fill="#38BDF8" text-anchor="middle">${escapeXml(item.tier ?? "—")}</text>

      <!-- P/S -->
      <text x="500" y="55" font-size="20" font-family="monospace" fill="#E2E8F0" text-anchor="middle">${escapeXml(formattedPs)}</text>

      <!-- Valuation Gap -->
      <text x="660" y="55" font-size="22" font-family="monospace" font-weight="700" fill="${gapColor}" text-anchor="middle">${escapeXml(formattedGap)}</text>

      <!-- PEG -->
      <text x="800" y="55" font-size="18" font-family="monospace" fill="#CBD5E1" text-anchor="middle">${escapeXml(formattedPeg)}</text>

      <!-- Confidence -->
      <text x="900" y="55" font-size="18" font-family="monospace" fill="#94A3B8" text-anchor="middle">${escapeXml(formattedConf)}</text>
    </g>`;
    })
    .join("\n");

  return `<?xml version="1.0" encoding="UTF-8"?>
<svg width="${XHS_CARD_WIDTH}" height="${XHS_CARD_HEIGHT}" viewBox="0 0 ${XHS_CARD_WIDTH} ${XHS_CARD_HEIGHT}" fill="none" xmlns="http://www.w3.org/2000/svg" font-family="system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif">
  <defs>
    <linearGradient id="bgGradient" x1="0" y1="0" x2="1080" y2="1440" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#080C14"/>
      <stop offset="50%" stop-color="#0F172A"/>
      <stop offset="100%" stop-color="#0B0F19"/>
    </linearGradient>
    <linearGradient id="brandLine" x1="60" y1="0" x2="1020" y2="0" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#06B6D4"/>
      <stop offset="50%" stop-color="#3B82F6"/>
      <stop offset="100%" stop-color="#8B5CF6"/>
    </linearGradient>
  </defs>

  <!-- Background Base -->
  <rect width="${XHS_CARD_WIDTH}" height="${XHS_CARD_HEIGHT}" fill="url(#bgGradient)"/>
  
  <!-- Outer Border -->
  <rect x="24" y="24" width="1032" height="1392" rx="32" stroke="#1E293B" stroke-width="2"/>

  <!-- Top Decorative Accent Line -->
  <rect x="60" y="48" width="960" height="4" rx="2" fill="url(#brandLine)"/>

  <!-- Brand Header -->
  <g transform="translate(60, 80)">
    <text font-size="16" font-weight="700" fill="#06B6D4" letter-spacing="3">TRUEALPHA · QUANTITATIVE RESEARCH</text>
    <rect x="830" y="-8" width="130" height="28" rx="6" fill="#1E293B"/>
    <text x="895" y="11" font-size="12" font-weight="600" fill="#94A3B8" text-anchor="middle">1080 × 1440 · 3:4</text>
  </g>

  <!-- Title & Subtitle -->
  <g transform="translate(60, 150)">
    <text font-size="44" font-weight="800" fill="#FFFFFF" letter-spacing="-0.5">Valuation Rankings</text>
    <text y="48" font-size="26" font-weight="500" fill="#94A3B8">大模型价值主题榜单 · 估值偏差排序</text>
  </g>

  <!-- Meta Info Banner -->
  <g transform="translate(60, 246)">
    <rect width="960" height="54" rx="12" fill="#131B2E" stroke="#1E293B"/>
    <circle cx="28" cy="27" r="6" fill="#10B981"/>
    <text x="46" y="33" font-size="16" font-weight="500" fill="#E2E8F0">Governed Execution · Cutoff: <tspan font-family="monospace" font-weight="700">${escapeXml(data.cutoffAt)}</tspan></text>
    <text x="930" y="33" font-size="15" fill="#64748B" text-anchor="end">${data.totalMembers} total members</text>
  </g>

  <!-- Table Header -->
  <g transform="translate(60, 332)">
    <text x="56" y="0" font-size="14" font-weight="600" fill="#64748B" text-anchor="middle">RANK</text>
    <text x="110" y="0" font-size="14" font-weight="600" fill="#64748B">ISSUER</text>
    <text x="395" y="0" font-size="14" font-weight="600" fill="#64748B" text-anchor="middle">TIER</text>
    <text x="500" y="0" font-size="14" font-weight="600" fill="#64748B" text-anchor="middle">P/S</text>
    <text x="660" y="0" font-size="14" font-weight="600" fill="#64748B" text-anchor="middle">VALUATION GAP</text>
    <text x="800" y="0" font-size="14" font-weight="600" fill="#64748B" text-anchor="middle">PEG</text>
    <text x="900" y="0" font-size="14" font-weight="600" fill="#64748B" text-anchor="middle">CONF</text>
  </g>

  <!-- Table Rows -->
  ${rowsSvg}

  <!-- Footer & Governance Section -->
  <g transform="translate(60, 1190)">
    <rect width="960" height="175" rx="16" fill="#0C1322" stroke="#1E293B" stroke-dasharray="4 4"/>
    
    <text x="32" y="36" font-size="15" font-weight="700" fill="#F59E0B" letter-spacing="1">RESEARCH HYPOTHESIS · NOT INVESTMENT ADVICE</text>
    <text x="32" y="66" font-size="15" fill="#94A3B8">Ranking reflects one versioned strategy run at one cutoff; historical rank is not a forward-looking guarantee.</text>
    <text x="32" y="92" font-size="15" fill="#64748B">Traceable to materialized factor output (mart.governed_strategy_run, schema: ${CARD_SCHEMA_VERSION}).</text>
    
    <!-- Bottom Brand Marks -->
    <line x1="32" y1="120" x2="928" y2="120" stroke="#1E293B" stroke-width="1"/>
    <text x="32" y="148" font-size="14" font-weight="600" fill="#38BDF8">TrueAlpha Research Card Export</text>
    <text x="928" y="148" font-size="13" font-family="monospace" fill="#475569" text-anchor="end">template: ${CARD_TEMPLATE_VERSION}</text>
  </g>
</svg>`;
}

export interface EntityCardData {
  issuerId: string;
  displayName: string;
  cutoffAt: string;
  tier: string | null;
  currentPriceToSales: string | null;
  valuationGap: string | null;
  peg: string | null;
  gppe: string | null;
  operatingBranch: string | null;
  themes: Array<{ theme: string; themeShare: string | null }>;
  confidence: string | null;
}

function formatTierLabel(tier: string | null): string {
  if (!tier) return "—";
  if (tier === "large_model_native") return "Large Model Native";
  if (tier === "tech") return "Tech";
  if (tier === "traditional") return "Traditional";
  return tier;
}

export function buildEntityCardJson(data: EntityCardData) {
  return {
    schema_version: CARD_SCHEMA_VERSION,
    card_kind: "entity",
    template_version: CARD_TEMPLATE_VERSION,
    title: "Entity Deep Dive",
    issuer_id: data.issuerId,
    display_name: data.displayName,
    cutoff_at: data.cutoffAt,
    generated_at: new Date().toISOString(),
    research_risk_note:
      "Deep dive reflects one versioned strategy run at one cutoff; historical indicators are not forward-looking guarantees.",
    source_attribution:
      "TrueAlpha research — traceable to materialized factor outputs (mart.strategy_decisions, mart.topt_gppe_results, mart.issuer_theme_purity).",
    metrics: {
      tier: data.tier ?? "—",
      current_price_to_sales: data.currentPriceToSales,
      valuation_gap: data.valuationGap,
      peg: data.peg,
      gppe: data.gppe,
      operating_branch: data.operatingBranch,
      confidence: data.confidence,
    },
    themes: data.themes.map((t) => ({
      theme: t.theme,
      theme_share: t.themeShare,
    })),
  };
}

export function renderEntityDeepDiveCardSvg(data: EntityCardData): string {
  const gapVal = Number(data.valuationGap);
  const gapColor =
    isNaN(gapVal) || gapVal === 0 ? "#94A3B8" : gapVal > 0 ? "#10B981" : "#F43F5E";
  const formattedGap = formatSignedRatio(data.valuationGap) ?? "—";
  const formattedPs = formatRatio(data.currentPriceToSales) ?? "—";
  const formattedPeg = formatRatio(data.peg) ?? "—";
  const formattedGppe = formatUsdMagnitude(data.gppe) ?? (data.gppe ? `$${data.gppe}` : "—");
  const formattedConf = formatRatio(data.confidence) ?? "—";
  const tierText = formatTierLabel(data.tier);

  const displayThemes = data.themes.slice(0, 4);
  const themesSvg =
    displayThemes.length > 0
      ? displayThemes
          .map((t, idx) => {
            const y = 85 + idx * 65;
            const pctStr = formatPercentFromFraction(t.themeShare) ?? "—";
            const shareNum = Number(t.themeShare ?? 0);
            const barWidth = isNaN(shareNum)
              ? 0
              : Math.min(896, Math.max(0, Math.round(shareNum * 896)));
            return `
      <!-- Theme ${idx + 1} -->
      <g transform="translate(32, ${y})">
        <text x="0" y="0" font-size="18" font-weight="600" fill="#E2E8F0">${escapeXml(t.theme)}</text>
        <text x="896" y="0" font-size="18" font-family="monospace" font-weight="700" fill="#38BDF8" text-anchor="end">${escapeXml(pctStr)}</text>
        <rect x="0" y="12" width="896" height="10" rx="5" fill="#1E293B"/>
        <rect x="0" y="12" width="${barWidth}" height="10" rx="5" fill="#38BDF8"/>
      </g>`;
          })
          .join("\n")
      : `
      <g transform="translate(480, 200)">
        <text font-size="16" fill="#64748B" text-anchor="middle">No theme purity materialization recorded for this issuer</text>
      </g>`;

  return `<?xml version="1.0" encoding="UTF-8"?>
<svg width="${XHS_CARD_WIDTH}" height="${XHS_CARD_HEIGHT}" viewBox="0 0 ${XHS_CARD_WIDTH} ${XHS_CARD_HEIGHT}" fill="none" xmlns="http://www.w3.org/2000/svg" font-family="system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif">
  <defs>
    <linearGradient id="bgGradient" x1="0" y1="0" x2="1080" y2="1440" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#080C14"/>
      <stop offset="50%" stop-color="#0F172A"/>
      <stop offset="100%" stop-color="#0B0F19"/>
    </linearGradient>
    <linearGradient id="brandLine" x1="60" y1="0" x2="1020" y2="0" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#06B6D4"/>
      <stop offset="50%" stop-color="#3B82F6"/>
      <stop offset="100%" stop-color="#8B5CF6"/>
    </linearGradient>
  </defs>

  <!-- Background Base -->
  <rect width="${XHS_CARD_WIDTH}" height="${XHS_CARD_HEIGHT}" fill="url(#bgGradient)"/>
  
  <!-- Outer Border -->
  <rect x="24" y="24" width="1032" height="1392" rx="32" stroke="#1E293B" stroke-width="2"/>

  <!-- Top Decorative Accent Line -->
  <rect x="60" y="48" width="960" height="4" rx="2" fill="url(#brandLine)"/>

  <!-- Brand Header -->
  <g transform="translate(60, 80)">
    <text font-size="16" font-weight="700" fill="#06B6D4" letter-spacing="3">TRUEALPHA · RESEARCH DEEP DIVE</text>
    <rect x="830" y="-8" width="130" height="28" rx="6" fill="#1E293B"/>
    <text x="895" y="11" font-size="12" font-weight="600" fill="#94A3B8" text-anchor="middle">1080 × 1440 · 3:4</text>
  </g>

  <!-- Title & Subtitle -->
  <g transform="translate(60, 145)">
    <text font-size="42" font-weight="800" fill="#FFFFFF" letter-spacing="-0.5">TrueAlpha Deep Dive</text>
    <text y="42" font-size="22" font-weight="500" fill="#94A3B8">单票 360° 研究卡片 · 估值 / 效率 / 增速 / 主题纯度</text>
  </g>

  <!-- Meta Info Banner -->
  <g transform="translate(60, 235)">
    <rect width="960" height="48" rx="10" fill="#131B2E" stroke="#1E293B"/>
    <circle cx="24" cy="24" r="5" fill="#10B981"/>
    <text x="42" y="30" font-size="15" font-weight="500" fill="#E2E8F0">Governed Cutoff: <tspan font-family="monospace" font-weight="700">${escapeXml(data.cutoffAt)}</tspan></text>
    <text x="936" y="30" font-size="14" fill="#64748B" text-anchor="end">Confidence: <tspan font-family="monospace" font-weight="600" fill="#94A3B8">${escapeXml(formattedConf)}</tspan></text>
  </g>

  <!-- Issuer Header Card -->
  <g transform="translate(60, 305)">
    <rect width="960" height="110" rx="16" fill="#131B2E" stroke="#1E293B" stroke-width="1.5"/>
    <text x="32" y="48" font-size="30" font-weight="700" fill="#F8FAFC">${escapeXml(data.displayName)}</text>
    <text x="32" y="82" font-size="16" font-family="monospace" fill="#64748B">${escapeXml(data.issuerId)}</text>

    <!-- Tier Badge -->
    <rect x="710" y="32" width="220" height="44" rx="10" fill="#1E293B" stroke="#38BDF8" stroke-width="1.5"/>
    <text x="820" y="60" font-size="15" font-weight="600" fill="#38BDF8" text-anchor="middle">${escapeXml(tierText)}</text>
  </g>

  <!-- Module 7 Valuation & Module 1 Growth Row -->
  <g transform="translate(60, 435)">
    <!-- Valuation Block -->
    <g transform="translate(0, 0)">
      <rect width="465" height="155" rx="16" fill="#131B2E" stroke="#1E293B"/>
      <text x="24" y="32" font-size="13" font-weight="700" fill="#64748B" letter-spacing="1">VALUATION · MODULE 7</text>
      
      <text x="24" y="70" font-size="15" fill="#94A3B8">Current P/S</text>
      <text x="24" y="105" font-size="28" font-family="monospace" font-weight="700" fill="#E2E8F0">${escapeXml(formattedPs)}</text>

      <text x="250" y="70" font-size="15" fill="#94A3B8">Valuation Gap</text>
      <text x="250" y="105" font-size="28" font-family="monospace" font-weight="700" fill="${gapColor}">${escapeXml(formattedGap)}</text>
    </g>

    <!-- Growth Block (PEG) -->
    <g transform="translate(495, 0)">
      <rect width="465" height="155" rx="16" fill="#131B2E" stroke="#1E293B"/>
      <text x="24" y="32" font-size="13" font-weight="700" fill="#64748B" letter-spacing="1">GROWTH · MODULE 1</text>
      
      <text x="24" y="70" font-size="15" fill="#94A3B8">PEG</text>
      <text x="24" y="112" font-size="34" font-family="monospace" font-weight="700" fill="#38BDF8">${escapeXml(formattedPeg)}</text>
      <text x="440" y="110" font-size="13" fill="#64748B" text-anchor="end">Historical CAGR</text>
    </g>
  </g>

  <!-- Module 2 Labor Efficiency / GPPE Block -->
  <g transform="translate(60, 610)">
    <rect width="960" height="145" rx="16" fill="#131B2E" stroke="#1E293B"/>
    <text x="32" y="34" font-size="13" font-weight="700" fill="#64748B" letter-spacing="1">LABOR EFFICIENCY · MODULE 2</text>
    
    <text x="32" y="72" font-size="15" fill="#94A3B8">Gross Profit / Employee (GPPE)</text>
    <text x="32" y="114" font-size="36" font-family="monospace" font-weight="700" fill="#F8FAFC">${escapeXml(formattedGppe)}</text>

    <!-- Operating Branch Tag -->
    <rect x="740" y="55" width="190" height="38" rx="8" fill="#1E293B" stroke="#334155"/>
    <text x="835" y="79" font-size="14" font-weight="600" fill="#38BDF8" text-anchor="middle">${escapeXml(data.operatingBranch ?? "non_financial")}</text>
  </g>

  <!-- Module 6 Theme Purity Block -->
  <g transform="translate(60, 775)">
    <rect width="960" height="385" rx="16" fill="#131B2E" stroke="#1E293B"/>
    <text x="32" y="36" font-size="14" font-weight="700" fill="#64748B" letter-spacing="1">THEME PURITY &amp; BUSINESS EXPOSURE · MODULE 6</text>
    <text x="32" y="60" font-size="13" fill="#475569">Revenue partition classified by traceable segment purity</text>
    
    ${themesSvg}
  </g>

  <!-- Footer & Governance Section -->
  <g transform="translate(60, 1180)">
    <rect width="960" height="185" rx="16" fill="#0C1322" stroke="#1E293B" stroke-dasharray="4 4"/>
    
    <text x="32" y="36" font-size="15" font-weight="700" fill="#F59E0B" letter-spacing="1">RESEARCH HYPOTHESIS · NOT INVESTMENT ADVICE</text>
    <text x="32" y="66" font-size="15" fill="#94A3B8">Deep dive reflects one versioned strategy run at one cutoff; historical indicators are not forward-looking guarantees.</text>
    <text x="32" y="92" font-size="15" fill="#64748B">Traceable to materialized factor outputs (mart.strategy_decisions, mart.topt_gppe_results, mart.issuer_theme_purity, schema: ${CARD_SCHEMA_VERSION}).</text>
    
    <!-- Bottom Brand Marks -->
    <line x1="32" y1="126" x2="928" y2="126" stroke="#1E293B" stroke-width="1"/>
    <text x="32" y="156" font-size="14" font-weight="600" fill="#38BDF8">TrueAlpha Research Card Export</text>
    <text x="928" y="156" font-size="13" font-family="monospace" fill="#475569" text-anchor="end">template: ${CARD_TEMPLATE_VERSION}</text>
  </g>
</svg>`;
}
