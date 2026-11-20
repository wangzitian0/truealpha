import Link from "next/link";
import { api } from "@/client/api";
import { formatCell, formatNumberMagnitude, formatRatio, formatSignedRatio, formatTierLabel, formatUsdMagnitude, signColor } from "@/client/format";
import { PEG_CONVENTION_LABELS, PEG_GROWTH_CONVENTIONS, type PegGrowthConvention, valuationBasisLabel } from "@/client/strategy";
import { CardExportButton } from "@/components/card-export-button";
import { ClaimCeilingBanner } from "@/components/claim-ceiling";
import { PanelNodeTd } from "@/components/panel-node-cell";
import { ProvenanceCell } from "@/components/provenance-cell";
import { AvailabilityBadge, ReadStateNotice } from "@/components/read-state";
import { ResearchTable } from "@/components/research-table";
import { UNIVERSES, isHeadScheduledFromContract } from "@/contracts/environmentContract.generated";
import { universesWithRole } from "@/contracts/universes";
import { loadAuthed } from "../../_load";

export const dynamic = "force-dynamic";

const pill = (active: boolean) =>
  active ? "border border-sky-500 bg-sky-950/70 text-sky-300" : "border border-border bg-slate-900 text-gray-400 hover:border-gray-600 hover:text-gray-200";

export default async function RankingsPage({ searchParams }: { searchParams: Promise<{ cutoff?: string; convention?: string; universe?: string }> }) {
  const params = await searchParams;
  const convention: PegGrowthConvention = params.convention === "consensus" || params.convention === "guidance" ? params.convention : "historical_cagr";
  const strategyUniverses = UNIVERSES.filter(
    (u) => u.roles.includes("strategy") && isHeadScheduledFromContract(u.id_prefix, process.env.ENVIRONMENT),
  ).map((u) => u.key);
  const universes =
    strategyUniverses.length > 0
      ? strategyUniverses
      : UNIVERSES.filter((u) => !u.roles.includes("sentinel") && u.scheduled_somewhere).map((u) => u.key);
  const universe = params.universe || universesWithRole("strategy")[0] || universes[0] || "all";
  const { data, error } = await loadAuthed("/research/rankings", (opts) => api.getRankings({ cutoff: params.cutoff, universe }, opts));
  const rows = data?.rows ?? [];
  const basisLabel = valuationBasisLabel(rows.find((r) => r.valuation_basis)?.valuation_basis);
  const headers = ["Rank", "Issuer", "Tier", "Current P/S", `Current ${basisLabel}`, `Target ${basisLabel}`, `Valuation gap (${basisLabel})`, "GPPE (uniform)", "GPPE (banking)", "GPPE (opfin)", "Employees", `PEG (${PEG_CONVENTION_LABELS[convention]})`, "PEG rank", "Confidence", "Availability", "Inputs"];

  return (
    <section aria-labelledby="rankings-heading" className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 id="rankings-heading" className="text-2xl font-bold tracking-tight">Valuation rankings</h1>
          <p className="mt-1 text-sm text-gray-400">Governed strategy rankings served from OpenAPI <code className="text-accent">/api/v1/strategy/rankings</code>.</p>
        </div>
        <CardExportButton cutoff={data?.cutoff_at ?? undefined} convention={convention} universe={universe} />
      </div>
      <div className="flex flex-wrap gap-2 text-xs">
        {universes.map((u) => (
          <Link key={u} href={`/research/rankings?universe=${encodeURIComponent(u)}${convention !== "historical_cagr" ? `&convention=${convention}` : ""}`} className={`rounded-md px-2.5 py-1 ${pill(u === universe)}`}>{u}</Link>
        ))}
        {PEG_GROWTH_CONVENTIONS.map((c) => (
          <Link key={c} href={`/research/rankings?universe=${encodeURIComponent(universe)}&convention=${c}`} className={`rounded-md px-2.5 py-1 ${pill(c === convention)}`}>{PEG_CONVENTION_LABELS[c]}</Link>
        ))}
      </div>
      <ClaimCeilingBanner />
      {error && <ReadStateNotice state={{ kind: "error", message: error }} />}
      {data && !data.available && <ReadStateNotice state={{ kind: "unavailable", reason: data.reason ?? "unavailable" }} />}
      {data?.available && rows.length === 0 && <ReadStateNotice state={{ kind: "empty" }} />}
      {data?.available && rows.length > 0 && (
        <ResearchTable caption={`Valuation rankings at ${data.cutoff_at ?? ""}`} headers={headers}>
          {rows.map((row) => {
            const c = "px-4 py-3";
            return (
              <tr key={row.issuer_id} className="border-t border-border">
                <td className={`${c} font-mono`}>{formatCell(row.rank)}</td>
                <th scope="row" className={`${c} font-medium`}><Link href={`/research/entities/${encodeURIComponent(row.issuer_id)}`} className="text-accent hover:underline" title={row.issuer_id}>{row.label}</Link></th>
                <td className={c}>{formatCell(formatTierLabel(row.tier))}</td>
                <td className={`${c} tabular-nums`}>{formatCell(formatRatio(row.current_price_to_sales))}</td>
                <td className={`${c} tabular-nums`}>{formatCell(formatRatio(row.current_multiple))}</td>
                <td className={`${c} tabular-nums`}>{formatCell(formatRatio(row.target_multiple))}</td>
                <td className={`${c} tabular-nums ${signColor(row.valuation_gap)}`}>{formatCell(formatSignedRatio(row.valuation_gap))}</td>
                <PanelNodeTd node={row.gppe_variants?.gppe_uniform_charge_v0} formatted={formatUsdMagnitude(row.gppe_variants?.gppe_uniform_charge_v0?.value ?? null)} />
                <PanelNodeTd node={row.gppe_variants?.gppe_banking_tce_v1} formatted={formatUsdMagnitude(row.gppe_variants?.gppe_banking_tce_v1?.value ?? null)} />
                <PanelNodeTd node={row.gppe_variants?.gppe_operating_financial_v1} formatted={formatUsdMagnitude(row.gppe_variants?.gppe_operating_financial_v1?.value ?? null)} />
                <PanelNodeTd node={row.panel_inputs?.employees_total} formatted={formatNumberMagnitude(row.panel_inputs?.employees_total?.value ?? null)} />
                <td className={`${c} tabular-nums`}>{convention === "historical_cagr" ? formatCell(formatRatio(row.peg)) : "unsourced"}</td>
                <td className={`${c} tabular-nums`}>{convention === "historical_cagr" ? formatCell(row.peg_rank) : "—"}</td>
                <td className={`${c} tabular-nums`}>{formatCell(formatRatio(row.confidence))}</td>
                <td className={c}><AvailabilityBadge status={row.availability ?? "unavailable"} /></td>
                <td className={c}><ProvenanceCell row={row} /></td>
              </tr>
            );
          })}
        </ResearchTable>
      )}
    </section>
  );
}
