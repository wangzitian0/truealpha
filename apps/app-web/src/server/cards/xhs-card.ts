/**
 * Xiaohongshu Card Renderer (1080x1440 3:4 portrait) — see #372 / #44.
 *
 * Provides deterministic SVG and JSON card generation for research rankings,
 * embedding immutable cutoff, governed run lineage, and strict research-risk attribution.
 */

import { formatRatio, formatSignedRatio } from "@/client/format";

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
