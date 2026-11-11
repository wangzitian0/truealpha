/**
 * Xiaohongshu 1080x1440 HTML/PNG/JSON card rendering and export route handler (#372 / #44 / #1360).
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
export const MAX_SUBJECTS_PER_CARD = 4;

export interface CardPresetConfig {
  presetId: string;
  style: string;
  layout: string;
  palette: string;
  bgCss: string;
  surfaceBg: string;
  fg: string;
  mute: string;
  accent: string;
  border: string;
}

const PRESET_SPECS: ReadonlyArray<readonly [string, string, string, string, string, string, string, string, string]> = [
  ["bold", "sparse", "neon", "radial-gradient(circle at 85% 12%, #311042 0%, #140C1C 55%, #0B0711 100%)", "#191126", "#F5F5F7", "#B8B8D4", "#39FF14", "#00F5FF"],
  ["screen-print", "list", "earth", "#F5E6D0", "#FFFDF9", "#121212", "#4A3F35", "#E8751A", "#121212"],
  ["notion", "list", "macaron", "#FFFFFF", "#FBFBFA", "#37352F", "#6B6860", "#2F7D32", "#E3E2DE"],
  ["minimal", "comparison", "cool", "#FAFAFA", "#FFFFFF", "#111111", "#555555", "#0A6847", "#111111"],
  ["study-notes", "dense", "warm", "#FAF6EE", "#FFFFFF", "#222222", "#5C5852", "#1E3A5F", "#1E3A5F"],
  ["retro", "balanced", "warm", "#F2E6D0", "#FAF3E6", "#2A1F18", "#634F40", "#2E6B4E", "#2A1F18"],
  ["chalkboard", "quadrant", "monochrome", "#1E3226", "#263D2F", "#F2EFE9", "#B8C7BC", "#FFE066", "#5E7A68"],
  ["pop", "quadrant", "vivid", "#FFE600", "#FFFFFF", "#000000", "#333333", "#FF0055", "#000000"],
  ["sketch-notes", "flow", "warm", "#FCF9F2", "#FFFFFF", "#2B2927", "#5C5852", "#E85D04", "#2B2927"],
  ["fresh", "mindmap", "pastel", "linear-gradient(160deg, #E8F8F5 0%, #F4FBF7 55%, #FFFFFF 100%)", "#FFFFFF", "#1B3B2B", "#4A6B5D", "#148F77", "#48C9B0"],
];

export const CARD_PRESETS: Record<string, CardPresetConfig> = Object.fromEntries(
  PRESET_SPECS.map(([presetId, layout, palette, bgCss, surfaceBg, fg, mute, accent, border]) => [
    presetId,
    { presetId, style: presetId, layout, palette, bgCss, surfaceBg, fg, mute, accent, border },
  ]),
);

export interface RankingCardItem {
  rank: number | null;
  issuerId: string;
  displayName: string;
  tier: string | null;
  currentPriceToSales: string | null;
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
  universe: string;
  stylePreset?: string;
  seriesIndex?: number;
  seriesTotal?: number;
}

export interface EntityCardData {
  issuerId: string;
  displayName: string;
  cutoffAt: string;
  tier: string | null;
  currentPriceToSales: string | null;
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

export function paginateRankingCardSeries(
  data: RankingCardData,
  maxItemsPerCard: number = MAX_SUBJECTS_PER_CARD,
): RankingCardData[] {
  const step = Math.max(1, maxItemsPerCard);
  if (data.items.length === 0) return [data];
  const totalPages = Math.ceil(data.items.length / step);
  return Array.from({ length: totalPages }, (_, idx) => ({
    ...data,
    items: data.items.slice(idx * step, (idx + 1) * step),
    seriesIndex: idx + 1,
    seriesTotal: totalPages,
  }));
}

const FALLBACK_PNG_BYTES = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4//8/AAX+Av4N70a4AAAAAElFTkSuQmCC",
  "base64",
);

export async function renderCardPngBuffer(html: string): Promise<Buffer> {
  try {
    const pw = await import(/* webpackIgnore: true */ "playwright").catch(() => null);
    if (pw?.chromium) {
      const browser = await pw.chromium.launch({ headless: true });
      try {
        const page = await browser.newPage({
          viewport: { width: XHS_CARD_WIDTH, height: XHS_CARD_HEIGHT },
          deviceScaleFactor: 1,
        });
        await page.setContent(html, { waitUntil: "load" });
        const screenshot = await page.screenshot({
          type: "png",
          clip: { x: 0, y: 0, width: XHS_CARD_WIDTH, height: XHS_CARD_HEIGHT },
        });
        return Buffer.from(screenshot);
      } finally {
        await browser.close();
      }
    }
  } catch {
    // Fall back to deterministic PNG header when browser binaries are absent.
  }
  return FALLBACK_PNG_BYTES;
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

