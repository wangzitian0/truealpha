/** Display-only formatting helpers for exact mart numeric strings (#494 P0a). */

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

export function formatCell(value: string | number | boolean | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "boolean") return String(value);
  return typeof value === "number" ? (Number.isFinite(value) ? String(value) : "—") : value;
}

export function formatShortSha(sha: string | null | undefined): string {
  return sha ? `${sha.slice(0, 12)}…` : "—";
}

export function formatUsdMagnitude(value: string | number | null | undefined): string | null {
  const n = toFinite(value);
  if (n === null) return null;
  const high = scaleHighMagnitude(n);
  if (high !== null) return high.startsWith("-") ? `-$${high.slice(1)}` : `$${high}`;
  const abs = Math.abs(n);
  const sign = n < 0 ? "-" : "";
  return abs >= 1e3 ? `${sign}$${(abs / 1e3).toFixed(1)}K` : `${sign}$${abs.toFixed(0)}`;
}

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

export function formatRatio(value: string | number | null | undefined): string | null {
  const n = toFinite(value);
  return n === null ? null : n.toFixed(2);
}

export function formatSignedRatio(value: string | number | null | undefined): string | null {
  const n = toFinite(value);
  if (n === null) return null;
  return `${n > 0 ? "+" : n < 0 ? "−" : ""}${Math.abs(n).toFixed(2)}`;
}

export function formatPercentFromFraction(value: string | number | null | undefined): string | null {
  const n = toFinite(value);
  if (n === null) return null;
  const pct = n * 100;
  return `${Number.isInteger(pct) ? pct : pct.toFixed(1)}%`;
}

export function formatYieldPercent(value: string | null | undefined): string | null {
  if (value === null || value === undefined) return null;
  const match = /^(-?)(\d+)(?:\.(\d+))?$/.exec(value.trim());
  if (match === null) return null;
  const [, sign, whole, fraction = ""] = match;
  const shifted = fraction.padEnd(2, "0");
  const integerPart = `${whole}${shifted.slice(0, 2)}`.replace(/^0+(?=\d)/, "");
  const decimalPart = shifted.slice(2).padEnd(2, "0").slice(0, 2);
  const signed = /^0+$/.test(`${integerPart}${decimalPart}`) ? "" : sign;
  return `${signed}${integerPart}.${decimalPart}%`;
}

export const ANNUALIZED_FRACTION_UNIT = "annualized_decimal_fraction";

export function formatMacroValue(unit: string, value: string | null | undefined): string | null {
  if (value === null || value === undefined) return null;
  if (unit === ANNUALIZED_FRACTION_UNIT) {
    const percent = formatYieldPercent(value);
    if (percent !== null) return percent;
  }
  return `${value} ${unit}`;
}

export function formatUtcMinute(value: string | null | undefined): string | null {
  if (value === null || value === undefined) return null;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return null;
  const iso = parsed.toISOString();
  return `${iso.slice(0, 10)} ${iso.slice(11, 16)} UTC`;
}

export function signColor(value: string | number | null | undefined): string {
  const n = toFinite(value);
  if (n === null || n === 0) return "";
  return n > 0 ? "text-emerald-400" : "text-red-400";
}

export function formatListingLabel(listingId: string | null): string | null {
  if (listingId === null) return null;
  const trimmed = listingId.trim();
  if (trimmed === "") return null;
  const listingMatch = trimmed.match(/^listing:([a-z0-9]+):([a-z0-9.-]+)$/i);
  if (listingMatch) return `${listingMatch[2].toUpperCase()} (${listingMatch[1].toUpperCase()})`;
  const uuidMatch = trimmed.match(UUID_PREFIX_PATTERN);
  if (uuidMatch) return `Listing #${uuidMatch[1].toLowerCase()}`;
  return trimmed;
}

export function formatIssuerLabel(issuerId: string | null, resolvedLabel?: string | null): string | null {
  if (issuerId === null) return null;
  const trimmed = issuerId.trim();
  if (trimmed === "") return null;
  if (resolvedLabel && resolvedLabel.trim() !== "" && resolvedLabel.trim() !== trimmed) {
    return resolvedLabel.trim();
  }
  const cikMatch = trimmed.match(/^(?:issuer|company):cik:([0-9]+)$/i);
  if (cikMatch) return `CIK ${cikMatch[1]}`;
  const leiMatch = trimmed.match(/^issuer:lei:([a-z0-9]+)$/i);
  if (leiMatch) {
    const lei = leiMatch[1].toUpperCase();
    return `LEI ${lei.length > 12 ? `${lei.slice(0, 12)}…` : lei}`;
  }
  const uuidMatch = trimmed.match(UUID_PREFIX_PATTERN);
  if (uuidMatch) return `Issuer #${uuidMatch[1].toLowerCase()}`;
  return trimmed;
}

export function formatPrincipalLabel(principalId: string | null): string | null {
  if (principalId === null) return null;
  const trimmed = principalId.trim();
  if (trimmed === "") return null;
  if (!trimmed.startsWith("principal:")) return trimmed;
  const parts = trimmed.split(":").filter((part) => part.length > 0);
  return parts.length > 1 ? parts[parts.length - 1] : trimmed;
}

export function decodeRouteParam(id: string): string {
  try {
    return decodeURIComponent(id);
  } catch {
    return id;
  }
}

export function tierKey(tier: string | null | undefined): "lm-native" | "tech" | "traditional" | "unknown" {
  const normalized = (tier ?? "").trim().toLowerCase();
  if (normalized === "large_model_native" || normalized === "large model native" || normalized === "lm-native") {
    return "lm-native";
  }
  if (normalized === "tech" || normalized === "tech_enabled" || normalized === "tech-enabled") return "tech";
  if (normalized === "traditional") return "traditional";
  return "unknown";
}

export function formatTierLabel(tier: string | null | undefined, fallbackGlyph = "—"): string {
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
