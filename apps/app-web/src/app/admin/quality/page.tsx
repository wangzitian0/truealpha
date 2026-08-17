import { redirect } from "next/navigation";
import { AvailabilityBadge, ReadStateNotice } from "@/components/read-state";
import { captureAvailableCount, loadToptQuality } from "@/server/topt-quality";
import { loadQualityFunnel, type FunnelLayer } from "@/server/admin/funnel";
import { loadOpsOverview } from "@/server/admin/ops";
import { formatRatio, formatUsdMagnitude } from "@/client/format";
import { getServerPrincipal } from "@/server/auth/request-context";

const LAYER_BAR: Record<FunnelLayer["status"], string> = {
  ok: "bg-emerald-500/70",
  warn: "bg-amber-400/70",
  crit: "bg-red-500/70",
  muted: "bg-gray-600/40",
};

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
  const funnel = await loadQualityFunnel({
    context: principal.context,
    principalKind: principal.principalKind,
  });
  const ops = await loadOpsOverview({ principalKind: principal.principalKind });
  const quotaHeadline =
    ops.kind === "ready" && ops.data.quotaToday.length > 0
      ? ops.data.quotaToday.map((q) => `${q.source} ${q.fetches}`).join(" · ")
      : "no fetches recorded today";

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

      {/* #495: an `&&` with no else made the headline diagnostic vanish without
          comment whenever it could not be computed, which reads as "nothing is
          wrong" rather than "not computed". Every state of this block now says
          something. */}
      {funnel.kind !== "ready" && (
        <div className="rounded-xl border border-border bg-card p-5">
          <h2 className="font-semibold">L0–L5 funnel</h2>
          {/* The same renderer and the same words as everything else — the
              funnel is a ReadState now, so its absence states do not get their
              own prose. `unavailable` carries an operationally useful reason,
              so it is surfaced rather than flattened into "not computed". */}
          <ReadStateNotice
            state={funnel}
            overrides={{
              denied: "Not computed: this view requires a verified administrator identity.",
              empty:
                "Not computed yet — no accepted run has reported the layer metrics this funnel reads.",
              unavailable:
                funnel.kind === "unavailable"
                  ? `Not computed yet: ${funnel.reason}. No accepted run has reported the layer metrics this funnel reads.`
                  : undefined,
            }}
          />
        </div>
      )}

      {funnel.kind === "ready" && (
        <div className="rounded-xl border border-border bg-card p-5">
          <h2 className="font-semibold">L0–L5 funnel — where the data narrows is where the problem is</h2>
          <div className="mt-4 space-y-2">
            <div className="grid grid-cols-[7rem_1fr] items-center gap-3">
              <div className="text-right">
                <span className="font-mono text-xs text-gray-400">L0 Sources</span>
              </div>
              <div>
                <div className="flex h-8 items-center rounded bg-gray-600/40 px-3 font-mono text-xs">
                  {quotaHeadline}
                </div>
                <p className="mt-0.5 text-xs text-gray-500">raw.fetches today, per source</p>
              </div>
            </div>
            {funnel.data.layers.map((layer) => (
              <div key={layer.key} className="grid grid-cols-[7rem_1fr] items-center gap-3">
                <div className="text-right">
                  <span className="font-mono text-xs text-gray-400">
                    {layer.key} {layer.title}
                  </span>
                </div>
                <div>
                  <div
                    className={`flex h-8 items-center rounded px-3 font-mono text-xs text-white ${LAYER_BAR[layer.status]}`}
                    style={{ width: `${Math.round((layer.ratio ?? 1) * 100)}%`, minWidth: "9rem" }}
                  >
                    {layer.headline}
                  </div>
                  <p className="mt-0.5 text-xs text-gray-500">{layer.detail}</p>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {state.kind === "ready" && (
        <>
          <p className="text-sm text-gray-500">
            Run <code className="text-accent">{state.data.run_id}</code> — GPPE available for{" "}
            {state.data.cells.filter((row) => row.availability === "available").length} / {state.data.cells.length}{" "}
            listings
            {/* The capture-plane numerator is the quality report's own cell-level count —
                NOT `available_count`, which is the mart's listing-level figure (scale /20)
                and produced "18 / 84 cells" here while the funnel said 82/84 (#539). */}
            {captureAvailableCount(state.data) !== null && (
              <>
                ; capture plane {captureAvailableCount(state.data)} / {state.data.requested_count} cells available
              </>
            )}
            .
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
