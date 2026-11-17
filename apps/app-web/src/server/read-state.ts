export type ReadState<T> =
  | { kind: "loading" }
  | { kind: "ready"; data: T }
  | { kind: "empty" }
  | { kind: "unavailable"; reason: string }
  | { kind: "stale"; data: T; asOf: string }
  | { kind: "error"; message: string }
  | { kind: "denied" };

export type ReadStateKind = ReadState<unknown>["kind"];

export const NOTICE_KINDS = ["loading", "empty", "unavailable", "error", "denied"] as const;
export type NoticeKind = (typeof NOTICE_KINDS)[number];

export type ReadStateOverrides = Partial<Record<NoticeKind, string>>;

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

export const NOTICE_STYLE: Record<NoticeKind, string> = {
  loading: "text-gray-400",
  empty: "text-gray-400",
  unavailable: "text-gray-400",
  error: "text-red-400",
  denied: "text-amber-400",
};
