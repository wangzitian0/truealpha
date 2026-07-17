/**
 * Cursor pagination over an already-read, already-ordered result set — see #370.
 *
 * This is index arithmetic over rows the mart read adapter already produced; it never
 * touches a metric value. It is deliberately isolated from `research-read.ts` so the
 * cross-factor-computation boundary scan there stays about metrics, not page offsets.
 * Pagination is explicitly allowed App-side deterministic reformatting (init.md Section 1,
 * rule 2).
 */

export const DEFAULT_PAGE_SIZE = 20;
export const MAX_PAGE_SIZE = 100;

export interface PageInfo {
  total: number;
  nextCursor: string | null;
  hasMore: boolean;
}

export interface Page<T> {
  items: readonly T[];
  info: PageInfo;
}

/** Decodes an opaque cursor to a start offset, clamping out-of-range/garbage to 0.
 * `total` bounds the offset so a stale/malformed cursor (e.g. a shrunk result set, or
 * "2oops" — Number.parseInt would otherwise silently accept its leading "2") never slices
 * from beyond the dataset and returns a confusing empty "ready" page (Copilot review on
 * #387). Requires the cursor to be all digits, not just parseInt-parseable. */
function decodeCursor(cursor: string | null, total: number): number {
  if (cursor === null || !/^\d+$/.test(cursor)) return 0;
  const parsed = Number.parseInt(cursor, 10);
  if (!Number.isInteger(parsed) || parsed < 0 || parsed >= total) return 0;
  return parsed;
}

function clampLimit(limit: number): number {
  if (!Number.isInteger(limit) || limit < 1) return DEFAULT_PAGE_SIZE;
  return Math.min(limit, MAX_PAGE_SIZE);
}

/** Slices `items` into one stable page. Row order is the caller's; this never reorders. */
export function paginate<T>(items: readonly T[], cursor: string | null, limit: number = DEFAULT_PAGE_SIZE): Page<T> {
  const size = clampLimit(limit);
  const start = decodeCursor(cursor, items.length);
  const end = start + size;
  const pageItems = items.slice(start, end);
  const hasMore = end < items.length;
  return {
    items: pageItems,
    info: {
      total: items.length,
      nextCursor: hasMore ? String(end) : null,
      hasMore,
    },
  };
}
