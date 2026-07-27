/**
 * #494 P0a: display formatting for mart numerics. The mart serves exact
 * `numeric` strings (init.md: no binary floating point where money is
 * involved); these helpers are DISPLAY-ONLY lossy renderings — every call
 * site keeps the exact string in a `title` attribute (or export path), so
 * precision is demoted from the first screen, never destroyed.
 *
 * All helpers accept the nullable mart string and return null when the
 * input is null or not parseable as a finite number (callers render "—"),
 * so a malformed row degrades to the honest-absence glyph instead of NaN.
 */

function toFinite(value: string | null): number | null {
  if (value === null) return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

/** `1077115.2439…` → `$1.08M`; magnitude suffixes K/M/B, 2 significant decimals. */
export function formatUsdMagnitude(value: string | null): string | null {
  const n = toFinite(value);
  if (n === null) return null;
  const abs = Math.abs(n);
  const sign = n < 0 ? "-" : "";
  if (abs >= 1e9) return `${sign}$${(abs / 1e9).toFixed(2)}B`;
  if (abs >= 1e6) return `${sign}$${(abs / 1e6).toFixed(2)}M`;
  if (abs >= 1e3) return `${sign}$${(abs / 1e3).toFixed(1)}K`;
  return `${sign}$${abs.toFixed(0)}`;
}

/** Plain ratio at 2dp: `13.0359` → `13.04`. */
export function formatRatio(value: string | null): string | null {
  const n = toFinite(value);
  return n === null ? null : n.toFixed(2);
}

/** Signed ratio at 2dp: `1.1917` → `+1.19`, `-0.2367` → `−0.24`. */
export function formatSignedRatio(value: string | null): string | null {
  const n = toFinite(value);
  if (n === null) return null;
  return `${n > 0 ? "+" : n < 0 ? "−" : ""}${Math.abs(n).toFixed(2)}`;
}

/** Fraction to percent: `0.500000` → `50%`. */
export function formatPercentFromFraction(value: string | null): string | null {
  const n = toFinite(value);
  if (n === null) return null;
  const pct = n * 100;
  return `${Number.isInteger(pct) ? pct : pct.toFixed(1)}%`;
}

/** Tailwind text color for a signed quantity (positive = favorable gap). */
export function signColor(value: string | null): string {
  const n = toFinite(value);
  if (n === null || n === 0) return "";
  return n > 0 ? "text-emerald-400" : "text-red-400";
}
