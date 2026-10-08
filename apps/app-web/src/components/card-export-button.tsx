"use client";

import { useState } from "react";

interface CardExportButtonProps {
  cutoff?: string;
  issuerId?: string;
  kind?: "ranking" | "entity";
}

export function CardExportButton({ cutoff, issuerId, kind }: CardExportButtonProps) {
  const [downloading, setDownloading] = useState(false);

  const handleExport = () => {
    setDownloading(true);
    const effectiveKind = kind ?? (issuerId ? "entity" : "ranking");
    const query = new URLSearchParams();
    query.set("kind", effectiveKind);
    if (cutoff) query.set("cutoff", cutoff);
    if (issuerId) query.set("issuer", issuerId);
    query.set("format", "svg");

    const url = `/research/api/cards/export?${query.toString()}`;
    const a = document.createElement("a");
    a.href = url;
    a.download = issuerId
      ? `truealpha-card-entity-${issuerId.replace(/[:]/g, "-")}.svg`
      : `truealpha-card-ranking.svg`;
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
