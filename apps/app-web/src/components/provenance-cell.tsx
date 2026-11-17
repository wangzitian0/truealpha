import Link from "next/link";
import type { StrategyDecisionItem } from "@/client/api";
import { formatShortSha } from "@/client/format";

export function ProvenanceCell({ row }: { row: StrategyDecisionItem }) {
  const p = row.provenance;
  const vintages: [string, string | null | undefined][] = [
    ["Operating", p?.operating_period_end],
    ["Revenue", p?.revenue_period_end],
    ["Shares", p?.shares_period_end],
  ];
  const known = vintages.filter(([, value]) => Boolean(value)).map(([, value]) => value as string);
  const summary = p?.operating_period_end ?? (known[0] ?? "vintage unknown");

  return (
    <details className="group" data-evidence="true">
      <summary className="cursor-pointer list-none font-mono text-xs text-gray-400 hover:text-accent">
        <span className="underline decoration-dotted underline-offset-2">{summary}</span>
        <span className="ml-1 text-gray-600 group-open:hidden">▸</span>
        <span className="ml-1 hidden text-gray-600 group-open:inline">▾</span>
      </summary>
      <dl className="mt-2 space-y-1 font-mono text-[11px] leading-relaxed text-gray-400">
        {vintages.map(([label, value]) => (
          <div key={label} className="flex gap-2">
            <dt className="w-20 shrink-0 text-gray-600">{label}</dt>
            <dd className={value ? "" : "text-amber-500"}>{value ?? "unknown"}</dd>
          </div>
        ))}
        <div className="flex gap-2">
          <dt className="w-20 shrink-0 text-gray-600">Universe</dt>
          <dd>{p?.universe_version ?? "unknown"}</dd>
        </div>
        <div className="flex gap-2">
          <dt className="w-20 shrink-0 text-gray-600">GPPE def</dt>
          <dd title={p?.gppe_definition_sha256 ?? undefined}>
            {p?.gppe_definition_sha256 ? formatShortSha(p.gppe_definition_sha256) : "unknown"}
          </dd>
        </div>
      </dl>
      <Link
        href={`/research/trace?issuer=${encodeURIComponent(row.issuer_id)}&cutoff=${encodeURIComponent(row.cutoff_at)}`}
        className="mt-2 inline-block font-mono text-[11px] text-accent hover:underline"
      >
        full trace →
      </Link>
    </details>
  );
}
