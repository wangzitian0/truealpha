/**
 * Shared, server-rendered presentation of the typed read states — #370, and
 * #495 which brought the administrator surfaces onto it too.
 *
 * The words live in `@/server/read-state` (`readStateMessage`), so every state
 * is proven to have a sentence by a unit test instead of by whichever page
 * happened to write a branch for it. This component only decides where the
 * sentence goes.
 */

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
    <span
      className={`inline-block rounded-full border px-2 py-0.5 text-xs font-medium ${AVAILABILITY_STYLE[status]}`}
    >
      {status}
    </span>
  );
}

/**
 * Renders the notice for a non-`ready` state, or `null` when the caller should
 * render its own data (`ready`, or `stale` which still shows data with its own
 * badge).
 *
 * `overrides` re-words one state for one surface — the administrator world
 * denies for a different reason than the research world — without introducing
 * a second vocabulary to do it.
 */
export function ReadStateNotice({
  state,
  overrides,
}: {
  state: ReadState<unknown>;
  overrides?: ReadStateOverrides;
}) {
  // Narrowing, not casting: after this the compiler knows `state.kind` is a
  // NoticeKind, so NOTICE_STYLE is total by construction and a kind added to
  // the union without a style is a type error rather than an undefined lookup.
  if (state.kind === "ready" || state.kind === "stale") return null;
  const message = readStateMessage(state, overrides);
  if (message === null) return null;

  return (
    <p role="status" className={`mt-4 rounded-lg border border-border bg-card p-4 ${NOTICE_STYLE[state.kind]}`}>
      {message}
    </p>
  );
}
