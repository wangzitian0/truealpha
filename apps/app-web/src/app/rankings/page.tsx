import Link from "next/link";
import { DashboardNav } from "@/components/dashboard-nav";
import { AvailabilityBadge, ReadStateNotice } from "@/components/read-state";
import { loadRanking } from "@/server/dashboard";

export const dynamic = "force-dynamic";

function cell(value: string | null): string {
  return value ?? "—";
}

export default async function RankingsPage({
  searchParams,
}: {
  searchParams: Promise<{ cursor?: string; cutoff?: string }>;
}) {
  const params = await searchParams;
  const state = loadRanking({ cutoffAt: params.cutoff, cursor: params.cursor ?? null });

  return (
    <section aria-labelledby="rankings-heading" className="space-y-6">
      <div>
        <h1 id="rankings-heading" className="text-2xl font-bold tracking-tight">
          Theme rankings
        </h1>
        <p className="mt-2 text-sm text-gray-400">
          Purest large-model-value names, ranked from the materialized strategy run. Values are read straight from the
          mart — not recomputed.
        </p>
      </div>

      <DashboardNav />

      <ReadStateNotice state={state} />

      {state.kind === "ready" && (
        <>
          <p className="text-sm text-gray-500">
            Cutoff {state.data.cutoffAt} — {state.data.page.total} member(s).
          </p>
          <div className="overflow-x-auto rounded-xl border border-border">
            <table className="w-full text-left text-sm">
              <caption className="sr-only">Ranked issuers for the large-model-value theme at {state.data.cutoffAt}</caption>
              <thead className="bg-card text-xs uppercase text-gray-500">
                <tr>
                  <th scope="col" className="px-4 py-3">Rank</th>
                  <th scope="col" className="px-4 py-3">Issuer</th>
                  <th scope="col" className="px-4 py-3">Tier</th>
                  <th scope="col" className="px-4 py-3">Current P/S</th>
                  <th scope="col" className="px-4 py-3">Valuation gap</th>
                  <th scope="col" className="px-4 py-3">Confidence</th>
                  <th scope="col" className="px-4 py-3">Availability</th>
                  <th scope="col" className="px-4 py-3">Trace</th>
                </tr>
              </thead>
              <tbody>
                {state.data.rows.map((row) => (
                  <tr key={row.issuerId} className="border-t border-border">
                    <td className="px-4 py-3">{row.rank ?? "—"}</td>
                    <th scope="row" className="px-4 py-3 font-medium">
                      <Link
                        href={`/entities/${encodeURIComponent(row.issuerId)}`}
                        className="text-accent hover:underline"
                      >
                        {row.issuerId}
                      </Link>
                    </th>
                    <td className="px-4 py-3">{cell(row.tier)}</td>
                    <td className="px-4 py-3">{cell(row.currentPriceToSales)}</td>
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
          {state.data.page.hasMore && state.data.page.nextCursor !== null && (
            <Link
              href={`/rankings?cursor=${encodeURIComponent(state.data.page.nextCursor)}`}
              className="inline-block rounded-lg border border-border bg-card px-4 py-2 text-sm text-gray-300 hover:border-accent"
            >
              Next page →
            </Link>
          )}
        </>
      )}
    </section>
  );
}
