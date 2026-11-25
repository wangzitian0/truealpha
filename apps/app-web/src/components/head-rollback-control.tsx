"use client";

import { useState } from "react";
import { api, ApiError } from "@/client/api";

interface Props {
  heads?: Record<string, unknown>[];
  captureRuns?: Record<string, unknown>[];
}

export function HeadRollbackControl({ heads = [], captureRuns = [] }: Props) {
  const firstHead = heads[0] ?? {};
  const [selectedHeadIdx, setSelectedHeadIdx] = useState(0);
  const [universeId, setUniverseId] = useState(String(firstHead.universe_id ?? ""));
  const [universeVersion, setUniverseVersion] = useState(String(firstHead.universe_version ?? ""));
  const [factorId, setFactorId] = useState(String(firstHead.factor_id ?? "gross_profit_per_employee"));
  const [targetRunId, setTargetRunId] = useState(String(captureRuns[0]?.run_id ?? ""));
  const [reason, setReason] = useState("");
  const [state, setState] = useState<
    | { kind: "idle" }
    | { kind: "submitting" }
    | { kind: "accepted"; requestId: number; dedupeKey: string; targetRunId: string }
    | { kind: "failed"; message: string }
  >({ kind: "idle" });

  function selectHead(idx: number) {
    setSelectedHeadIdx(idx);
    const h = heads[idx];
    if (!h) return;
    setUniverseId(String(h.universe_id ?? ""));
    setUniverseVersion(String(h.universe_version ?? ""));
    setFactorId(String(h.factor_id ?? "gross_profit_per_employee"));
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setState({ kind: "submitting" });
    try {
      const res = await api.rollbackHead({
        universe_id: universeId.trim(),
        universe_version: universeVersion.trim(),
        factor_id: factorId.trim() || "gross_profit_per_employee",
        target_run_id: targetRunId.trim(),
        reason: reason.trim(),
      });
      setState({
        kind: "accepted",
        requestId: res.request_id,
        dedupeKey: res.dedupe_key,
        targetRunId: res.target_run_id,
      });
      setReason("");
    } catch (error) {
      setState({
        kind: "failed",
        message: error instanceof ApiError ? error.detail : error instanceof Error ? error.message : String(error),
      });
    }
  }

  return (
    <details
      data-testid="head-rollback-control"
      className="rounded-xl border border-amber-500/30 bg-card/70 p-4 text-sm"
    >
      <summary className="cursor-pointer font-semibold text-amber-300">
        Head rollback (append-only pointer advance)
      </summary>
      <form onSubmit={submit} className="mt-4 grid gap-3 md:grid-cols-2">
        {heads.length > 0 && (
          <label className="space-y-1 text-xs text-gray-300 md:col-span-2">
            <span>Select served head</span>
            <select
              value={selectedHeadIdx}
              onChange={(e) => selectHead(Number(e.target.value))}
              className="w-full rounded border border-border bg-background px-2.5 py-1.5 font-mono text-xs"
            >
              {heads.map((h, i) => (
                <option key={`${String(h.universe_id)}-${String(h.factor_id)}-${i}`} value={i}>
                  {String(h.universe_id ?? "")} · {String(h.factor_id ?? "")} · v{String(h.universe_version ?? "")} (seq{" "}
                  {String(h.sequence ?? 0)})
                </option>
              ))}
            </select>
          </label>
        )}
        <label className="space-y-1 text-xs text-gray-300">
          <span>Universe ID</span>
          <input
            type="text"
            required
            value={universeId}
            onChange={(e) => setUniverseId(e.target.value)}
            placeholder="universe:topt-us-v1"
            className="w-full rounded border border-border bg-background px-2.5 py-1.5 font-mono text-xs"
          />
        </label>
        <label className="space-y-1 text-xs text-gray-300">
          <span>Universe version</span>
          <input
            type="text"
            required
            value={universeVersion}
            onChange={(e) => setUniverseVersion(e.target.value)}
            placeholder="2026-11-01"
            className="w-full rounded border border-border bg-background px-2.5 py-1.5 font-mono text-xs"
          />
        </label>
        <label className="space-y-1 text-xs text-gray-300">
          <span>Factor ID</span>
          <input
            type="text"
            required
            value={factorId}
            onChange={(e) => setFactorId(e.target.value)}
            className="w-full rounded border border-border bg-background px-2.5 py-1.5 font-mono text-xs"
          />
        </label>
        <label className="space-y-1 text-xs text-gray-300">
          <span>Target capture run ID</span>
          <input
            type="text"
            required
            value={targetRunId}
            onChange={(e) => setTargetRunId(e.target.value)}
            placeholder="capture-run:..."
            list="capture-run-choices"
            className="w-full rounded border border-border bg-background px-2.5 py-1.5 font-mono text-xs"
          />
          {captureRuns.length > 0 && (
            <datalist id="capture-run-choices">
              {captureRuns.map((r) => (
                <option key={String(r.run_id ?? "")} value={String(r.run_id ?? "")}>
                  {String(r.universe_id ?? "")} · {String(r.cutoff ?? "")}
                </option>
              ))}
            </datalist>
          )}
        </label>
        <label className="space-y-1 text-xs text-gray-300 md:col-span-2">
          <span>Reason</span>
          <input
            type="text"
            required
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="Why this pointer should point to the target capture run"
            className="w-full rounded border border-border bg-background px-2.5 py-1.5 text-xs"
          />
        </label>
        <div className="flex flex-wrap items-center gap-3 md:col-span-2">
          <button
            type="submit"
            disabled={state.kind === "submitting"}
            className="rounded-lg border border-amber-400/50 bg-amber-500/20 px-4 py-1.5 text-xs font-semibold text-amber-200 hover:bg-amber-500/30 disabled:opacity-50"
          >
            {state.kind === "submitting" ? "Submitting rollback…" : "Queue head rollback"}
          </button>
          {state.kind === "accepted" && (
            <span role="status" className="text-xs text-emerald-400">
              Queued rollback #{state.requestId} (<code>{state.dedupeKey}</code>) →{" "}
              <code>{state.targetRunId}</code>
            </span>
          )}
          {state.kind === "failed" && (
            <span role="status" className="text-xs text-red-400">
              {state.message}
            </span>
          )}
        </div>
      </form>
    </details>
  );
}
