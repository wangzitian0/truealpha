/**
 * #1360: Standing acceptance checks for Root C reader surfaces (M1, M3, M4)
 * and 1080x1440 PNG card export + Library persistence.
 *
 * Verifies via real adapter calls and rendered SSR React components (`renderToStaticMarkup`):
 * - AC1: Module 4 (`mart.issuer_analyst_ratings`) read adapter, dashboard loader,
 *   and `EntityDetailPage` SSR rendering (`consensusRating`, `targetPrice`, `buyCount`,
 *   `holdCount`, `sellCount`, `extractor`, `availabilityStatus`, `reasonCodes`).
 * - AC2: Module 3 (`mart.issuer_supply_chain_exposure`) read adapter, dashboard loader,
 *   and `EntityDetailPage` SSR rendering (`exposureScore`, `directPartners`, `suppliersCount`,
 *   `customersCount`, `maxPartnerShare`, `extractor`, `availabilityStatus`, `reasonCodes`).
 * - AC3: Module 1 PEG growth-rate convention contract (`historical_cagr`, `consensus`,
 *   `guidance`), `peg_reason_codes` projection from `DECISIONS_SQL` -> `DecisionProvenance`
 *   -> `RankingRow` / `ComparisonRow`, end-to-end `universe` (`topt` / `qqq`) filtering in
 *   `loadRanking`, and SSR selector/table rendering on `RankingsPage` and `EntityDetailPage`.
 * - AC4: `/research/api/cards/export` `GET` route handler (`format=png` default,
 *   `Content-Type: image/png`, `1080x1440` PNG magic/IHDR header, convention/universe
 *   forwarding, and owner-scoped Library persistence via `DocumentsRepository.createDocument`),
 *   plus `CardExportButton` and `LibraryPage` SSR rendering (`data-evidence` UUID wrapping).
 *
 * Run standalone: `bun run tests/root-c-reader-surfaces.test.ts`.
 */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { formatTierLabel, tierKey } from "../src/client/format";

const bunTestModuleId = "bun:test";
const { mock } = (await import(bunTestModuleId)) as {
  mock: { module: (id: string, factory: () => Record<string, unknown>) => void };
};
import { CardExportButton } from "../src/components/card-export-button";
import {
  FixtureStrategyRunRepository,
  GROWTH_CONVENTION_UNSOURCED_PREFIX,
  matchesRankingUniverse,
  normalizePegConvention,
  normalizeRankingUniverse,
  PEG_CONVENTION_LABELS,
  PEG_GROWTH_CONVENTIONS,
  projectPegByConvention,
  RANKING_UNIVERSE_LABELS,
  RANKING_UNIVERSES,
  type AccessContext,
  type StrategyRunDecision,
  type StrategyRunReport,
} from "../src/contracts/strategyRun";
import { handleCardExportGet, XHS_CARD_HEIGHT, XHS_CARD_WIDTH } from "../src/server/cards/xhs-card";
import { loadEntityDetail, loadRanking } from "../src/server/dashboard";
import {
  assertStableId,
  type DocumentPage,
  type DocumentsRepository,
  type NewDocumentRevisionInput,
  type ResearchDocument,
  type ResearchDocumentRevision,
} from "../src/server/documents";
import type { EntityDisplay } from "../src/server/mart/entity-resolution";
import { StrategyRunReadAdapter, toAvailabilityStatus } from "../src/server/mart/research-read";
import { DECISIONS_SQL, provenanceFromRow, type DecisionProvenance } from "../src/server/mart/strategy-run-repository";
import type { MartClientLike } from "../src/server/mart/topt-gppe-repository";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

const TEST_CONTEXT: AccessContext = {
  contextId: "ctx:root-c-test",
  principalId: "principal:root-c-owner",
  tenantId: "tenant:truealpha",
  sessionId: "session:root-c",
  authenticationMethod: "password",
  issuedAt: "2026-10-19T00:00:00Z",
  expiresAt: "2026-10-19T01:00:00Z",
};

