/**
 * One row-coercion and formatting toolkit for database rows (#1679).
 *
 * Each helper keeps the exact semantics of the file-local copy it replaced.
 * Add a helper here only when two or more modules need the same conversion.
 */

/** A string value, or `null` for every other type. */
export function textOrNull(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

/** `String(value)`, or `null` when the value is `null` or `undefined`. */
export function nullableString(value: unknown): string | null {
  return value !== null && value !== undefined ? String(value) : null;
}

/** Every array element as a string, or an empty array for a non-array. */
export function stringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.map(String) : [];
}

/** `Number(value)` when it is finite, else `null`. */
export function finiteOrNull(value: unknown): number | null {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

/** A finite number value, or `0` for every other value. */
export function numberOrZero(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

/** A plain object value, or `null` for arrays, `null`, and primitives. */
export function recordOf(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

/** Text of a timestamp column: ISO-8601 for a `Date`, else `String(value)`. */
export function timestampText(value: unknown): string {
  if (value instanceof Date) return value.toISOString();
  return String(value);
}

/**
 * Elapsed hours as `Nm` below one hour, else `N.Nh`. With `dayAfterHours`,
 * values at or above that limit show as `N.Nd`.
 */
export function formatAgeHours(hours: number, dayAfterHours: number = Number.POSITIVE_INFINITY): string {
  if (hours < 1) return `${Math.round(hours * 60)}m`;
  return hours < dayAfterHours ? `${hours.toFixed(1)}h` : `${(hours / 24).toFixed(1)}d`;
}
