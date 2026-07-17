import Link from "next/link";
import { DashboardNav } from "@/components/dashboard-nav";
import { AvailabilityBadge, ReadStateNotice } from "@/components/read-state";
import { loadEntityDetail } from "@/server/dashboard";

export const dynamic = "force-dynamic";

function cell(value: string | null): string {
  return value ?? "—";
}

export default async function EntityDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const issuerId = decodeURIComponent(id);
  const state = loadEntityDetail(issuerId);

  return (
    <section aria-labelledby="entity-heading" className="space-y-6">
      <div>
        <h1 id="entity-heading" className="text-2xl font-bold tracking-tight">
          {issuerId}
        </h1>
        <p className="mt-2 text-sm text-gray-400">
          Materialized operating efficiency, valuation, and traceability across cutoffs.
        </p>
      </div>

      <DashboardNav />

      <ReadStateNotice state={state} />

      {state.kind === "ready" && (
        <div className="overflow-x-auto rounded-xl border border-border">
          <table className="w-full text-left text-sm">
            <caption className="sr-only">{issuerId} across cutoffs</caption>
            <thead className="bg-card text-xs uppercase text-gray-500">
              <tr>
                <th scope="col" className="px-4 py-3">Cutoff</th>
                <th scope="col" className="px-4 py-3">Capital-adjusted labor efficiency</th>
                <th scope="col" className="px-4 py-3">Current P/S</th>
                <th scope="col" className="px-4 py-3">Tier</th>
                <th scope="col" className="px-4 py-3">Valuation gap</th>
                <th scope="col" className="px-4 py-3">Confidence</th>
                <th scope="col" className="px-4 py-3">Availability</th>
                <th scope="col" className="px-4 py-3">Trace</th>
              </tr>
            </thead>
            <tbody>
              {state.data.rows.map((row) => (
                <tr key={row.cutoffAt} className="border-t border-border">
                  <th scope="row" className="px-4 py-3 font-medium">
                    {row.cutoffAt}
                  </th>
                  <td className="px-4 py-3">{cell(row.capitalAdjustedLaborEfficiency)}</td>
                  <td className="px-4 py-3">{cell(row.currentPriceToSales)}</td>
                  <td className="px-4 py-3">{cell(row.tier)}</td>
                  <td className="px-4 py-3">{cell(row.valuationGap)}</td>
                  <td className="px-4 py-3">{cell(row.confidence)}</td>
                  <td className="px-4 py-3">
                    <AvailabilityBadge status={row.availability} />
                  </td>
                  <td className="px-4 py-3">
                    <Link
                      href={`/trace?issuer=${encodeURIComponent(row.issuerId)}&cutoff=${encodeURIComponent(row.cutoffAt)}`}
                      className="font-mono text-xs text-gray-400 hover:text-accent"
                    >
                      {row.traceId}
                    </Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
