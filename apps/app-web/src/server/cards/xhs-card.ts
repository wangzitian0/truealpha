/**
 * Xiaohongshu card generation for research rankings and entity deep-dives (#372 / #44 / #1360).
 *
 * Single Source of Truth (`/ssot`):
 * - Serves 1080x1440 (3:4 portrait) self-contained Xiaohongshu HTML cards (`text/html; charset=utf-8`),
 *   reading pre-materialized `glm-5.3-flash` cards from `mart.research_card_artifacts` (joined to
 *   `mart.served_head`) when available and verified, or rendering the canonical Cold-Verdict Rubric HTML
 *   document verified by `verifyCardHtmlDocument`.
 * - Serves canonical JSON metadata payloads (`application/json; charset=utf-8`).
 * - Persists exported HTML cards to `/research/library` via `PostgresDocumentsRepository`.
 */

import { NextResponse } from "next/server";
import {
  formatCell,
  formatPercentFromFraction,
  formatRatio,
  formatSignedRatio,
  formatTierLabel,
  formatUsdMagnitude,
} from "@/client/format";
import {
  normalizePegConvention,
  PEG_CONVENTION_LABELS,
  projectPegByConvention,
  type AccessContext,
  type PegGrowthConvention,
  type ValuationBasis,
  valuationBasisLabel,
} from "@/contracts/strategyRun";
import { loadEntityDetail, loadRanking, type MartAdapterLike } from "@/server/dashboard";
import { PostgresDocumentsRepository, type DocumentsRepository } from "@/server/documents";
import { withMartReadonly } from "@/server/mart/db";
import { entityLabel, loadEntityDisplayMap } from "@/server/mart/entity-resolution";
import type { AnalystRatingsDetail, SupplyChainExposureDetail } from "@/server/mart/research-read";

export const XHS_CARD_WIDTH = 1080;
export const XHS_CARD_HEIGHT = 1440;
export const CARD_TEMPLATE_VERSION = "card_template.v1";
export const CARD_SCHEMA_VERSION = "research_card.v1";
export const XHS_CARD_PROMPT_VERSION = "xhs-card-html:v1";
export const CARD_EXPORT_CACHE_CONTROL = "public, max-age=300" as const;

export interface RankingCardItem {
  rank: number | null;
  issuerId: string;
  displayName: string;
  tier: string | null;
  currentPriceToSales: string | null;
  /** #1235: the multiple the gap divides (P/GP under definition 1.0.0) and its basis. */
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
  pegConvention?: PegGrowthConvention;
  /** The key of the universe the rows belong to. */
  universe: string;
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
  pegConvention?: PegGrowthConvention;
  gppe: string | null;
  operatingBranch: string | null;
  themes: Array<{ theme: string; themeShare: string | null }>;
  analystRatings?: AnalystRatingsDetail | null;
  supplyChainExposure?: SupplyChainExposureDetail | null;
  confidence: string | null;
}

