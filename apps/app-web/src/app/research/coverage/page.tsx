import Link from "next/link";
import { api } from "@/client/api";
import { formatCell, formatNumberMagnitude, formatUsdMagnitude } from "@/client/format";
import { DASHBOARD_STRATEGY_ID, WIDE_PANEL_INPUT_COLUMNS } from "@/client/strategy";
import { PanelNodeTd } from "@/components/panel-node-cell";
import { ResearchTable } from "@/components/research-table";
import { loadAuthed } from "../../_load";

export const dynamic = "force-dynamic";

export default async function CoveragePage() {
  const { data, error } = await loadAuthed("/research/coverage", (opts) => api.getStrategy({ strategy_id: DASHBOARD_STRATEGY_ID }, opts));
  const decisions = data?.decisions ?? [];
  const excluded = decisions.filter((d) => !d.eligible);
  const widePanel = data?.wide_panel ?? {};

  const td = "px-4 py-3";
  return (
    <section aria-labelledby="coverage-heading" className="space-y-6">
      <div><h1 id="coverage-heading" className="text-2xl font-bold tracking-tight">Universe input coverage</h1><p className="mt-2 text-sm text-gray-400">Excluded subjects and wide-panel inputs from <code className="text-accent">mart.metric_node_values</code>.</p></div>
      {error && <p className="text-sm text-red-400">{error}</p>}
      {data && !data.available && <p className="text-sm text-gray-400">Unavailable: {data.reason}</p>}
      {data?.available && (
        <>
          <p className="text-sm text-gray-400">{decisions.length - excluded.length} usable / {decisions.length} total ({excluded.length} excluded)</p>
          <ResearchTable caption="Excluded issuers" headers={["Issuer", "Outcome", "Reason code", "Explanation", "Cutoff"]}>
            {excluded.map((d) => (
              <tr key={d.issuer_id} className="border-t border-border">
                <th scope="row" className={`${td} font-medium`}><Link href={`/research/entities/${encodeURIComponent(d.issuer_id)}`} className="text-accent hover:underline">{d.label}</Link></th>
                <td className={`${td} font-mono text-xs`}>{d.outcome}</td>
                <td className={`${td} font-mono text-xs`}>{formatCell(d.exclusion_reason)}</td>
                <td className={`${td} text-xs text-gray-300`}>{formatCell(d.exclusion_label)}</td>
                <td className={`${td} font-mono text-xs`}>{d.cutoff_at}</td>
              </tr>
            ))}
          </ResearchTable>
          <div>
            <h2 className="mb-3 text-lg font-semibold">Wide panel input coverage</h2>
            <ResearchTable caption="Wide panel inputs" headers={["Issuer", ...WIDE_PANEL_INPUT_COLUMNS.map((c) => c.label)]}>
              {decisions.map((d) => {
                const cells = widePanel[d.issuer_id] ?? {};
                return (
                  <tr key={d.issuer_id} className="border-t border-border">
                    <th scope="row" className={`${td} font-medium`}>{d.label}</th>
                    {WIDE_PANEL_INPUT_COLUMNS.map((col) => {
                      const node = cells[col.key];
                      const fmt = node?.value ? (col.monetary ? formatUsdMagnitude(node.value) : formatNumberMagnitude(node.value)) : null;
                      return <PanelNodeTd key={col.key} node={node} formatted={fmt} />;
                    })}
                  </tr>
                );
              })}
            </ResearchTable>
          </div>
        </>
      )}
    </section>
  );
}
