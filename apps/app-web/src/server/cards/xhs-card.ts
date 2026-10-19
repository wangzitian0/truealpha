/**
 * Xiaohongshu card generation for research rankings and entity deep-dives (#372 / #44 / #1360).
 *
 * Outputs:
 * - PNG at 1080x1440 (3:4 portrait) via `next/og` (`ImageResponse`).
 * - Deterministic SVG (`image/svg+xml`) and JSON metadata payload.
 *
 * Persists exported binary cards to `/research/library` via `PostgresDocumentsRepository`.
 */

import React from "react";
import { ImageResponse } from "next/og";
import { NextResponse } from "next/server";
import {
  formatPercentFromFraction,
  formatRatio,
  formatSignedRatio,
  formatTierLabel,
  formatUsdMagnitude,
} from "@/client/format";
import { type AccessContext, type ValuationBasis, valuationBasisLabel } from "@/contracts/strategyRun";
import { loadEntityDetail, loadRanking, type MartAdapterLike } from "@/server/dashboard";
import { PostgresDocumentsRepository, type DocumentsRepository } from "@/server/documents";
import { entityLabel, loadEntityDisplayMap } from "@/server/mart/entity-resolution";

export const XHS_CARD_WIDTH = 1080;
export const XHS_CARD_HEIGHT = 1440;
export const CARD_TEMPLATE_VERSION = "card_template.v1";
export const CARD_SCHEMA_VERSION = "research_card.v1";
export const CARD_EXPORT_CACHE_CONTROL = "public, max-age=300" as const;

export interface RankingCardItem {
  rank: number | null;
  issuerId: string;
  displayName: string;
  tier: string | null;
  currentPriceToSales: string | null;
  /** #1235: the multiple the gap divides (P/GP under definition 1.0.0) and its basis.
   * Null basis: the decision carries no target, so the card names no multiple. */
  currentMultiple: string | null;
  valuationBasis: ValuationBasis | null;
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

function gapHexColor(valuationGap: string | null): string {
  const gapVal = Number(valuationGap);
  if (Number.isNaN(gapVal) || gapVal === 0) return "#94A3B8";
  return gapVal > 0 ? "#10B981" : "#F43F5E";
}

interface FormattedRankingRow {
  issuerId: string;
  displayName: string;
  rankStr: string;
  tierStr: string;
  gapColor: string;
  formattedGap: string;
  formattedMultiple: string;
  formattedPeg: string;
  formattedConf: string;
}

function prepareRankingCardViewModel(
  data: RankingCardData,
  fallbackGlyph: string,
): { basisLabel: string; rows: FormattedRankingRow[] } {
  const displayItems = data.items.slice(0, 7);
  const basisLabel = valuationBasisLabel(
    data.items.find((item) => item.valuationBasis)?.valuationBasis ?? null,
  );
  const rows = displayItems.map((item) => ({
    issuerId: item.issuerId,
    displayName: item.displayName,
    rankStr: item.rank !== null ? `#${item.rank}` : fallbackGlyph,
    tierStr: item.tier ?? fallbackGlyph,
    gapColor: gapHexColor(item.valuationGap),
    formattedGap: formatSignedRatio(item.valuationGap) ?? fallbackGlyph,
    formattedMultiple: formatRatio(item.currentMultiple) ?? fallbackGlyph,
    formattedPeg: formatRatio(item.peg) ?? fallbackGlyph,
    formattedConf: formatRatio(item.confidence) ?? fallbackGlyph,
  }));
  return { basisLabel, rows };
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
      valuation_basis: item.valuationBasis,
      metrics: [
        { key: "current_price_to_sales", value: item.currentPriceToSales },
        {
          key: "current_price_to_gross_profit",
          value: item.valuationBasis === "price_to_gross_profit" ? item.currentMultiple : null,
        },
        { key: "valuation_gap", value: item.valuationGap },
        { key: "peg", value: item.peg },
        { key: "confidence", value: item.confidence },
      ],
    })),
  };
}

