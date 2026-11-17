export type ReadState<T> =
  | { kind: "loading" }
  | { kind: "ready"; data: T }
  | { kind: "stale"; data: T; asOf: string }
  | { kind: "empty" }
  | { kind: "unavailable"; reason: string }
  | { kind: "denied" }
  | { kind: "error"; message: string };

export const NOTICE_KINDS = ["loading", "empty", "unavailable", "denied", "error"] as const;
export type NoticeKind = (typeof NOTICE_KINDS)[number];
export type ReadStateOverrides = Partial<Record<NoticeKind, string>>;
export const NOTICE_STYLE: Record<NoticeKind, string> = {
  loading: "border-gray-700 text-gray-400",
  empty: "border-gray-700 text-gray-400",
  unavailable: "border-amber-500/40 text-amber-300",
  denied: "border-rose-500/40 text-rose-300",
  error: "border-red-500/40 text-red-300",
};

export const RUNS_EMPTY_MESSAGE = "No Dagster runs have been recorded in this database yet.";
export const RUNS_UNAVAILABLE_MESSAGE = "Dagster run history is unavailable for the current database role.";

const DEFAULT_MESSAGES: Record<NoticeKind, (state: ReadState<unknown>) => string> = {
  loading: () => "Loading...",
  empty: () => "No records available for the current governed head.",
  unavailable: (state) =>
    state.kind === "unavailable" && state.reason
      ? `Data is unavailable: ${state.reason}`
      : "Data is temporarily unavailable.",
  denied: () => "Access denied. Please sign in with an authorized account.",
  error: (state) =>
    state.kind === "error" && state.message
      ? `Error loading data: ${state.message}`
      : "An unexpected error occurred while loading data.",
};

export function readStateMessage(
  state: ReadState<unknown>,
  overrides: Partial<Record<NoticeKind, string>> = {},
): string | null {
  if (state.kind === "ready" || state.kind === "stale") {
    return null;
  }
  const override = overrides[state.kind];
  if (typeof override === "string" && override.trim().length > 0) {
    return override;
  }
  return DEFAULT_MESSAGES[state.kind](state);
}
