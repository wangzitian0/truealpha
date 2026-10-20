"use client";

/**
 * Card export button (#1360).
 *
 * Default: PNG export at 1080x1440 via `/research/api/cards/export`.
 * Optional: SVG export when `format="svg"` is specified.
 * Downloads the rendered card and disables the button while export is in progress.
 */

import { useState } from "react";
import type { PegGrowthConvention, RankingUniverse } from "@/contracts/strategyRun";

interface CardExportButtonProps {
  cutoff?: string;
  issuerId?: string;
  kind?: "ranking" | "entity";
  format?: "png" | "svg";
  convention?: PegGrowthConvention;
  universe?: RankingUniverse;
}

export function CardExportButton({
  cutoff,
  issuerId,
  kind,
  format = "png",
  convention = "historical_cagr",
  universe = "topt",
}: CardExportButtonProps) {
  const [downloading, setDownloading] = useState(false);

  const handleExport = () => {
    setDownloading(true);
    const effectiveKind = kind ?? (issuerId ? "entity" : "ranking");
    const query = new URLSearchParams();
    query.set("kind", effectiveKind);
    if (cutoff) query.set("cutoff", cutoff);
    if (issuerId) query.set("issuer", issuerId);
    if (convention !== "historical_cagr") query.set("convention", convention);
    if (!issuerId && universe !== "topt") query.set("universe", universe);
    query.set("format", format);

    const ext = format === "svg" ? "svg" : "png";
    const url = `/research/api/cards/export?${query.toString()}`;
    const a = document.createElement("a");
    a.href = url;
    a.download = issuerId
      ? `truealpha-card-entity-${issuerId.replace(/[:]/g, "-")}.${ext}`
      : `truealpha-card-ranking.${ext}`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);

    setTimeout(() => setDownloading(false), 1500);
  };

  return (
    <button
      type="button"
      onClick={handleExport}
      disabled={downloading}
      data-testid="card-export-button"
      data-export-kind={kind ?? (issuerId ? "entity" : "ranking")}
      data-export-format={format}
      data-export-convention={convention}
      data-export-universe={universe}
      className="inline-flex items-center gap-2 rounded-lg border border-accent/40 bg-accent/10 px-3.5 py-1.5 text-xs font-semibold text-accent transition hover:border-accent hover:bg-accent/20 focus:outline-none focus:ring-2 focus:ring-accent disabled:opacity-50"
      title="Export 1080x1440 3:4 portrait card for Xiaohongshu"
    >
      <svg
        className="h-3.5 w-3.5"
        fill="none"
        stroke="currentColor"
        viewBox="0 0 24 24"
        xmlns="http://www.w3.org/2000/svg"
      >
        <path
          strokeLinecap="round"
          strokeLinejoin="round"
          strokeWidth={2}
          d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4"
        />
      </svg>
      {downloading
        ? "Exporting..."
        : issuerId
          ? "Export Entity 360 Card (1080×1440)"
          : "Export Xiaohongshu Card (1080×1440)"}
    </button>
  );
}

