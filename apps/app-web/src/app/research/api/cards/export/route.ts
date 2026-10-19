/**
 * Export endpoint for Xiaohongshu research cards (1080x1440 3:4 portrait) — see #372 / #44 / #1360.
 *
 * GET /research/api/cards/export?kind=ranking&cutoff=...&format=png
 *
 * Emits deterministic 1080x1440 PNG (`image/png`), SVG (`image/svg+xml`), or JSON
 * derived from the materialized strategy run in mart, and persists exported card
 * artifacts to the owner-scoped `/research/library` repository when available.
 */

import { handleCardExportGet } from "@/server/cards/xhs-card";

export const dynamic = "force-dynamic";

export async function GET(request: Request): Promise<Response> {
  return handleCardExportGet(request);
}
