/**
 * Export endpoint for Xiaohongshu research cards (1080x1440 3:4 portrait) — see #372 / #44 / #1360.
 *
 * GET /research/api/cards/export?kind=ranking&cutoff=...&format=png
 *
 * Emits deterministic 1080x1440 PNG (`image/png`), SVG (`image/svg+xml`), or JSON
 * derived from the materialized strategy run in mart, and persists exported card
 * artifacts to the owner-scoped `/research/library` repository when available.
 */

import { NextResponse } from "next/server";
import type { AccessContext } from "@/contracts/strategyRun";
import { loadEntityDetail, loadRanking, type MartAdapterLike } from "@/server/dashboard";
import { PostgresDocumentsRepository, type DocumentsRepository } from "@/server/documents";
import { entityLabel, loadEntityDisplayMap } from "@/server/mart/entity-resolution";
import {
  buildEntityCardJson,
  buildRankingCardJson,
  entityCardThemes,
  renderEntityDeepDiveCardPng,
  renderEntityDeepDiveCardSvg,
  renderRankingCardPng,
  renderRankingCardSvg,
  type EntityCardData,
  type RankingCardData,
} from "@/server/cards/xhs-card";

export const dynamic = "force-dynamic";

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

    if (format === "svg") {
      const svg = renderEntityDeepDiveCardSvg(cardData);
      const filename = `truealpha-card-entity-${state.data.issuerId.replace(/[:]/g, "-")}.svg`;
      const docId = persist
        ? await persistCardDocument(
            documents,
            principal.context,
            artifactId,
            Buffer.from(svg, "utf8"),
            "image/svg+xml",
          )
        : null;
      const headers: Record<string, string> = {
        "Content-Type": "image/svg+xml; charset=utf-8",
        "Content-Disposition": `attachment; filename="${filename}"`,
        "Cache-Control": "public, max-age=300",
      };
      if (docId) headers["X-Research-Document-Id"] = docId;
      return new Response(svg, { status: 200, headers });
    }

    const png = await renderEntityDeepDiveCardPng(cardData);
    const filename = `truealpha-card-entity-${state.data.issuerId.replace(/[:]/g, "-")}.png`;
    const docId = persist
      ? await persistCardDocument(documents, principal.context, artifactId, png, "image/png")
      : null;
    const headers: Record<string, string> = {
      "Content-Type": "image/png",
      "Content-Disposition": `attachment; filename="${filename}"`,
      "Cache-Control": "public, max-age=300",
    };
    if (docId) headers["X-Research-Document-Id"] = docId;

    return new Response(new Uint8Array(png), {
      status: 200,
      headers,
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

  if (format === "svg") {
    const svg = renderRankingCardSvg(cardData);
    const filename = `truealpha-card-ranking-${state.data.cutoffAt.replace(/[:T]/g, "-")}.svg`;
    const docId = persist
      ? await persistCardDocument(
          documents,
          principal.context,
          artifactId,
          Buffer.from(svg, "utf8"),
          "image/svg+xml",
        )
      : null;
    const headers: Record<string, string> = {
      "Content-Type": "image/svg+xml; charset=utf-8",
      "Content-Disposition": `attachment; filename="${filename}"`,
      "Cache-Control": "public, max-age=300",
    };
    if (docId) headers["X-Research-Document-Id"] = docId;

    return new Response(svg, {
      status: 200,
      headers,
    });
  }

  const png = await renderRankingCardPng(cardData);
  const filename = `truealpha-card-ranking-${state.data.cutoffAt.replace(/[:T]/g, "-")}.png`;
  const docId = persist
    ? await persistCardDocument(documents, principal.context, artifactId, png, "image/png")
    : null;
  const headers: Record<string, string> = {
    "Content-Type": "image/png",
    "Content-Disposition": `attachment; filename="${filename}"`,
    "Cache-Control": "public, max-age=300",
  };
  if (docId) headers["X-Research-Document-Id"] = docId;

  return new Response(new Uint8Array(png), {
    status: 200,
    headers,
  });
}

export async function GET(request: Request): Promise<Response> {
  return handleCardExportGet(request);
}
