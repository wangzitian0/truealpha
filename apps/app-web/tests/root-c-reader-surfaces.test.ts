/**
 * #1360 / #1716: Standing UI checks for Root C reader surfaces (M1 PEG convention & universe switch,
 * M3 supply-chain exposure, M4 analyst ratings, XHS card export, and sandboxed HTML preview on Library).
 */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

const bunTestModuleId = "bun:test";
const { mock } = (await import(bunTestModuleId)) as {
  mock: { module: (id: string, factory: () => Record<string, unknown>) => void };
};

mock.module("@/server/auth/request-context", () => ({
  getServerPrincipal: async () => ({
    principalKind: "administrator",
    context: {
      contextId: "ctx:root-c",
      principalId: "principal:root-c",
      tenantId: "tenant:truealpha",
      sessionId: "sess:root-c",
      authenticationMethod: "session",
      issuedAt: "2026-10-19T00:00:00Z",
      expiresAt: "2026-10-19T12:00:00Z",
    },
    requestOpts: { headers: { Cookie: "ta_session=test" } },
  }),
}));

const { __setApiFetch } = await import("../src/client/api");
const { CardExportButton } = await import("../src/components/card-export-button");
const { default: RankingsPage } = await import("../src/app/research/rankings/page");
const { default: EntityDetailPage } = await import("../src/app/research/entities/[id]/page");
const { default: DocumentDetailPage } = await import("../src/app/research/library/[id]/page");

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

const ISSUER_ID = "018f9e6a-0001-7a1b-8c2d-9e0f1a2b3c4d";

__setApiFetch(async (input) => {
  const url = String(input);
  if (url.includes("/api/v1/research/rankings")) {
    return new Response(
      JSON.stringify({
        available: true,
        cutoff_at: "2026-06-30T23:59:59Z",
        total: 1,
        rows: [
          {
            issuer_id: ISSUER_ID,
            label: "AAPL · APPLE INC",
            rank: 1,
            tier: "tier_1_high_conviction",
            valuation_basis: "enterprise_value_to_gppe",
            current_price_to_sales: "7.80",
            current_multiple: "12.40",
            target_multiple: "15.00",
            valuation_gap: "0.21",
            peg: "1.35",
            peg_rank: 1,
            confidence: "0.91",
            availability: "available",
            gppe_variants: {},
            provenance: { operating_period_end: "2026-06-30", universe_version: "v1" },
          },
        ],
      }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    );
  }
  if (url.includes(`/api/v1/research/entities/${encodeURIComponent(ISSUER_ID)}`)) {
    return new Response(
      JSON.stringify({
        issuer_id: ISSUER_ID,
        label: "AAPL · APPLE INC",
        decisions: [
          {
            issuer_id: ISSUER_ID,
            cutoff_at: "2026-06-30T23:59:59Z",
            rank: 1,
            tier: "tier_1_high_conviction",
            valuation_basis: "enterprise_value_to_gppe",
            current_multiple: "12.40",
            target_multiple: "15.00",
            valuation_gap: "0.21",
            peg: "1.35",
            target_weight: "0.08",
            availability: "available",
          },
        ],
        node_values: [
          { node_key: "gppe_uniform_charge_v0", period: "2026-06-30", value: "1000000000", unit: "USD", confidence: "0.9", availability_status: "available", reason_codes: [] },
        ],
        analyst_ratings: { consensus_rating: "4.35", analysts_count: 18 },
        supply_chain_exposure: { exposure_score: "0.42", direct_partners: 9 },
      }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    );
  }
  if (url.includes("/api/v1/research/documents/doc-card-1")) {
    return new Response(
      JSON.stringify({
        document: { document_id: "doc-card-1", created_at: "2026-10-19T00:00:00Z" },
        revisions: [{ revision_id: "rev-1", source_artifact_id: "card-1", artifact_content_type: "text/html", artifact_byte_length: 128 }],
        latest_content: "<!DOCTYPE html><html><body><div>AAPL Card</div></body></html>",
      }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    );
  }
  return new Response(JSON.stringify({ detail: `unmatched ${url}` }), { status: 404 });
});

try {
  const rankingsHtml = renderToStaticMarkup(
    await RankingsPage({ searchParams: Promise.resolve({ convention: "consensus", universe: "qqq" }) }),
  );
  assert(rankingsHtml.includes("AAPL · APPLE INC"), "RankingsPage renders resolved issuer label");
  assert(rankingsHtml.includes("convention=guidance") && rankingsHtml.includes("universe=qqq"), "RankingsPage keeps convention + universe switch links");

  const entityHtml = renderToStaticMarkup(
    await EntityDetailPage({ params: Promise.resolve({ id: ISSUER_ID }), searchParams: Promise.resolve({ convention: "historical_cagr" }) }),
  );
  assert(entityHtml.includes("Analyst Consensus:") && entityHtml.includes("4.35"), "EntityDetailPage renders Module 4 consensus");
  assert(entityHtml.includes("Supply Chain Scenario Exposure:") && entityHtml.includes("0.42"), "EntityDetailPage renders Module 3 exposure");
  assert(entityHtml.includes("Metric forest nodes") && entityHtml.includes("gppe_uniform_charge_v0"), "EntityDetailPage renders metric nodes");

  const buttonHtml = renderToStaticMarkup(createElement(CardExportButton, { cutoff: "2026-06-30", convention: "historical_cagr" }));
  assert(buttonHtml.includes("Export Xiaohongshu HTML Card") && buttonHtml.includes('data-testid="card-export-button"'), "CardExportButton renders trigger label");

  const docHtml = renderToStaticMarkup(await DocumentDetailPage({ params: Promise.resolve({ id: "doc-card-1" }) }));
  assert(docHtml.includes('<iframe sandbox=""') && docHtml.includes("srcDoc="), "Library detail renders sandboxed HTML preview iframe");
  console.log("#1360 Root C UI reader surfaces verified via OpenAPI client mock");
} finally {
  __setApiFetch(null);
}