// ─── AC1 & AC2: Module 4 and Module 3 Mart Read Adapter + loadEntityDetail ──
const executedSqls: string[] = [];
const fakeClient: MartClientLike = {
  query: async (sql: string) => {
    executedSqls.push(sql);
    if (sql.includes("from mart.issuer_analyst_ratings")) {
      return {
        rows: [
          {
            run_id: "run-live-20261019",
            cutoff: "2026-06-30T23:59:59Z",
            consensus_rating: "4.35",
            target_price: null,
            analysts_count: 18,
            buy_count: 12,
            hold_count: 5,
            sell_count: 1,
            confidence: "0.88",
            reason_codes: ["target_price:195.00", "consensus:buy"],
            extractor: "origin:moomoo:v1",
            availability_status: "available",
            source_evidence_status: "verified",
            factor_validation_status: "accepted",
          },
        ],
      };
    }
    if (sql.includes("from mart.issuer_supply_chain_exposure")) {
      return {
        rows: [
          {
            run_id: "run-live-20261019",
            cutoff: "2026-06-30T23:59:59Z",
            exposure_score: "0.72",
            direct_partners: 6,
            suppliers_count: 3,
            customers_count: 3,
            max_partner_share: "0.34",
            confidence: "0.85",
            reason_codes: ["supplier:tsmc", "customer:msft"],
            extractor: "graph:kg-edges:v1",
            availability_status: "available",
            source_evidence_status: "verified",
            factor_validation_status: "accepted",
          },
        ],
      };
    }
    return { rows: [] };
  },
};

const seededDecisions: StrategyRunDecision[] = [
  {
    issuer_id: "issuer:adm",
    cutoff_at: "2026-06-30T23:59:59Z",
    outcome: "selected",
    eligible: true,
    tier: "large_model_native",
    capital_adjusted_labor_efficiency: "825000.00",
    current_price_to_sales: "4.50",
    target_price_to_sales: null,
    current_price_to_gross_profit: "11.20",
    target_price_to_gross_profit: "15.00",
    valuation_gap: "0.3393",
    confidence: "0.91",
    exclusion_reason: null,
    rank: 1,
    target_weight: "0.08",
    peg: "1.42",
    peg_rank: 2,
    labor_efficiency_metric: "gppe_non_financial_v1",
  },
  {
    issuer_id: "issuer:qqq-only",
    cutoff_at: "2026-06-30T23:59:59Z",
    outcome: "selected",
    eligible: true,
    tier: "tech",
    capital_adjusted_labor_efficiency: "640000.00",
    current_price_to_sales: "6.10",
    target_price_to_sales: null,
    current_price_to_gross_profit: "12.50",
    target_price_to_gross_profit: "16.00",
    valuation_gap: "0.2800",
    confidence: "0.87",
    exclusion_reason: null,
    rank: 2,
    target_weight: "0.05",
    peg: null,
    peg_rank: null,
    labor_efficiency_metric: "gppe_non_financial_v1",
  },
];

const seededProvenance = new Map<string, DecisionProvenance>([
  [
    "issuer:adm",
    {
      operating_period_end: "2025-12-31",
      revenue_period_end: "2025-12-31",
      shares_period_end: "2025-12-31",
      universe_version: "universe:topt-us-2026-06-30",
      universe_sha256: "a".repeat(64),
      gppe_definition_sha256: "b".repeat(64),
      tier_definition_sha256: "c".repeat(64),
      peg_reason_codes: ["negative_earnings"],
    },
  ],
  [
    "issuer:qqq-only",
    {
      operating_period_end: "2025-12-31",
      revenue_period_end: "2025-12-31",
      shares_period_end: "2025-12-31",
      universe_version: "universe:qqq-us-2026-06-30",
      universe_sha256: "d".repeat(64),
      gppe_definition_sha256: "b".repeat(64),
      tier_definition_sha256: "c".repeat(64),
      peg_reason_codes: ["growth_cagr_non_positive"],
    },
  ],
]);

const seededReport: StrategyRunReport & { provenance: ReadonlyMap<string, DecisionProvenance> } = {
  strategy_id: "large_model_value_v0",
  source: "mart",
  corpus_sha256: "f".repeat(64),
  decisions: seededDecisions,
  provenance: seededProvenance,
  golden_mismatches: [],
};

const adapter = new StrategyRunReadAdapter(
  {
    getLatest: async () => seededReport,
  },
  (fn) => fn(fakeClient),
);

