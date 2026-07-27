import { redirect } from "next/navigation";
import { AvailabilityBadge, ReadStateNotice } from "@/components/read-state";
import { loadToptQuality } from "@/server/topt-quality";
import { formatRatio, formatUsdMagnitude } from "@/client/format";
import { getServerPrincipal } from "@/server/auth/request-context";

export const dynamic = "force-dynamic";

function cell(value: string | null): string {
  return value ?? "—";
}

/**
 * #494 P0a: headline metrics lifted out of the raw report. Reads
 * defensively — the payload is the persisted, content-addressed report
 * (append-only); a missing key renders as an absent card, never a crash.
 */
function qualityHeadline(quality: unknown): { label: string; value: string }[] {
  if (typeof quality !== "object" || quality === null) return [];
  const q = quality as Record<string, unknown>;
  const ratio = (key: string): string | null => {
    const raw = q[key];
    return typeof raw === "string" || typeof raw === "number" ? formatRatio(String(raw)) : null;
  };
  const cards: { label: string; value: string | null }[] = [
    { label: "Availability (cells)", value: ratio("availability") },
    { label: "Freshness", value: ratio("freshness") },
    { label: "Independent reconciliation", value: ratio("independent_reconciliation") },
    { label: "Lineage completeness", value: ratio("lineage_completeness") },
    { label: "Mean confidence", value: ratio("denominator_mean_confidence") },
    { label: "Complete", value: typeof q.complete === "boolean" ? String(q.complete) : null },
  ];
  return cards.filter((c): c is { label: string; value: string } => c.value !== null);
}

export default async function ToptQualityPage() {
  const principal = await getServerPrincipal();
  if (!principal) redirect("/login?from=%2Fadmin%2Fquality");
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
            Run <code className="text-accent">{state.data.run_id}</code> — GPPE available for{" "}
            {state.data.cells.filter((row) => row.availability === "available").length} / {state.data.cells.length}{" "}
            listings; capture plane {state.data.available_count} / {state.data.requested_count} cells available.
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
                    <td className="px-4 py-3 tabular-nums" title={row.gppe ?? undefined}>
                      {cell(formatUsdMagnitude(row.gppe))}
                    </td>
                    <td className="px-4 py-3 tabular-nums" title={row.confidence ?? undefined}>
                      {cell(formatRatio(row.confidence))}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {state.data.quality && (
            <div className="rounded-xl border border-border bg-card p-5">
              <h2 className="font-semibold">Quality report</h2>
              <dl className="mt-4 grid grid-cols-2 gap-3 md:grid-cols-3">
                {qualityHeadline(state.data.quality).map(({ label, value }) => (
                  <div key={label} className="rounded-lg border border-border bg-background px-4 py-3">
                    <dt className="text-xs uppercase tracking-wide text-gray-500">{label}</dt>
                    <dd className="mt-1 text-lg font-semibold tabular-nums">{value}</dd>
                  </div>
                ))}
              </dl>
              <details className="mt-4">
                <summary className="cursor-pointer text-sm text-gray-400">Raw report (exact, content-addressed)</summary>
                <pre className="mt-2 overflow-x-auto text-xs text-gray-400">
                  {JSON.stringify(state.data.quality, null, 2)}
                </pre>
              </details>
            </div>
          )}
        </>
      )}
    </section>
  );
}
