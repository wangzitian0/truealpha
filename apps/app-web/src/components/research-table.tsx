import type { ReactNode } from "react";
import Link from "next/link";
import {
  formatCell,
  formatPercentFromFraction,
  formatRatio,
  formatSignedRatio,
  signColor,
} from "@/client/format";

export type TableHeader = string | { label: string; className?: string };

/**
 * Shared research table wrapper and header row (#1679).
 * Preserves the exact DOM structure and classes of the research tables it replaces.
 */
export function ResearchTable({
  caption,
  headers,
  children,
  className = "overflow-x-auto rounded-xl border border-border",
  tableClassName = "w-full text-left text-sm",
  theadClassName = "bg-card text-xs uppercase text-gray-500",
  tableTestId,
}: {
  caption?: string;
  headers: readonly TableHeader[];
  children: ReactNode;
  className?: string;
  tableClassName?: string;
  theadClassName?: string;
  tableTestId?: string;
}) {
  return (
    <div className={className}>
      <table className={tableClassName} data-testid={tableTestId}>
        {caption && <caption className="sr-only">{caption}</caption>}
        <thead className={theadClassName}>
          <tr>
            {headers.map((h) => {
              const { label, className: thCls } = typeof h === "string" ? { label: h, className: undefined } : h;
              return (
                <th key={label} scope="col" className={`px-4 py-3 ${thCls ?? ""}`.trim()}>
                  {label}
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}

export function EntityLink({
  issuerId,
  label,
  convention,
  className = "text-accent hover:underline",
  showEvidence,
}: {
  issuerId: string;
  label: string;
  convention?: string;
  className?: string;
  showEvidence?: boolean;
}) {
  const qs = convention && convention !== "historical_cagr" ? `?convention=${encodeURIComponent(convention)}` : "";
  return (
    <Link
      href={`/research/entities/${encodeURIComponent(issuerId)}${qs}`}
      className={className}
      title={issuerId}
      data-evidence={showEvidence ? issuerId : undefined}
    >
      {label}
    </Link>
  );
}

export function EntityTh({
  issuerId,
  label,
  convention,
}: {
  issuerId: string;
  label: string;
  convention?: string;
}) {
  return (
    <th scope="row" className="px-4 py-3 font-medium">
      <EntityLink issuerId={issuerId} label={label} convention={convention} />
    </th>
  );
}

export function RatioTd({
  value,
  mono,
}: {
  value: string | number | null | undefined;
  mono?: boolean;
}) {
  return (
    <td
      className={`px-4 py-3 ${mono ? "font-mono " : ""}tabular-nums`}
      title={value != null ? String(value) : undefined}
    >
      {formatCell(formatRatio(value ?? null))}
    </td>
  );
}

export function SignedRatioTd({ value }: { value: string | null | undefined }) {
  return (
    <td className={`px-4 py-3 tabular-nums ${signColor(value ?? null)}`} title={value ?? undefined}>
      {formatCell(formatSignedRatio(value ?? null))}
    </td>
  );
}

export function PercentTd({ value }: { value: string | number | null | undefined }) {
  return (
    <td className="px-4 py-3 tabular-nums" title={value != null ? String(value) : undefined}>
      {formatCell(formatPercentFromFraction(value ?? null))}
    </td>
  );
}
