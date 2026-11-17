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

export const CARD_PRESETS: Record<string, CardPresetConfig> = Object.fromEntries(
  ([
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
  ] as const).map(([presetId, layout, palette, bgCss, surfaceBg, fg, mute, accent, border]) => [
    presetId,
    { presetId, style: presetId, layout, palette, bgCss, surfaceBg, fg, mute, accent, border },
  ]),
);

interface CardMetricSnapshot {
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

export interface RankingCardItem extends CardMetricSnapshot {
  rank: number | null;
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

export interface EntityCardData extends CardMetricSnapshot {
  cutoffAt: string;
  pegConvention?: PegGrowthConvention;
  gppe: string | null;
  operatingBranch: string | null;
  themes: Array<{ theme: string; themeShare: string | null }>;
  analystRatings?: AnalystRatingsDetail | null;
  supplyChainExposure?: SupplyChainExposureDetail | null;
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
        return Buffer.from(
          await page.screenshot({
            type: "png",
            clip: { x: 0, y: 0, width: XHS_CARD_WIDTH, height: XHS_CARD_HEIGHT },
          }),
        );
      } finally {
        await browser.close();
      }
    }
  } catch {
    // Fall back to deterministic PNG header when browser binaries are absent.
  }
  return FALLBACK_PNG_BYTES;
}

