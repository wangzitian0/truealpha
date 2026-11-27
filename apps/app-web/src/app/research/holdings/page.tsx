import { api } from "@/client/api";
import { formatCell, formatNumberMagnitude, formatRatio, formatUsdMagnitude } from "@/client/format";
import { valuationBasisLabel } from "@/client/strategy";
import { ResearchTable } from "@/components/research-table";
import { loadAuthed } from "../../_load";
import { ReadStateNotice } from "@/components/read-state";

export const dynamic = "force-dynamic";

export default async function HoldingsPage() {
  const { data, error } = await loadAuthed("/research/holdings", (opts) => api.getFunds(opts));
  const funds = data?.funds ?? [];

  return (
    <section aria-labelledby="holdings-heading" className="space-y-6">
      <div>
        <h1 id="holdings-heading" className="text-2xl font-bold tracking-tight">Fund holdings &amp; valuation</h1>
        <p className="mt-2 text-sm text-gray-400">SEC N-PORT weights joined to governed strategy valuation outputs.</p>
      </div>
      {error && <ReadStateNotice state={{ kind: "error", message: error }} />}
      {funds.length === 0 && !error ? (
        <p className="text-sm text-gray-400">No holdings captured yet.</p>
      ) : (
        funds.map((fund) => (
          <div key={fund.fund_id} className="space-y-3">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
                <h2 className="text-lg font-semibold">{fund.fund_name}</h2>
                <span className="text-xs text-gray-400">
                  period {fund.report_period} · filed weight {fund.total_weight_pct}% · resolved {fund.resolved_weight_pct}% · valued {fund.valued_weight_pct}%
                  {fund.weighted_gap ? ` · weighted gap (${valuationBasisLabel(fund.weighted_gap_basis)}) ${fund.weighted_gap}` : ""}
                  {fund.virtual_revenue ? ` · virtual rev ${formatUsdMagnitude(fund.virtual_revenue)}` : ""}
                  {fund.virtual_gross_profit ? ` · virtual GP ${formatUsdMagnitude(fund.virtual_gross_profit)}` : ""}
                  {fund.virtual_employees ? ` · virtual emp ${formatNumberMagnitude(fund.virtual_employees)}` : ""}
                  {fund.virtual_gppe ? ` · virtual GPPE ${formatUsdMagnitude(fund.virtual_gppe)}` : ""}
                  {fund.availability_status
                    ? ` · ${[fund.availability_status, fund.source_evidence_status].filter(Boolean).join("/")}`
                    : ""}
                </span>
              </div>
            </div>
            <ResearchTable caption={`Holdings for ${fund.fund_name}`} headers={["Holding", "Ticker", "Weight", "P/S", "P/GP", "Target mid", "Gap", "GPPE tier"]}>
              {(fund.lines ?? []).map((line, index) => {
                const wt = formatRatio(line.weight_pct);
                const tgt = formatRatio(line.target_pgp_midpoint) ?? formatRatio(line.target_ps_midpoint);
                return (
                  <tr key={`${line.holding_name}-${index}`} className="border-t border-border">
                    <td className="px-3 py-1.5">{line.holding_name}</td>
                    <td className="px-3 py-1.5 font-mono text-xs text-gray-400">{formatCell(line.ticker)}</td>
                    <td className="px-3 py-1.5 tabular-nums">{formatCell(wt ? `${wt}%` : null)}</td>
                    <td className="px-3 py-1.5 tabular-nums">{formatCell(formatRatio(line.current_ps))}</td>
                    <td className="px-3 py-1.5 tabular-nums">{formatCell(formatRatio(line.current_pgp))}</td>
                    <td className="px-3 py-1.5 tabular-nums">{formatCell(tgt)}</td>
                    <td className="px-3 py-1.5 tabular-nums">{formatCell(formatRatio(line.valuation_gap))}</td>
                    <td className="px-3 py-1.5 text-xs">{formatCell(line.tier)}</td>
                  </tr>
                );
              })}
            </ResearchTable>
          </div>
        ))
      )}
    </section>
  );
}