{
  const state = await loadEntityDetail(TEST_CONTEXT, "issuer:adm", adapter);
  assert(state.kind === "ready", `expected ready entity detail, got ${state.kind}`);

  // Verify bidirectional alias resolution predicate in all 4 entity detail queries
  assert(executedSqls.length === 4, `expected 4 entity detail queries, got ${executedSqls.length}`);
  for (const sql of executedSqls) {
    assert(
      sql.includes("select entity_id::text from mart.entity_identity where legacy_id = $1") &&
        sql.includes("select legacy_id from mart.entity_identity where entity_id::text = $1"),
      "entity detail queries must resolve issuer aliases bidirectionally (legacy_id <-> entity_id) via bidirectionalIssuerPredicate",
    );
  }

  // AC1 assertions: Module 4 Analyst Ratings & Consensus
  const m4 = state.data.analystRatings;
  assert(m4 !== null && m4 !== undefined, "AC1: analystRatings must be loaded by loadEntityDetail");
  assert(m4.consensusRating === "4.35", `AC1: expected consensusRating 4.35, got ${m4.consensusRating}`);
  assert(m4.targetPrice === "195.00", `AC1: expected targetPrice 195.00, got ${m4.targetPrice}`);
  assert(m4.analystsCount === 18, `AC1: expected analystsCount 18, got ${m4.analystsCount}`);
  assert(m4.buyCount === 12 && m4.holdCount === 5 && m4.sellCount === 1, "AC1: expected buy/hold/sell counts 12/5/1");
  assert(m4.extractor === "origin:moomoo:v1", `AC1: expected extractor origin:moomoo:v1, got ${m4.extractor}`);
  assert(m4.availabilityStatus === "available", "AC1: expected availabilityStatus available");

  // AC2 assertions: Module 3 Supply-Chain Exposure & Scenario Graph
  const m3 = state.data.supplyChainExposure;
  assert(m3 !== null && m3 !== undefined, "AC2: supplyChainExposure must be loaded by loadEntityDetail");
  assert(m3.exposureScore === "0.72", `AC2: expected exposureScore 0.72, got ${m3.exposureScore}`);
  assert(m3.directPartners === 6, `AC2: expected directPartners 6, got ${m3.directPartners}`);
  assert(m3.suppliersCount === 3 && m3.customersCount === 3, "AC2: expected directional suppliers=3 and customers=3");
  assert(m3.maxPartnerShare === "0.34", `AC2: expected maxPartnerShare 0.34, got ${m3.maxPartnerShare}`);
  assert(m3.extractor === "graph:kg-edges:v1", `AC2: expected extractor graph:kg-edges:v1, got ${m3.extractor}`);
  assert(m3.reasonCodes.includes("supplier:tsmc") && m3.reasonCodes.includes("customer:msft"), "AC2: expected partner edge reason codes");
}

