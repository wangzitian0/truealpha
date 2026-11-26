"use client";

import { useState } from "react";
import { api } from "@/client/api";
import type { PegGrowthConvention } from "@/client/strategy";
import { universesWithRole } from "@/contracts/universes";

interface CardExportButtonProps {
  cutoff?: string;
  issuerId?: string;
  kind?: "ranking" | "entity" | "etf";
  format?: "html" | "json";
  convention?: PegGrowthConvention;
  universe?: string;
}

function effectiveKindLabel(kind?: "ranking" | "entity" | "etf", issuerId?: string): string {
  if (kind === "etf") return "Export ETF HTML Card (1080×1440)";
  return issuerId ? "Export Entity 360 HTML Card (1080×1440)" : "Export Xiaohongshu HTML Card (1080×1440)";
}

export function CardExportButton({
  cutoff,
  issuerId,
  kind,
  format = "html",
  convention = "historical_cagr",
  universe = universesWithRole("strategy")[0] ?? "all",
}: CardExportButtonProps) {
  const [downloading, setDownloading] = useState(false);

  const handleExport = async () => {
    setDownloading(true);
    try {
      const effectiveKind = kind ?? (issuerId ? "entity" : "ranking");
      const cardKind = effectiveKind === "entity" ? "company" : effectiveKind === "etf" ? "etf" : "ranking";
      const res = await api.exportCard({
        card_kind: cardKind,
        cutoff: cutoff ?? null,
        issuer_id: issuerId ?? null,
        convention,
        preset: null,
        universe: effectiveKind === "ranking" ? universe : null,
        format,
        persist: true,
      });
      const content =
        format === "json"
          ? JSON.stringify(res.card, null, 2)
          : (res.rendered_html ?? "");
      const mime = format === "json" ? "application/json;charset=utf-8" : "text/html;charset=utf-8";
      const ext = format === "json" ? "json" : "html";
      const blob = new Blob([content], { type: mime });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = issuerId
        ? `truealpha-card-entity-${issuerId.replace(/[:]/g, "-")}.${ext}`
        : `truealpha-card-ranking.${ext}`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } finally {
      setTimeout(() => setDownloading(false), 500);
    }
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
      title="Export 1080x1440 3:4 portrait HTML card for Xiaohongshu"
    >
      {downloading
        ? "Exporting..."
        : effectiveKindLabel(kind, issuerId)}
    </button>
  );
}
