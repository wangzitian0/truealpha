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

const HIGH_MAGNITUDE_TIERS: ReadonlyArray<readonly [number, string]> = [
  [1e12, "T"],
  [1e9, "B"],
  [1e6, "M"],
];

const UUID_PREFIX_PATTERN = /^([0-9a-f]{8})-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function toFinite(value: string | number | null | undefined): number | null {
  if (value === null || value === undefined) return null;
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (value.trim() === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function scaleHighMagnitude(n: number): string | null {
  const abs = Math.abs(n);
  const sign = n < 0 ? "-" : "";
  for (const [divisor, suffix] of HIGH_MAGNITUDE_TIERS) {
    if (abs >= divisor) return `${sign}${(abs / divisor).toFixed(2)}${suffix}`;
  }
  return null;
}

/** Honest-absence cell fallback: renders em-dash when a formatted or raw value is absent. */
export function formatCell(value: string | number | boolean | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "boolean") return String(value);
  return typeof value === "number" ? (Number.isFinite(value) ? String(value) : "—") : value;
}

/** Truncates a SHA-256 hex digest to 12 chars with an ellipsis, or em-dash when empty. */
export function formatShortSha(sha: string | null | undefined): string {
  return sha ? `${sha.slice(0, 12)}…` : "—";
}

/** `1077115.2439…` → `$1.08M`; magnitude suffixes K/M/B/T, 2 significant decimals. */
export function formatUsdMagnitude(value: string | number | null | undefined): string | null {
  const n = toFinite(value);
  if (n === null) return null;
  const high = scaleHighMagnitude(n);
  if (high !== null) {
    return high.startsWith("-") ? `-$${high.slice(1)}` : `$${high}`;
  }
  const abs = Math.abs(n);
  const sign = n < 0 ? "-" : "";
  if (abs >= 1e3) return `${sign}$${(abs / 1e3).toFixed(1)}K`;
  return `${sign}$${abs.toFixed(0)}`;
}

/**
 * Currency-neutral magnitude / thousand-separator formatter for decision inputs and share counts:
 * `171018000000` → `171.02B`, `7933746241` → `7.93B`, `110000` → `110,000`, `55.5` → `55.5`.
 */
export function formatNumberMagnitude(value: string | number | null | undefined): string | null {
  const n = toFinite(value);
  if (n === null) return null;
  const high = scaleHighMagnitude(n);
  if (high !== null) return high;
  const abs = Math.abs(n);
  const sign = n < 0 ? "-" : "";
  if (abs >= 1e3) return `${sign}${abs.toLocaleString("en-US", { maximumFractionDigits: 2 })}`;
  return typeof value === "number" ? String(value) : value!.trim();
}

/** Plain ratio at 2dp: `13.0359` → `13.04`. */
export function formatRatio(value: string | number | null | undefined): string | null {
  const n = toFinite(value);
  return n === null ? null : n.toFixed(2);
}

/** Signed ratio at 2dp: `1.1917` → `+1.19`, `-0.2367` → `−0.24`. */
export function formatSignedRatio(value: string | number | null | undefined): string | null {
  const n = toFinite(value);
  if (n === null) return null;
  return `${n > 0 ? "+" : n < 0 ? "−" : ""}${Math.abs(n).toFixed(2)}`;
}

/** Fraction to percent: `0.500000` → `50%`. */
export function formatPercentFromFraction(value: string | number | null | undefined): string | null {
  const n = toFinite(value);
  if (n === null) return null;
  const pct = n * 100;
  return `${Number.isInteger(pct) ? pct : pct.toFixed(1)}%`;
}

/**
 * A yield as a percent with two decimals: `0.0426` → `4.26%`, `0.05` → `5.00%`.
 *
 * The mart serves an exact decimal fraction. The shift of the decimal point by two places works on
 * the digits of the string, so the display carries no binary-float error. Digits beyond the second
 * decimal of the percent are cut, and the exact string stays in the `title` of the cell. A text that
 * is not a plain decimal returns null (callers render "—").
 */
export function formatYieldPercent(value: string | null | undefined): string | null {
  if (value === null || value === undefined) return null;
  const match = /^(-?)(\d+)(?:\.(\d+))?$/.exec(value.trim());
  if (match === null) return null;
  const [, sign, whole, fraction = ""] = match;
  const shifted = fraction.padEnd(2, "0");
  const integerPart = `${whole}${shifted.slice(0, 2)}`.replace(/^0+(?=\d)/, "");
  const decimalPart = shifted.slice(2).padEnd(2, "0").slice(0, 2);
  return `${sign}${integerPart}.${decimalPart}%`;
}

/** An instant as `2026-10-20 20:31 UTC`, or null when it is not a valid date. */
export function formatUtcMinute(value: string | null | undefined): string | null {
  if (value === null || value === undefined) return null;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return null;
  return `${parsed.toISOString().slice(0, 10)} ${parsed.toISOString().slice(11, 16)} UTC`;
}

/** Tailwind text color for a signed quantity (positive = favorable gap). */
export function signColor(value: string | number | null | undefined): string {
  const n = toFinite(value);
  if (n === null || n === 0) return "";
  return n > 0 ? "text-emerald-400" : "text-red-400";
}

/**
 * Formats a raw listing identifier into a human-readable ticker/venue label:
 * `listing:xnas:aapl` → `AAPL (XNAS)`, `listing:xnys:brk.b` → `BRK.B (XNYS)`,
 * UUID `3dc96dda-763e-5379-bf95-2f729b3ed6da` → `Listing #3dc96dda`.
 */
export function formatListingLabel(listingId: string | null): string | null {
  if (listingId === null) return null;
  const trimmed = listingId.trim();
  if (trimmed === "") return null;
  const listingMatch = trimmed.match(/^listing:([a-z0-9]+):([a-z0-9.-]+)$/i);
  if (listingMatch) {
    const mic = listingMatch[1].toUpperCase();
    const symbol = listingMatch[2].toUpperCase();
    return `${symbol} (${mic})`;
  }
  const uuidMatch = trimmed.match(UUID_PREFIX_PATTERN);
  if (uuidMatch) {
    return `Listing #${uuidMatch[1].toLowerCase()}`;
  }
  return trimmed;
}

/**
 * Formats a raw issuer identifier into a human-readable label when not resolved in the KG map:
 * `issuer:cik:0000104169` / `company:cik:0000104169` → `CIK 0000104169`,
 * `issuer:lei:HWUPKR0MPOU8FGXBT394` → `LEI HWUPKR0MPOU8…`.
 */
export function formatIssuerLabel(issuerId: string | null, resolvedLabel?: string | null): string | null {
  if (issuerId === null) return null;
  const trimmed = issuerId.trim();
  if (trimmed === "") return null;
  if (resolvedLabel && resolvedLabel.trim() !== "" && resolvedLabel.trim() !== trimmed) {
    return resolvedLabel.trim();
  }
  const cikMatch = trimmed.match(/^(?:issuer|company):cik:([0-9]+)$/i);
  if (cikMatch) {
    return `CIK ${cikMatch[1]}`;
  }
  const leiMatch = trimmed.match(/^issuer:lei:([a-z0-9]+)$/i);
  if (leiMatch) {
    const lei = leiMatch[1].toUpperCase();
    return `LEI ${lei.length > 12 ? `${lei.slice(0, 12)}…` : lei}`;
  }
  const uuidMatch = trimmed.match(UUID_PREFIX_PATTERN);
  if (uuidMatch) {
    return `Issuer #${uuidMatch[1].toLowerCase()}`;
  }
  return trimmed;
}

/**
 * Formats an internal principal identifier (`principal:owner`, `principal:e2e:admin`,
 * `principal:friend-alice`) into a human-readable display label.
 */
export function formatPrincipalLabel(principalId: string | null): string | null {
  if (principalId === null) return null;
  const trimmed = principalId.trim();
  if (trimmed === "") return null;
  if (!trimmed.startsWith("principal:")) return trimmed;
  const parts = trimmed.split(":").filter((part) => part.length > 0);
  return parts.length > 1 ? parts[parts.length - 1] : trimmed;
}

/**
 * Decodes a Next.js dynamic route segment (`[id]`).
 * Returns the raw segment when percent-encoding is malformed so the route does not throw URIError.
 */
export function decodeRouteParam(id: string): string {
  try {
    return decodeURIComponent(id);
  } catch {
    return id;
  }
}

/** Canonical tier key for palette and badge lookups (#1360). */
export function tierKey(
  tier: string | null | undefined,
): "lm-native" | "tech" | "traditional" | "unknown" {
  const normalized = (tier ?? "").trim().toLowerCase();
  if (
    normalized === "large_model_native" ||
    normalized === "large model native" ||
    normalized === "lm-native"
  ) {
    return "lm-native";
  }
  if (normalized === "tech" || normalized === "tech_enabled" || normalized === "tech-enabled") {
    return "tech";
  }
  if (normalized === "traditional") {
    return "traditional";
  }
  return "unknown";
}

/** Human-readable display label for a three-tier valuation classification (#1360). */
export function formatTierLabel(
  tier: string | null | undefined,
  fallbackGlyph = "—",
): string {
  if (!tier || tier.trim() === "") return fallbackGlyph;
  switch (tierKey(tier)) {
    case "lm-native":
      return "Large Model Native";
    case "tech":
      return "Tech";
    case "traditional":
      return "Traditional";
    default:
      return tier;
  }
}


