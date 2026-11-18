/**
 * #1361 (Root D): Number formatting, identifier labels, TraceInputsTable, ProvenanceCell, and formatting SSOT guards.
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
  formatTierLabel,
  formatUsdMagnitude,
  signColor,
} from "../src/client/format";
import { ProvenanceCell } from "../src/components/provenance-cell";
import { TraceInputsTable } from "../src/components/trace-inputs-table";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

const APP_ROOT = new URL("../", import.meta.url).pathname;

// 1. Core formatting helpers
assert(formatUsdMagnitude("171018000000") === "$171.02B", "USD magnitude formats billions");
assert(formatNumberMagnitude("7933746241") === "7.93B", "number magnitude formats billions");
assert(formatRatio("12.345") === "12.35", "ratio formats 2 decimal places");
assert(formatSignedRatio("-0.123") === "−0.12", "negative signed ratio uses minus sign");
assert(formatSignedRatio("0.123") === "+0.12", "positive signed ratio includes +");
assert(signColor("0.12").includes("emerald"), "positive gap highlighted");
assert(formatCell(null) === "—", "null cell renders em-dash");
assert(formatShortSha("0123456789abcdef0123").startsWith("0123456789ab"), "short sha truncates cleanly");
assert(formatTierLabel("large_model_native") === "Large Model Native", "tier label readable");
assert(formatIssuerLabel("issuer:cik:0000104169") === "CIK 0000104169", "CIK fallback readable");
assert(formatListingLabel("3dc96dda-763e-5379-bf95-2f729b3ed6da") === "Listing #3dc96dda", "UUID listing fallback readable");
assert((formatPrincipalLabel("principal:alice") ?? "").includes("alice"), "principal label readable");

// 2. TraceInputsTable + ProvenanceCell rendering
const traceHtml = renderToStaticMarkup(
  createElement(TraceInputsTable, {
    nodeValues: [
      { node_key: "gppe_uniform_charge_v0", period: "2026-06-30", value: "171018000000", unit: "USD", confidence: "0.923", availability_status: "available", reason_codes: [] },
    ],
  }),
);
assert(traceHtml.includes("171.02B USD") && traceHtml.includes("0.92"), "TraceInputsTable formats magnitude and confidence");

const provHtml = renderToStaticMarkup(
  createElement(ProvenanceCell, {
    row: {
      issuer_id: "issuer:wmt",
      label: "WMT",
      cutoff_at: "2026-06-30T23:59:59Z",
      outcome: "selected",
      eligible: true,
      trace_id: "trace:wmt",
      availability: "available",
      gppe_variants: {},
      provenance: { operating_period_end: "2026-06-30", universe_version: "v1", gppe_definition_sha256: "abcdef0123456789" },
    },
  }),
);
assert(provHtml.includes('data-evidence="true"') && provHtml.includes("abcdef012345"), "ProvenanceCell renders evidence + short sha");

// 3. Formatting SSOT across surviving frontend files
const ROOT_D_FILES = [
  "src/app/research/coverage/page.tsx",
  "src/app/research/rankings/page.tsx",
  "src/app/research/entities/[id]/page.tsx",
  "src/app/admin/page.tsx",
  "src/app/admin/datahub/page.tsx",
  "src/app/research/themes/page.tsx",
  "src/app/research/holdings/page.tsx",
  "src/app/research/trace/page.tsx",
  "src/app/research/page.tsx",
  "src/app/research/backtest/page.tsx",
  "src/app/research/backtest/[id]/page.tsx",
  "src/app/admin/datahub/ledger-coverage.tsx",
  "src/components/library-report-preview-card.tsx",
  "src/components/app-chrome.tsx",
  "src/components/trace-inputs-table.tsx",
  "src/components/provenance-cell.tsx",
  "src/components/backtest-basis.tsx",
  "src/components/backtest-chart.tsx",
];

for (const rel of ROOT_D_FILES) {
  const src = readFileSync(join(APP_ROOT, rel), "utf8");
  assert(!/(?:function\s+cell\s*\(|(?:const|let|var)\s+cell\s*=\s*\()/.test(src), `${rel} defines duplicate cell()`);
  assert(!/(?:function\s+(?:shortSha|shorten)\s*\(|(?:const|let|var)\s+(?:shortSha|shorten)\s*=\s*\()/.test(src), `${rel} defines duplicate shortSha()`);
  assert(!/(?:function\s+(?:formatRatio|formatPct|pct|percent)\s*\(|(?:const|let|var)\s+(?:formatRatio|formatPct|pct|percent)\s*=\s*\()/.test(src), `${rel} defines duplicate format helper`);
  assert(!/\?\?\s*["'`]—["'`]/.test(src), `${rel} inlines ?? "—" instead of formatCell()`);
  assert(!/\.slice\(\s*0\s*,\s*12\s*\)/.test(src), `${rel} inlines .slice(0, 12) instead of formatShortSha`);
  assert(!/\.toFixed\s*\(\s*2\s*\)/.test(src), `${rel} inlines .toFixed(2) instead of formatRatio`);
  assert(!/\.toLocaleString\s*\(\s*\)/.test(src), `${rel} calls zero-arg .toLocaleString()`);
  assert(!/formatIssuerLabel\s*\([^)]*entityLabel\s*\(/.test(src), `${rel} double-formats entityLabel()`);
}
console.log("#1361 Root D formatting and SSOT checks passed");
