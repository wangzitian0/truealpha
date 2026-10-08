/**
 * Export endpoint for Xiaohongshu research cards (1080x1440 3:4 portrait) — see #372 / #44.
 *
 * GET /research/api/cards/export?kind=ranking&cutoff=...&format=svg
 *
 * Emits either deterministic SVG (1080x1440) or JSON representation derived from
 * the materialized strategy run in mart.
 */

import { NextResponse } from "next/server";
import { getServerPrincipal } from "@/server/auth/request-context";
import { loadEntityDetail, loadRanking } from "@/server/dashboard";
import { entityLabel, loadEntityDisplayMap } from "@/server/mart/entity-resolution";
import {
  buildEntityCardJson,
  buildRankingCardJson,
  renderEntityDeepDiveCardSvg,
  renderRankingCardSvg,
  type EntityCardData,
  type RankingCardData,
} from "@/server/cards/xhs-card";

export const dynamic = "force-dynamic";

export async function GET(request: Request): Promise<Response> {
  const principal = await getServerPrincipal();
  if (!principal) {
    return NextResponse.json({ error: "authentication required" }, { status: 401 });
  }

  const { searchParams } = new URL(request.url);
  const kind = searchParams.get("kind") ?? "ranking";
  const cutoff = searchParams.get("cutoff") ?? undefined;
  const format = searchParams.get("format") ?? "svg";

  if (kind === "entity") {
    const issuerId = searchParams.get("issuer");
    if (!issuerId) {
      return NextResponse.json({ error: "missing issuer param" }, { status: 400 });
    }

    const state = await loadEntityDetail(principal.context, issuerId);
    if (state.kind !== "ready") {
      return NextResponse.json(
        { error: "entity detail not ready for card export", read_state: state.kind },
        { status: 404 },
      );
    }

    const names = await loadEntityDisplayMap();
    const latestRow = state.data.rows
      .slice()
      .sort((a, b) => b.cutoffAt.localeCompare(a.cutoffAt))[0];

    const cardData: EntityCardData = {
      issuerId: state.data.issuerId,
      displayName: entityLabel(state.data.issuerId, names),
      cutoffAt: latestRow?.cutoffAt ?? new Date().toISOString(),
      tier: latestRow?.tier ?? null,
      currentPriceToSales: latestRow?.currentPriceToSales ?? null,
      valuationGap: latestRow?.valuationGap ?? null,
      peg: latestRow?.peg ?? null,
      gppe: state.data.gppeDetail?.gppe ?? latestRow?.capitalAdjustedLaborEfficiency ?? null,
      operatingBranch: state.data.gppeDetail?.operatingBranch ?? null,
      themes: (state.data.themes ?? []).map((t) => ({ theme: t.theme, themeShare: t.themeShare })),
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

    const svg = renderEntityDeepDiveCardSvg(cardData);
    const filename = `truealpha-card-entity-${state.data.issuerId.replace(/[:]/g, "-")}.svg`;

    return new Response(svg, {
      status: 200,
      headers: {
        "Content-Type": "image/svg+xml; charset=utf-8",
        "Content-Disposition": `attachment; filename="${filename}"`,
        "Cache-Control": "public, max-age=300",
      },
    });
  }

  const state = await loadRanking(principal.context, { cutoffAt: cutoff, cursor: null });
  if (state.kind !== "ready") {
    return NextResponse.json(
      { error: "ranking data not ready for card export", read_state: state.kind },
      { status: 404 },
    );
  }

  const names = await loadEntityDisplayMap();

  const cardData: RankingCardData = {
    cutoffAt: state.data.cutoffAt,
    totalMembers: state.data.page.total,
    items: state.data.rows.map((row) => ({
      rank: row.rank,
      issuerId: row.issuerId,
      displayName: entityLabel(row.issuerId, names),
      tier: row.tier,
      currentPriceToSales: row.currentPriceToSales,
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

  const svg = renderRankingCardSvg(cardData);
  const filename = `truealpha-card-ranking-${state.data.cutoffAt.replace(/[:T]/g, "-")}.svg`;

  return new Response(svg, {
    status: 200,
    headers: {
      "Content-Type": "image/svg+xml; charset=utf-8",
      "Content-Disposition": `attachment; filename="${filename}"`,
      "Cache-Control": "public, max-age=300",
    },
  });
}
