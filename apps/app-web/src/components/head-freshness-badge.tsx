import { formatRatio } from "@/client/format";

export interface HeadFreshnessProps {
  ageHours?: number | null;
  limitHours?: number | null;
  freshness?: string | null;
  cutoffAt?: string | null;
  governed?: boolean;
}

function toneFor(freshness: string | null | undefined): string {
  if (freshness === "fresh") return "border-emerald-500/40 text-emerald-300";
  if (freshness === "stale" || freshness === "withheld") return "border-amber-500/40 text-amber-300";
  return "border-border text-gray-300";
}

export function HeadFreshnessBadge({ ageHours, limitHours, freshness, cutoffAt, governed }: HeadFreshnessProps) {
  if (!cutoffAt && ageHours == null && !freshness) return null;
  const ageText = ageHours != null ? formatRatio(ageHours) : null;
  const limitText = limitHours != null ? formatRatio(limitHours) : null;
  const datePart = cutoffAt ? `as of ${cutoffAt.slice(0, 10)}` : "served head";
  const scopePart = governed === true ? "governed" : governed === false ? "latest" : null;
  const meterPart =
    ageText && limitText
      ? `${ageText}h / ${limitText}h`
      : ageText
        ? `${ageText}h`
        : null;
  const statusPart = freshness || "untracked";
  const summary = [datePart, scopePart, meterPart, statusPart].filter(Boolean).join(" · ");

  return (
    <span
      data-served-run-age={ageHours != null ? String(Math.floor(ageHours)) : ""}
      data-head-freshness={statusPart}
      className={`rounded-full border px-3 py-1 font-mono text-xs ${toneFor(freshness)}`}
    >
      {summary}
    </span>
  );
}
