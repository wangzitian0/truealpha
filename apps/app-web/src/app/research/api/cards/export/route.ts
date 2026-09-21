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
import { loadRanking } from "@/server/dashboard";
import { entityLabel, loadEntityDisplayMap } from "@/server/mart/entity-resolution";
import {
  buildRankingCardJson,
  renderRankingCardSvg,
  type RankingCardData,
} from "@/server/cards/xhs-card";

export const dynamic = "force-dynamic";

export async function GET(request: Request): Promise<Response> {
  const principal = await getServerPrincipal();
  if (!principal) {
    return NextResponse.json({ error: "authentication required" }, { status: 401 });
  }

  const { searchParams } = new URL(request.url);
  const cutoff = searchParams.get("cutoff") ?? undefined;
  const format = searchParams.get("format") ?? "svg";

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
