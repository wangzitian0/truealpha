"use client";

import { useState } from "react";
import { api, ApiError } from "@/client/api";

export function LibraryReportPreviewCard() {
  const [status, setStatus] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function previewThemeReport() {
    setBusy(true);
    setStatus(null);
    try {
      const report = await api.getLibraryReport({
        report_kind: "theme_ranking",
        target_entity_ids: "theme:large_model_value",
        format: "markdown",
      });
      setStatus(`${report.report_id} (${report.schema_version})`);
    } catch (err) {
      setStatus(err instanceof ApiError ? err.detail : err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="rounded-xl border border-border bg-card p-4 space-y-2">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-sm font-semibold text-white">Library Report Preview</h2>
          <p className="text-xs text-gray-400">
            Generate a deterministic theme report via <code className="text-accent">GET /api/v1/library/reports</code>.
          </p>
        </div>
        <button
          type="button"
          onClick={previewThemeReport}
          disabled={busy}
          className="rounded-lg border border-border px-3 py-1.5 text-xs text-gray-200 hover:border-accent disabled:opacity-50"
        >
          {busy ? "Loading…" : "Preview theme report"}
        </button>
      </div>
      {status && <p className="font-mono text-xs text-gray-300">{status}</p>}
    </div>
  );
}
