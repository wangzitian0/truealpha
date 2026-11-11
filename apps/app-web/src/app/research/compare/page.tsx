import Link from "next/link";
import { redirect } from "next/navigation";
import { formatCell, formatRatio, formatSignedRatio, formatUsdMagnitude, signColor } from "@/client/format";
import { AvailabilityBadge, ReadStateNotice } from "@/components/read-state";
import { NextPageLink } from "@/components/next-page-link";
import { ResearchTable } from "@/components/research-table";
import { valuationBasisLabel } from "@/contracts/strategyRun";
import { loadComparison } from "@/server/dashboard";
import { entityLabel, loadEntityDisplayMap } from "@/server/mart/entity-resolution";
import { getServerPrincipal } from "@/server/auth/request-context";

export const dynamic = "force-dynamic";

export default async function ComparePage({
  searchParams,
}: {
  searchParams: Promise<{ cursor?: string; cutoff?: string }>;
}) {
  const principal = await getServerPrincipal();
  if (!principal) redirect("/login?from=%2Fresearch%2Fcompare");
  const params = await searchParams;
  const state = await loadComparison(principal.context, { cutoffAt: params.cutoff, cursor: params.cursor ?? null });
  const names = await loadEntityDisplayMap();
  const basisLabel = valuationBasisLabel(
    state.kind === "ready" ? (state.data.rows.find((row) => row.valuationBasis)?.valuationBasis ?? null) : null,
  );
  const headers = [
    "Issuer",
    "Capital-adjusted labor efficiency",
    "Current P/S",
    `Current ${basisLabel}`,
    "Tier",
    `Valuation gap (${basisLabel})`,
    "Confidence",
    "Availability",
  ];

  return (
    <section aria-labelledby="compare-heading" className="space-y-6">
      <div>
        <h1 id="compare-heading" className="text-2xl font-bold tracking-tight">Issuer comparison</h1>
        <p className="mt-2 text-sm text-gray-400">
          Side-by-side operating efficiency and valuation for the materialized universe at one cutoff. Read-only; no
          cross-issuer metric is computed here.
        </p>
      </div>

      <ReadStateNotice state={state} />

      {state.kind === "ready" && (
        <>
          <p className="text-sm text-gray-500">Cutoff {state.data.cutoffAt} — {state.data.page.total} issuer(s).</p>
          <ResearchTable caption={`Issuer comparison at ${state.data.cutoffAt}`} headers={headers}>
            {state.data.rows.map((row) => (
              <tr key={row.issuerId} className="border-t border-border">
                <th scope="row" className="px-4 py-3 font-medium">
                  <Link href={`/research/entities/${encodeURIComponent(row.issuerId)}`} className="text-accent hover:underline" title={row.issuerId}>
                    {entityLabel(row.issuerId, names)}
                  </Link>
                </th>
                <td className="px-4 py-3 tabular-nums" title={row.capitalAdjustedLaborEfficiency ?? undefined}>
                  {formatCell(formatUsdMagnitude(row.capitalAdjustedLaborEfficiency))}
                  {row.capitalAdjustedLaborEfficiency !== null && (
                    <div className="text-[11px] text-gray-500">{row.laborEfficiencyMetric ?? "metric not recorded"}</div>
                  )}
                </td>
                <td className="px-4 py-3 tabular-nums" title={row.currentPriceToSales ?? undefined}>
                  {formatCell(formatRatio(row.currentPriceToSales))}
                </td>
                <td className="px-4 py-3 tabular-nums" title={row.currentMultiple ?? undefined}>
                  {formatCell(formatRatio(row.currentMultiple ?? null))}
                </td>
                <td className="px-4 py-3">{formatCell(row.tier)}</td>
                <td className={`px-4 py-3 tabular-nums ${signColor(row.valuationGap)}`} title={row.valuationGap ?? undefined}>
                  {formatCell(formatSignedRatio(row.valuationGap))}
                </td>
                <td className="px-4 py-3 tabular-nums" title={row.confidence ?? undefined}>
                  {formatCell(formatRatio(row.confidence))}
                </td>
                <td className="px-4 py-3"><AvailabilityBadge status={row.availability} /></td>
              </tr>
            ))}
          </ResearchTable>
          <NextPageLink
            hasMore={state.data.page.hasMore}
            nextCursor={state.data.page.nextCursor}
            href={`/research/compare?cursor=${encodeURIComponent(state.data.page.nextCursor ?? "")}${
              params.cutoff ? `&cutoff=${encodeURIComponent(params.cutoff)}` : ""
            }`}
          />
        </>
      )}
    </section>
  );
}