const HTML_ESCAPE_MAP: Record<string, string> = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
export const escapeHtml = (unsafe: string): string => unsafe.replace(/[&<>"']/g, (ch) => HTML_ESCAPE_MAP[ch]);

function gapHexColor(valuationGap: string | null): string {
  const gapVal = Number(valuationGap);
  return Number.isNaN(gapVal) || gapVal === 0 ? "#475569" : gapVal > 0 ? "#047857" : "#BE123C";
}

export function entityCardThemes(
  themes: ReadonlyArray<{ theme: string; themeShare: string | null; unvisitedReason?: string | null }>,
): EntityCardData["themes"] {
  return themes.filter((t) => !t.unvisitedReason).map((t) => ({ theme: t.theme, themeShare: t.themeShare }));
}

function cardJsonEnvelope(
  cardKind: "ranking" | "entity",
  title: string,
  cutoffAt: string,
  riskNote: string,
  sourceAttribution: string,
) {
  return {
    schema_version: CARD_SCHEMA_VERSION,
    card_kind: cardKind,
    template_version: CARD_TEMPLATE_VERSION,
    prompt_version: XHS_CARD_PROMPT_VERSION,
    title,
    cutoff_at: cutoffAt,
    generated_at: new Date().toISOString(),
    research_risk_note: riskNote,
    source_attribution: sourceAttribution,
  };
}

const DEFAULT_PEG_CONVENTION: PegGrowthConvention = "historical_cagr";

function metricRecord(item: CardMetricSnapshot) {
  return {
    current_price_to_sales: item.currentPriceToSales,
    current_price_to_gross_profit: item.valuationBasis === "price_to_gross_profit" ? item.currentMultiple : null,
    valuation_gap: item.valuationGap,
    peg: item.peg,
    confidence: item.confidence,
  };
}

export function buildRankingCardJson(data: RankingCardData) {
  return {
    ...cardJsonEnvelope(
      "ranking",
      "Valuation Rankings",
      data.cutoffAt,
      "Ranking reflects one versioned strategy run at one cutoff; historical rank is not a forward-looking guarantee.",
      "TrueAlpha research — traceable to a materialized factor output (mart.served_head).",
    ),
    universe: data.universe,
    peg_convention: data.pegConvention ?? DEFAULT_PEG_CONVENTION,
    subjects: data.items.map((item) => ({
      subject_id: item.issuerId,
      display_name: item.displayName,
      rank: item.rank,
      tier: formatCell(item.tier),
      valuation_basis: item.valuationBasis,
      metrics: Object.entries(metricRecord(item)).map(([key, value]) => ({ key, value })),
    })),
  };
}

export function buildEntityCardJson(data: EntityCardData) {
  return {
    ...cardJsonEnvelope(
      "entity",
      "Entity Deep Dive",
      data.cutoffAt,
      "Deep dive reflects one versioned strategy run at one cutoff; historical indicators are not forward-looking guarantees.",
      "TrueAlpha research — traceable to materialized factor outputs (mart.strategy_decisions, mart.topt_gppe_results, mart.issuer_theme_purity, mart.issuer_analyst_ratings, mart.issuer_supply_chain_exposure).",
    ),
    issuer_id: data.issuerId,
    display_name: data.displayName,
    metrics: {
      tier: formatCell(data.tier),
      ...metricRecord(data),
      valuation_basis: data.valuationBasis,
      peg_convention: data.pegConvention ?? DEFAULT_PEG_CONVENTION,
      gppe: data.gppe,
      operating_branch: data.operatingBranch,
    },
    themes: data.themes.map((t) => ({ theme: t.theme, theme_share: t.themeShare })),
    analyst_ratings: data.analystRatings ?? null,
    supply_chain_exposure: data.supplyChainExposure ?? null,
  };
}

const INLINE_EVENT_HANDLER_RE = /\bon[a-z]+\s*=/i;
const WHITESPACE_RE = /\s+/g;
const ENTITY_REPLACEMENTS: ReadonlyArray<readonly [RegExp, string]> = [
  [/&amp;/gi, "&"], [/&lt;/gi, "<"], [/&gt;/gi, ">"], [/&quot;/gi, '"'], [/&#39;|&apos;/gi, "'"],
  [/&mdash;|&#8212;|&#x2014;/gi, "—"], [/&ndash;|&#8211;|&#x2013;/gi, "–"], [/&minus;|&#8722;|&#x2212;/gi, "−"],
  [/&middot;|&#183;/gi, "·"], [/&nbsp;|&#160;/gi, " "],
];

function normalizeHtmlText(html: string): string {
  let out = html.replace(/<[^>]+>/g, " ");
  for (const [pattern, char] of ENTITY_REPLACEMENTS) out = out.replace(pattern, char);
  return out.replace(WHITESPACE_RE, " ").trim();
}

export function verifyCardHtmlDocument(
  html: string,
  requiredFacts: ReadonlyArray<string>,
): { ok: boolean; violations: string[] } {
  const violations: string[] = [];
  const lower = html.toLowerCase();
  if ((!lower.includes("<!doctype html") && !lower.includes("<html")) || !lower.includes("</html>")) violations.push("missing_html_envelope");
  if (!html.includes(String(XHS_CARD_WIDTH)) || !html.includes(String(XHS_CARD_HEIGHT))) violations.push("missing_1080x1440_dimensions");
  if (lower.includes("<script") || lower.includes("javascript:") || INLINE_EVENT_HANDLER_RE.test(html)) violations.push("forbidden_script_or_event_handler");
  if (lower.includes("http://") || lower.includes("https://")) violations.push("forbidden_external_url");
  const normalized = normalizeHtmlText(html);
  for (const fact of requiredFacts) {
    if (!fact) continue;
    const unicodeMinus = fact.startsWith("-") ? `−${fact.slice(1)}` : fact;
    const checks = [fact, escapeHtml(fact), unicodeMinus];
    if (!checks.some((c) => html.includes(c)) && !normalized.includes(fact.replace(WHITESPACE_RE, " ").trim()) && !normalized.includes(unicodeMinus)) {
      violations.push(`missing_fact:${fact}`);
    }
  }
  return { ok: violations.length === 0, violations };
}

const SHARED_CARD_STYLESHEET = `html,body,.card-canvas{width:${XHS_CARD_WIDTH}px;height:${XHS_CARD_HEIGHT}px;background:#F7F5F0;box-sizing:border-box}
html,body{margin:0;padding:0;color:#111315;font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;overflow:hidden}
.card-canvas{padding:44px 48px;display:flex;flex-direction:column;justify-content:space-between;border:3px solid #111315}
.brand-kicker,.dim-badge,.hero-sub,.meta-bar,.ranking-table th,.rank-pill,.issuer-id,.cell-num,.tile-label,.themes-title,.tile-value,.theme-pct,.panel-kicker,.panel-metrics,.footer-tag,.footer-meta{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.header-top,.hero-header,.meta-bar,.theme-labels,.panel-metrics,.footer-meta{display:flex;justify-content:space-between}
.brand-kicker,.tier-pill,.ranking-table th,.rank-pill,.issuer-name,.tile-label,.themes-title,.panel-kicker,.footer-tag{font-weight:800}
.title-block .subtitle,.hero-sub,.ranking-table th,.cell-conf,.tile-label,.panel-kicker{color:#475569}
.issuer-id,.tile-sub,.footer-meta{color:#64748B}
.dim-badge,.tier-pill{background:#111315;color:#F7F5F0}
.header-top,.title-block .subtitle,.hero-header{margin-bottom:18px}
.header-top{align-items:center;border-bottom:2px solid #111315;padding-bottom:14px}
.brand-kicker{font-size:14px;letter-spacing:0.1em}
.dim-badge{font-size:12px;font-weight:700;padding:4px 10px;border-radius:6px}
.title-block h1,.hero-header h1{font-size:40px;font-weight:900;margin:0 0 6px}
.title-block .subtitle{font-size:20px;font-weight:600;margin-top:0}
.hero-header{align-items:flex-start}
.hero-sub{font-size:14px}
.tier-pill{padding:8px 16px;border-radius:10px;font-size:16px}
.meta-bar,.bento-tile,.themes-box,.footer-box,.ranking-row td,.sub-panel{background:#FFF}
.meta-bar,.bento-tile,.themes-box,.footer-box{border:2px solid #111315;border-radius:12px;box-shadow:4px 4px 0 #111315}
.meta-bar,.bento-grid{margin-bottom:20px}
.meta-bar{align-items:center;padding:12px 18px;font-size:13px}
.ranking-table,.theme-bar-bg{width:100%}
.ranking-table{border-collapse:separate;border-spacing:0 10px}
.ranking-table th{font-size:12px;text-transform:uppercase;padding:0 12px 6px;text-align:left}
.ranking-table th.num-head,.cell-num{text-align:right}
.ranking-row td{border-top:1.5px solid #111315;border-bottom:1.5px solid #111315;padding:14px 12px;vertical-align:middle}
.ranking-row td:first-child{border-left:1.5px solid #111315;border-radius:12px 0 0 12px;width:64px;text-align:center}
.ranking-row td:last-child{border-right:1.5px solid #111315;border-radius:0 12px 12px 0}
.rank-pill{display:inline-block;color:#FFF;font-size:15px;padding:4px 10px;border-radius:999px}
.issuer-name{font-size:19px}
.issuer-id{font-size:12px;margin-top:2px}
.tier-badge{display:inline-block;background:#F1F5F9;border:1px solid #111315;border-radius:6px;padding:3px 8px;font-size:12px;font-weight:700}
.cell-num{font-variant-numeric:tabular-nums;font-size:18px;font-weight:700}
.cell-gap{font-size:20px;font-weight:900}
.bento-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}
.bento-tile,.themes-box{padding:18px 20px}
.themes-box{margin-bottom:16px}
.tile-label,.themes-title{font-size:13px;text-transform:uppercase}
.themes-title,.sub-panel{margin-bottom:12px}
.themes-title{color:#111315}
.tile-value{font-variant-numeric:tabular-nums;font-size:34px;font-weight:900;margin:8px 0 4px}
.tile-sub{font-size:13px}
.theme-row{margin-bottom:10px}
.theme-labels{font-size:15px;font-weight:700;margin-bottom:4px}
.theme-bar-bg,.theme-bar-fill{height:10px;border-radius:999px}
.theme-bar-bg{background:#E2E8F0;overflow:hidden}
.theme-bar-fill{background:#111315}
.sub-panel{border:1.5px solid #111315;border-radius:10px;padding:12px 16px}
.panel-kicker{font-size:11px;margin-bottom:6px}
.panel-metrics{font-size:14px}
.footer-box{padding:14px 18px}
.footer-tag{font-size:12px;color:#B45309;margin-bottom:4px}
.footer-note{font-size:12px;color:#334155;margin:0 0 3px;line-height:1.4}
.footer-meta{font-size:11px;border-top:1px solid #E2E8F0;padding-top:6px;margin-top:6px}`;

const escCell = (value: string | number | boolean | null | undefined): string => escapeHtml(formatCell(value));
const escRatio = (value: string | null | undefined): string => escCell(formatRatio(value));

function renderCardShell(options: {
  title: string;
  cardKind: "ranking" | "entity";
  kicker: string;
  headerHtml: string;
  metaLeft: string;
  metaRight: string;
  bodyHtml: string;
  riskNote: string;
  traceNote: string;
}): string {
  return `<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8" /><title>${escapeHtml(options.title)}</title><style>${SHARED_CARD_STYLESHEET}</style></head>
<body>
  <div class="card-canvas" data-card-kind="${options.cardKind}" data-card-dimensions="1080x1440">
    <div>
      <div class="header-top"><span class="brand-kicker">${options.kicker}</span><span class="dim-badge">${XHS_CARD_WIDTH} × ${XHS_CARD_HEIGHT} · 3:4 HTML</span></div>
      ${options.headerHtml}
      <div class="meta-bar"><span>${options.metaLeft}</span><span>${options.metaRight}</span></div>
      ${options.bodyHtml}
    </div>
    <div class="footer-box">
      <div class="footer-tag">RESEARCH HYPOTHESIS · NOT INVESTMENT ADVICE</div>
      <p class="footer-note">${options.riskNote}</p>
      <p class="footer-note">${options.traceNote} (mart.served_head, schema: ${CARD_SCHEMA_VERSION}).</p>
      <div class="footer-meta"><span>TrueAlpha Research HTML Card Export</span><span>template: ${CARD_TEMPLATE_VERSION} · prompt: ${XHS_CARD_PROMPT_VERSION}</span></div>
    </div>
  </div>
</body>
</html>`;
}

function renderSubPanel(kicker: string, metrics: ReadonlyArray<[string, string | number | null | undefined, string?]>): string {
  const spans = metrics
    .map(([label, val, extra = ""]) => `<span>${label}: <strong>${escCell(val)}</strong>${extra}</span>`)
    .join("");
  return `<div class="sub-panel"><div class="panel-kicker">${kicker}</div><div class="panel-metrics">${spans}</div></div>`;
}

export function renderRankingCardHtml(data: RankingCardData): string {
  const basisLabel = valuationBasisLabel(data.items.find((item) => item.valuationBasis)?.valuationBasis ?? null);
  const conventionLabel = PEG_CONVENTION_LABELS[data.pegConvention ?? DEFAULT_PEG_CONVENTION];
  const rankBg = ["#F59E0B", "#334155", "#B45309"];
  const heads = ["RANK", "ISSUER", "TIER", basisLabel, `VALUATION GAP (${basisLabel})`, "PEG", "CONF"];
  const rowsHtml = data.items
    .slice(0, 7)
    .map((item, idx) => {
      const rankStr = item.rank !== null ? `#${item.rank}` : formatCell(null);
      return `<tr class="ranking-row">
        <td><span class="rank-pill" style="background:${rankBg[idx] ?? "#111315"}">${escapeHtml(rankStr)}</span></td>
        <td><div class="issuer-name">${escapeHtml(item.displayName)}</div><div class="issuer-id">${escapeHtml(item.issuerId)}</div></td>
        <td><span class="tier-badge">${escapeHtml(formatTierLabel(item.tier))}</span></td>
        <td class="cell-num">${escRatio(item.currentMultiple)}</td>
        <td class="cell-num cell-gap" style="color:${gapHexColor(item.valuationGap)}">${escCell(formatSignedRatio(item.valuationGap))}</td>
        <td class="cell-num">${escRatio(item.peg)}</td>
        <td class="cell-num cell-conf">${escRatio(item.confidence)}</td>
      </tr>`;
    })
    .join("\n");

  return renderCardShell({
    title: `Valuation Rankings · ${data.universe.toUpperCase()}`,
    cardKind: "ranking",
    kicker: "TRUEALPHA · COLD-VERDICT RESEARCH",
    riskNote: "Ranking reflects one versioned strategy run at one cutoff; historical rank is not a forward-looking guarantee.",
    traceNote: "Traceable to materialized factor output",
    headerHtml: `<div class="title-block"><h1>Valuation Rankings</h1><p class="subtitle">大模型价值主题榜单 · ${escapeHtml(data.universe.toUpperCase())} · PEG (${escapeHtml(conventionLabel)})</p></div>`,
    metaLeft: `Governed Cutoff: <strong>${escapeHtml(data.cutoffAt)}</strong>`,
    metaRight: `Universe: <strong>${escapeHtml(data.universe)}</strong> (${data.totalMembers} total members)`,
    bodyHtml: `<table class="ranking-table">
        <thead><tr>${heads.map((h, i) => `<th${i >= 3 ? ' class="num-head"' : ""}>${escapeHtml(h)}</th>`).join("")}</tr></thead>
        <tbody>${rowsHtml}</tbody>
      </table>`,
  });
}

export function renderEntityDeepDiveCardHtml(data: EntityCardData): string {
  const basisLabel = valuationBasisLabel(data.valuationBasis);
  const pegConvention = data.pegConvention ?? DEFAULT_PEG_CONVENTION;
  const displayThemes = data.themes.slice(0, 4);
  const themesHtml =
    displayThemes.length > 0
      ? displayThemes
          .map((t) => {
            const shareNum = Number(t.themeShare ?? 0);
            const pctWidth = Number.isNaN(shareNum) ? 0 : Math.min(100, Math.max(0, Math.round(shareNum * 100)));
            return `<div class="theme-row">
              <div class="theme-labels"><span>${escapeHtml(t.theme)}</span><span class="theme-pct">${escCell(formatPercentFromFraction(t.themeShare))}</span></div>
              <div class="theme-bar-bg"><div class="theme-bar-fill" style="width:${pctWidth}%"></div></div>
            </div>`;
          })
          .join("\n")
      : `<div>No theme purity materialization recorded for this issuer</div>`;

  const tiles: Array<[string, string, string, string?]> = [
    [`Valuation Gap (${basisLabel})`, formatCell(formatSignedRatio(data.valuationGap)), "Tier-implied relative gap", `color:${gapHexColor(data.valuationGap)}`],
    [`Current ${basisLabel}`, formatCell(formatRatio(data.currentMultiple)), `Basis: ${formatCell(data.valuationBasis)}`],
    ["PEG Ratio", formatCell(formatRatio(data.peg)), `${PEG_CONVENTION_LABELS[pegConvention]} (${pegConvention})`],
    ["GP / Employee (GPPE)", formatCell(formatUsdMagnitude(data.gppe) ?? (data.gppe ? `$${data.gppe}` : null)), `Operating branch: ${formatCell(data.operatingBranch)}`],
  ];
  const bentoHtml = tiles
    .map(([label, val, sub, style]) => `<div class="bento-tile"><div class="tile-label">${escapeHtml(label)}</div><div class="tile-value"${style ? ` style="${style}"` : ""}>${escapeHtml(val)}</div><div class="tile-sub">${escapeHtml(sub)}</div></div>`)
    .join("");

  const m4 = data.analystRatings;
  const analystHtml = m4
    ? renderSubPanel("MODULE 4 · ANALYST CONSENSUS", [
        ["Rating", m4.consensusRating],
        ["Target", m4.targetPrice ? `$${m4.targetPrice}` : null],
        ["Analysts", m4.analystsCount, ` (B:${m4.buyCount ?? 0}/H:${m4.holdCount ?? 0}/S:${m4.sellCount ?? 0})`],
      ])
    : "";
  const m3 = data.supplyChainExposure;
  const supplyChainHtml = m3
    ? renderSubPanel("MODULE 3 · SUPPLY-CHAIN SCENARIO (scenario_only)", [
        ["Exposure", m3.exposureScore],
        ["Partners", m3.directPartners, ` (Sup:${m3.suppliersCount ?? 0}/Cust:${m3.customersCount ?? 0})`],
        ["Max Share", formatPercentFromFraction(m3.maxPartnerShare)],
      ])
    : "";

  return renderCardShell({
    title: `TrueAlpha Deep Dive · ${data.displayName}`,
    cardKind: "entity",
    kicker: "TRUEALPHA DEEP DIVE · ENTITY 360 CARD",
    riskNote: "Deep dive reflects one versioned strategy run at one cutoff; historical indicators are not forward-looking guarantees.",
    traceNote: "Traceable to materialized factor outputs",
    headerHtml: `<div class="hero-header">
        <div>
          <h1>TrueAlpha Deep Dive: ${escapeHtml(data.displayName)}</h1>
          <div class="hero-sub">${escapeHtml(data.issuerId)} · Branch: ${escCell(data.operatingBranch)}</div>
        </div>
        <div class="tier-pill">${escapeHtml(formatTierLabel(data.tier))}</div>
      </div>`,
    metaLeft: `Cutoff: <strong>${escapeHtml(data.cutoffAt)}</strong>`,
    metaRight: `Confidence: <strong>${escRatio(data.confidence)}</strong>`,
    bodyHtml: `<div class="bento-grid">${bentoHtml}</div>
      <div class="themes-box"><div class="themes-title">Module 6 · Theme Purity Exposure</div>${themesHtml}</div>
      ${analystHtml}${supplyChainHtml}`,
  });
}

const SERVED_CARD_ARTIFACT_SQL = `select a.html, a.render_mode, a.model
from mart.research_card_artifacts a join mart.served_head sh on sh.head_run_id = a.run_id
where a.card_kind = $1 and a.subject_key = $2 and a.convention = $3 and a.universe = $4
order by a.recorded_at desc limit 1`;

async function queryPreMaterializedCardHtml(
  cardKind: string,
  subjectKey: string,
  convention: string,
  universe: string,
  requiredFacts: ReadonlyArray<string>,
): Promise<{ html: string; renderMode: string } | null> {
  if (!process.env.DATABASE_URL) return null;
  try {
    const row = await withMartReadonly(async (client) => {
      const res = await client.query<{ html: string; render_mode: string }>(SERVED_CARD_ARTIFACT_SQL, [cardKind, subjectKey, convention, universe]);
      return res.rows[0] ?? null;
    });
    if (!row?.html || !verifyCardHtmlDocument(row.html, requiredFacts).ok) return null;
    return { html: row.html, renderMode: row.render_mode || "model_glm" };
  } catch {
    return null;
  }
}

const toStableToken = (raw: string): string =>
  raw.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").split("-").filter((t) => t && !["latest", "current", "default", "head"].includes(t)).join("-") || "v1";

export interface CardExportRouteDeps {
  resolvePrincipal?: () => Promise<{ context: AccessContext } | null>;
  adapter?: MartAdapterLike;
  documents?: DocumentsRepository;
  displayNames?: Awaited<ReturnType<typeof loadEntityDisplayMap>>;
}

function snapshotFromRow(
  issuerId: string,
  displayName: string,
  row: (Partial<CardMetricSnapshot> & Parameters<typeof projectPegByConvention>[0]) | undefined,
  convention: PegGrowthConvention,
): CardMetricSnapshot {
  return {
    issuerId,
    displayName,
    tier: row?.tier ?? null,
    currentPriceToSales: row?.currentPriceToSales ?? null,
    currentMultiple: row?.currentMultiple ?? null,
    valuationBasis: row?.valuationBasis ?? null,
    valuationGap: row?.valuationGap ?? null,
    peg: projectPegByConvention(row ?? {}, convention).peg,
    confidence: row?.confidence ?? null,
  };
}

export async function handleCardExportGet(request: Request, deps: CardExportRouteDeps = {}): Promise<Response> {
  const resolvePrincipal = deps.resolvePrincipal ?? (await import("@/server/auth/request-context")).getServerPrincipal;
  const principal = await resolvePrincipal();
  if (!principal) return NextResponse.json({ error: "authentication required" }, { status: 401 });

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
  const loadNames = () => deps.displayNames ?? loadEntityDisplayMap();
  const artifactId = (cardKind: string, ...parts: string[]) => `card:${cardKind}:${parts.map(toStableToken).join(":")}:html`;

  async function respond(
    jsonBody: () => unknown,
    renderHtml: () => string,
    preArgs: Parameters<typeof queryPreMaterializedCardHtml> | null,
    fileStem: string,
    sourceArtifactId: string,
  ): Promise<Response> {
    if (format === "json") {
      return NextResponse.json(jsonBody(), { status: 200, headers: { "Cache-Control": CARD_EXPORT_CACHE_CONTROL } });
    }
    const pre = preArgs ? await queryPreMaterializedCardHtml(...preArgs) : null;
    const html = pre?.html ?? renderHtml();
    const headers: Record<string, string> = {
      "Content-Type": format === "png" ? "image/png" : "text/html; charset=utf-8",
      "Content-Disposition": `attachment; filename="${fileStem}.${format}"`,
      "Cache-Control": CARD_EXPORT_CACHE_CONTROL,
      "X-Research-Card-Render-Mode": pre?.renderMode ?? "cold_verdict_html",
    };
    if (format === "png") return new Response(new Uint8Array(await renderCardPngBuffer(html)), { status: 200, headers });
    if (persist) {
      try {
        const created = await documents.createDocument(principal!.context, { sourceArtifactId, bytes: Buffer.from(html, "utf8"), contentType: "text/html" });
        headers["X-Research-Document-Id"] = created.document.documentId;
      } catch (error) {
        console.error("persistCardDocument error:", error);
      }
    }
    return new Response(html, { status: 200, headers });
  }

  if (kind === "entity") {
    const issuerId = searchParams.get("issuer");
    if (!issuerId) return NextResponse.json({ error: "missing issuer param" }, { status: 400 });
    const state = await loadEntityDetail(principal.context, issuerId, deps.adapter);
    if (state.kind !== "ready") return NextResponse.json({ error: "entity detail not ready for card export", read_state: state.kind }, { status: 404 });
    const names = await loadNames();
    const latestRow = state.data.rows.slice().sort((a, b) => b.cutoffAt.localeCompare(a.cutoffAt))[0];
    const cardData: EntityCardData = {
      ...snapshotFromRow(state.data.issuerId, entityLabel(state.data.issuerId, names), latestRow, convention),
      cutoffAt: latestRow?.cutoffAt ?? new Date().toISOString(),
      pegConvention: convention,
      gppe: state.data.gppeDetail?.gppe ?? latestRow?.capitalAdjustedLaborEfficiency ?? null,
      operatingBranch: state.data.gppeDetail?.operatingBranch ?? null,
      themes: entityCardThemes(state.data.themes ?? []),
      analystRatings: state.data.analystRatings ?? null,
      supplyChainExposure: state.data.supplyChainExposure ?? null,
    };
    return respond(
      () => buildEntityCardJson(cardData),
      () => renderEntityDeepDiveCardHtml(cardData),
      ["company", state.data.issuerId, convention, universeParam ?? "topt", [state.data.issuerId, cardData.cutoffAt.slice(0, 10)]],
      `truealpha-card-entity-${state.data.issuerId.replace(/[:]/g, "-")}`,
      artifactId("entity", state.data.issuerId, cardData.cutoffAt, convention),
    );
  }

  const state = await loadRanking(principal.context, { cutoffAt: searchParams.get("cutoff") ?? undefined, cursor: null, universe: universeParam }, deps.adapter);
  if (state.kind !== "ready") return NextResponse.json({ error: "ranking data not ready for card export", read_state: state.kind }, { status: 404 });
  const names = await loadNames();
  const fullCardData: RankingCardData = {
    cutoffAt: state.data.cutoffAt,
    totalMembers: state.data.page.total,
    pegConvention: convention,
    universe: state.data.universe,
    stylePreset: searchParams.get("preset") ?? undefined,
    items: state.data.rows.map((row) => ({ rank: row.rank, ...snapshotFromRow(row.issuerId, entityLabel(row.issuerId, names), row, convention) })),
  };
  const pages = pageNumber !== null ? paginateRankingCardSeries(fullCardData, MAX_SUBJECTS_PER_CARD) : [fullCardData];
  const cardData = pages[Math.min((pageNumber ?? 1) - 1, pages.length - 1)] ?? fullCardData;
  return respond(
    () => buildRankingCardJson(cardData),
    () => renderRankingCardHtml(cardData),
    pageNumber === null ? ["ranking", "theme:large_model_value", convention, state.data.universe, [state.data.cutoffAt.slice(0, 10)]] : null,
    `truealpha-card-ranking-${state.data.cutoffAt.replace(/[:T]/g, "-")}`,
    artifactId("ranking", state.data.cutoffAt, state.data.universe, convention),
  );
}
