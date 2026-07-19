import { redirect } from "next/navigation";
import { AvailabilityBadge, ReadStateNotice } from "@/components/read-state";
import { loadToptQuality } from "@/server/topt-quality";
import { getServerPrincipal } from "@/server/auth/request-context";

export const dynamic = "force-dynamic";

function cell(value: string | null): string {
  return value ?? "—";
}

export default async function ToptQualityPage() {
  const principal = await getServerPrincipal();
  if (!principal) redirect("/login?from=%2Fresearch%2Fquality");
  const state = await loadToptQuality(principal.context);

  return (
    <section aria-labelledby="quality-heading" className="space-y-8">
      <div>
        <h1 id="quality-heading" className="text-3xl font-bold tracking-tight">
          TOPT data quality
        </h1>
        <p className="mt-2 text-gray-400">
          Reads the governed production TOPT run through the <code className="text-accent">mart</code> read-only
          role — the same run the MCP <code className="text-accent">topt_gppe</code> tool serves (#433).
        </p>
      </div>

      <ReadStateNotice state={state} />

      {state.kind === "ready" && (
        <>
          <p className="text-sm text-gray-500">
            Run <code className="text-accent">{state.data.run_id}</code> — {state.data.available_count} /{" "}
            {state.data.requested_count} listings available.
          </p>

          <div className="overflow-x-auto rounded-xl border border-border">
            <table className="w-full text-left text-sm">
              <caption className="sr-only">GPPE results by listing for the governed production run</caption>
              <thead className="bg-card text-xs uppercase text-gray-500">
                <tr>
                  <th scope="col" className="px-4 py-3">
                    Listing
                  </th>
                  <th scope="col" className="px-4 py-3">
                    Availability
                  </th>
                  <th scope="col" className="px-4 py-3">
                    GPPE
                  </th>
                  <th scope="col" className="px-4 py-3">
                    Confidence
                  </th>
                </tr>
              </thead>
              <tbody>
                {state.data.cells.map((row) => (
                  <tr key={row.listing_id} className="border-t border-border">
                    <th scope="row" className="px-4 py-3 font-medium">
                      {row.listing_id}
                    </th>
                    <td className="px-4 py-3">
                      <AvailabilityBadge status={row.availability === "available" ? "available" : "unavailable"} />
                    </td>
                    <td className="px-4 py-3">{cell(row.gppe)}</td>
                    <td className="px-4 py-3">{cell(row.confidence)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {state.data.quality && (
            <div className="rounded-xl border border-border bg-card p-5">
              <h2 className="font-semibold">Quality report</h2>
              <pre className="mt-2 overflow-x-auto text-xs text-gray-400">
                {JSON.stringify(state.data.quality, null, 2)}
              </pre>
            </div>
          )}
        </>
      )}
    </section>
  );
}
