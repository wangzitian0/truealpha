/**
 * #495: THE read-state vocabulary, for both route groups.
 *
 * The research surfaces have had one typed union and one shared renderer
 * since #370, which is why absence always has words there. The administrator
 * surfaces grew their own (`OpsOverviewOutcome`, three cases) and rendered it
 * with hand-written branches — so `runs === []` fell through to `.map()` over
 * an empty array and drew a table header with nothing under it, and the
 * `/admin/quality` funnel block simply vanished when it could not be computed.
 * Two vocabularies meant the second one never got the first one's discipline.
 *
 * This module owns the union and the words. `dashboard.ts` re-exports the type
 * so existing research imports are unchanged; `components/read-state.tsx` is
 * the only renderer; `server/admin/*` returns this union like everything else.
 *
 * Wording that must differ per surface (the administrator world denies for a
 * different reason than the research world) goes through `overrides` rather
 * than through a second vocabulary.
 */

export type ReadState<T> =
  | { kind: "loading" }
  | { kind: "ready"; data: T }
  | { kind: "empty" }
  | { kind: "unavailable"; reason: string }
  | { kind: "stale"; data: T; asOf: string }
  | { kind: "error"; message: string }
  | { kind: "denied" };

export type ReadStateKind = ReadState<unknown>["kind"];

/** The kinds that render a notice instead of data. `ready` renders the
 * caller's own content; `stale` renders data plus its own staleness badge. */
export const NOTICE_KINDS = ["loading", "empty", "unavailable", "error", "denied"] as const;
export type NoticeKind = (typeof NOTICE_KINDS)[number];

export type ReadStateOverrides = Partial<Record<NoticeKind, string>>;

/**
 * The user-facing sentence for a state, or `null` when the caller renders its
 * own content (`ready`, `stale`).
 *
 * Every notice kind returns a non-empty string — `tests/read-state.test.ts`
 * asserts that for all of them, so a new kind added to the union without words
 * fails the test rather than rendering an empty region.
 */
export function readStateMessage(
  state: ReadState<unknown>,
  overrides: ReadStateOverrides = {},
): string | null {
  switch (state.kind) {
    case "ready":
    case "stale":
      return null;
    case "loading":
      return overrides.loading ?? "Loading…";
    case "empty":
      return overrides.empty ?? "No materialized results for this view yet.";
    case "unavailable":
      return overrides.unavailable ?? `Unavailable: ${state.reason}`;
    case "error":
      return overrides.error ?? `Error reading the mart: ${state.message}`;
    case "denied":
      return overrides.denied ?? "Access denied. No owner identity configured for this request.";
  }
}

/** Tailwind text colour per notice kind. Semantic, not decorative: amber is a
 * permission/attention state, red is a failure, grey is honest absence. */
export const NOTICE_STYLE: Record<NoticeKind, string> = {
  loading: "text-gray-400",
  empty: "text-gray-400",
  unavailable: "text-gray-400",
  error: "text-red-400",
  denied: "text-amber-400",
};
