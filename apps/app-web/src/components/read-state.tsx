import {
  NOTICE_STYLE,
  readStateMessage,
  type ReadState,
  type ReadStateOverrides,
} from "@/client/read-state";
export type { ReadState };

type Availability = "available" | "unavailable" | "stale" | "excluded" | "low_confidence" | "error" | string;

const AVAILABILITY_STYLE: Record<string, string> = {
  available: "border-emerald-500/40 text-emerald-300",
  unavailable: "border-gray-600 text-gray-400",
  stale: "border-amber-500/40 text-amber-300",
  excluded: "border-gray-600 text-gray-400",
  low_confidence: "border-amber-500/40 text-amber-300",
  not_applicable: "border-gray-600 text-gray-400",
  error: "border-red-500/40 text-red-300",
};

export function AvailabilityBadge({ status }: { status: Availability }) {
  return (
    <span
      className={`inline-block rounded-full border px-2 py-0.5 text-xs font-medium ${AVAILABILITY_STYLE[status] ?? AVAILABILITY_STYLE.unavailable}`}
    >
      {status}
    </span>
  );
}

/** Renders the notice for a non-ready state (`ready` and `stale` render data). */
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
    <p role="status" data-read-state={state.kind} className={`mt-4 rounded-lg border border-border bg-card p-4 ${NOTICE_STYLE[state.kind]}`}>
      {message}
    </p>
  );
}
