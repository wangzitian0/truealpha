/**
 * Shared, server-rendered presentation of the dashboard's typed read states — see #370.
 * Keeps every route's loading/empty/unavailable/stale/error/denied handling consistent.
 */

import type { ReadState } from "@/server/dashboard";
import type { Availability } from "@/server/mart/research-read";

const AVAILABILITY_STYLE: Record<Availability, string> = {
  available: "border-emerald-500/40 text-emerald-300",
  unavailable: "border-gray-600 text-gray-400",
  stale: "border-amber-500/40 text-amber-300",
  excluded: "border-gray-600 text-gray-400",
  low_confidence: "border-amber-500/40 text-amber-300",
  error: "border-red-500/40 text-red-300",
};

export function AvailabilityBadge({ status }: { status: Availability }) {
  return (
    <span
      className={`inline-block rounded-full border px-2 py-0.5 text-xs font-medium ${AVAILABILITY_STYLE[status]}`}
    >
      {status}
    </span>
  );
}

/**
 * Renders the notice for a non-`ready` state, or `null` when the caller should render its
 * own data (`ready`, or `stale` which still shows data with its own badge).
 */
export function ReadStateNotice({ state }: { state: ReadState<unknown> }) {
  if (state.kind === "ready" || state.kind === "stale") return null;

  const styles: Record<string, string> = {
    denied: "text-amber-400",
    error: "text-red-400",
    unavailable: "text-gray-400",
    empty: "text-gray-400",
    loading: "text-gray-400",
  };

  const messages: Record<string, string> = {
    denied: "Access denied. No owner identity configured for this request.",
    error: state.kind === "error" ? `Error reading the mart: ${state.message}` : "",
    unavailable: state.kind === "unavailable" ? `Unavailable: ${state.reason}` : "",
    empty: "No materialized results for this view yet.",
    loading: "Loading…",
  };

  return (
    <p role="status" className={`mt-4 rounded-lg border border-border bg-card p-4 ${styles[state.kind]}`}>
      {messages[state.kind]}
    </p>
  );
}
