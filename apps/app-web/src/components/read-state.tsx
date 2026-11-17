import {
  NOTICE_STYLE,
  readStateMessage,
  type ReadState,
  type ReadStateOverrides,
} from "@/server/read-state";
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
    <span className={`inline-block rounded-full border px-2 py-0.5 text-xs font-medium ${AVAILABILITY_STYLE[status]}`}>
      {status}
    </span>
  );
}

export function ReadStateNotice({
  state,
  overrides,
}: {
  state: ReadState<unknown>;
  overrides?: ReadStateOverrides;
}) {
  if (state.kind === "ready" || state.kind === "stale") return null;
  const message = readStateMessage(state, overrides);
  if (message === null) return null;

  return (
    <p role="status" className={`mt-4 rounded-lg border border-border bg-card p-4 ${NOTICE_STYLE[state.kind]}`}>
      {message}
    </p>
  );
}