// ─── AC3: Module 1 PEG Conventions, Universe Filtering & SSOT Helpers ────────
{
  assert(
    PEG_GROWTH_CONVENTIONS.join(",") === "historical_cagr,consensus,guidance",
    "AC3: PEG_GROWTH_CONVENTIONS must define historical_cagr, consensus, guidance",
  );
  assert(normalizePegConvention(undefined) === "historical_cagr", "AC3: default convention must be historical_cagr");
  assert(normalizePegConvention("consensus") === "consensus", "AC3: consensus must normalize to consensus");
  assert(normalizePegConvention("analyst_consensus") === "consensus", "AC3: legacy analyst_consensus must normalize to consensus");
  assert(normalizePegConvention("guidance") === "guidance", "AC3: guidance must normalize to guidance");
  assert(normalizePegConvention("company_guidance") === "guidance", "AC3: legacy company_guidance must normalize to guidance");
  assert(PEG_CONVENTION_LABELS.historical_cagr === "Historical CAGR", "AC3: label for historical_cagr");
  assert(DECISIONS_SQL.includes("d.peg_reason_codes"), "AC3: DECISIONS_SQL must select d.peg_reason_codes");

  const cagrProjected = projectPegByConvention(
    { peg: "1.42", pegRank: 2, pegReasonCodes: ["ok"] },
    "historical_cagr",
  );
  assert(cagrProjected.peg === "1.42" && cagrProjected.pegRank === 2, "AC3: historical_cagr preserves materialized peg and pegRank");
  assert(cagrProjected.pegReasonCodes.join(",") === "ok", "AC3: historical_cagr preserves materialized reason codes");

  const consensusProjected = projectPegByConvention(
    { peg: "1.42", pegRank: 2, pegReasonCodes: ["ok"] },
    "consensus",
  );
  assert(consensusProjected.peg === null && consensusProjected.pegRank === null, "AC3: non-historical convention without materialized column returns null peg/pegRank");
  assert(
    consensusProjected.pegReasonCodes.includes(`${GROWTH_CONVENTION_UNSOURCED_PREFIX}consensus`),
    "AC3: non-historical convention appends explicit growth_convention_unsourced:consensus code",
  );

  // SSOT helper: RankingUniverse & end-to-end universe filtering in loadRanking
  assert(RANKING_UNIVERSES.join(",") === "topt,qqq", "SSOT: RANKING_UNIVERSES must define topt,qqq");
  assert(normalizeRankingUniverse(undefined) === "topt", "SSOT: default universe must be topt");
  assert(normalizeRankingUniverse("QQQ") === "qqq", "SSOT: QQQ normalizes to qqq");
  assert(RANKING_UNIVERSE_LABELS.qqq.includes("QQQ"), "SSOT: qqq label must describe QQQ universe");
  assert(matchesRankingUniverse("universe:topt-us-2026-06-30", "topt"), "SSOT: topt matches universe:topt-*");
  assert(!matchesRankingUniverse("universe:topt-us-2026-06-30", "qqq"), "SSOT: qqq rejects universe:topt-*");
  assert(matchesRankingUniverse("universe:qqq-us-2026-06-30", "qqq"), "SSOT: qqq matches universe:qqq-*");
  assert(!matchesRankingUniverse("universe:qqq-us-2026-06-30", "topt"), "SSOT: topt rejects universe:qqq-*");

  const toptRanking = await loadRanking(TEST_CONTEXT, { universe: "topt" }, adapter);
  assert(toptRanking.kind === "ready", "AC3: loadRanking(topt) must return ready");
  assert(
    toptRanking.data.universe === "topt" &&
      toptRanking.data.rows.length === 1 &&
      toptRanking.data.rows[0].issuerId === "issuer:adm",
    `AC3: loadRanking(topt) must filter to TOPT rows only, got ${toptRanking.data.rows.map((r) => r.issuerId).join(",")}`,
  );

  const qqqRanking = await loadRanking(TEST_CONTEXT, { universe: "qqq" }, adapter);
  assert(qqqRanking.kind === "ready", "AC3: loadRanking(qqq) must return ready for QQQ rows");
  assert(
    qqqRanking.data.universe === "qqq" &&
      qqqRanking.data.rows.length === 1 &&
      qqqRanking.data.rows[0].issuerId === "issuer:qqq-only",
    `AC3: loadRanking(qqq) must filter to QQQ rows only, got ${qqqRanking.data.rows.map((r) => r.issuerId).join(",")}`,
  );

  // When a run has only TOPT rows (like the fixture), selecting universe=qqq honestly returns empty
  const fixtureOnlyAdapter = new StrategyRunReadAdapter(
    {
      getLatest: (strategyId, ctx) => new FixtureStrategyRunRepository().getLatest(strategyId, ctx),
    },
    async (fn) => fn({ query: async () => ({ rows: [] }) }),
  );
  const emptyQqqRanking = await loadRanking(TEST_CONTEXT, { universe: "qqq" }, fixtureOnlyAdapter);
  assert(
    emptyQqqRanking.kind === "empty",
    `AC3: loadRanking(qqq) against a TOPT-only run must return empty instead of mislabeling TOPT rows, got ${emptyQqqRanking.kind}`,
  );

  assert(tierKey("large_model_native") === "lm-native" && formatTierLabel("large_model_native") === "Large Model Native", "SSOT: formatTierLabel formats Large Model Native");
  assert(tierKey("tech_enabled") === "tech" && formatTierLabel("tech_enabled") === "Tech", "SSOT: formatTierLabel formats Tech");
  assert(tierKey("traditional") === "traditional" && formatTierLabel("traditional") === "Traditional", "SSOT: formatTierLabel formats Traditional");
  assert(formatTierLabel(null, "-") === "-", "SSOT: formatTierLabel honors custom fallbackGlyph");
  for (const status of ["available", "unavailable", "stale", "excluded", "low_confidence", "error"] as const) {
    assert(toAvailabilityStatus(status) === status, `SSOT: toAvailabilityStatus must preserve ${status}`);
  }
  assert(toAvailabilityStatus("unknown_status") === "unavailable", "SSOT: toAvailabilityStatus falls back to unavailable");

  const prov = provenanceFromRow({
    operating_period_end: "2025-12-31",
    revenue_period_end: "2025-12-31",
    shares_period_end: "2025-12-31",
    universe_version: "v1",
    universe_sha256: "a".repeat(64),
    gppe_definition_sha256: "b".repeat(64),
    tier_definition_sha256: "c".repeat(64),
    peg_reason_codes: ["negative_earnings", "growth_cagr_non_positive"],
  });
  assert(
    prov.peg_reason_codes?.join(",") === "negative_earnings,growth_cagr_non_positive",
    "AC3: provenanceFromRow must preserve peg_reason_codes from mart.strategy_decisions",
  );
}

