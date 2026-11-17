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