export function renderRankingCardSvg(data: RankingCardData): string {
  const { basisLabel, rows } = prepareRankingCardViewModel(data, "—");
  const startY = 360;
  const rowHeight = 110;

  const rowsSvg = rows
    .map((row, idx) => {
      const y = startY + idx * rowHeight;

      return `
    <!-- Row ${idx + 1} -->
    <g transform="translate(60, ${y})">
      <rect width="960" height="96" rx="16" fill="#131B2E" stroke="#1E293B" stroke-width="1.5"/>
      <!-- Rank Badge -->
      <circle cx="56" cy="48" r="28" fill="${idx < 3 ? "#1E293B" : "#0F172A"}" stroke="${idx === 0 ? "#F59E0B" : idx === 1 ? "#94A3B8" : idx === 2 ? "#D97706" : "#334155"}" stroke-width="2"/>
      <text x="56" y="55" font-size="20" font-weight="700" fill="${idx === 0 ? "#F59E0B" : idx === 1 ? "#F1F5F9" : idx === 2 ? "#F59E0B" : "#94A3B8"}" text-anchor="middle">${escapeXml(row.rankStr)}</text>
      
      <!-- Issuer Name & ID -->
      <text x="110" y="44" font-size="24" font-weight="600" fill="#F8FAFC">${escapeXml(row.displayName)}</text>
      <text x="110" y="68" font-size="16" font-family="monospace" fill="#64748B">${escapeXml(row.issuerId)}</text>

      <!-- Tier Badge -->
      <rect x="360" y="32" width="70" height="32" rx="8" fill="#1E293B" />
      <text x="395" y="53" font-size="14" font-weight="500" fill="#38BDF8" text-anchor="middle">${escapeXml(row.tierStr)}</text>

      <!-- Current multiple of the valuation basis -->
      <text x="500" y="55" font-size="20" font-family="monospace" fill="#E2E8F0" text-anchor="middle">${escapeXml(row.formattedMultiple)}</text>

      <!-- Valuation Gap -->
      <text x="660" y="55" font-size="22" font-family="monospace" font-weight="700" fill="${row.gapColor}" text-anchor="middle">${escapeXml(row.formattedGap)}</text>

      <!-- PEG -->
      <text x="800" y="55" font-size="18" font-family="monospace" fill="#CBD5E1" text-anchor="middle">${escapeXml(row.formattedPeg)}</text>

      <!-- Confidence -->
      <text x="900" y="55" font-size="18" font-family="monospace" fill="#94A3B8" text-anchor="middle">${escapeXml(row.formattedConf)}</text>
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
    <text x="500" y="0" font-size="14" font-weight="600" fill="#64748B" text-anchor="middle">${escapeXml(basisLabel)}</text>
    <text x="660" y="0" font-size="14" font-weight="600" fill="#64748B" text-anchor="middle">VALUATION GAP (${escapeXml(basisLabel)})</text>
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
  /** #1235: the multiple the gap divides (P/GP under definition 1.0.0) and its basis. */
  currentMultiple: string | null;
  valuationBasis: ValuationBasis | null;
  valuationGap: string | null;
  peg: string | null;
  gppe: string | null;
  operatingBranch: string | null;
  themes: Array<{ theme: string; themeShare: string | null }>;
  confidence: string | null;
}

interface FormattedEntityViewModel {
  gapColor: string;
  formattedGap: string;
  formattedMultiple: string;
  basisLabel: string;
  formattedPeg: string;
  formattedGppe: string;
  formattedConf: string;
  tierText: string;
  displayThemes: Array<{ theme: string; themeShare: string | null }>;
}

function prepareEntityCardViewModel(
  data: EntityCardData,
  fallbackGlyph: string,
): FormattedEntityViewModel {
  return {
    gapColor: gapHexColor(data.valuationGap),
    formattedGap: formatSignedRatio(data.valuationGap) ?? fallbackGlyph,
    formattedMultiple: formatRatio(data.currentMultiple) ?? fallbackGlyph,
    basisLabel: valuationBasisLabel(data.valuationBasis),
    formattedPeg: formatRatio(data.peg) ?? fallbackGlyph,
    formattedGppe: formatUsdMagnitude(data.gppe) ?? (data.gppe ? `$${data.gppe}` : fallbackGlyph),
    formattedConf: formatRatio(data.confidence) ?? fallbackGlyph,
    tierText: formatTierLabel(data.tier),
    displayThemes: data.themes.slice(0, 4),
  };
}

/**
 * The themes of an entity card. A fill row (an issuer without a segment partition, #1117) is not
 * a theme: the card lists judged themes and refused real rows only.
 */
export function entityCardThemes(
  themes: ReadonlyArray<{ theme: string; themeShare: string | null; unvisitedReason?: string | null }>,
): EntityCardData["themes"] {
  return themes
    .filter((t) => !t.unvisitedReason)
    .map((t) => ({ theme: t.theme, themeShare: t.themeShare }));
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
      current_price_to_gross_profit: data.valuationBasis === "price_to_gross_profit" ? data.currentMultiple : null,
      valuation_basis: data.valuationBasis,
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
  const {
    gapColor,
    formattedGap,
    formattedMultiple: formattedPs,
    basisLabel,
    formattedPeg,
    formattedGppe,
    formattedConf,
    tierText,
    displayThemes,
  } = prepareEntityCardViewModel(data, "—");
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
      
      <text x="24" y="70" font-size="15" fill="#94A3B8">Current ${escapeXml(basisLabel)}</text>
      <text x="24" y="105" font-size="28" font-family="monospace" font-weight="700" fill="#E2E8F0">${escapeXml(formattedPs)}</text>

      <text x="250" y="70" font-size="15" fill="#94A3B8">Valuation Gap (${escapeXml(basisLabel)})</text>
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
    <text x="835" y="79" font-size="14" font-weight="600" fill="#38BDF8" text-anchor="middle">${escapeXml(data.operatingBranch ?? "—")}</text>
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

const h = React.createElement;

export async function renderRankingCardPng(data: RankingCardData): Promise<Buffer> {
  const { basisLabel, rows } = prepareRankingCardViewModel(data, "-");

  const rowNodes = rows.map((row, idx) =>
    h(
      "div",
      {
        key: `${row.issuerId}-${idx}`,
        style: {
          display: "flex",
          flexDirection: "row",
          alignItems: "center",
          justifyContent: "space-between",
          width: "960px",
          height: "92px",
          borderRadius: "16px",
          backgroundColor: "#131B2E",
          border: "2px solid #1E293B",
          paddingLeft: "24px",
          paddingRight: "24px",
          marginBottom: "14px",
        },
      },
      h(
        "div",
        {
          style: {
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            width: "56px",
            height: "56px",
            borderRadius: "28px",
            backgroundColor: "#1E293B",
            color: idx === 0 ? "#F59E0B" : "#F8FAFC",
            fontSize: 20,
            fontWeight: 700,
          },
        },
        row.rankStr,
      ),
      h(
        "div",
        {
          style: {
            display: "flex",
            flexDirection: "column",
            width: "260px",
            marginLeft: "16px",
          },
        },
        h("div", { style: { fontSize: 22, fontWeight: 700, color: "#F8FAFC" } }, row.displayName),
        h("div", { style: { fontSize: 14, color: "#64748B", marginTop: "4px" } }, row.issuerId),
      ),
      h(
        "div",
        {
          style: {
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            width: "110px",
            height: "34px",
            borderRadius: "8px",
            backgroundColor: "#1E293B",
            color: "#38BDF8",
            fontSize: 14,
            fontWeight: 600,
          },
        },
        row.tierStr,
      ),
      h(
        "div",
        { style: { width: "110px", textAlign: "center", fontSize: 20, color: "#E2E8F0" } },
        row.formattedMultiple,
      ),
      h(
        "div",
        {
          style: {
            width: "140px",
            textAlign: "center",
            fontSize: 22,
            fontWeight: 700,
            color: row.gapColor,
          },
        },
        row.formattedGap,
      ),
      h(
        "div",
        { style: { width: "90px", textAlign: "center", fontSize: 18, color: "#CBD5E1" } },
        row.formattedPeg,
      ),
      h(
        "div",
        { style: { width: "90px", textAlign: "center", fontSize: 18, color: "#94A3B8" } },
        row.formattedConf,
      ),
    ),
  );

  const tree = h(
    "div",
    {
      style: {
        display: "flex",
        flexDirection: "column",
        justifyContent: "space-between",
        width: `${XHS_CARD_WIDTH}px`,
        height: `${XHS_CARD_HEIGHT}px`,
        backgroundColor: "#0B0F19",
        color: "#F8FAFC",
        padding: "56px",
      },
    },
    h(
      "div",
      { style: { display: "flex", flexDirection: "column" } },
      h(
        "div",
        {
          style: {
            display: "flex",
            flexDirection: "row",
            justifyContent: "space-between",
            alignItems: "center",
          },
        },
        h(
          "div",
          { style: { fontSize: 16, fontWeight: 700, color: "#06B6D4", letterSpacing: "2px" } },
          "TRUEALPHA · QUANTITATIVE RESEARCH",
        ),
        h(
          "div",
          {
            style: {
              backgroundColor: "#1E293B",
              color: "#94A3B8",
              fontSize: 13,
              fontWeight: 600,
              padding: "6px 12px",
              borderRadius: "6px",
            },
          },
          `${XHS_CARD_WIDTH} x ${XHS_CARD_HEIGHT} · 3:4`,
        ),
      ),
      h(
        "div",
        { style: { fontSize: 44, fontWeight: 700, color: "#FFFFFF", marginTop: "24px" } },
        "Valuation Rankings",
      ),
      h(
        "div",
        { style: { fontSize: 22, color: "#94A3B8", marginTop: "8px" } },
        `Large-Model Value Universe · Valuation Gap (${basisLabel})`,
      ),
      h(
        "div",
        {
          style: {
            display: "flex",
            flexDirection: "row",
            justifyContent: "space-between",
            alignItems: "center",
            backgroundColor: "#131B2E",
            border: "1px solid #1E293B",
            borderRadius: "12px",
            padding: "14px 20px",
            marginTop: "24px",
          },
        },
        h(
          "div",
          { style: { fontSize: 16, color: "#E2E8F0" } },
          `Governed Execution · Cutoff: ${data.cutoffAt}`,
        ),
        h(
          "div",
          { style: { fontSize: 15, color: "#64748B" } },
          `${data.totalMembers} total members`,
        ),
      ),
      h(
        "div",
        { style: { display: "flex", flexDirection: "column", marginTop: "28px" } },
        ...rowNodes,
      ),
    ),
    h(
      "div",
      {
        style: {
          display: "flex",
          flexDirection: "column",
          backgroundColor: "#0C1322",
          border: "1px solid #1E293B",
          borderRadius: "16px",
          padding: "24px",
        },
      },
      h(
        "div",
        { style: { fontSize: 15, fontWeight: 700, color: "#F59E0B" } },
        "RESEARCH HYPOTHESIS · NOT INVESTMENT ADVICE",
      ),
      h(
        "div",
        { style: { fontSize: 14, color: "#94A3B8", marginTop: "8px" } },
        "Ranking reflects one versioned strategy run at one cutoff; historical rank is not a forward-looking guarantee.",
      ),
      h(
        "div",
        {
          style: {
            display: "flex",
            flexDirection: "row",
            justifyContent: "space-between",
            marginTop: "14px",
            fontSize: 13,
            color: "#38BDF8",
          },
        },
        h("div", null, "TrueAlpha Research Card Export"),
        h("div", { style: { color: "#64748B" } }, `template: ${CARD_TEMPLATE_VERSION}`),
      ),
    ),
  );

  const response = new ImageResponse(tree, {
    width: XHS_CARD_WIDTH,
    height: XHS_CARD_HEIGHT,
  });
  return Buffer.from(await response.arrayBuffer());
}

export async function renderEntityDeepDiveCardPng(data: EntityCardData): Promise<Buffer> {
  const vm = prepareEntityCardViewModel(data, "-");
  const themeNodes =
    vm.displayThemes.length > 0
      ? vm.displayThemes.map((t, idx) =>
          h(
            "div",
            {
              key: `${t.theme}-${idx}`,
              style: {
                display: "flex",
                flexDirection: "row",
                justifyContent: "space-between",
                alignItems: "center",
                backgroundColor: "#0F172A",
                borderRadius: "10px",
                padding: "14px 18px",
                marginBottom: "10px",
              },
            },
            h("div", { style: { fontSize: 18, fontWeight: 600, color: "#E2E8F0" } }, t.theme),
            h(
              "div",
              { style: { fontSize: 18, fontWeight: 700, color: "#38BDF8" } },
              formatPercentFromFraction(t.themeShare) ?? "-",
            ),
          ),
        )
      : [
          h(
            "div",
            { key: "empty", style: { fontSize: 16, color: "#64748B", marginTop: "12px" } },
            "No theme purity materialization recorded for this issuer",
          ),
        ];

  const tree = h(
    "div",
    {
      style: {
        display: "flex",
        flexDirection: "column",
        justifyContent: "space-between",
        width: `${XHS_CARD_WIDTH}px`,
        height: `${XHS_CARD_HEIGHT}px`,
        backgroundColor: "#0B0F19",
        color: "#F8FAFC",
        padding: "56px",
      },
    },
    h(
      "div",
      { style: { display: "flex", flexDirection: "column" } },
      h(
        "div",
        {
          style: {
            display: "flex",
            flexDirection: "row",
            justifyContent: "space-between",
            alignItems: "center",
          },
        },
        h(
          "div",
          { style: { fontSize: 16, fontWeight: 700, color: "#06B6D4", letterSpacing: "2px" } },
          "TRUEALPHA · RESEARCH DEEP DIVE",
        ),
        h(
          "div",
          {
            style: {
              backgroundColor: "#1E293B",
              color: "#94A3B8",
              fontSize: 13,
              fontWeight: 600,
              padding: "6px 12px",
              borderRadius: "6px",
            },
          },
          `${XHS_CARD_WIDTH} x ${XHS_CARD_HEIGHT} · 3:4`,
        ),
      ),
      h(
        "div",
        { style: { fontSize: 42, fontWeight: 700, color: "#FFFFFF", marginTop: "20px" } },
        "TrueAlpha Deep Dive",
      ),
      h(
        "div",
        {
          style: {
            display: "flex",
            flexDirection: "row",
            justifyContent: "space-between",
            alignItems: "center",
            backgroundColor: "#131B2E",
            border: "1px solid #1E293B",
            borderRadius: "16px",
            padding: "24px",
            marginTop: "20px",
          },
        },
        h(
          "div",
          { style: { display: "flex", flexDirection: "column" } },
          h("div", { style: { fontSize: 30, fontWeight: 700, color: "#F8FAFC" } }, data.displayName),
          h("div", { style: { fontSize: 16, color: "#64748B", marginTop: "6px" } }, data.issuerId),
        ),
        h(
          "div",
          {
            style: {
              backgroundColor: "#1E293B",
              border: "1px solid #38BDF8",
              borderRadius: "10px",
              padding: "10px 20px",
              color: "#38BDF8",
              fontSize: 16,
              fontWeight: 600,
            },
          },
          vm.tierText,
        ),
      ),
      h(
        "div",
        {
          style: {
            display: "flex",
            flexDirection: "row",
            justifyContent: "space-between",
            marginTop: "20px",
          },
        },
        h(
          "div",
          {
            style: {
              display: "flex",
              flexDirection: "column",
              width: "465px",
              backgroundColor: "#131B2E",
              border: "1px solid #1E293B",
              borderRadius: "16px",
              padding: "24px",
            },
          },
          h("div", { style: { fontSize: 13, fontWeight: 700, color: "#64748B" } }, "VALUATION · MODULE 7"),
          h(
            "div",
            { style: { fontSize: 16, color: "#94A3B8", marginTop: "12px" } },
            `Current ${vm.basisLabel}: ${vm.formattedMultiple}`,
          ),
          h(
            "div",
            { style: { fontSize: 24, fontWeight: 700, color: vm.gapColor, marginTop: "8px" } },
            `Valuation Gap (${vm.basisLabel}): ${vm.formattedGap}`,
          ),
        ),
        h(
          "div",
          {
            style: {
              display: "flex",
              flexDirection: "column",
              width: "465px",
              backgroundColor: "#131B2E",
              border: "1px solid #1E293B",
              borderRadius: "16px",
              padding: "24px",
            },
          },
          h("div", { style: { fontSize: 13, fontWeight: 700, color: "#64748B" } }, "GROWTH · MODULE 1"),
          h(
            "div",
            { style: { fontSize: 28, fontWeight: 700, color: "#38BDF8", marginTop: "14px" } },
            `PEG: ${vm.formattedPeg}`,
          ),
          h("div", { style: { fontSize: 14, color: "#64748B", marginTop: "6px" } }, "Historical CAGR"),
        ),
      ),
      h(
        "div",
        {
          style: {
            display: "flex",
            flexDirection: "row",
            justifyContent: "space-between",
            alignItems: "center",
            backgroundColor: "#131B2E",
            border: "1px solid #1E293B",
            borderRadius: "16px",
            padding: "24px",
            marginTop: "20px",
          },
        },
        h(
          "div",
          { style: { display: "flex", flexDirection: "column" } },
          h("div", { style: { fontSize: 13, fontWeight: 700, color: "#64748B" } }, "LABOR EFFICIENCY · MODULE 2"),
          h("div", { style: { fontSize: 30, fontWeight: 700, color: "#F8FAFC", marginTop: "10px" } }, vm.formattedGppe),
        ),
        h(
          "div",
          { style: { fontSize: 15, color: "#94A3B8" } },
          `Branch: ${data.operatingBranch ?? "-"} · Conf: ${vm.formattedConf}`,
        ),
      ),
      h(
        "div",
        {
          style: {
            display: "flex",
            flexDirection: "column",
            backgroundColor: "#131B2E",
            border: "1px solid #1E293B",
            borderRadius: "16px",
            padding: "24px",
            marginTop: "20px",
          },
        },
        h(
          "div",
          { style: { fontSize: 14, fontWeight: 700, color: "#64748B", marginBottom: "14px" } },
          "THEME PURITY & BUSINESS EXPOSURE · MODULE 6",
        ),
        ...themeNodes,
      ),
    ),
    h(
      "div",
      {
        style: {
          display: "flex",
          flexDirection: "column",
          backgroundColor: "#0C1322",
          border: "1px solid #1E293B",
          borderRadius: "16px",
          padding: "24px",
        },
      },
      h(
        "div",
        { style: { fontSize: 15, fontWeight: 700, color: "#F59E0B" } },
        "RESEARCH HYPOTHESIS · NOT INVESTMENT ADVICE",
      ),
      h(
        "div",
        { style: { fontSize: 14, color: "#94A3B8", marginTop: "8px" } },
        `Cutoff: ${data.cutoffAt} · Traceable to materialized factor outputs (schema: ${CARD_SCHEMA_VERSION}).`,
      ),
      h(
        "div",
        {
          style: {
            display: "flex",
            flexDirection: "row",
            justifyContent: "space-between",
            marginTop: "14px",
            fontSize: 13,
            color: "#38BDF8",
          },
        },
        h("div", null, "TrueAlpha Research Card Export"),
        h("div", { style: { color: "#64748B" } }, `template: ${CARD_TEMPLATE_VERSION}`),
      ),
    ),
  );

  const response = new ImageResponse(tree, {
    width: XHS_CARD_WIDTH,
    height: XHS_CARD_HEIGHT,
  });
  return Buffer.from(await response.arrayBuffer());
}

function toStableToken(raw: string): string {
  const cleaned = raw
    .replace(/[^A-Za-z0-9._:@+-]/g, "-")
    .replace(/^[._:@+-]+/, "v")
    .split(/[._:/@+-]/)
    .map((token) => {
      const lower = token.toLowerCase();
      if (lower === "latest" || lower === "current" || lower === "default" || lower === "head") {
        return `v-${token}`;
      }
      return token;
    })
    .filter((token) => token.length > 0)
    .join("-");
  return cleaned.length > 0 ? cleaned : "v1";
}

async function persistCardDocument(
  documents: DocumentsRepository,
  context: AccessContext,
  sourceArtifactId: string,
  bytes: Buffer,
  contentType: string,
): Promise<string | null> {
  try {
    const created = await documents.createDocument(context, {
      sourceArtifactId,
      bytes,
      contentType,
    });
    return created.document.documentId;
  } catch {
    return null;
  }
}

async function buildBinaryCardResponse(options: {
  body: BodyInit;
  bytes: Buffer;
  artifactContentType: "image/svg+xml" | "image/png";
  headerContentType: "image/svg+xml; charset=utf-8" | "image/png";
  filename: string;
  persist: boolean;
  documents: DocumentsRepository;
  context: AccessContext;
  artifactId: string;
}): Promise<Response> {
  const docId = options.persist
    ? await persistCardDocument(
        options.documents,
        options.context,
        options.artifactId,
        options.bytes,
        options.artifactContentType,
      )
    : null;
  const headers: Record<string, string> = {
    "Content-Type": options.headerContentType,
    "Content-Disposition": `attachment; filename="${options.filename}"`,
    "Cache-Control": "public, max-age=300",
  };
  if (docId) headers["X-Research-Document-Id"] = docId;
  return new Response(options.body, { status: 200, headers });
}

export interface CardExportRouteDeps {
  resolvePrincipal?: () => Promise<{ context: AccessContext } | null>;
  adapter?: MartAdapterLike;
  documents?: DocumentsRepository;
  displayNames?: Awaited<ReturnType<typeof loadEntityDisplayMap>>;
}

export async function handleCardExportGet(
  request: Request,
  deps: CardExportRouteDeps = {},
): Promise<Response> {
  const resolvePrincipal =
    deps.resolvePrincipal ?? (await import("@/server/auth/request-context")).getServerPrincipal;
  const principal = await resolvePrincipal();
  if (!principal) {
    return NextResponse.json({ error: "authentication required" }, { status: 401 });
  }

  const { searchParams } = new URL(request.url);
  const kind = searchParams.get("kind") ?? "ranking";
  const cutoff = searchParams.get("cutoff") ?? undefined;
  const format = searchParams.get("format") ?? "png";
  const persist = searchParams.get("persist") !== "0";
  const documents = deps.documents ?? new PostgresDocumentsRepository();

  if (kind === "entity") {
    const issuerId = searchParams.get("issuer");
    if (!issuerId) {
      return NextResponse.json({ error: "missing issuer param" }, { status: 400 });
    }

    const state = await loadEntityDetail(principal.context, issuerId, deps.adapter);
    if (state.kind !== "ready") {
      return NextResponse.json(
        { error: "entity detail not ready for card export", read_state: state.kind },
        { status: 404 },
      );
    }

    const names = deps.displayNames ?? (await loadEntityDisplayMap());
    const latestRow = state.data.rows
      .slice()
      .sort((a, b) => b.cutoffAt.localeCompare(a.cutoffAt))[0];

    const cardData: EntityCardData = {
      issuerId: state.data.issuerId,
      displayName: entityLabel(state.data.issuerId, names),
      cutoffAt: latestRow?.cutoffAt ?? new Date().toISOString(),
      tier: latestRow?.tier ?? null,
      currentPriceToSales: latestRow?.currentPriceToSales ?? null,
      currentMultiple: latestRow?.currentMultiple ?? null,
      valuationBasis: latestRow?.valuationBasis ?? null,
      valuationGap: latestRow?.valuationGap ?? null,
      peg: latestRow?.peg ?? null,
      gppe: state.data.gppeDetail?.gppe ?? latestRow?.capitalAdjustedLaborEfficiency ?? null,
      operatingBranch: state.data.gppeDetail?.operatingBranch ?? null,
      themes: entityCardThemes(state.data.themes ?? []),
      confidence: latestRow?.confidence ?? null,
    };

    if (format === "json") {
      return NextResponse.json(buildEntityCardJson(cardData), {
        status: 200,
        headers: {
          "Cache-Control": "public, max-age=300",
        },
      });
    }

    const artifactId = `card:entity:${toStableToken(state.data.issuerId)}:${toStableToken(cardData.cutoffAt)}`;
    const fileStem = `truealpha-card-entity-${state.data.issuerId.replace(/[:]/g, "-")}`;

    if (format === "svg") {
      const svg = renderEntityDeepDiveCardSvg(cardData);
      return buildBinaryCardResponse({
        body: svg,
        bytes: Buffer.from(svg, "utf8"),
        artifactContentType: "image/svg+xml",
        headerContentType: "image/svg+xml; charset=utf-8",
        filename: `${fileStem}.svg`,
        persist,
        documents,
        context: principal.context,
        artifactId,
      });
    }

    const png = await renderEntityDeepDiveCardPng(cardData);
    return buildBinaryCardResponse({
      body: new Uint8Array(png),
      bytes: png,
      artifactContentType: "image/png",
      headerContentType: "image/png",
      filename: `${fileStem}.png`,
      persist,
      documents,
      context: principal.context,
      artifactId,
    });
  }

  const state = await loadRanking(principal.context, { cutoffAt: cutoff, cursor: null }, deps.adapter);
  if (state.kind !== "ready") {
    return NextResponse.json(
      { error: "ranking data not ready for card export", read_state: state.kind },
      { status: 404 },
    );
  }

  const names = deps.displayNames ?? (await loadEntityDisplayMap());

  const cardData: RankingCardData = {
    cutoffAt: state.data.cutoffAt,
    totalMembers: state.data.page.total,
    items: state.data.rows.map((row) => ({
      rank: row.rank,
      issuerId: row.issuerId,
      displayName: entityLabel(row.issuerId, names),
      tier: row.tier,
      currentPriceToSales: row.currentPriceToSales,
      currentMultiple: row.currentMultiple,
      valuationBasis: row.valuationBasis,
      valuationGap: row.valuationGap,
      peg: row.peg,
      confidence: row.confidence,
    })),
  };

  if (format === "json") {
    return NextResponse.json(buildRankingCardJson(cardData), {
      status: 200,
      headers: {
        "Cache-Control": "public, max-age=300",
      },
    });
  }

  const artifactId = `card:ranking:${toStableToken(state.data.cutoffAt)}`;
  const fileStem = `truealpha-card-ranking-${state.data.cutoffAt.replace(/[:T]/g, "-")}`;

  if (format === "svg") {
    const svg = renderRankingCardSvg(cardData);
    return buildBinaryCardResponse({
      body: svg,
      bytes: Buffer.from(svg, "utf8"),
      artifactContentType: "image/svg+xml",
      headerContentType: "image/svg+xml; charset=utf-8",
      filename: `${fileStem}.svg`,
      persist,
      documents,
      context: principal.context,
      artifactId,
    });
  }

  const png = await renderRankingCardPng(cardData);
  return buildBinaryCardResponse({
    body: new Uint8Array(png),
    bytes: png,
    artifactContentType: "image/png",
    headerContentType: "image/png",
    filename: `${fileStem}.png`,
    persist,
    documents,
    context: principal.context,
    artifactId,
  });
}