// ─── AC4: 1080x1440 PNG Export Route + Library Persistence ───────────────────
const savedDocs: Array<{ doc: ResearchDocument; rev: ResearchDocumentRevision }> = [];
const fakeDocumentsRepo: DocumentsRepository = {
  async listDocuments(): Promise<DocumentPage> {
    return { documents: savedDocs.map((d) => d.doc), nextBefore: null };
  },
  async getDocument(_ctx, documentId) {
    return savedDocs.find((d) => d.doc.documentId === documentId)?.doc ?? null;
  },
  async listRevisions(_ctx, documentId) {
    return savedDocs.filter((d) => d.doc.documentId === documentId).map((d) => d.rev);
  },
  async createDocument(ctx, input: NewDocumentRevisionInput) {
    assertStableId(input.sourceArtifactId, "sourceArtifactId");
    const idx = savedDocs.length + 136;
    const doc: ResearchDocument = {
      documentId: `document:00000000-0000-4000-8000-000000000${idx}`,
      tenantId: ctx.tenantId,
      ownerPrincipalId: ctx.principalId,
      createdAt: "2026-10-19T12:00:00.000Z",
    };
    const rev: ResearchDocumentRevision = {
      revisionId: `revision:00000000-0000-4000-8000-000000000${idx}`,
      documentId: doc.documentId,
      tenantId: ctx.tenantId,
      ownerPrincipalId: ctx.principalId,
      sourceArtifactId: input.sourceArtifactId,
      artifactSha256: "e".repeat(64),
      artifactByteLength: input.bytes.byteLength,
      artifactContentType: input.contentType,
      createdAt: doc.createdAt,
    };
    savedDocs.push({ doc, rev });
    return { document: doc, revision: rev };
  },
  async appendRevision() {
    throw new Error("not used");
  },
  async tombstoneDocument() {
    throw new Error("not used");
  },
  async issueDownloadTicket() {
    throw new Error("not used");
  },
  async redeemDownloadTicket() {
    return null;
  },
};

