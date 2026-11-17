import Link from "next/link";
import { api } from "@/client/api";
import { formatCell, formatRatio, formatSignedRatio, formatUsdMagnitude, signColor } from "@/client/format";
import { valuationBasisLabel } from "@/client/strategy";
import { AvailabilityBadge, ReadStateNotice } from "@/components/read-state";
import { ResearchTable } from "@/components/research-table";
import { loadAuthed } from "../../_load";

export const dynamic = "force-dynamic";

export default async function ComparePage({ searchParams }: { searchParams: Promise<{ cutoff?: string }> }) {
  const { cutoff } = await searchParams;
  const { data, error } = await loadAuthed("/research/compare", (opts) => api.getRankings({ cutoff, universe: "all" }, opts));
  const rows = data?.rows ?? [];
  const basisLabel = valuationBasisLabel(rows.find((r) => r.valuation_basis)?.valuation_basis);
  const headers = ["Issuer", "Capital-adjusted labor efficiency", "Current P/S", `Current ${basisLabel}`, "Tier", `Valuation gap (${basisLabel})`, "Confidence", "Availability"];

  return (
    <section aria-labelledby="compare-heading" className="space-y-6">
      <div>
        <h1 id="compare-heading" className="text-2xl font-bold tracking-tight">Issuer comparison</h1>
        <p className="mt-2 text-sm text-gray-400">Side-by-side operating efficiency and valuation at one cutoff.</p>
      </div>
      {error && <ReadStateNotice state={{ kind: "error", message: error }} />}
      {data && !data.available && <ReadStateNotice state={{ kind: "unavailable", reason: data.reason ?? "unavailable" }} />}
      {data?.available && rows.length > 0 && (
        <>
          <p className="text-sm text-gray-500">Cutoff {data.cutoff_at} — {data.total} issuer(s).</p>
          <ResearchTable caption={`Issuer comparison at ${data.cutoff_at ?? ""}`} headers={headers}>
            {rows.map((row) => (
              <tr key={row.issuer_id} className="border-t border-border">
                <th scope="row" className="px-4 py-3 font-medium">
                  <Link href={`/research/entities/${encodeURIComponent(row.issuer_id)}`} className="text-accent hover:underline" title={row.issuer_id}>{row.label}</Link>
                </th>
                <td className="px-4 py-3 tabular-nums">
                  {formatCell(formatUsdMagnitude(row.capital_adjusted_labor_efficiency))}
                  {row.capital_adjusted_labor_efficiency && <div className="text-[11px] text-gray-500">{row.labor_efficiency_metric ?? "metric not recorded"}</div>}
                </td>
                <td className="px-4 py-3 tabular-nums">{formatCell(formatRatio(row.current_price_to_sales))}</td>
                <td className="px-4 py-3 tabular-nums">{formatCell(formatRatio(row.current_multiple))}</td>
                <td className="px-4 py-3">{formatCell(row.tier)}</td>
                <td className={`px-4 py-3 tabular-nums ${signColor(row.valuation_gap)}`}>{formatCell(formatSignedRatio(row.valuation_gap))}</td>
                <td className="px-4 py-3 tabular-nums">{formatCell(formatRatio(row.confidence))}</td>
                <td className="px-4 py-3"><AvailabilityBadge status={row.availability ?? "unavailable"} /></td>
              </tr>
            ))}
          </ResearchTable>
        </>
      )}
    </section>
  );
}
