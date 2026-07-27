"use client";

/**
 * #495 (surface 2b): the manual-trigger control — the SAME thin trigger the
 * schedule uses, only the `executed_at` parameter differs (defaults to now).
 * POSTs to /admin/api/trigger, which INSERTs the Postgres-mediated request
 * the data-engine sensor consumes; shows the run_key so the operator can
 * find the launched run in the table above after the sensor's next poll.
 */

import { useState } from "react";

export function TriggerRunButton() {
  const [state, setState] = useState<
    | { kind: "idle" }
    | { kind: "submitting" }
    | { kind: "accepted"; runKey: string }
    | { kind: "failed"; message: string }
  >({ kind: "idle" });

  async function submit() {
    setState({ kind: "submitting" });
    try {
      const response = await fetch("/admin/api/trigger", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      const body = await response.json().catch(() => ({}));
      if (response.status === 202 && typeof body.run_key === "string") {
        setState({ kind: "accepted", runKey: body.run_key });
      } else {
        setState({ kind: "failed", message: String(body.error ?? `HTTP ${response.status}`) });
      }
    } catch (error) {
      setState({ kind: "failed", message: error instanceof Error ? error.message : String(error) });
    }
  }

  return (
    <div className="flex items-center gap-3">
      <button
        type="button"
        onClick={submit}
        disabled={state.kind === "submitting"}
        className="rounded-lg bg-accent px-4 py-2 text-sm font-semibold text-white disabled:opacity-50"
      >
        {state.kind === "submitting" ? "Requesting…" : "Trigger a run now"}
      </button>
      {state.kind === "accepted" && (
        <span role="status" className="text-sm text-emerald-400">
          Accepted — the sensor launches <code>{state.runKey}</code> within ~30s. Idempotent: same
          timestamp reproduces the same run.
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