{
  // 1. Default format on /research/api/cards/export is PNG (1080x1440) and persists to Library
  const rankingResponse = await handleCardExportGet(
    new Request("http://localhost:3000/research/api/cards/export?kind=ranking"),
    {
      resolvePrincipal: async () => ({ context: TEST_CONTEXT }),
      adapter,
      documents: fakeDocumentsRepo,
      displayNames: new Map(),
    },
  );
  assert(rankingResponse.status === 200, `AC4: expected status 200, got ${rankingResponse.status}`);
  assert(
    rankingResponse.headers.get("content-type") === "image/png",
    `AC4: expected content-type image/png, got ${rankingResponse.headers.get("content-type")}`,
  );
  assert(
    rankingResponse.headers.get("x-research-document-id") === "document:00000000-0000-4000-8000-000000000136",
    "AC4: exported PNG card must be persisted to Library and return X-Research-Document-Id header",
  );
  const pngBytes = Buffer.from(await rankingResponse.arrayBuffer());
  assert(pngBytes.subarray(0, 8).toString("hex") === "89504e470d0a1a0a", "AC4: response body must be valid PNG");
  assert(pngBytes.readUInt32BE(16) === XHS_CARD_WIDTH, "AC4: response PNG width must be 1080");
  assert(pngBytes.readUInt32BE(20) === XHS_CARD_HEIGHT, "AC4: response PNG height must be 1440");
  assert((savedDocs.length as number) === 1, "AC4: Library repository must hold the saved card document");
  assert(savedDocs[0].rev.artifactContentType === "image/png", "AC4: saved revision contentType must be image/png");
  assert(
    savedDocs[0].rev.sourceArtifactId.endsWith(":topt:historical-cagr:png"),
    `AC4: ranking sourceArtifactId must include universe, convention, and format, got ${savedDocs[0].rev.sourceArtifactId}`,
  );

  // 2. Entity PNG export with convention=consensus
  const entityResponse = await handleCardExportGet(
    new Request("http://localhost:3000/research/api/cards/export?kind=entity&issuer=issuer:adm&convention=consensus&format=png"),
    {
      resolvePrincipal: async () => ({ context: TEST_CONTEXT }),
      adapter,
      documents: fakeDocumentsRepo,
      displayNames: new Map(),
    },
  );
  assert(entityResponse.status === 200, `AC4: expected entity PNG status 200, got ${entityResponse.status}`);
  assert(entityResponse.headers.get("content-type") === "image/png", "AC4: entity card export must return image/png");
  const entityPngBytes = Buffer.from(await entityResponse.arrayBuffer());
  assert(entityPngBytes.readUInt32BE(16) === XHS_CARD_WIDTH && entityPngBytes.readUInt32BE(20) === XHS_CARD_HEIGHT, "AC4: entity PNG must be 1080x1440");
  assert((savedDocs.length as number) === 2, "AC4: Library repository must hold both ranking and entity card documents");
  assert(
    savedDocs[1].rev.sourceArtifactId.endsWith(":consensus:png"),
    `AC4: entity sourceArtifactId must include convention and format, got ${savedDocs[1].rev.sourceArtifactId}`,
  );

  // 3. Entity JSON export includes Module 4 analyst_ratings, Module 3 supply_chain_exposure, and projected peg_convention
  const entityJsonResponse = await handleCardExportGet(
    new Request("http://localhost:3000/research/api/cards/export?kind=entity&issuer=issuer:adm&convention=consensus&format=json"),
    {
      resolvePrincipal: async () => ({ context: TEST_CONTEXT }),
      adapter,
      documents: fakeDocumentsRepo,
      displayNames: new Map(),
    },
  );
  const entityJson = (await entityJsonResponse.json()) as {
    metrics: { peg: string | null; peg_convention: string };
    analyst_ratings: { consensusRating: string; targetPrice: string } | null;
    supply_chain_exposure: { exposureScore: string; directPartners: number } | null;
  };
  assert(entityJson.metrics.peg === null && entityJson.metrics.peg_convention === "consensus", "AC3/AC4: consensus convention projects null peg and records consensus convention");
  assert(entityJson.analyst_ratings?.consensusRating === "4.35" && entityJson.analyst_ratings?.targetPrice === "195.00", "AC1/AC4: entity card JSON includes Module 4 analyst_ratings");
  assert(entityJson.supply_chain_exposure?.exposureScore === "0.72" && entityJson.supply_chain_exposure?.directPartners === 6, "AC2/AC4: entity card JSON includes Module 3 supply_chain_exposure");

  const routeModule = await import("../src/app/research/api/cards/export/route");
  const exportedKeys = Object.keys(routeModule).sort();
  assert(
    exportedKeys.join(",") === "GET,dynamic",
    `AC4: route.ts must export only valid Next.js Route fields (GET,dynamic), got ${exportedKeys.join(",")}`,
  );
}