export function escapeHtml(unsafe: string): string {
  return unsafe
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function gapHexColor(valuationGap: string | null): string {
  const gapVal = Number(valuationGap);
  if (Number.isNaN(gapVal) || gapVal === 0) return "#475569";
  return gapVal > 0 ? "#047857" : "#BE123C";
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

export function buildRankingCardJson(data: RankingCardData) {
  return {
    schema_version: CARD_SCHEMA_VERSION,
    card_kind: "ranking",
    template_version: CARD_TEMPLATE_VERSION,
    prompt_version: XHS_CARD_PROMPT_VERSION,
    title: "Valuation Rankings",
    cutoff_at: data.cutoffAt,
    universe: data.universe,
    peg_convention: data.pegConvention ?? "historical_cagr",
    generated_at: new Date().toISOString(),
    research_risk_note:
      "Ranking reflects one versioned strategy run at one cutoff; historical rank is not a forward-looking guarantee.",
    source_attribution:
      "TrueAlpha research — traceable to a materialized factor output (mart.served_head).",
    subjects: data.items.map((item) => ({
      subject_id: item.issuerId,
      display_name: item.displayName,
      rank: item.rank,
      tier: formatCell(item.tier),
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

export function buildEntityCardJson(data: EntityCardData) {
  return {
    schema_version: CARD_SCHEMA_VERSION,
    card_kind: "entity",
    template_version: CARD_TEMPLATE_VERSION,
    prompt_version: XHS_CARD_PROMPT_VERSION,
    title: "Entity Deep Dive",
    issuer_id: data.issuerId,
    display_name: data.displayName,
    cutoff_at: data.cutoffAt,
    generated_at: new Date().toISOString(),
    research_risk_note:
      "Deep dive reflects one versioned strategy run at one cutoff; historical indicators are not forward-looking guarantees.",
    source_attribution:
      "TrueAlpha research — traceable to materialized factor outputs (mart.strategy_decisions, mart.topt_gppe_results, mart.issuer_theme_purity, mart.issuer_analyst_ratings, mart.issuer_supply_chain_exposure).",
    metrics: {
      tier: formatCell(data.tier),
      current_price_to_sales: data.currentPriceToSales,
      current_price_to_gross_profit: data.valuationBasis === "price_to_gross_profit" ? data.currentMultiple : null,
      valuation_basis: data.valuationBasis,
      valuation_gap: data.valuationGap,
      peg: data.peg,
      peg_convention: data.pegConvention ?? "historical_cagr",
      gppe: data.gppe,
      operating_branch: data.operatingBranch,
      confidence: data.confidence,
    },
    themes: data.themes.map((t) => ({
      theme: t.theme,
      theme_share: t.themeShare,
    })),
    analyst_ratings: data.analystRatings ?? null,
    supply_chain_exposure: data.supplyChainExposure ?? null,
  };
}

const INLINE_EVENT_HANDLER_RE = /\bon[a-z]+\s*=/i;
const HTML_TAG_RE = /<[^>]+>/g;
const WHITESPACE_RE = /\s+/g;

function normalizeHtmlText(html: string): string {
  return html
    .replace(HTML_TAG_RE, " ")
    .replace(/&amp;/gi, "&")
    .replace(/&lt;/gi, "<")
    .replace(/&gt;/gi, ">")
    .replace(/&quot;/gi, '"')
    .replace(/&#39;|&apos;/gi, "'")
    .replace(/&mdash;|&#8212;|&#x2014;/gi, "—")
    .replace(/&ndash;|&#8211;|&#x2013;/gi, "–")
    .replace(/&minus;|&#8722;|&#x2212;/gi, "−")
    .replace(/&middot;|&#183;/gi, "·")
    .replace(/&nbsp;|&#160;/gi, " ")
    .replace(WHITESPACE_RE, " ")
    .trim();
}

/**
 * Deterministic fact-preservation verifier for 1080x1440 Xiaohongshu HTML cards (#44).
 * Ensures the rendered HTML is self-contained, carries exact 1080x1440 dimensions,
 * has zero `<script>` or external URLs, and preserves every required fact verbatim.
 */
export function verifyCardHtmlDocument(
  html: string,
  requiredFacts: ReadonlyArray<string>,
): { ok: boolean; violations: string[] } {
  const violations: string[] = [];
  const lower = html.toLowerCase();
  if ((!lower.includes("<!doctype html") && !lower.includes("<html")) || !lower.includes("</html>")) {
    violations.push("missing_html_envelope");
  }
  if (!html.includes(String(XHS_CARD_WIDTH)) || !html.includes(String(XHS_CARD_HEIGHT))) {
    violations.push("missing_1080x1440_dimensions");
  }
  if (
    lower.includes("<script") ||
    lower.includes("javascript:") ||
    INLINE_EVENT_HANDLER_RE.test(html)
  ) {
    violations.push("forbidden_script_or_event_handler");
  }
  if (lower.includes("http://") || lower.includes("https://")) {
    violations.push("forbidden_external_url");
  }
  const normalized = normalizeHtmlText(html);
  for (const fact of requiredFacts) {
    if (!fact) continue;
    const escaped = escapeHtml(fact);
    const unicodeMinus = fact.startsWith("-") ? `−${fact.slice(1)}` : fact;
    const normFact = fact.replace(WHITESPACE_RE, " ").trim();
    if (
      !html.includes(fact) &&
      !html.includes(escaped) &&
      !html.includes(unicodeMinus) &&
      !normalized.includes(normFact) &&
      !normalized.includes(unicodeMinus)
    ) {
      violations.push(`missing_fact:${fact}`);
    }
  }
  return { ok: violations.length === 0, violations };
}

export function renderRankingCardHtml(data: RankingCardData): string {
  const displayItems = data.items.slice(0, 7);
  const basisLabel = valuationBasisLabel(
    data.items.find((item) => item.valuationBasis)?.valuationBasis ?? null,
  );
  const conventionLabel = PEG_CONVENTION_LABELS[data.pegConvention ?? "historical_cagr"];

  const rowsHtml = displayItems
    .map((item, idx) => {
      const rankStr = item.rank !== null ? `#${item.rank}` : formatCell(null);
      const tierStr = formatTierLabel(item.tier);
      const gapColor = gapHexColor(item.valuationGap);
      const formattedGap = formatCell(formatSignedRatio(item.valuationGap));
      const formattedMultiple = formatCell(formatRatio(item.currentMultiple));
      const formattedPeg = formatCell(formatRatio(item.peg));
      const formattedConf = formatCell(formatRatio(item.confidence));
      const badgeBg = idx === 0 ? "#F59E0B" : idx === 1 ? "#334155" : idx === 2 ? "#B45309" : "#111315";

      return `<tr class="ranking-row">
        <td class="cell-rank"><span class="rank-pill" style="background:${badgeBg}">${escapeHtml(rankStr)}</span></td>
        <td class="cell-issuer">
          <div class="issuer-name">${escapeHtml(item.displayName)}</div>
          <div class="issuer-id">${escapeHtml(item.issuerId)}</div>
        </td>
        <td class="cell-tier"><span class="tier-badge">${escapeHtml(tierStr)}</span></td>
        <td class="cell-num">${escapeHtml(formattedMultiple)}</td>
        <td class="cell-num cell-gap" style="color:${gapColor}">${escapeHtml(formattedGap)}</td>
        <td class="cell-num">${escapeHtml(formattedPeg)}</td>
        <td class="cell-num cell-conf">${escapeHtml(formattedConf)}</td>
      </tr>`;
    })
    .join("\n");

  return `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<title>Valuation Rankings · ${escapeHtml(data.universe.toUpperCase())}</title>
<style>
  html, body {
    width: ${XHS_CARD_WIDTH}px;
    height: ${XHS_CARD_HEIGHT}px;
    margin: 0;
    padding: 0;
    background: #F7F5F0;
    color: #111315;
    font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
    box-sizing: border-box;
    overflow: hidden;
  }
  .card-canvas {
    width: ${XHS_CARD_WIDTH}px;
    height: ${XHS_CARD_HEIGHT}px;
    padding: 44px 48px;
    box-sizing: border-box;
    display: flex;
    flex-direction: column;
    justify-content: space-between;
    border: 3px solid #111315;
    background: #F7F5F0;
  }
  .header-top {
    display: flex;
    justify-content: space-between;
    align-items: center;
    border-bottom: 2px solid #111315;
    padding-bottom: 14px;
    margin-bottom: 18px;
  }
  .brand-kicker {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 14px;
    font-weight: 800;
    letter-spacing: 0.12em;
    color: #111315;
  }
  .dim-badge {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 12px;
    font-weight: 700;
    background: #111315;
    color: #F7F5F0;
    padding: 4px 10px;
    border-radius: 6px;
  }
  .title-block h1 {
    font-size: 42px;
    font-weight: 900;
    margin: 0 0 6px 0;
    letter-spacing: -0.02em;
  }
  .title-block .subtitle {
    font-size: 20px;
    font-weight: 600;
    color: #475569;
    margin: 0 0 18px 0;
  }
  .meta-bar {
    display: flex;
    justify-content: space-between;
    align-items: center;
    background: #FFFFFF;
    border: 2px solid #111315;
    border-radius: 12px;
    padding: 12px 18px;
    margin-bottom: 22px;
    box-shadow: 4px 4px 0 #111315;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 13px;
  }
  .ranking-table {
    width: 100%;
    border-collapse: separate;
    border-spacing: 0 10px;
  }
  .ranking-table th {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 12px;
    font-weight: 800;
    color: #475569;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    padding: 0 12px 6px 12px;
    text-align: left;
  }
  .ranking-table th.num-head {
    text-align: right;
  }
  .ranking-row td {
    background: #FFFFFF;
    border-top: 1.5px solid #111315;
    border-bottom: 1.5px solid #111315;
    padding: 14px 12px;
    vertical-align: middle;
  }
  .ranking-row td:first-child {
    border-left: 1.5px solid #111315;
    border-top-left-radius: 12px;
    border-bottom-left-radius: 12px;
    width: 64px;
    text-align: center;
  }
  .ranking-row td:last-child {
    border-right: 1.5px solid #111315;
    border-top-right-radius: 12px;
    border-bottom-right-radius: 12px;
  }
  .rank-pill {
    display: inline-block;
    color: #FFFFFF;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 15px;
    font-weight: 800;
    padding: 4px 10px;
    border-radius: 999px;
  }
  .issuer-name {
    font-size: 19px;
    font-weight: 800;
    color: #111315;
  }
  .issuer-id {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 12px;
    color: #64748B;
    margin-top: 2px;
  }
  .tier-badge {
    display: inline-block;
    background: #F1F5F9;
    border: 1px solid #111315;
    border-radius: 6px;
    padding: 3px 8px;
    font-size: 12px;
    font-weight: 700;
    color: #0F172A;
  }
  .cell-num {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-variant-numeric: tabular-nums;
    font-size: 18px;
    font-weight: 700;
    text-align: right;
  }
  .cell-gap {
    font-size: 20px;
    font-weight: 900;
  }
  .cell-conf {
    color: #475569;
  }
  .footer-box {
    background: #FFFFFF;
    border: 2px solid #111315;
    border-radius: 12px;
    padding: 16px 20px;
    box-shadow: 4px 4px 0 #111315;
  }
  .footer-tag {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 12px;
    font-weight: 800;
    color: #B45309;
    letter-spacing: 0.06em;
    margin-bottom: 6px;
  }
  .footer-note {
    font-size: 13px;
    color: #334155;
    margin: 0 0 4px 0;
    line-height: 1.4;
  }
  .footer-meta {
    display: flex;
    justify-content: space-between;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 11px;
    color: #64748B;
    border-top: 1px solid #E2E8F0;
    padding-top: 8px;
    margin-top: 8px;
  }
</style>
</head>
<body>
  <div class="card-canvas" data-card-kind="ranking" data-card-dimensions="1080x1440">
    <div>
      <div class="header-top">
        <span class="brand-kicker">TRUEALPHA · COLD-VERDICT RESEARCH</span>
        <span class="dim-badge">${XHS_CARD_WIDTH} × ${XHS_CARD_HEIGHT} · 3:4 HTML</span>
      </div>
      <div class="title-block">
        <h1>Valuation Rankings</h1>
        <p class="subtitle">大模型价值主题榜单 · ${escapeHtml(data.universe.toUpperCase())} · PEG (${escapeHtml(conventionLabel)})</p>
      </div>
      <div class="meta-bar">
        <span>Governed Cutoff: <strong>${escapeHtml(data.cutoffAt)}</strong></span>
        <span>Universe: <strong>${escapeHtml(data.universe)}</strong> (${data.totalMembers} total members)</span>
      </div>
      <table class="ranking-table">
        <thead>
          <tr>
            <th>RANK</th>
            <th>ISSUER</th>
            <th>TIER</th>
            <th class="num-head">${escapeHtml(basisLabel)}</th>
            <th class="num-head">VALUATION GAP (${escapeHtml(basisLabel)})</th>
            <th class="num-head">PEG</th>
            <th class="num-head">CONF</th>
          </tr>
        </thead>
        <tbody>
          ${rowsHtml}
        </tbody>
      </table>
    </div>
    <div class="footer-box">
      <div class="footer-tag">RESEARCH HYPOTHESIS · NOT INVESTMENT ADVICE</div>
      <p class="footer-note">Ranking reflects one versioned strategy run at one cutoff; historical rank is not a forward-looking guarantee.</p>
      <p class="footer-note">Traceable to materialized factor output (mart.served_head, schema: ${CARD_SCHEMA_VERSION}).</p>
      <div class="footer-meta">
        <span>TrueAlpha Research HTML Card Export</span>
        <span>template: ${CARD_TEMPLATE_VERSION} · prompt: ${XHS_CARD_PROMPT_VERSION}</span>
      </div>
    </div>
  </div>
</body>
</html>`;
}

export function renderEntityDeepDiveCardHtml(data: EntityCardData): string {
  const gapColor = gapHexColor(data.valuationGap);
  const formattedGap = formatCell(formatSignedRatio(data.valuationGap));
  const formattedMultiple = formatCell(formatRatio(data.currentMultiple));
  const basisLabel = valuationBasisLabel(data.valuationBasis);
  const formattedPeg = formatCell(formatRatio(data.peg));
  const pegConventionLabel = PEG_CONVENTION_LABELS[data.pegConvention ?? "historical_cagr"];
  const formattedGppe = formatCell(formatUsdMagnitude(data.gppe) ?? (data.gppe ? `$${data.gppe}` : null));
  const formattedConf = formatCell(formatRatio(data.confidence));
  const tierText = formatTierLabel(data.tier);
  const displayThemes = data.themes.slice(0, 4);

  const themesHtml =
    displayThemes.length > 0
      ? displayThemes
          .map((t) => {
            const pctStr = formatCell(formatPercentFromFraction(t.themeShare));
            const shareNum = Number(t.themeShare ?? 0);
            const pctWidth = Number.isNaN(shareNum)
              ? 0
              : Math.min(100, Math.max(0, Math.round(shareNum * 100)));
            return `<div class="theme-row">
              <div class="theme-labels">
                <span class="theme-name">${escapeHtml(t.theme)}</span>
                <span class="theme-pct">${escapeHtml(pctStr)}</span>
              </div>
              <div class="theme-bar-bg"><div class="theme-bar-fill" style="width:${pctWidth}%"></div></div>
            </div>`;
          })
          .join("\n")
      : `<div class="empty-note">No theme purity materialization recorded for this issuer</div>`;

  const m4 = data.analystRatings;
  const analystHtml = m4
    ? `<div class="sub-panel">
        <div class="panel-kicker">MODULE 4 · ANALYST CONSENSUS</div>
        <div class="panel-metrics">
          <span>Rating: <strong>${escapeHtml(formatCell(m4.consensusRating))}</strong></span>
          <span>Target: <strong>${escapeHtml(formatCell(m4.targetPrice ? `$${m4.targetPrice}` : null))}</strong></span>
          <span>Analysts: <strong>${formatCell(m4.analystsCount !== null && m4.analystsCount !== undefined ? String(m4.analystsCount) : null)}</strong> (B:${m4.buyCount ?? 0}/H:${m4.holdCount ?? 0}/S:${m4.sellCount ?? 0})</span>
        </div>
      </div>`
    : "";

  const m3 = data.supplyChainExposure;
  const supplyChainHtml = m3
    ? `<div class="sub-panel">
        <div class="panel-kicker">MODULE 3 · SUPPLY-CHAIN SCENARIO (scenario_only)</div>
        <div class="panel-metrics">
          <span>Exposure: <strong>${escapeHtml(formatCell(m3.exposureScore))}</strong></span>
          <span>Partners: <strong>${formatCell(m3.directPartners !== null && m3.directPartners !== undefined ? String(m3.directPartners) : null)}</strong> (Sup:${m3.suppliersCount ?? 0}/Cust:${m3.customersCount ?? 0})</span>
          <span>Max Share: <strong>${escapeHtml(formatCell(formatPercentFromFraction(m3.maxPartnerShare)))}</strong></span>
        </div>
      </div>`
    : "";

  return `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<title>TrueAlpha Deep Dive · ${escapeHtml(data.displayName)}</title>
<style>
  html, body {
    width: ${XHS_CARD_WIDTH}px;
    height: ${XHS_CARD_HEIGHT}px;
    margin: 0;
    padding: 0;
    background: #F7F5F0;
    color: #111315;
    font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
    box-sizing: border-box;
    overflow: hidden;
  }
  .card-canvas {
    width: ${XHS_CARD_WIDTH}px;
    height: ${XHS_CARD_HEIGHT}px;
    padding: 44px 48px;
    box-sizing: border-box;
    display: flex;
    flex-direction: column;
    justify-content: space-between;
    border: 3px solid #111315;
    background: #F7F5F0;
  }
  .header-top {
    display: flex;
    justify-content: space-between;
    align-items: center;
    border-bottom: 2px solid #111315;
    padding-bottom: 14px;
    margin-bottom: 18px;
  }
  .brand-kicker {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 14px;
    font-weight: 800;
    letter-spacing: 0.1em;
  }
  .dim-badge {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 12px;
    font-weight: 700;
    background: #111315;
    color: #F7F5F0;
    padding: 4px 10px;
    border-radius: 6px;
  }
  .hero-header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    margin-bottom: 18px;
  }
  .hero-header h1 {
    font-size: 38px;
    font-weight: 900;
    margin: 0 0 6px 0;
  }
  .hero-sub {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 14px;
    color: #475569;
  }
  .tier-pill {
    background: #111315;
    color: #F7F5F0;
    padding: 8px 16px;
    border-radius: 10px;
    font-size: 16px;
    font-weight: 800;
  }
  .meta-bar {
    display: flex;
    justify-content: space-between;
    background: #FFFFFF;
    border: 2px solid #111315;
    border-radius: 12px;
    padding: 12px 18px;
    margin-bottom: 20px;
    box-shadow: 4px 4px 0 #111315;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 13px;
  }
  .bento-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 16px;
    margin-bottom: 20px;
  }
  .bento-tile {
    background: #FFFFFF;
    border: 2px solid #111315;
    border-radius: 14px;
    padding: 18px 20px;
    box-shadow: 4px 4px 0 #111315;
  }
  .tile-label {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 13px;
    font-weight: 800;
    color: #475569;
    text-transform: uppercase;
  }
  .tile-value {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-variant-numeric: tabular-nums;
    font-size: 34px;
    font-weight: 900;
    margin: 8px 0 4px 0;
  }
  .tile-sub {
    font-size: 13px;
    color: #64748B;
  }
  .themes-box {
    background: #FFFFFF;
    border: 2px solid #111315;
    border-radius: 14px;
    padding: 18px 20px;
    margin-bottom: 16px;
    box-shadow: 4px 4px 0 #111315;
  }
  .themes-title {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 13px;
    font-weight: 800;
    color: #111315;
    margin-bottom: 12px;
    text-transform: uppercase;
  }
  .theme-row {
    margin-bottom: 10px;
  }
  .theme-labels {
    display: flex;
    justify-content: space-between;
    font-size: 15px;
    font-weight: 700;
    margin-bottom: 4px;
  }
  .theme-pct {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  }
  .theme-bar-bg {
    width: 100%;
    height: 10px;
    background: #E2E8F0;
    border-radius: 999px;
    overflow: hidden;
  }
  .theme-bar-fill {
    height: 10px;
    background: #111315;
    border-radius: 999px;
  }
  .sub-panel {
    background: #FFFFFF;
    border: 1.5px solid #111315;
    border-radius: 10px;
    padding: 12px 16px;
    margin-bottom: 12px;
  }
  .panel-kicker {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 11px;
    font-weight: 800;
    color: #475569;
    margin-bottom: 6px;
  }
  .panel-metrics {
    display: flex;
    justify-content: space-between;
    font-size: 14px;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  }
  .footer-box {
    background: #FFFFFF;
    border: 2px solid #111315;
    border-radius: 12px;
    padding: 14px 18px;
    box-shadow: 4px 4px 0 #111315;
  }
  .footer-tag {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 12px;
    font-weight: 800;
    color: #B45309;
    margin-bottom: 4px;
  }
  .footer-note {
    font-size: 12px;
    color: #334155;
    margin: 0 0 3px 0;
  }
  .footer-meta {
    display: flex;
    justify-content: space-between;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 11px;
    color: #64748B;
    border-top: 1px solid #E2E8F0;
    padding-top: 6px;
    margin-top: 6px;
  }
</style>
</head>
<body>
  <div class="card-canvas" data-card-kind="entity" data-card-dimensions="1080x1440">
    <div>
      <div class="header-top">
        <span class="brand-kicker">TRUEALPHA DEEP DIVE · ENTITY 360 CARD</span>
        <span class="dim-badge">${XHS_CARD_WIDTH} × ${XHS_CARD_HEIGHT} · 3:4 HTML</span>
      </div>
      <div class="hero-header">
        <div>
          <h1>TrueAlpha Deep Dive: ${escapeHtml(data.displayName)}</h1>
          <div class="hero-sub">${escapeHtml(data.issuerId)} · Branch: ${escapeHtml(formatCell(data.operatingBranch))}</div>
        </div>
        <div class="tier-pill">${escapeHtml(tierText)}</div>
      </div>
      <div class="meta-bar">
        <span>Cutoff: <strong>${escapeHtml(data.cutoffAt)}</strong></span>
        <span>Confidence: <strong>${escapeHtml(formattedConf)}</strong></span>
      </div>
      <div class="bento-grid">
        <div class="bento-tile">
          <div class="tile-label">Valuation Gap (${escapeHtml(basisLabel)})</div>
          <div class="tile-value" style="color:${gapColor}">${escapeHtml(formattedGap)}</div>
          <div class="tile-sub">Tier-implied relative gap</div>
        </div>
        <div class="bento-tile">
          <div class="tile-label">Current ${escapeHtml(basisLabel)}</div>
          <div class="tile-value">${escapeHtml(formattedMultiple)}</div>
          <div class="tile-sub">Basis: ${escapeHtml(formatCell(data.valuationBasis))}</div>
        </div>
        <div class="bento-tile">
          <div class="tile-label">PEG Ratio</div>
          <div class="tile-value">${escapeHtml(formattedPeg)}</div>
          <div class="tile-sub">${escapeHtml(pegConventionLabel)} (${escapeHtml(data.pegConvention ?? "historical_cagr")})</div>
        </div>
        <div class="bento-tile">
          <div class="tile-label">GP / Employee (GPPE)</div>
          <div class="tile-value">${escapeHtml(formattedGppe)}</div>
          <div class="tile-sub">Operating branch: ${escapeHtml(formatCell(data.operatingBranch))}</div>
        </div>
      </div>
      <div class="themes-box">
        <div class="themes-title">Module 6 · Theme Purity Exposure</div>
        ${themesHtml}
      </div>
      ${analystHtml}
      ${supplyChainHtml}
    </div>
    <div class="footer-box">
      <div class="footer-tag">RESEARCH HYPOTHESIS · NOT INVESTMENT ADVICE</div>
      <p class="footer-note">Deep dive reflects one versioned strategy run at one cutoff; historical indicators are not forward-looking guarantees.</p>
      <p class="footer-note">Traceable to materialized factor outputs (mart.served_head, schema: ${CARD_SCHEMA_VERSION}).</p>
      <div class="footer-meta">
        <span>TrueAlpha Research HTML Card Export</span>
        <span>template: ${CARD_TEMPLATE_VERSION} · prompt: ${XHS_CARD_PROMPT_VERSION}</span>
      </div>
    </div>
  </div>
</body>
</html>`;
}

const SERVED_CARD_ARTIFACT_SQL = `
  select a.html, a.render_mode, a.model
  from mart.research_card_artifacts a
  join mart.served_head sh on sh.head_run_id = a.run_id
  where a.card_kind = $1
    and a.subject_key = $2
    and a.convention = $3
    and a.universe = $4
  order by a.recorded_at desc
  limit 1
`;

async function queryPreMaterializedCardHtml(params: {
  cardKind: string;
  subjectKey: string;
  convention: string;
  universe: string;
  requiredFacts: ReadonlyArray<string>;
}): Promise<{ html: string; renderMode: string } | null> {
  if (!process.env.DATABASE_URL) return null;
  try {
    const row = await withMartReadonly(async (client) => {
      const res = await client.query<{ html: string; render_mode: string; model: string | null }>(
        SERVED_CARD_ARTIFACT_SQL,
        [params.cardKind, params.subjectKey, params.convention, params.universe],
      );
      return res.rows[0] ?? null;
    });
    if (!row?.html) return null;
    const verification = verifyCardHtmlDocument(row.html, params.requiredFacts);
    if (!verification.ok) return null;
    return { html: row.html, renderMode: row.render_mode || "model_glm" };
  } catch {
    return null;
  }
}

function toStableToken(raw: string): string {
  const cleaned = raw
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .split("-")
    .filter((token) => token.length > 0 && !["latest", "current", "default", "head"].includes(token))
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
  } catch (error) {
    console.error("persistCardDocument error:", error);
    return null;
  }
}

async function buildHtmlCardResponse(options: {
  html: string;
  renderMode: string;
  filename: string;
  persist: boolean;
  documents: DocumentsRepository;
  context: AccessContext;
  artifactId: string;
}): Promise<Response> {
  const bytes = Buffer.from(options.html, "utf8");
  const docId = options.persist
    ? await persistCardDocument(
        options.documents,
        options.context,
        options.artifactId,
        bytes,
        "text/html",
      )
    : null;
  const headers: Record<string, string> = {
    "Content-Type": "text/html; charset=utf-8",
    "Content-Disposition": `attachment; filename="${options.filename}"`,
    "Cache-Control": CARD_EXPORT_CACHE_CONTROL,
    "X-Research-Card-Render-Mode": options.renderMode,
  };
  if (docId) headers["X-Research-Document-Id"] = docId;
  return new Response(options.html, { status: 200, headers });
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
  const rawFormat = searchParams.get("format") ?? "html";
  const format = rawFormat === "json" ? "json" : "html";
  const convention = normalizePegConvention(searchParams.get("convention"));
  const universeParam = searchParams.get("universe") ?? undefined;
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
    const projectedPeg = projectPegByConvention(latestRow ?? {}, convention);

    const cardData: EntityCardData = {
      issuerId: state.data.issuerId,
      displayName: entityLabel(state.data.issuerId, names),
      cutoffAt: latestRow?.cutoffAt ?? new Date().toISOString(),
      tier: latestRow?.tier ?? null,
      currentPriceToSales: latestRow?.currentPriceToSales ?? null,
      currentMultiple: latestRow?.currentMultiple ?? null,
      valuationBasis: latestRow?.valuationBasis ?? null,
      valuationGap: latestRow?.valuationGap ?? null,
      peg: projectedPeg.peg,
      pegConvention: convention,
      gppe: state.data.gppeDetail?.gppe ?? latestRow?.capitalAdjustedLaborEfficiency ?? null,
      operatingBranch: state.data.gppeDetail?.operatingBranch ?? null,
      themes: entityCardThemes(state.data.themes ?? []),
      analystRatings: state.data.analystRatings ?? null,
      supplyChainExposure: state.data.supplyChainExposure ?? null,
      confidence: latestRow?.confidence ?? null,
    };

    if (format === "json") {
      return NextResponse.json(buildEntityCardJson(cardData), {
        status: 200,
        headers: {
          "Cache-Control": CARD_EXPORT_CACHE_CONTROL,
        },
      });
    }

    const artifactId = `card:entity:${toStableToken(state.data.issuerId)}:${toStableToken(cardData.cutoffAt)}:${toStableToken(convention)}:html`;
    const fileStem = `truealpha-card-entity-${state.data.issuerId.replace(/[:]/g, "-")}`;
    const preMaterialized = await queryPreMaterializedCardHtml({
      cardKind: "company",
      subjectKey: state.data.issuerId,
      convention,
      universe: universeParam ?? "topt",
      requiredFacts: [state.data.issuerId, cardData.cutoffAt.slice(0, 10)],
    });
    const html = preMaterialized?.html ?? renderEntityDeepDiveCardHtml(cardData);
    const renderMode = preMaterialized?.renderMode ?? "cold_verdict_html";

    return buildHtmlCardResponse({
      html,
      renderMode,
      filename: `${fileStem}.html`,
      persist,
      documents,
      context: principal.context,
      artifactId,
    });
  }

  const state = await loadRanking(
    principal.context,
    { cutoffAt: cutoff, cursor: null, universe: universeParam },
    deps.adapter,
  );
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
    pegConvention: convention,
    universe: state.data.universe,
    items: state.data.rows.map((row) => ({
      rank: row.rank,
      issuerId: row.issuerId,
      displayName: entityLabel(row.issuerId, names),
      tier: row.tier,
      currentPriceToSales: row.currentPriceToSales,
      currentMultiple: row.currentMultiple,
      valuationBasis: row.valuationBasis,
      valuationGap: row.valuationGap,
      peg: projectPegByConvention(row, convention).peg,
      confidence: row.confidence,
    })),
  };

  if (format === "json") {
    return NextResponse.json(buildRankingCardJson(cardData), {
      status: 200,
      headers: {
        "Cache-Control": CARD_EXPORT_CACHE_CONTROL,
      },
    });
  }

  const artifactId = `card:ranking:${toStableToken(state.data.cutoffAt)}:${toStableToken(state.data.universe)}:${toStableToken(convention)}:html`;
  const fileStem = `truealpha-card-ranking-${state.data.cutoffAt.replace(/[:T]/g, "-")}`;
  const preMaterialized = await queryPreMaterializedCardHtml({
    cardKind: "ranking",
    subjectKey: "theme:large_model_value",
    convention,
    universe: state.data.universe,
    requiredFacts: [state.data.cutoffAt.slice(0, 10)],
  });
  const html = preMaterialized?.html ?? renderRankingCardHtml(cardData);
  const renderMode = preMaterialized?.renderMode ?? "cold_verdict_html";

  return buildHtmlCardResponse({
    html,
    renderMode,
    filename: `${fileStem}.html`,
    persist,
    documents,
    context: principal.context,
    artifactId,
  });
}
