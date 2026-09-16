"use client";

/**
 * #495 (surface 2b): the manual-trigger control — the SAME thin trigger the
 * schedule uses, only the `executed_at` parameter differs (defaults to now).
 * POSTs to /admin/api/trigger, which INSERTs the Postgres-mediated request
 * the data-engine sensor consumes; shows the run_key so the operator can
 * find the launched run in the table above after the sensor's next poll.
 *
 * #874: the checkbox asks for a forced fetch. The tick skips the 12-hour
 * reuse window and fetches every obligation again under its own capture
 * identity, which is how a new origin or a corrected capture is proven by
 * hand. The run plan and the quality report record `forced_fetch`.
 */

import { useState } from "react";

export function TriggerRunButton() {
  const [forceFetch, setForceFetch] = useState(false);
  const [state, setState] = useState<
    | { kind: "idle" }
    | { kind: "submitting" }
    | { kind: "accepted"; runKey: string; forced: boolean }
    | { kind: "failed"; message: string }
  >({ kind: "idle" });

  async function submit() {
    setState({ kind: "submitting" });
    try {
      const response = await fetch("/admin/api/trigger", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ force_fetch: forceFetch }),
      });
      const body = await response.json().catch(() => ({}));
      if (response.status === 202 && typeof body.run_key === "string") {
        setState({ kind: "accepted", runKey: body.run_key, forced: body.force_fetch === true });
      } else {
        setState({ kind: "failed", message: String(body.error ?? `HTTP ${response.status}`) });
      }
    } catch (error) {
      setState({ kind: "failed", message: error instanceof Error ? error.message : String(error) });
    }
  }

  return (
    <div className="flex flex-wrap items-center gap-3">
      <button
        type="button"
        onClick={submit}
        disabled={state.kind === "submitting"}
        className="rounded-lg bg-accent px-4 py-2 text-sm font-semibold text-white disabled:opacity-50"
      >
        {state.kind === "submitting" ? "Requesting…" : "Trigger a run now"}
      </button>
      <label className="flex items-center gap-2 text-sm text-gray-300">
        <input
          type="checkbox"
          checked={forceFetch}
          onChange={(event) => setForceFetch(event.target.checked)}
          disabled={state.kind === "submitting"}
        />
        Force a fresh vendor fetch
        <span className="text-xs text-gray-500">(skips the 12-hour reuse window; spends vendor calls)</span>
      </label>
      {state.kind === "accepted" && (
        <span role="status" className="text-sm text-emerald-400">
          Accepted — the sensor launches <code>{state.runKey}</code> within ~30s
          {state.forced ? " with a forced fetch of every obligation" : ""}. Each click is a new
          launch at the current time; a retry of that launched run reproduces its capture.
        </span>
      )}
      {state.kind === "failed" && (
        <span role="status" className="text-sm text-red-400">
          {state.message}
        </span>
      )}
    </div>
  );
}
