"use client";

import { useState } from "react";
import { api } from "@/client/api";

export function BacktestTriggerButton() {
  const [statusText, setStatusText] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onTrigger() {
    setBusy(true);
    setStatusText(null);
    try {
      const res = await api.triggerBacktest({});
      setStatusText(`Queued backtest (${res.run_key})`);
    } catch (err) {
      setStatusText(err instanceof Error ? err.message : "Trigger failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col items-end gap-1">
      <button
        type="button"
        onClick={onTrigger}
        disabled={busy}
        className="rounded-lg border border-sky-700 bg-sky-950/60 px-3 py-1.5 text-xs font-semibold text-sky-300 hover:bg-sky-900/70 disabled:opacity-50"
      >
        {busy ? "Queuing…" : "Run Backtest"}
      </button>
      {statusText && <span className="font-mono text-[11px] text-gray-300">{statusText}</span>}
    </div>
  );
}
