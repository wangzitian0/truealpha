/**
 * #1361 (Root D): Number formatting, human-readable identifiers, and typography hygiene
 * across UI tables and components.
 *
 * Standing checks for AC1–AC4 and SSOT invariants:
 *   AC1: /research/compare renders capitalAdjustedLaborEfficiency with formatUsdMagnitude,
 *        currentPriceToSales/currentMultiple/confidence with formatRatio, and valuationGap
 *        with formatSignedRatio and signColor (with raw strings in title attributes).
 *   AC2: Market cap values on /admin/universes and monetary/share counts in TraceInputsTable
 *        render human-readable formatted numbers (with exact raw values preserved in title).
 *   AC3: /admin/quality, /research/trace, and AppChrome render human-readable ticker,
 *        issuer, and principal labels while preserving raw IDs in title or <code data-evidence="true">.
 *   AC4: /research, /research/themes, and /research/strategy include explicit whitespace
 *        around inline <code> and badge spans so visible text never concatenates tokens.
 *   SSOT: No duplicate local `function cell(`, `function shortSha(`, `function formatRatio(`,
 *        or `function formatPct(` definitions in Root D surfaces.
 *
 * Run standalone: `bun run tests/root-d-format-and-layout.test.ts`.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import {
  formatCell,
  formatIssuerLabel,
  formatListingLabel,
  formatNumberMagnitude,
  formatPrincipalLabel,
  formatRatio,
  formatShortSha,
  formatSignedRatio,
  formatUsdMagnitude,
  signColor,
} from "../src/client/format";
import { ProvenanceCell } from "../src/components/provenance-cell";
import { TraceInputsTable } from "../src/components/trace-inputs-table";
import { entityLabel, type EntityDisplay } from "../src/server/mart/entity-resolution";
import { traceInputsFromLineage, type RankingRow } from "../src/server/mart/research-read";

const bunTestModuleId = "bun:test";
const { mock } = (await import(bunTestModuleId)) as {
  mock: { module: (id: string, factory: () => Record<string, unknown>) => void };
};

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

function visibleText(html: string): string {
  return html.replace(/<[^>]*>/g, "");
}

const APP_ROOT = new URL("../", import.meta.url).pathname;

function readAppFile(relativePath: string): string {
  return readFileSync(join(APP_ROOT, relativePath), "utf8");
}

// --- Mock server loaders to render async Server Components directly via renderToStaticMarkup ---
const MOCK_ADMIN_PRINCIPAL = {
  principalKind: "administrator" as const,
  context: {
    contextId: "ctx:root-d",
    principalId: "principal:e2e:admin",
    tenantId: "tenant:truealpha",
    sessionId: "sess:root-d",
    authenticationMethod: "session" as const,
    issuedAt: "2026-10-19T00:00:00Z",
    expiresAt: "2026-10-19T12:00:00Z",
  },
};

mock.module("@/server/auth/request-context", () => ({
  getServerPrincipal: async () => MOCK_ADMIN_PRINCIPAL,
}));

mock.module("@/components/sign-out-button", () => ({
  SignOutButton: () => createElement("button", { type: "button" }, "Sign out"),
}));

mock.module("@/components/card-export-button", () => ({
  CardExportButton: () => createElement("button", { type: "button" }, "Export Card"),
}));

mock.module("@/components/trigger-run-button", () => ({
  TriggerRunButton: () => createElement("button", { type: "button" }, "Trigger run"),
}));

mock.module("@/server/mart/entity-resolution", () => ({
  loadEntityDisplayMap: async () =>
    new Map<string, EntityDisplay>([
      ["issuer:cik:0000104169", { ticker: "WMT", displayName: "WALMART INC" }],
      ["issuer:brk", { ticker: "BRK.B", displayName: null }],
      ["issuer:aapl", { ticker: "AAPL", displayName: "APPLE INC" }],
    ]),
  entityLabel,
}));

mock.module("@/server/dashboard", () => ({
  loadComparison: async () => ({
    kind: "ready" as const,
    data: {
      cutoffAt: "2026-10-18T00:00:00Z",
      page: { total: 1, hasMore: false, nextCursor: null },
      rows: [
        {
          issuerId: "issuer:aapl",
          capitalAdjustedLaborEfficiency: "3375549.33",
          laborEfficiencyMetric: "gppe",
          currentPriceToSales: "1.3344",
          currentMultiple: "5.5117",
          valuationBasis: "price_to_gross_profit",
          tier: "large_model_native",
          valuationGap: "-0.4992",
          confidence: "0.8812",
          availability: "available",
        },
      ],
    },
  }),
  loadOverview: async () => ({
    kind: "ready" as const,
    data: {
      run: {
        strategyRunId: "run:overview-1",
        executedAt: "2026-10-18T01:00:00Z",
        source: "mart.strategy_runs",
      },
      latestCutoff: "2026-10-18T00:00:00Z",
      modules: [],
    },
  }),
  loadTrace: async () => ({
    kind: "ready" as const,
    data: {
      traceId: "trace:wmt:2026-10-18",
      strategyId: "large_model_value_v0",
      source: "mart",
      issuerId: "issuer:cik:0000104169",
      cutoffAt: "2026-10-18T00:00:00Z",
      governed: true,
      lineageResultId: "topt-core-result:wmt",
      listings: [],
      links: [],
    },
  }),
}));

mock.module("@/server/admin/universes", () => ({
  loadUniverses: async () => ({
    heads: [
      {
        kind: "topt",
        universe_id: "univ:topt:1",
        sequence: 1,
        instrument_count: 1,
        mapping_sha256: "abcdef0123456789abcdef0123456789",
        advanced_at: "2026-10-18T00:00:00Z",
        note: "initial",
      },
    ],
    members: {
      topt: [
        {
          ticker: "NVDA",
          company_name: "NVIDIA CORP",
          cik: null,
          figi: "BBG000BBJQV0",
          market_cap: "5525648000000",
          as_of: "2026-10-18",
          source: "sec",
          fetch_sha256: "1234567890abcdef1234567890abcdef",
          object_uri: "s3://raw/nvda.json",
        },
      ],
    },
    history: [],
  }),
}));

mock.module("@/server/topt-quality", () => ({
  captureAvailableCount: () => 1,
  loadToptQuality: async () => ({
    kind: "ready" as const,
    data: {
      run_id: "run:topt:1",
      requested_count: 1,
      cells: [
        {
          listing_id: "listing:xnas:aapl",
          availability: "available",
          gppe: "1091637.30",
          confidence: "0.9123",
        },
      ],
      quality: { availability: "1.0000" },
    },
  }),
}));

mock.module("@/server/admin/funnel", () => ({
  loadQualityFunnel: async () => ({ kind: "empty" as const }),
}));

mock.module("@/server/admin/ops", () => ({
  RUNS_EMPTY_MESSAGE: "No runs recorded yet.",
  RUNS_UNAVAILABLE_MESSAGE: "Run history unavailable.",
  loadOpsOverview: async () => ({
    kind: "ready" as const,
    data: {
      runs: [
        {
          runId: "1234567890abcdef1234567890abcdef",
          jobName: "topt_live_pipeline",
          status: "SUCCESS",
          createdAt: "2026-10-18T22:15:00Z",
          durationSeconds: 185,
        },
      ],
      pointers: [
        {
          universeId: "topt-core",
          targetRunId: "capture-run:1234567890abcdef",
          sequence: 7,
          advancedAt: "2026-10-18T22:20:00Z",
          ageHours: 0.5,
          freshness: "fresh" as const,
          availability: "available" as const,
        },
      ],
      dataEngine: {
        gitSha: "4cf7291deadbeef0123456789",
        imageDigest: "sha256:00f7",
        runId: "capture-run:1234567890abcdef",
        createdAt: "2026-10-18T22:15:00Z",
      },
      appGitSha: "a30d19e000000000000000000",
      quotaToday: [],
    },
  }),
}));

mock.module("@/server/strategy-page", () => ({
  loadStrategyRunPage: async () => ({
    kind: "ready" as const,
    report: {
      strategy_id: "large_model_value_v0",
      source: "mart",
      corpus_sha256: "deadbeefcafe1234",
      governed: true,
      golden_mismatches: ["mismatch-1"],
      definition: {
        version: "1.0.0",
        review_state: "reviewed" as const,
        content_sha256: "a0c6f1f003373b5eff85872fd493de91e2fd6d9fa3d15e78e278f96b88bd8803",
      },
      decisions: [
        {
          issuer_id: "issuer:aapl",
          cutoff_at: "2026-10-18T00:00:00Z",
          outcome: "selected" as const,
          exclusion_reason: null,
          tier: "large_model_native",
          valuation_basis: "price_to_gross_profit" as const,
          current_price_to_sales: "8.1234",
          current_price_to_gross_profit: "14.5678",
          target_price_to_sales: "10.0000",
          target_price_to_gross_profit: "18.0000",
          valuation_gap: "0.2356",
          confidence: "0.9234",
          rank: 1,
          target_weight: "0.5000",
        },
        {
          issuer_id: "issuer:brk",
          cutoff_at: "2026-10-18T00:00:00Z",
          outcome: "excluded" as const,
          exclusion_reason: "missing_gross_profit_fact",
          tier: null,
          valuation_basis: "price_to_gross_profit" as const,
          current_price_to_sales: null,
          current_price_to_gross_profit: null,
          target_price_to_sales: null,
          target_price_to_gross_profit: null,
          valuation_gap: null,
          confidence: null,
          rank: null,
          target_weight: null,
        },
      ],
    },
  }),
}));

mock.module("@/server/mart/theme-purity", () => ({
  classifiedShare: () => "0.8500",
  loadThemePurity: async () => [
    {
      themeId: "theme:ai",
      theme: "Large Model Value",
      definitionVersion: "1.0.0",
      definitionSha256: "abcdef0123456789abcdef0123456789",
      runId: "run:theme:1",
      cutoff: "2026-10-18",
      rows: [
        {
          issuerId: "issuer:aapl",
          themeShare: "0.6250",
          inThemeRevenue: "125000000000",
          consolidatedRevenue: "200000000000",
          segments: 3,
          periodEnd: "2026-09-30",
          confidence: "0.8765",
          availabilityStatus: "available",
          extractor: "sec-xbrl",
          reasonCodes: ["unclassified_revenue"],
        },
      ],
    },
  ],
}));

mock.module("@/server/mart/fund-valuation", () => ({
  loadFundValuation: async () => [
    {
      fundId: "fund:brk",
      fundName: "Berkshire Hathaway",
      reportPeriod: "2026-06-30",
      totalWeightPct: "100.00",
      resolvedWeightPct: "95.00",
      valuedWeightPct: "90.00",
      weightedGap: "0.1234",
      weightedGapBasis: "price_to_gross_profit" as const,
      reasonCodes: [],
      availabilityStatus: "available",
      sourceEvidenceStatus: "verified",
      factorValidationStatus: "accepted",
      runId: "run:fund:12345678901234567890",
      lines: [
        {
          holdingName: "Apple Inc",
          ticker: "AAPL",
          weightPct: "25.4321",
          currentPs: "8.1234",
          currentPgp: "14.5678",
          targetPsMidpoint: null,
          targetPgpMidpoint: "18.0000",
          valuationGap: "0.2356",
          tier: "large_model_native",
        },
      ],
    },
  ],
}));

const { default: ComparePage } = await import("../src/app/research/compare/page");
const { default: AdminUniversesPage } = await import("../src/app/admin/universes/page");
const { default: AdminOverviewPage } = await import("../src/app/admin/page");
const { default: ToptQualityPage } = await import("../src/app/admin/quality/page");
const { default: TracePage } = await import("../src/app/research/trace/page");
const { AppChrome } = await import("../src/components/app-chrome");
const { default: ResearchOverviewPage } = await import("../src/app/research/page");
const { default: ThemesPage } = await import("../src/app/research/themes/page");
const { default: StrategyRunsPage } = await import("../src/app/research/strategy/page");
const { default: HoldingsPage } = await import("../src/app/research/holdings/page");

// --- AC1: /research/compare formatting parity (rendered SSR component) ---
{
  assert(formatUsdMagnitude("3375549.33") === "$3.38M", "3375549.33 must format as $3.38M");
  assert(formatUsdMagnitude("1091637.30") === "$1.09M", "1091637.30 must format as $1.09M");
  assert(formatUsdMagnitude("76328.64") === "$76.3K", "76328.64 must format as $76.3K");
  assert(formatRatio("1.3344") === "1.33", "1.3344 must format as 1.33");
  assert(formatRatio("5.5117") === "5.51", "5.5117 must format as 5.51");
  assert(formatSignedRatio("-0.4992") === "−0.50", "-0.4992 must format as −0.50");
  assert(signColor("-0.4992") === "text-red-400", "negative valuationGap must use text-red-400");
  assert(signColor("0.4200") === "text-emerald-400", "positive valuationGap must use text-emerald-400");
  assert(formatCell(null) === "—", "formatCell(null) must render em-dash");
  assert(formatShortSha("abcdef0123456789") === "abcdef012345…", "formatShortSha must truncate to 12 chars");

  const compareHtml = renderToStaticMarkup(await ComparePage({ searchParams: Promise.resolve({}) }));
  assert(compareHtml.includes("$3.38M"), `ComparePage must render $3.38M: ${compareHtml}`);
  assert(compareHtml.includes('title="3375549.33"'), "ComparePage must preserve raw 3375549.33 in title");
  assert(compareHtml.includes(">1.33<") && compareHtml.includes('title="1.3344"'), "ComparePage must render 1.33 with raw title");
  assert(compareHtml.includes(">5.51<") && compareHtml.includes('title="5.5117"'), "ComparePage must render 5.51 with raw title");
  assert(
    compareHtml.includes(">−0.50<") && compareHtml.includes("text-red-400") && compareHtml.includes('title="-0.4992"'),
    "ComparePage must render signed valuationGap −0.50 with text-red-400 and raw title",
  );
  assert(compareHtml.includes(">0.88<") && compareHtml.includes('title="0.8812"'), "ComparePage must render 0.88 with raw title");
}

// --- AC2: /admin/universes, /admin, ProvenanceCell, and TraceInputsTable formatting (rendered SSR components) ---
{
  assert(formatUsdMagnitude("5525648000000") === "$5.53T", "5525648000000 must format as $5.53T");
  assert(formatUsdMagnitude("4912984755200") === "$4.91T", "4912984755200 must format as $4.91T");
  assert(formatNumberMagnitude("171018000000") === "171.02B", "171018000000 must format as 171.02B");
  assert(formatNumberMagnitude("706413000000") === "706.41B", "706413000000 must format as 706.41B");
  assert(formatNumberMagnitude("7933746241") === "7.93B", "7933746241 must format as 7.93B");
  assert(formatNumberMagnitude("110000") === "110,000", "110000 must format with thousand separator");
  assert(formatNumberMagnitude("55.5") === "55.5", "small decimal prices must stay exact");

  const universesHtml = renderToStaticMarkup(await AdminUniversesPage());
  assert(universesHtml.includes(">$5.53T<"), `AdminUniversesPage must render $5.53T: ${universesHtml}`);
  assert(universesHtml.includes('title="5525648000000"'), "AdminUniversesPage must preserve raw 5525648000000 in title");
  assert(universesHtml.includes("abcdef012345…"), "AdminUniversesPage must render truncated mapping sha256");

  const adminOverviewHtml = renderToStaticMarkup(await AdminOverviewPage());
  assert(
    adminOverviewHtml.includes(">4cf7291deadb…<") &&
      adminOverviewHtml.includes("app a30d19e00000…") &&
      adminOverviewHtml.includes(">1234567890ab…<") &&
      adminOverviewHtml.includes('title="1234567890abcdef1234567890abcdef"'),
    `AdminOverviewPage must format git/run digests via formatShortSha and preserve runId in title: ${adminOverviewHtml}`,
  );
  assert(
    adminOverviewHtml.includes(">30m ago<"),
    `AdminOverviewPage must format pointer.ageHours=0.5 via formatElapsedHours as 30m ago: ${adminOverviewHtml}`,
  );

  const provenanceRow = {
    issuerId: "issuer:aapl",
    cutoffAt: "2026-10-18T00:00:00Z",
    provenance: {
      operatingPeriodEnd: "2026-09-30",
      revenuePeriodEnd: "2026-09-30",
      sharesPeriodEnd: "2026-09-30",
      universeVersion: "v1",
      gppeDefinitionSha256: "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
      tierDefinitionSha256: null,
    },
  } as unknown as RankingRow;
  const provenanceHtml = renderToStaticMarkup(createElement(ProvenanceCell, { row: provenanceRow }));
  assert(
    provenanceHtml.includes(">abcdef012345…<") &&
      provenanceHtml.includes('title="abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"'),
    `ProvenanceCell must format SHA-256 via formatShortSha and preserve full digest in title: ${provenanceHtml}`,
  );
  assert(
    provenanceHtml.includes("text-amber-500") && provenanceHtml.includes(">unknown<"),
    "ProvenanceCell must render unknown in amber when a definition digest is null",
  );

  const uuidListing = "3dc96dda-763e-5379-bf95-2f729b3ed6da";
  const lineage = [
    {
      observation_id: "normalized-observation:fin-wmt",
      semantic_type: "financial-fact",
      source_vintage_id: "source-vintage:fin-wmt",
      knowable_at: "2026-03-31T00:00:00Z",
      vintage: null,
      inputs: {
        listing_id: uuidListing,
        currency: "USD",
        gross_profit: "171018000000",
        total_assets: "252399000000",
        headcount: "2100000",
        revenue: "706413000000",
        shares_outstanding: "7933746241",
      },
    },
  ];
  const listings = traceInputsFromLineage(lineage, uuidListing);
  const html = renderToStaticMarkup(
    createElement(TraceInputsTable, { listings, lineageResultId: "topt-core-result:wmt" }),
  );
  assert(html.includes(">171.02B USD<"), `TraceInputsTable must render formatted 171.02B USD: ${html}`);
  assert(html.includes('title="171018000000 USD"'), "TraceInputsTable must preserve raw 171018000000 USD in title");
  assert(html.includes(">706.41B USD<"), "TraceInputsTable must render formatted 706.41B USD");
  assert(html.includes('title="706413000000 USD"'), "TraceInputsTable must preserve raw 706413000000 USD in title");
  assert(html.includes(">7.93B<"), "TraceInputsTable must render formatted share count 7.93B");
  assert(html.includes('title="7933746241"'), "TraceInputsTable must preserve raw 7933746241 in title");
  assert(html.includes("Listing: Listing #3dc96dda"), "TraceInputsTable must render human-readable UUID listing label");
}

// --- AC3: Human-readable labels on entityLabel, /admin/quality, /research/trace, and AppChrome ---
{
  const displayMap = new Map<string, EntityDisplay>([
    ["issuer:wmt", { ticker: "WMT", displayName: "WALMART INC" }],
    ["issuer:brk", { ticker: "BRK.B", displayName: null }],
    ["issuer:name-only", { ticker: "", displayName: "NAME ONLY CORP" }],
  ]);
  assert(entityLabel("issuer:wmt", displayMap) === "WMT · WALMART INC", "entityLabel must join ticker and displayName");
  assert(entityLabel("issuer:brk", displayMap) === "BRK.B", "entityLabel must return ticker when displayName is null");
  assert(
    entityLabel("issuer:name-only", displayMap) === "NAME ONLY CORP",
    "entityLabel must preserve displayName when ticker is blank",
  );
  assert(
    entityLabel("issuer:cik:0000104169", displayMap) === "CIK 0000104169",
    "entityLabel must delegate unmapped CIK fallback to formatIssuerLabel",
  );
  assert(
    entityLabel("issuer:lei:HWUPKR0MPOU8FGXBT394", displayMap) === "LEI HWUPKR0MPOU8…",
    "entityLabel must delegate unmapped LEI fallback to formatIssuerLabel",
  );
  assert(
    entityLabel("3dc96dda-763e-5379-bf95-2f729b3ed6da", displayMap) === "Issuer #3dc96dda",
    "entityLabel must delegate unmapped UUID fallback to formatIssuerLabel",
  );

  assert(formatListingLabel("listing:xnas:aapl") === "AAPL (XNAS)", "listing:xnas:aapl must format as AAPL (XNAS)");
  assert(
    formatListingLabel("listing:xnys:brk.b") === "BRK.B (XNYS)",
    "listing:xnys:brk.b must format as BRK.B (XNYS)",
  );
  assert(
    formatListingLabel("3dc96dda-763e-5379-bf95-2f729b3ed6da") === "Listing #3dc96dda",
    "UUID listing must format as Listing #3dc96dda",
  );
  assert(
    formatIssuerLabel("issuer:cik:0000104169") === "CIK 0000104169",
    "unmapped CIK issuer must format as CIK 0000104169",
  );
  assert(
    formatIssuerLabel("issuer:cik:0000104169", "WMT · WALMART INC") === "WMT · WALMART INC",
    "resolved issuer label must take precedence over raw CIK fallback",
  );
  assert(formatPrincipalLabel("principal:e2e:admin") === "admin", "principal:e2e:admin must format as admin");
  assert(formatPrincipalLabel("principal:owner") === "owner", "principal:owner must format as owner");
  assert(
    formatPrincipalLabel("principal:friend-alice") === "friend-alice",
    "principal:friend-alice must format as friend-alice",
  );

  const qualityHtml = renderToStaticMarkup(await ToptQualityPage());
  assert(qualityHtml.includes(">AAPL (XNAS)<"), `ToptQualityPage must render AAPL (XNAS): ${qualityHtml}`);
  assert(qualityHtml.includes('title="listing:xnas:aapl"'), "ToptQualityPage must preserve listing:xnas:aapl in title");
  assert(
    qualityHtml.includes('data-evidence="true"') && qualityHtml.includes(">listing:xnas:aapl</code>"),
    "ToptQualityPage must mark raw listing_id code badge with data-evidence=\"true\"",
  );

  const traceHtml = renderToStaticMarkup(
    await TracePage({ searchParams: Promise.resolve({ issuer: "issuer:cik:0000104169", cutoff: "2026-10-18T00:00:00Z" }) }),
  );
  assert(traceHtml.includes(">WMT · WALMART INC<"), `TracePage must render resolved WMT · WALMART INC: ${traceHtml}`);
  assert(
    traceHtml.includes('data-evidence="true"') && traceHtml.includes(">issuer:cik:0000104169</code>"),
    "TracePage must mark raw issuerId code badge with data-evidence=\"true\"",
  );

  const chromeElement = await AppChrome();
  assert(chromeElement !== null, "AppChrome must render for authenticated principal");
  const chromeHtml = renderToStaticMarkup(chromeElement);
  assert(chromeHtml.includes(">admin<"), `AppChrome must render formatted principal label 'admin': ${chromeHtml}`);
  assert(
    chromeHtml.includes('title="principal:e2e:admin · tenant:truealpha"'),
    "AppChrome must preserve full principalId and tenantId in title",
  );
}

// --- AC4: Whitespace hygiene and title parity on /research, /research/themes, /research/strategy, and /research/holdings ---
{
  const researchHtml = renderToStaticMarkup(await ResearchOverviewPage());
  const researchText = visibleText(researchHtml);
  assert(
    !researchText.includes("one.stale") && !/one\.\d+\s*h ago/.test(researchText),
    `/research visibleText must separate period and served-run-age badge with a space: ${researchText}`,
  );
  assert(
    researchText.includes("BRK.B missing_gross_profit_fact") && !researchText.includes("BRK.Bmissing_"),
    `/research visibleText must separate ticker and exclusion_reason badge with a space: ${researchText}`,
  );
  assert(
    researchHtml.includes('title="0.5000"') && researchHtml.includes(">50%<"),
    `/research must preserve raw target_weight in title and format 50%: ${researchHtml}`,
  );
  assert(
    researchHtml.includes('title="issuer:brk"'),
    "/research excluded disclosure must preserve raw issuer_id in title",
  );

  const themesHtml = renderToStaticMarkup(await ThemesPage());
  const themesText = visibleText(themesHtml);
  assert(
    themesText.includes("read from mart.issuer_theme_purity") && !themesText.includes("frommart."),
    `/research/themes visibleText must separate 'read from' and 'mart.issuer_theme_purity': ${themesText}`,
  );
  assert(
    themesText.includes("available (unclassified_revenue)") && !themesText.includes("available("),
    `/research/themes visibleText must separate status and reason codes with a space: ${themesText}`,
  );
  assert(
    themesHtml.includes(">0.88<") && themesHtml.includes('title="0.8765"'),
    "/research/themes must format confidence with formatRatio and preserve raw confidence in title",
  );
  assert(
    themesHtml.includes(">62.5%<") &&
      themesHtml.includes('title="0.6250"') &&
      themesHtml.includes(">85%<") &&
      themesHtml.includes('title="0.8500"'),
    `/research/themes must format themeShare and classifiedShare via formatPercentFromFraction with raw titles: ${themesHtml}`,
  );
  assert(
    themesHtml.includes(">abcdef012345…<") &&
      themesHtml.includes('title="abcdef0123456789abcdef0123456789"') &&
      themesHtml.includes('title="125000000000"') &&
      themesHtml.includes('title="200000000000"'),
    `/research/themes must format definitionSha256 via formatShortSha and preserve raw revenue strings in title: ${themesHtml}`,
  );

  const strategyHtml = renderToStaticMarkup(await StrategyRunsPage());
  const strategyText = visibleText(strategyHtml);
  assert(
    strategyText.includes("Corpus deadbeefcafe1234. 1 golden mismatch(es).") &&
      !strategyText.includes("deadbeefcafe1234.1"),
    `/research/strategy visibleText must separate corpus_sha256 period and golden_mismatches badge: ${strategyText}`,
  );

  const holdingsHtml = renderToStaticMarkup(await HoldingsPage());
  assert(
    holdingsHtml.includes(">25.43%<") && holdingsHtml.includes('title="25.4321"'),
    `/research/holdings must render formatted weightPct 25.43% with raw title: ${holdingsHtml}`,
  );
  assert(
    holdingsHtml.includes(">8.12<") && holdingsHtml.includes('title="8.1234"'),
    "/research/holdings must render formatted currentPs 8.12 with raw title",
  );
}

// --- SSOT static guard: no duplicate local formatting functions in Root D files ---
{
  const ROOT_D_FILES = [
    "src/app/research/compare/page.tsx",
    "src/app/admin/quality/page.tsx",
    "src/app/admin/universes/page.tsx",
    "src/app/admin/page.tsx",
    "src/app/research/themes/page.tsx",
    "src/app/research/trace/page.tsx",
    "src/app/research/page.tsx",
    "src/app/research/strategy/page.tsx",
    "src/app/research/holdings/page.tsx",
    "src/components/trace-inputs-table.tsx",
    "src/components/provenance-cell.tsx",
    "src/components/app-chrome.tsx",
  ];

  for (const relativePath of ROOT_D_FILES) {
    const source = readAppFile(relativePath);
    assert(
      !/function\s+cell\s*\(/.test(source),
      `${relativePath} must not define a local cell() helper; import formatCell from @/client/format`,
    );
    assert(
      !/function\s+(?:shortSha|shorten)\s*\(/.test(source),
      `${relativePath} must not define a local shortSha()/shorten() helper; import formatShortSha from @/client/format`,
    );
    assert(
      !/function\s+(?:formatRatio|formatPct|pct)\s*\(/.test(source),
      `${relativePath} must not define a local formatRatio/formatPct/pct helper; import from @/client/format`,
    );
    assert(
      !/\.slice\(\s*0\s*,\s*12\s*\)/.test(source),
      `${relativePath} must not inline .slice(0, 12); use formatShortSha from @/client/format`,
    );
    assert(
      !/Number\s*\(\s*row\.confidence\s*\)\s*\.toFixed/.test(source),
      `${relativePath} must not inline Number(row.confidence).toFixed; use formatRatio from @/client/format`,
    );
    assert(
      !/formatIssuerLabel\s*\([^)]*entityLabel\s*\(/.test(source),
      `${relativePath} must not wrap entityLabel() in formatIssuerLabel(); entityLabel() is the single upstream entry point`,
    );
  }

  for (const martLoader of ["src/server/mart/fund-holdings.ts"]) {
    const source = readAppFile(martLoader);
    assert(
      !/\.toFixed\s*\(\s*2\s*\)/.test(source),
      `${martLoader} must not inline .toFixed(2); use formatRatio from @/client/format`,
    );
  }
}

console.log("#1361 Root D formatting, identifier labels, whitespace hygiene, and SSOT guards passed");
