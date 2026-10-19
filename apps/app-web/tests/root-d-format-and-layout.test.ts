/**
 * #1361 (Root D): Number formatting, human-readable identifiers, and typography hygiene
 * across UI tables and components.
 *
 * Standing checks for AC1–AC4 and SSOT invariants:
 *   AC1: /research/compare formats capitalAdjustedLaborEfficiency with formatUsdMagnitude,
 *        currentPriceToSales/currentMultiple/confidence with formatRatio, and valuationGap
 *        with formatSignedRatio and signColor.
 *   AC2: Market cap values on /admin/universes and monetary/share counts in TraceInputsTable
 *        render human-readable formatted numbers (with exact raw values preserved in title).
 *   AC3: /admin/quality, /research/trace, and AppChrome display human-readable ticker,
 *        issuer, and principal labels while preserving raw IDs in title or <code> metadata.
 *   AC4: /research and /research/themes include explicit whitespace around inline <code>
 *        and badge spans so text never contains "frommart.", "BRK.Bmissing_", or "available(".
 *   SSOT: No duplicate local `function cell(` or `function shortSha(` definitions or inline
 *        `Number(row.confidence).toFixed` calls across Root D surfaces.
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
import { TraceInputsTable } from "../src/components/trace-inputs-table";
import { traceInputsFromLineage } from "../src/server/mart/research-read";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

const APP_ROOT = new URL("../", import.meta.url).pathname;

function readAppFile(relativePath: string): string {
  return readFileSync(join(APP_ROOT, relativePath), "utf8");
}

// --- AC1: /research/compare formatting parity ---
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

  const compareSource = readAppFile("src/app/research/compare/page.tsx");
  assert(
    compareSource.includes("formatCell(formatUsdMagnitude(row.capitalAdjustedLaborEfficiency))"),
    "/research/compare must format capitalAdjustedLaborEfficiency with formatUsdMagnitude",
  );
  assert(
    compareSource.includes("formatCell(formatRatio(row.currentPriceToSales))"),
    "/research/compare must format currentPriceToSales with formatRatio",
  );
  assert(
    compareSource.includes("formatCell(formatRatio(row.currentMultiple ?? null))"),
    "/research/compare must format currentMultiple with formatRatio",
  );
  assert(
    compareSource.includes("formatCell(formatSignedRatio(row.valuationGap))") &&
      compareSource.includes("signColor(row.valuationGap)"),
    "/research/compare must format valuationGap with formatSignedRatio and signColor",
  );
  assert(
    compareSource.includes("formatCell(formatRatio(row.confidence))"),
    "/research/compare must format confidence with formatRatio",
  );
  assert(
    compareSource.includes("title={row.capitalAdjustedLaborEfficiency ?? undefined}") &&
      compareSource.includes("title={row.valuationGap ?? undefined}"),
    "/research/compare must preserve raw numeric strings in title attributes",
  );
}

// --- AC2: /admin/universes and TraceInputsTable number formatting ---
{
  assert(formatUsdMagnitude("5525648000000") === "$5.53T", "5525648000000 must format as $5.53T");
  assert(formatUsdMagnitude("4912984755200") === "$4.91T", "4912984755200 must format as $4.91T");
  assert(formatNumberMagnitude("171018000000") === "171.02B", "171018000000 must format as 171.02B");
  assert(formatNumberMagnitude("706413000000") === "706.41B", "706413000000 must format as 706.41B");
  assert(formatNumberMagnitude("7933746241") === "7.93B", "7933746241 must format as 7.93B");
  assert(formatNumberMagnitude("110000") === "110,000", "110000 must format with thousand separator");
  assert(formatNumberMagnitude("55.5") === "55.5", "small decimal prices must stay exact");

  const universesSource = readAppFile("src/app/admin/universes/page.tsx");
  assert(
    universesSource.includes("formatUsdMagnitude(member.market_cap)") &&
      universesSource.includes("title={member.market_cap ?? undefined}"),
    "/admin/universes must format market_cap with formatUsdMagnitude and keep raw value in title",
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

// --- AC3: Human-readable labels on /admin/quality, /research/trace, and AppChrome ---
{
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

  const qualitySource = readAppFile("src/app/admin/quality/page.tsx");
  assert(
    qualitySource.includes("formatListingLabel(row.listing_id)") && qualitySource.includes("title={row.listing_id}"),
    "/admin/quality must render formatListingLabel(row.listing_id) and preserve raw listing_id in title",
  );

  const traceSource = readAppFile("src/app/research/trace/page.tsx");
  assert(
    traceSource.includes("formatIssuerLabel(state.data.issuerId, entityLabel(state.data.issuerId, names))") &&
      traceSource.includes('data-evidence="true"'),
    "/research/trace must display resolved issuer label and mark raw ID code badge with data-evidence",
  );

  const chromeSource = readAppFile("src/components/app-chrome.tsx");
  assert(
    chromeSource.includes("formatPrincipalLabel(principal.context.principalId)") &&
      chromeSource.includes("title={`${principal.context.principalId} · ${principal.context.tenantId}`}"),
    "AppChrome must render formatPrincipalLabel and preserve raw principalId in title",
  );
}

// --- AC4: Whitespace hygiene on /research and /research/themes ---
{
  const researchSource = readAppFile("src/app/research/page.tsx");
  assert(
    /\{\s*" "\s*\}\s*<span\s+data-testid="served-run-age"/.test(researchSource),
    "/research must include explicit whitespace before the served-run-age badge",
  );
  assert(
    /\{\s*" "\s*\}\s*<code[^>]*>\s*\{item\.exclusion_reason\}/.test(researchSource),
    "/research must include explicit whitespace before the exclusion_reason code badge",
  );

  const themesSource = readAppFile("src/app/research/themes/page.tsx");
  assert(
    /read from\{" "\}\s*<code[^>]*>mart\.issuer_theme_purity<\/code>/.test(themesSource),
    "/research/themes must include explicit whitespace before <code>mart.issuer_theme_purity</code>",
  );
  assert(
    /\{\s*" "\s*\}\s*<span[^>]*>\(\{row\.reasonCodes\.join\(", "\)\}\)<\/span>/.test(themesSource),
    "/research/themes must include explicit whitespace before the reasonCodes span",
  );
}

// --- SSOT static guard: no duplicate local formatting functions in Root D files ---
{
  const ROOT_D_FILES = [
    "src/app/research/compare/page.tsx",
    "src/app/admin/quality/page.tsx",
    "src/app/admin/universes/page.tsx",
    "src/app/research/themes/page.tsx",
    "src/app/research/trace/page.tsx",
    "src/app/research/page.tsx",
    "src/components/trace-inputs-table.tsx",
    "src/components/app-chrome.tsx",
  ];

  for (const relativePath of ROOT_D_FILES) {
    const source = readAppFile(relativePath);
    assert(
      !/function\s+cell\s*\(/.test(source),
      `${relativePath} must not define a local cell() helper; import formatCell from @/client/format`,
    );
    assert(
      !/function\s+shortSha\s*\(/.test(source),
      `${relativePath} must not define a local shortSha() helper; import formatShortSha from @/client/format`,
    );
    assert(
      !/Number\s*\(\s*row\.confidence\s*\)\s*\.toFixed/.test(source),
      `${relativePath} must not inline Number(row.confidence).toFixed; use formatRatio from @/client/format`,
    );
  }
}

console.log("#1361 Root D formatting, identifier labels, whitespace hygiene, and SSOT guards passed");