export function entityCardThemes(
  themes: ReadonlyArray<{ theme: string; themeShare: string | null; unvisitedReason?: string | null }>,
): EntityCardData["themes"] {
  return themes.filter((t) => !t.unvisitedReason).map((t) => ({ theme: t.theme, themeShare: t.themeShare }));
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
    source_attribution: "TrueAlpha research — traceable to a materialized factor output (mart.served_head).",
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
    themes: data.themes.map((t) => ({ theme: t.theme, theme_share: t.themeShare })),
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
  if (lower.includes("<script") || lower.includes("javascript:") || INLINE_EVENT_HANDLER_RE.test(html)) {
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

const SHARED_CARD_STYLESHEET = `
  html,body{width:${XHS_CARD_WIDTH}px;height:${XHS_CARD_HEIGHT}px;margin:0;padding:0;background:#F7F5F0;color:#111315;font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;box-sizing:border-box;overflow:hidden}
  .card-canvas{width:${XHS_CARD_WIDTH}px;height:${XHS_CARD_HEIGHT}px;padding:44px 48px;box-sizing:border-box;display:flex;flex-direction:column;justify-content:space-between;border:3px solid #111315;background:#F7F5F0}
  .header-top{display:flex;justify-content:space-between;align-items:center;border-bottom:2px solid #111315;padding-bottom:14px;margin-bottom:18px}
  .brand-kicker{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:14px;font-weight:800;letter-spacing:0.1em}
  .dim-badge{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;font-weight:700;background:#111315;color:#F7F5F0;padding:4px 10px;border-radius:6px}
  .title-block h1,.hero-header h1{font-size:40px;font-weight:900;margin:0 0 6px}
  .title-block .subtitle{font-size:20px;font-weight:600;color:#475569;margin:0 0 18px}
  .hero-header{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:18px}
  .hero-sub{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:14px;color:#475569}
  .tier-pill{background:#111315;color:#F7F5F0;padding:8px 16px;border-radius:10px;font-size:16px;font-weight:800}
  .meta-bar,.bento-tile,.themes-box,.footer-box{background:#FFF;border:2px solid #111315;border-radius:12px;box-shadow:4px 4px 0 #111315}
  .meta-bar{display:flex;justify-content:space-between;align-items:center;padding:12px 18px;margin-bottom:20px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px}
  .ranking-table{width:100%;border-collapse:separate;border-spacing:0 10px}
  .ranking-table th{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;font-weight:800;color:#475569;text-transform:uppercase;padding:0 12px 6px;text-align:left}
  .ranking-table th.num-head{text-align:right}
  .ranking-row td{background:#FFF;border-top:1.5px solid #111315;border-bottom:1.5px solid #111315;padding:14px 12px;vertical-align:middle}
  .ranking-row td:first-child{border-left:1.5px solid #111315;border-radius:12px 0 0 12px;width:64px;text-align:center}
  .ranking-row td:last-child{border-right:1.5px solid #111315;border-radius:0 12px 12px 0}
  .rank-pill{display:inline-block;color:#FFF;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:15px;font-weight:800;padding:4px 10px;border-radius:999px}
  .issuer-name{font-size:19px;font-weight:800}
  .issuer-id{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;color:#64748B;margin-top:2px}
  .tier-badge{display:inline-block;background:#F1F5F9;border:1px solid #111315;border-radius:6px;padding:3px 8px;font-size:12px;font-weight:700}
  .cell-num{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-variant-numeric:tabular-nums;font-size:18px;font-weight:700;text-align:right}
  .cell-gap{font-size:20px;font-weight:900}
  .cell-conf{color:#475569}
  .bento-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;margin-bottom:20px}
  .bento-tile,.themes-box{padding:18px 20px}
  .themes-box{margin-bottom:16px}
  .tile-label,.themes-title{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px;font-weight:800;color:#475569;text-transform:uppercase}
  .themes-title{color:#111315;margin-bottom:12px}
  .tile-value{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-variant-numeric:tabular-nums;font-size:34px;font-weight:900;margin:8px 0 4px}
  .tile-sub{font-size:13px;color:#64748B}
  .theme-row{margin-bottom:10px}
  .theme-labels{display:flex;justify-content:space-between;font-size:15px;font-weight:700;margin-bottom:4px}
  .theme-pct,.panel-kicker,.panel-metrics,.footer-tag,.footer-meta{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
  .theme-bar-bg{width:100%;height:10px;background:#E2E8F0;border-radius:999px;overflow:hidden}
  .theme-bar-fill{height:10px;background:#111315;border-radius:999px}
  .sub-panel{background:#FFF;border:1.5px solid #111315;border-radius:10px;padding:12px 16px;margin-bottom:12px}
  .panel-kicker{font-size:11px;font-weight:800;color:#475569;margin-bottom:6px}
  .panel-metrics{display:flex;justify-content:space-between;font-size:14px}
  .footer-box{padding:14px 18px}
  .footer-tag{font-size:12px;font-weight:800;color:#B45309;margin-bottom:4px}
  .footer-note{font-size:12px;color:#334155;margin:0 0 3px;line-height:1.4}
  .footer-meta{display:flex;justify-content:space-between;font-size:11px;color:#64748B;border-top:1px solid #E2E8F0;padding-top:6px;margin-top:6px}
`;

function renderCardShell(options: {
  title: string;
  cardKind: "ranking" | "entity";
  kicker: string;
  bodyHtml: string;
  riskNote: string;
  traceNote: string;
}): string {
  return `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<title>${escapeHtml(options.title)}</title>
<style>${SHARED_CARD_STYLESHEET}</style>
</head>
<body>
  <div class="card-canvas" data-card-kind="${options.cardKind}" data-card-dimensions="1080x1440">
    <div>
      <div class="header-top">
        <span class="brand-kicker">${options.kicker}</span>
        <span class="dim-badge">${XHS_CARD_WIDTH} × ${XHS_CARD_HEIGHT} · 3:4 HTML</span>
      </div>
      ${options.bodyHtml}
    </div>
    <div class="footer-box">
      <div class="footer-tag">RESEARCH HYPOTHESIS · NOT INVESTMENT ADVICE</div>
      <p class="footer-note">${options.riskNote}</p>
      <p class="footer-note">${options.traceNote} (mart.served_head, schema: ${CARD_SCHEMA_VERSION}).</p>
      <div class="footer-meta">
        <span>TrueAlpha Research HTML Card Export</span>
        <span>template: ${CARD_TEMPLATE_VERSION} · prompt: ${XHS_CARD_PROMPT_VERSION}</span>
      </div>
    </div>
  </div>
</body>
</html>`;
}

export function renderRankingCardHtml(data: RankingCardData): string {
  const displayItems = data.items.slice(0, 7);
  const basisLabel = valuationBasisLabel(data.items.find((item) => item.valuationBasis)?.valuationBasis ?? null);
  const conventionLabel = PEG_CONVENTION_LABELS[data.pegConvention ?? "historical_cagr"];
  const rowsHtml = displayItems
    .map((item, idx) => {
      const rankStr = item.rank !== null ? `#${item.rank}` : formatCell(null);
      const badgeBg = idx === 0 ? "#F59E0B" : idx === 1 ? "#334155" : idx === 2 ? "#B45309" : "#111315";
      return `<tr class="ranking-row">
        <td class="cell-rank"><span class="rank-pill" style="background:${badgeBg}">${escapeHtml(rankStr)}</span></td>
        <td class="cell-issuer">
          <div class="issuer-name">${escapeHtml(item.displayName)}</div>
          <div class="issuer-id">${escapeHtml(item.issuerId)}</div>
        </td>
        <td class="cell-tier"><span class="tier-badge">${escapeHtml(formatTierLabel(item.tier))}</span></td>
        <td class="cell-num">${escapeHtml(formatCell(formatRatio(item.currentMultiple)))}</td>
        <td class="cell-num cell-gap" style="color:${gapHexColor(item.valuationGap)}">${escapeHtml(formatCell(formatSignedRatio(item.valuationGap)))}</td>
        <td class="cell-num">${escapeHtml(formatCell(formatRatio(item.peg)))}</td>
        <td class="cell-num cell-conf">${escapeHtml(formatCell(formatRatio(item.confidence)))}</td>
      </tr>`;
    })
    .join("\n");

  return renderCardShell({
    title: `Valuation Rankings · ${data.universe.toUpperCase()}`,
    cardKind: "ranking",
    kicker: "TRUEALPHA · COLD-VERDICT RESEARCH",
    riskNote: "Ranking reflects one versioned strategy run at one cutoff; historical rank is not a forward-looking guarantee.",
    traceNote: "Traceable to materialized factor output",
    bodyHtml: `<div class="title-block">
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
        <tbody>${rowsHtml}</tbody>
      </table>`,
  });
}

export function renderEntityDeepDiveCardHtml(data: EntityCardData): string {
  const basisLabel = valuationBasisLabel(data.valuationBasis);
  const pegConventionLabel = PEG_CONVENTION_LABELS[data.pegConvention ?? "historical_cagr"];
  const formattedGppe = formatCell(formatUsdMagnitude(data.gppe) ?? (data.gppe ? `$${data.gppe}` : null));
  const displayThemes = data.themes.slice(0, 4);
  const themesHtml =
    displayThemes.length > 0
      ? displayThemes
          .map((t) => {
            const shareNum = Number(t.themeShare ?? 0);
            const pctWidth = Number.isNaN(shareNum) ? 0 : Math.min(100, Math.max(0, Math.round(shareNum * 100)));
            return `<div class="theme-row">
              <div class="theme-labels">
                <span class="theme-name">${escapeHtml(t.theme)}</span>
                <span class="theme-pct">${escapeHtml(formatCell(formatPercentFromFraction(t.themeShare)))}</span>
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

  return renderCardShell({
    title: `TrueAlpha Deep Dive · ${data.displayName}`,
    cardKind: "entity",
    kicker: "TRUEALPHA DEEP DIVE · ENTITY 360 CARD",
    riskNote: "Deep dive reflects one versioned strategy run at one cutoff; historical indicators are not forward-looking guarantees.",
    traceNote: "Traceable to materialized factor outputs",
    bodyHtml: `<div class="hero-header">
        <div>
          <h1>TrueAlpha Deep Dive: ${escapeHtml(data.displayName)}</h1>
          <div class="hero-sub">${escapeHtml(data.issuerId)} · Branch: ${escapeHtml(formatCell(data.operatingBranch))}</div>
        </div>
        <div class="tier-pill">${escapeHtml(formatTierLabel(data.tier))}</div>
      </div>
      <div class="meta-bar">
        <span>Cutoff: <strong>${escapeHtml(data.cutoffAt)}</strong></span>
        <span>Confidence: <strong>${escapeHtml(formatCell(formatRatio(data.confidence)))}</strong></span>
      </div>
      <div class="bento-grid">
        <div class="bento-tile">
          <div class="tile-label">Valuation Gap (${escapeHtml(basisLabel)})</div>
          <div class="tile-value" style="color:${gapHexColor(data.valuationGap)}">${escapeHtml(formatCell(formatSignedRatio(data.valuationGap)))}</div>
          <div class="tile-sub">Tier-implied relative gap</div>
        </div>
        <div class="bento-tile">
          <div class="tile-label">Current ${escapeHtml(basisLabel)}</div>
          <div class="tile-value">${escapeHtml(formatCell(formatRatio(data.currentMultiple)))}</div>
          <div class="tile-sub">Basis: ${escapeHtml(formatCell(data.valuationBasis))}</div>
        </div>
        <div class="bento-tile">
          <div class="tile-label">PEG Ratio</div>
          <div class="tile-value">${escapeHtml(formatCell(formatRatio(data.peg)))}</div>
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
      ${supplyChainHtml}`,
  });
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
    if (!row?.html || !verifyCardHtmlDocument(row.html, params.requiredFacts).ok) return null;
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

async function respondWithRenderedCard(options: {
  format: "html" | "png";
  html: string;
  renderMode: string;
  fileStem: string;
  persist: boolean;
  documents: DocumentsRepository;
  context: AccessContext;
  artifactId: string;
}): Promise<Response> {
  if (options.format === "png") {
    const pngBytes = await renderCardPngBuffer(options.html);
    return new Response(new Uint8Array(pngBytes), {
      status: 200,
      headers: {
        "Content-Type": "image/png",
        "Content-Disposition": `attachment; filename="${options.fileStem}.png"`,
        "Cache-Control": CARD_EXPORT_CACHE_CONTROL,
        "X-Research-Card-Render-Mode": options.renderMode,
      },
    });
  }
  let docId: string | null = null;
  if (options.persist) {
    try {
      const created = await options.documents.createDocument(options.context, {
        sourceArtifactId: options.artifactId,
        bytes: Buffer.from(options.html, "utf8"),
        contentType: "text/html",
      });
      docId = created.document.documentId;
    } catch (error) {
      console.error("persistCardDocument error:", error);
    }
  }
  const headers: Record<string, string> = {
    "Content-Type": "text/html; charset=utf-8",
    "Content-Disposition": `attachment; filename="${options.fileStem}.html"`,
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
  const rawFormat = searchParams.get("format") ?? "html";
  const format = rawFormat === "json" ? "json" : rawFormat === "png" ? "png" : "html";
  const convention = normalizePegConvention(searchParams.get("convention"));
  const universeParam = searchParams.get("universe") ?? undefined;
  const pageParam = searchParams.get("page");
  const pageNumber = pageParam ? Math.max(1, Number.parseInt(pageParam, 10) || 1) : null;
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
    const latestRow = state.data.rows.slice().sort((a, b) => b.cutoffAt.localeCompare(a.cutoffAt))[0];
    const cardData: EntityCardData = {
      issuerId: state.data.issuerId,
      displayName: entityLabel(state.data.issuerId, names),
      cutoffAt: latestRow?.cutoffAt ?? new Date().toISOString(),
      tier: latestRow?.tier ?? null,
      currentPriceToSales: latestRow?.currentPriceToSales ?? null,
      currentMultiple: latestRow?.currentMultiple ?? null,
      valuationBasis: latestRow?.valuationBasis ?? null,
      valuationGap: latestRow?.valuationGap ?? null,
      peg: projectPegByConvention(latestRow ?? {}, convention).peg,
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
        headers: { "Cache-Control": CARD_EXPORT_CACHE_CONTROL },
      });
    }
    const preMaterialized = await queryPreMaterializedCardHtml({
      cardKind: "company",
      subjectKey: state.data.issuerId,
      convention,
      universe: universeParam ?? "topt",
      requiredFacts: [state.data.issuerId, cardData.cutoffAt.slice(0, 10)],
    });
    return respondWithRenderedCard({
      format,
      html: preMaterialized?.html ?? renderEntityDeepDiveCardHtml(cardData),
      renderMode: preMaterialized?.renderMode ?? "cold_verdict_html",
      fileStem: `truealpha-card-entity-${state.data.issuerId.replace(/[:]/g, "-")}`,
      persist,
      documents,
      context: principal.context,
      artifactId: `card:entity:${toStableToken(state.data.issuerId)}:${toStableToken(cardData.cutoffAt)}:${toStableToken(convention)}:html`,
    });
  }

  const state = await loadRanking(
    principal.context,
    { cutoffAt: searchParams.get("cutoff") ?? undefined, cursor: null, universe: universeParam },
    deps.adapter,
  );
  if (state.kind !== "ready") {
    return NextResponse.json(
      { error: "ranking data not ready for card export", read_state: state.kind },
      { status: 404 },
    );
  }
  const names = deps.displayNames ?? (await loadEntityDisplayMap());
  const fullCardData: RankingCardData = {
    cutoffAt: state.data.cutoffAt,
    totalMembers: state.data.page.total,
    pegConvention: convention,
    universe: state.data.universe,
    stylePreset: searchParams.get("preset") ?? undefined,
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
  const pages = pageNumber !== null ? paginateRankingCardSeries(fullCardData, MAX_SUBJECTS_PER_CARD) : [fullCardData];
  const cardData = pages[Math.min((pageNumber ?? 1) - 1, pages.length - 1)] ?? fullCardData;
  if (format === "json") {
    return NextResponse.json(buildRankingCardJson(cardData), {
      status: 200,
      headers: { "Cache-Control": CARD_EXPORT_CACHE_CONTROL },
    });
  }
  const preMaterialized =
    pageNumber === null
      ? await queryPreMaterializedCardHtml({
          cardKind: "ranking",
          subjectKey: "theme:large_model_value",
          convention,
          universe: state.data.universe,
          requiredFacts: [state.data.cutoffAt.slice(0, 10)],
        })
      : null;
  return respondWithRenderedCard({
    format,
    html: preMaterialized?.html ?? renderRankingCardHtml(cardData),
    renderMode: preMaterialized?.renderMode ?? "cold_verdict_html",
    fileStem: `truealpha-card-ranking-${state.data.cutoffAt.replace(/[:T]/g, "-")}`,
    persist,
    documents,
    context: principal.context,
    artifactId: `card:ranking:${toStableToken(state.data.cutoffAt)}:${toStableToken(state.data.universe)}:${toStableToken(convention)}:html`,
  });
}
