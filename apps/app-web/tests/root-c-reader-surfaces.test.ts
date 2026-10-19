/**
 * #1360: Standing acceptance checks for Root C reader surfaces (M1, M3, M4)
 * and 1080x1440 PNG card export + Library persistence.
 *
 * Verifies:
 * - AC1: Module 4 (`mart.issuer_analyst_ratings`) read adapter, dashboard loader,
 *   and entity detail UI rendering (`consensusRating`, `targetPrice`, `buyCount`,
 *   `holdCount`, `sellCount`, `extractor`, `availabilityStatus`, `reasonCodes`).
 * - AC2: Module 3 (`mart.issuer_supply_chain_exposure`) read adapter, dashboard loader,
 *   and entity detail UI rendering (`exposureScore`, `directPartners`, `suppliersCount`,
 *   `customersCount`, `maxPartnerShare`, `extractor`, `availabilityStatus`, `reasonCodes`).
 * - AC3: Module 1 PEG growth-rate convention contract (`historical_cagr`, `consensus`,
 *   `guidance`), `peg_reason_codes` projection from `DECISIONS_SQL` -> `DecisionProvenance`
 *   -> `RankingRow` / `ComparisonRow`, and selector wiring on `/research/rankings` and
 *   `/research/entities/[id]`.
 * - AC4: `/research/api/cards/export` `GET` route handler (`format=png` default,
 *   `Content-Type: image/png`, `1080x1440` PNG magic/IHDR header, and owner-scoped
 *   Library persistence via `DocumentsRepository.createDocument`), plus `CardExportButton`
 *   PNG default and `data-evidence` UUID wrapping on `/research/library`.
 *
 * Run standalone: `bun run tests/root-c-reader-surfaces.test.ts`.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import {
  FixtureStrategyRunRepository,
  normalizePegConvention,
  PEG_CONVENTION_LABELS,
  PEG_GROWTH_CONVENTIONS,
  type AccessContext,
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
import { StrategyRunReadAdapter } from "../src/server/mart/research-read";
import { DECISIONS_SQL, provenanceFromRow } from "../src/server/mart/strategy-run-repository";
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
{
  const fakeClient: MartClientLike = {
    query: async (sql: string) => {
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

  const adapter = new StrategyRunReadAdapter(
    {
      getLatest: (strategyId, ctx) => new FixtureStrategyRunRepository().getLatest(strategyId, ctx),
    },
    (fn) => fn(fakeClient),
  );

  const state = await loadEntityDetail(TEST_CONTEXT, "issuer:adm", adapter);
  assert(state.kind === "ready", `expected ready entity detail, got ${state.kind}`);

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

// ─── AC3: Module 1 PEG Conventions & peg_reason_codes Projection ─────────────
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

  const rankingsPageSource = readFileSync(join(process.cwd(), "src/app/research/rankings/page.tsx"), "utf8");
  assert(rankingsPageSource.includes("data-peg-convention"), "AC3: /research/rankings must render data-peg-convention selector");
  assert(rankingsPageSource.includes("data-universe-selector"), "AC3: /research/rankings must render data-universe-selector (TOPT / QQQ)");

  const entityPageSource = readFileSync(join(process.cwd(), "src/app/research/entities/[id]/page.tsx"), "utf8");
  assert(entityPageSource.includes("data-testid=\"module-4-analyst-ratings\""), "AC1: entity page must render module-4-analyst-ratings section");
  assert(entityPageSource.includes("data-testid=\"module-3-supply-chain\""), "AC2: entity page must render module-3-supply-chain section");
  assert(entityPageSource.includes("data-testid=\"entity-peg-convention-selector\""), "AC3: entity page must render entity-peg-convention-selector");
}

// ─── AC4: 1080x1440 PNG Export Route + Library Persistence + Raw UUID Guard ─
{
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
      // Must satisfy the real STABLE_ID_PATTERN and reject mutable tokens!
      assertStableId(input.sourceArtifactId, "sourceArtifactId");
      const doc: ResearchDocument = {
        documentId: "document:00000000-0000-4000-8000-000000000136",
        tenantId: ctx.tenantId,
        ownerPrincipalId: ctx.principalId,
        createdAt: "2026-10-19T12:00:00.000Z",
      };
      const rev: ResearchDocumentRevision = {
        revisionId: "revision:00000000-0000-4000-8000-000000000137",
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

  const fakeMartAdapter = new StrategyRunReadAdapter(
    {
      getLatest: (strategyId, ctx) => new FixtureStrategyRunRepository().getLatest(strategyId, ctx),
    },
    async (fn) => fn({ query: async () => ({ rows: [] }) }),
  );

  // 1. Default format on /research/api/cards/export is PNG (1080x1440) and persists to Library
  const rankingResponse = await handleCardExportGet(
    new Request("http://localhost:3000/research/api/cards/export?kind=ranking"),
    {
      resolvePrincipal: async () => ({ context: TEST_CONTEXT }),
      adapter: fakeMartAdapter,
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

  // 2. Entity PNG export
  const entityResponse = await handleCardExportGet(
    new Request("http://localhost:3000/research/api/cards/export?kind=entity&issuer=issuer:adm&format=png"),
    {
      resolvePrincipal: async () => ({ context: TEST_CONTEXT }),
      adapter: fakeMartAdapter,
      documents: fakeDocumentsRepo,
      displayNames: new Map(),
    },
  );
  assert(entityResponse.status === 200, `AC4: expected entity PNG status 200, got ${entityResponse.status}`);
  assert(entityResponse.headers.get("content-type") === "image/png", "AC4: entity card export must return image/png");
  const entityPngBytes = Buffer.from(await entityResponse.arrayBuffer());
  assert(entityPngBytes.readUInt32BE(16) === XHS_CARD_WIDTH && entityPngBytes.readUInt32BE(20) === XHS_CARD_HEIGHT, "AC4: entity PNG must be 1080x1440");
  assert((savedDocs.length as number) === 2, "AC4: Library repository must hold both ranking and entity card documents");

  // 3. CardExportButton defaults to format=png and Library wraps documentId in data-evidence
  const buttonSource = readFileSync(join(process.cwd(), "src/components/card-export-button.tsx"), "utf8");
  assert(
    buttonSource.includes('format = "png"') && buttonSource.includes("truealpha-card-ranking.${ext}"),
    "AC4: CardExportButton must default to format=png",
  );
  const libraryPageSource = readFileSync(join(process.cwd(), "src/app/research/library/page.tsx"), "utf8");
  assert(
    libraryPageSource.includes("data-evidence={document.documentId}"),
    "AC4: /research/library/page.tsx must wrap document.documentId in data-evidence so UUIDs pass walk-tree.mjs",
  );
  const routeModule = await import("../src/app/research/api/cards/export/route");
  const exportedKeys = Object.keys(routeModule).sort();
  assert(
    exportedKeys.join(",") === "GET,dynamic",
    `AC4: route.ts must export only valid Next.js Route fields (GET,dynamic), got ${exportedKeys.join(",")}`,
  );
}

console.log("#1360 root-c-reader-surfaces.test.ts: all AC1-AC4 assertions passed");
