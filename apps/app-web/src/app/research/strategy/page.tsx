import Link from "next/link";
import { api } from "@/client/api";
import { formatCell, formatPercentFromFraction, formatRatio, formatSignedRatio, signColor } from "@/client/format";
import { DASHBOARD_STRATEGY_ID, valuationBasisLabel } from "@/client/strategy";
import { ClaimCeilingBanner } from "@/components/claim-ceiling";
import { ResearchTable } from "@/components/research-table";
import { loadAuthed } from "../../_load";

export const dynamic = "force-dynamic";

const OUTCOME_LABEL: Record<string, string> = {
  selected: "Selected",
  ranked_beyond_selection_count: "Ranked (not selected)",
  rejected_valuation_above_tier_band: "Rejected — valuation above tier band",
  excluded: "Excluded",
};

export default async function StrategyRunsPage() {
  const { data, error } = await loadAuthed("/research/strategy", (opts) => api.getStrategy({ strategy_id: DASHBOARD_STRATEGY_ID }, opts));
  const decisions = data?.decisions ?? [];
  const basisLabel = valuationBasisLabel(decisions.find((d) => d.valuation_basis)?.valuation_basis);
  const headers = ["Issuer", "Cutoff", "Status", "Tier", `Current ${basisLabel}`, `Target ${basisLabel}`, `Gap (${basisLabel})`, "Confidence", "Rank", "Weight"];

  return (
    <section aria-labelledby="strategy-runs-heading" className="space-y-4">
      <h1 id="strategy-runs-heading" className="text-2xl font-bold tracking-tight">Strategy runs</h1>
      {error && <p role="status" className="rounded-lg border border-border bg-card p-4 text-red-400">{error}</p>}
      {data && !data.available && (
        <p role="status" className="rounded-lg border border-border bg-card p-4 text-gray-400">Unavailable: {data.reason ?? "unknown"} ({data.strategy_id})</p>
      )}
      {data?.available && (
        <p className="text-sm text-gray-400">
          {data.strategy_id} — source: {data.source}. Corpus <code className="text-accent">{data.corpus_sha256}</code>
          {data.definition_version ? ` · v${data.definition_version} (${data.review_state ?? "unknown"})` : ""}
        </p>
      )}
      <ClaimCeilingBanner />
      {data?.available && decisions.length > 0 && (
        <ResearchTable caption={`Decisions for ${data.strategy_id}`} headers={headers}>
          {decisions.map((d) => {
            const c = "px-4 py-3";
            return (
              <tr key={`${d.issuer_id}-${d.cutoff_at}`} className="border-t border-border">
                <th scope="row" className={`${c} font-medium`}><Link href={`/research/entities/${encodeURIComponent(d.issuer_id)}`} className="text-accent hover:underline" title={d.issuer_id}>{d.label}</Link></th>
                <td className={`${c} font-mono text-xs`}>{d.cutoff_at}</td>
                <td className={`${c} text-xs`}>{OUTCOME_LABEL[d.outcome] ?? d.outcome}{d.exclusion_reason ? `: ${d.exclusion_reason}` : ""}</td>
                <td className={c}>{formatCell(d.tier)}</td>
                <td className={`${c} tabular-nums`}>{formatCell(formatRatio(d.current_multiple))}</td>
                <td className={`${c} tabular-nums`}>{formatCell(formatRatio(d.target_multiple))}</td>
                <td className={`${c} tabular-nums ${signColor(d.valuation_gap)}`}>{formatCell(formatSignedRatio(d.valuation_gap))}</td>
                <td className={`${c} tabular-nums`}>{formatCell(formatRatio(d.confidence))}</td>
                <td className={`${c} tabular-nums`}>{formatCell(d.rank)}</td>
                <td className={`${c} tabular-nums`}>{formatCell(formatPercentFromFraction(d.target_weight))}</td>
              </tr>
            );
          })}
        </ResearchTable>
      )}
    </section>
  );
}
