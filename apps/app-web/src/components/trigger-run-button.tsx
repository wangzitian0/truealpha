"use client";

import { useState } from "react";
import { api, ApiError } from "@/client/api";

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
      const body = await api.triggerPipeline({ force_fetch: forceFetch });
      setState({ kind: "accepted", runKey: body.run_key, forced: body.force_fetch === true });
    } catch (error) {
      setState({
        kind: "failed",
        message: error instanceof ApiError ? error.detail : error instanceof Error ? error.message : String(error),
      });
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
