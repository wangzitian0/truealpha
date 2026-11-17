import Link from "next/link";
import { api } from "@/client/api";
import { decodeRouteParam, formatCell, formatPercentFromFraction, formatRatio, formatSignedRatio, formatTierLabel, formatUsdMagnitude, signColor } from "@/client/format";
import { PEG_CONVENTION_LABELS, PEG_GROWTH_CONVENTIONS, type PegGrowthConvention, valuationBasisLabel } from "@/client/strategy";
import { CardExportButton } from "@/components/card-export-button";
import { StatBox } from "@/components/entity-stat-card";
import { AvailabilityBadge, ReadStateNotice } from "@/components/read-state";
import { ResearchTable } from "@/components/research-table";
import { TraceInputsTable } from "@/components/trace-inputs-table";
import { loadAuthed } from "../../../_load";

export const dynamic = "force-dynamic";

export default async function EntityDetailPage({
  params,
  searchParams,
}: {
  params: Promise<{ id: string }>;
  searchParams?: Promise<{ convention?: string }>;
}) {
  const [{ id }, sp] = await Promise.all([params, searchParams ?? Promise.resolve<{ convention?: string }>({})]);
  const convention: PegGrowthConvention = sp.convention === "consensus" || sp.convention === "guidance" ? sp.convention : "historical_cagr";
  const issuerId = decodeRouteParam(id);
  const { data: detail, error } = await loadAuthed("/research/entities", (opts) => api.getEntity(issuerId, opts));
  const rows = detail?.decisions ?? [];
  const latest = rows[0];
  const basisLabel = valuationBasisLabel(latest?.valuation_basis);

  const td = "px-4 py-3";
  const pill = "rounded-md border px-2.5 py-0.5 text-xs";
  return (
    <section aria-labelledby="entity-heading" className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="flex flex-wrap items-center gap-3">
            <h1 id="entity-heading" className="text-2xl font-bold tracking-tight text-white md:text-3xl">{detail?.label ?? issuerId}</h1>
            <span className={`${pill} border-border bg-card font-mono text-gray-400`} data-evidence={detail?.issuer_id ?? issuerId}>{detail?.issuer_id ?? issuerId}</span>
            {latest?.tier && <span className={`${pill} border-sky-800 bg-sky-950/60 font-semibold text-sky-400`}>{formatTierLabel(latest.tier)}</span>}
          </div>
          <div className="mt-2 flex gap-2 text-xs">
            {PEG_GROWTH_CONVENTIONS.map((c) => <Link key={c} href={`/research/entities/${encodeURIComponent(issuerId)}?convention=${c}`} className={`rounded px-2 py-0.5 border ${c === convention ? "border-sky-500 text-sky-300" : "border-border text-gray-400"}`}>{PEG_CONVENTION_LABELS[c]}</Link>)}
          </div>
        </div>
        <CardExportButton issuerId={detail?.issuer_id ?? issuerId} cutoff={latest?.cutoff_at} convention={convention} />
      </div>
      {error && <ReadStateNotice state={{ kind: "error", message: error }} />}
      {detail && (
        <>
          <div className="grid gap-3 md:grid-cols-4">
            <StatBox label="Labor efficiency" value={formatCell(formatUsdMagnitude(latest?.capital_adjusted_labor_efficiency))} />
            <StatBox label={`Current ${basisLabel}`} value={formatCell(formatRatio(latest?.current_multiple))} />
            <StatBox label={`Gap (${basisLabel})`} value={formatCell(formatSignedRatio(latest?.valuation_gap))} />
            <StatBox label={`PEG (${PEG_CONVENTION_LABELS[convention]})`} value={convention === "historical_cagr" ? formatCell(formatRatio(latest?.peg)) : "unsourced"} />
          </div>
          {rows.length > 0 && (
            <ResearchTable caption="Strategy decisions" headers={["Cutoff", "Rank", "Tier", `Current ${basisLabel}`, `Target ${basisLabel}`, "Gap", "Weight", "Availability", "Trace"]}>
              {rows.map((r) => (
                <tr key={r.cutoff_at} className="border-t border-border">
                  <td className={`${td} font-mono text-xs`}>{r.cutoff_at}</td>
                  <td className={`${td} font-mono`}>{formatCell(r.rank)}</td>
                  <td className={td}>{formatCell(r.tier)}</td>
                  <td className={`${td} tabular-nums`}>{formatCell(formatRatio(r.current_multiple))}</td>
                  <td className={`${td} tabular-nums`}>{formatCell(formatRatio(r.target_multiple))}</td>
                  <td className={`${td} tabular-nums ${signColor(r.valuation_gap)}`}>{formatCell(formatSignedRatio(r.valuation_gap))}</td>
                  <td className={`${td} tabular-nums`}>{formatCell(formatPercentFromFraction(r.target_weight))}</td>
                  <td className={td}><AvailabilityBadge status={r.availability ?? "unavailable"} /></td>
                  <td className={td}><Link href={`/research/trace?issuer=${encodeURIComponent(r.issuer_id)}&cutoff=${encodeURIComponent(r.cutoff_at)}`} className="text-accent hover:underline text-xs">trace →</Link></td>
                </tr>
              ))}
            </ResearchTable>
          )}
          {(detail.node_values?.length ?? 0) > 0 && <div className="rounded-xl border border-border bg-card p-5"><h2 className="text-base font-semibold">Metric forest nodes</h2><TraceInputsTable nodeValues={detail.node_values ?? []} /></div>}
          {detail.analyst_ratings && <div className="rounded-xl border border-border bg-card p-4 text-xs text-gray-300"><strong className="text-white">Analyst Consensus:</strong> {formatCell(detail.analyst_ratings.consensus_rating as string | null)} · Analysts: {String(detail.analyst_ratings.analysts_count ?? 0)}</div>}
          {detail.supply_chain_exposure && <div className="rounded-xl border border-border bg-card p-4 text-xs text-gray-300"><strong className="text-white">Supply Chain Scenario Exposure:</strong> {formatCell(detail.supply_chain_exposure.exposure_score as string | null)} · Partners: {String(detail.supply_chain_exposure.direct_partners ?? 0)}</div>}
        </>
      )}
    </section>
  );
}