// ─── SSR Integration Verification (RankingsPage, EntityDetailPage, LibraryPage, CardExportButton) ─
{
  const displayMap = new Map<string, EntityDisplay>([
    ["issuer:adm", { ticker: "ADM", displayName: "ARCHER-DANIELS-MIDLAND CO" }],
    ["issuer:qqq-only", { ticker: "QQQ1", displayName: "NASDAQ CONSTITUENT CORP" }],
  ]);

  mock.module("@/server/auth/request-context", () => ({
    getServerPrincipal: async () => ({
      userId: "user-root-c",
      email: "owner@truealpha.test",
      role: "administrator",
      context: TEST_CONTEXT,
    }),
  }));

  mock.module("@/server/mart/entity-resolution", () => ({
    loadEntityDisplayMap: async () => displayMap,
    entityLabel: (id: string, map: ReadonlyMap<string, EntityDisplay>) => {
      const entry = map.get(id);
      return entry ? `${entry.ticker} · ${entry.displayName}` : id;
    },
  }));

  const realLoadRanking = loadRanking;
  const realLoadEntityDetail = loadEntityDetail;

  mock.module("@/server/dashboard", () => ({
    loadRanking: (ctx: AccessContext | null, params: Parameters<typeof realLoadRanking>[1]) =>
      realLoadRanking(ctx, params, adapter),
    loadEntityDetail: (ctx: AccessContext | null, issuerId: string) =>
      realLoadEntityDetail(ctx, issuerId, adapter),
  }));

  mock.module("@/server/documents", () => ({
    assertStableId,
    PostgresDocumentsRepository: class {
      listDocuments = fakeDocumentsRepo.listDocuments;
    },
  }));

  const { default: RankingsPage } = await import("../src/app/research/rankings/page");
  const { default: EntityDetailPage } = await import("../src/app/research/entities/[id]/page");
  const { default: LibraryPage } = await import("../src/app/research/library/page");

  // 1. RankingsPage SSR: default (topt + historical_cagr)
  const rankingsDefaultHtml = renderToStaticMarkup(
    await RankingsPage({ searchParams: Promise.resolve({}) }),
  );
  assert(
    rankingsDefaultHtml.includes('data-peg-convention="historical_cagr"') &&
      rankingsDefaultHtml.includes('data-universe-selector="topt"'),
    "AC3 SSR: RankingsPage must render data-peg-convention and data-universe-selector controls",
  );
  assert(
    rankingsDefaultHtml.includes(">Large Model Native</td>"),
    `SSOT SSR: RankingsPage must format row.tier via formatTierLabel ('Large Model Native'), got: ${rankingsDefaultHtml}`,
  );
  assert(
    rankingsDefaultHtml.includes(">ADM · ARCHER-DANIELS-MIDLAND CO<") &&
      !rankingsDefaultHtml.includes("NASDAQ CONSTITUENT CORP"),
    "AC3 SSR: RankingsPage (topt) must render TOPT rows and exclude QQQ-only rows",
  );
  assert(
    rankingsDefaultHtml.includes(">1.42<") && rankingsDefaultHtml.includes(">2</td>"),
    "AC3 SSR: RankingsPage (historical_cagr) must render materialized PEG 1.42 and PEG rank 2",
  );
  assert(
    rankingsDefaultHtml.includes('data-export-format="png"') &&
      rankingsDefaultHtml.includes('data-export-convention="historical_cagr"') &&
      rankingsDefaultHtml.includes('data-export-universe="topt"'),
    "AC4 SSR: RankingsPage CardExportButton must default to format=png, convention=historical_cagr, universe=topt",
  );

  // 2. RankingsPage SSR: consensus convention + qqq universe
  const rankingsQqqConsensusHtml = renderToStaticMarkup(
    await RankingsPage({ searchParams: Promise.resolve({ convention: "consensus", universe: "qqq" }) }),
  );
  assert(
    rankingsQqqConsensusHtml.includes(">QQQ1 · NASDAQ CONSTITUENT CORP<") &&
      !rankingsQqqConsensusHtml.includes("ARCHER-DANIELS-MIDLAND CO"),
    "AC3 SSR: RankingsPage (universe=qqq) must filter to QQQ rows via loadRanking",
  );
  assert(
    rankingsQqqConsensusHtml.includes(">Tech</td>"),
    "SSOT SSR: RankingsPage (qqq) must format 'tech' tier as 'Tech'",
  );
  assert(
    rankingsQqqConsensusHtml.includes("growth_convention_unsourced:consensus"),
    "AC3 SSR: RankingsPage (convention=consensus) must render growth_convention_unsourced:consensus reason code",
  );
  assert(
    rankingsQqqConsensusHtml.includes('data-export-convention="consensus"') &&
      rankingsQqqConsensusHtml.includes('data-export-universe="qqq"'),
    "AC4 SSR: RankingsPage CardExportButton must forward active convention=consensus and universe=qqq",
  );

  // 3. EntityDetailPage SSR: Module 4 (AC1), Module 3 (AC2), Module 1 (AC3)
  const entityDefaultHtml = renderToStaticMarkup(
    await EntityDetailPage({
      params: Promise.resolve({ id: "issuer%3Aadm" }),
      searchParams: Promise.resolve({}),
    }),
  );
  assert(
    entityDefaultHtml.includes('data-testid="module-4-analyst-ratings"') &&
      entityDefaultHtml.includes('data-testid="m4-consensus-rating"') &&
      entityDefaultHtml.includes(">4.35<") &&
      entityDefaultHtml.includes('data-testid="m4-target-price"') &&
      entityDefaultHtml.includes(">$195.00<") &&
      entityDefaultHtml.includes('data-testid="m4-analysts-count">18<') &&
      entityDefaultHtml.includes('Buy: <span class="font-bold">12</span>') &&
      entityDefaultHtml.includes('Hold: <span class="font-bold">5</span>') &&
      entityDefaultHtml.includes('Sell: <span class="font-bold">1</span>') &&
      entityDefaultHtml.includes('data-testid="m4-extractor">origin:moomoo:v1<'),
    `AC1 SSR: EntityDetailPage must render Module 4 analyst ratings fields: ${entityDefaultHtml}`,
  );
  assert(
    entityDefaultHtml.includes('data-testid="module-3-supply-chain"') &&
      entityDefaultHtml.includes('data-testid="m3-exposure-score"') &&
      entityDefaultHtml.includes(">0.72<") &&
      entityDefaultHtml.includes('data-testid="m3-direct-partners">6<') &&
      entityDefaultHtml.includes('data-testid="m3-max-partner-share"') &&
      entityDefaultHtml.includes(">34%<") &&
      entityDefaultHtml.includes('data-testid="m3-suppliers-count">3<') &&
      entityDefaultHtml.includes('data-testid="m3-customers-count">3<') &&
      entityDefaultHtml.includes('data-testid="m3-extractor">graph:kg-edges:v1<') &&
      entityDefaultHtml.includes("supplier:tsmc, customer:msft"),
    `AC2 SSR: EntityDetailPage must render Module 3 supply-chain exposure fields: ${entityDefaultHtml}`,
  );
  assert(
    entityDefaultHtml.includes('data-testid="entity-peg-convention-selector"') &&
      entityDefaultHtml.includes(">1.42<") &&
      entityDefaultHtml.includes(">#2<"),
    "AC3 SSR: EntityDetailPage (historical_cagr) must render PEG selector, 1.42 ratio, and #2 rank",
  );

  // 4. EntityDetailPage SSR: guidance convention
  const entityGuidanceHtml = renderToStaticMarkup(
    await EntityDetailPage({
      params: Promise.resolve({ id: "issuer%3Aadm" }),
      searchParams: Promise.resolve({ convention: "guidance" }),
    }),
  );
  assert(
    entityGuidanceHtml.includes("Convention: Guidance (guidance)") &&
      entityGuidanceHtml.includes("growth_convention_unsourced:guidance") &&
      entityGuidanceHtml.includes('data-export-convention="guidance"'),
    "AC3/AC4 SSR: EntityDetailPage (convention=guidance) must render guidance reason code and forward convention to CardExportButton",
  );

  // 5. CardExportButton & LibraryPage SSR
  const buttonHtml = renderToStaticMarkup(
    createElement(CardExportButton, { issuerId: "issuer:adm", convention: "consensus" }),
  );
  assert(
    buttonHtml.includes('data-export-format="png"') &&
      buttonHtml.includes('data-export-kind="entity"') &&
      buttonHtml.includes('data-export-convention="consensus"'),
    "AC4 SSR: CardExportButton must render data-export-* attributes with PNG default",
  );

  const libraryHtml = renderToStaticMarkup(await LibraryPage());
  assert(
    libraryHtml.includes('data-evidence="document:00000000-0000-4000-8000-000000000136"') &&
      libraryHtml.includes('data-evidence="document:00000000-0000-4000-8000-000000000137"') &&
      libraryHtml.includes("save a copy here automatically (#1360)"),
    `AC4 SSR: LibraryPage must render persisted card documents wrapped in data-evidence: ${libraryHtml}`,
  );
}

console.log("#1360 root-c-reader-surfaces.test.ts: all AC1-AC4 SSR and SSOT assertions passed");
