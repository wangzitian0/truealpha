export function textOrNull(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

export function nullableString(value: unknown): string | null {
  return value !== null && value !== undefined ? String(value) : null;
}

export function stringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.map(String) : [];
}

export function finiteOrNull(value: unknown): number | null {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

export function numberOrZero(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

export function recordOf(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

export function timestampText(value: unknown): string {
  if (value instanceof Date) return value.toISOString();
  return String(value);
}

export function formatAgeHours(hours: number, dayAfterHours: number = Number.POSITIVE_INFINITY): string {
  if (hours < 1) return `${Math.round(hours * 60)}m`;
  return hours < dayAfterHours ? `${hours.toFixed(1)}h` : `${(hours / 24).toFixed(1)}d`;
}
