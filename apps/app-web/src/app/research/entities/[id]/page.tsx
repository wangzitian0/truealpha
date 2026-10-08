import Link from "next/link";
import { redirect } from "next/navigation";
import { AvailabilityBadge, ReadStateNotice } from "@/components/read-state";
import { CardExportButton } from "@/components/card-export-button";
import { loadEntityDetail } from "@/server/dashboard";
import { getServerPrincipal } from "@/server/auth/request-context";
import { entityLabel, loadEntityDisplayMap } from "@/server/mart/entity-resolution";
import {
  formatPercentFromFraction,
  formatRatio,
  formatSignedRatio,
  formatUsdMagnitude,
  signColor,
} from "@/client/format";

export const dynamic = "force-dynamic";

function cell(value: string | null | undefined): string {
  return value ?? "—";
}

function tierLabel(tier: string | null | undefined): string {
  if (!tier) return "—";
  if (tier === "large_model_native") return "Large Model Native";
  if (tier === "tech") return "Tech";
  if (tier === "traditional") return "Traditional";
  return tier;
}

/** Next.js does NOT decode a dynamic route segment for us here (verified
 * live against this app's Next.js version — see #373/#424's PR discussion):
 * an encodeURIComponent'd link leaves the raw percent-encoded string in
 * params.id. Malformed percent-encoding (e.g. `/entities/%E0`) must not
 * 500 the route. */
function decodeIssuerId(id: string): string {
  try {
    return decodeURIComponent(id);
  } catch {
    return id;
  }
}

export default async function EntityDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const principal = await getServerPrincipal();
  if (!principal) redirect("/login?from=%2Fresearch%2Fentities");
  const { id } = await params;
  const issuerId = decodeIssuerId(id);
  const state = await loadEntityDetail(principal.context, issuerId);
  const names = await loadEntityDisplayMap();
  const displayName = entityLabel(issuerId, names);

  const sortedRows = state.kind === "ready"
    ? state.data.rows.slice().sort((a, b) => b.cutoffAt.localeCompare(a.cutoffAt))
    : [];
  const latestRow = sortedRows[0];
  const gppeDetail = state.kind === "ready" ? state.data.gppeDetail : null;
  const themes = state.kind === "ready" ? state.data.themes ?? [] : [];

  const gppeValue = gppeDetail?.gppe ?? latestRow?.capitalAdjustedLaborEfficiency ?? null;
  const operatingBranch = gppeDetail?.operatingBranch ?? null;
  const capitalAdjustedGrossProfit = gppeDetail?.capitalAdjustedGrossProfit ?? null;

  return (
    <section aria-labelledby="entity-heading" className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="flex flex-wrap items-center gap-3">
            <h1 id="entity-heading" className="text-2xl font-bold tracking-tight text-white md:text-3xl">
              {displayName}
            </h1>
            <span className="rounded-md border border-border bg-card px-2.5 py-0.5 font-mono text-xs text-gray-400">
              {issuerId}
            </span>
            {latestRow?.tier && (
              <span className="rounded-md border border-sky-800 bg-sky-950/60 px-2.5 py-0.5 text-xs font-semibold text-sky-400">
                {tierLabel(latestRow.tier)}
              </span>
            )}
            {latestRow?.valuationGap && (
              <span className={`font-mono text-sm font-semibold ${signColor(latestRow.valuationGap)}`}>
                Valuation Gap: {formatSignedRatio(latestRow.valuationGap)}
              </span>
            )}
          </div>
          <p className="mt-2 text-sm text-gray-400">
            Entity 360° deep dive: multi-factor valuation, labor efficiency, growth, and theme purity.
          </p>
        </div>

        <CardExportButton issuerId={issuerId} />
      </div>

      <ReadStateNotice state={state} />

      {state.kind === "ready" && (
        <>
          {/* 3-Column Summary Cards */}
          <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
            {/* Card 1: Valuation & AI Tier (Module 7) */}
            <div className="rounded-xl border border-border bg-card p-5">
              <div className="flex items-center justify-between">
                <span className="text-xs font-bold uppercase tracking-wider text-gray-500">
                  Valuation &amp; AI Tier (Module 7)
                </span>
                <span className="rounded bg-slate-800 px-2 py-0.5 text-[11px] font-medium text-sky-400">
                  {tierLabel(latestRow?.tier)}
                </span>
              </div>
              <div className="mt-4 grid grid-cols-2 gap-4">
                <div>
                  <div className="text-xs text-gray-400">Current P/S</div>
                  <div className="mt-1 font-mono text-2xl font-bold text-white">
                    {formatRatio(latestRow?.currentPriceToSales) ?? "—"}
                  </div>
                </div>
                <div>
                  <div className="text-xs text-gray-400">Target P/S</div>
                  <div className="mt-1 font-mono text-2xl font-bold text-gray-300">
                    {formatRatio(latestRow?.targetPriceToSales ?? null) ?? "—"}
                  </div>
                </div>
              </div>
              <div className="mt-4 border-t border-border/60 pt-3">
                <div className="flex items-center justify-between text-sm">
                  <span className="text-gray-400">Valuation Gap</span>
                  <span className={`font-mono font-bold ${signColor(latestRow?.valuationGap ?? null)}`}>
                    {formatSignedRatio(latestRow?.valuationGap ?? null) ?? "—"}
                  </span>
                </div>
              </div>
            </div>

            {/* Card 2: Labor Efficiency / GPPE (Module 2) */}
            <div className="rounded-xl border border-border bg-card p-5">
              <div className="flex items-center justify-between">
                <span className="text-xs font-bold uppercase tracking-wider text-gray-500">
                  Labor Efficiency / GPPE (Module 2)
                </span>
                {operatingBranch ? (
                  <span className="rounded bg-slate-800 px-2 py-0.5 font-mono text-[11px] font-medium text-sky-400">
                    {operatingBranch}
                  </span>
                ) : (
                  <span className="text-gray-500 font-mono text-[11px]">—</span>
                )}
              </div>
              <div className="mt-4">
                <div className="text-xs text-gray-400">Gross Profit / Employee (GPPE)</div>
                <div className="mt-1 font-mono text-2xl font-bold text-white" title={gppeValue ?? undefined}>
                  {formatUsdMagnitude(gppeValue) ?? cell(gppeValue)}
                </div>
              </div>
              <div className="mt-4 border-t border-border/60 pt-3">
                <div className="flex items-center justify-between text-sm">
                  <span className="text-gray-400">Cap-Adj Gross Profit</span>
                  <span className="font-mono text-gray-300" title={capitalAdjustedGrossProfit ?? undefined}>
                    {formatUsdMagnitude(capitalAdjustedGrossProfit) ?? cell(capitalAdjustedGrossProfit)}
                  </span>
                </div>
              </div>
            </div>

            {/* Card 3: Growth (Module 1) */}
            <div className="rounded-xl border border-border bg-card p-5">
              <div className="flex items-center justify-between">
                <span className="text-xs font-bold uppercase tracking-wider text-gray-500">
                  Growth / PEG (Module 1)
                </span>
                <span className="rounded bg-slate-800 px-2 py-0.5 text-[11px] font-medium text-gray-400">
                  Historical CAGR
                </span>
              </div>
              <div className="mt-4 grid grid-cols-2 gap-4">
                <div>
                  <div className="text-xs text-gray-400">PEG Ratio</div>
                  <div className="mt-1 font-mono text-2xl font-bold text-sky-400">
                    {formatRatio(latestRow?.peg ?? null) ?? "—"}
                  </div>
                </div>
                <div>
                  <div className="text-xs text-gray-400">PEG Rank</div>
                  <div className="mt-1 font-mono text-2xl font-bold text-gray-300">
                    {latestRow?.pegRank !== null && latestRow?.pegRank !== undefined ? `#${latestRow.pegRank}` : "—"}
                  </div>
                </div>
              </div>
              <div className="mt-4 border-t border-border/60 pt-3">
                <div className="text-xs text-gray-400">
                  Convention: Recency-weighted historical growth (historical CAGR)
                </div>
              </div>
            </div>
          </div>

          {/* Theme Purity Section (Module 6) */}
          <div className="rounded-xl border border-border bg-card p-5">
            <h2 className="text-base font-semibold text-gray-200">
              Theme Purity &amp; Business Exposure (Module 6)
            </h2>
            <p className="mt-1 text-xs text-gray-400">
              Traceable segment revenue classification across active investment themes.
            </p>

            {themes.length > 0 ? (
              <div className="mt-4 space-y-4">
                {themes.map((theme) => {
                  const sharePct = theme.themeShare
                    ? Math.min(100, Math.max(0, Number(theme.themeShare) * 100))
                    : 0;
                  return (
                    <div key={theme.themeId} className="space-y-1.5">
                      <div className="flex flex-wrap items-center justify-between text-sm">
                        <span className="font-medium text-gray-200">{theme.theme}</span>
                        <div className="flex items-center gap-3 font-mono text-xs">
                          <span className="text-gray-400">
                            In-theme: {formatUsdMagnitude(theme.inThemeRevenue) ?? "—"} / Consolidated: {formatUsdMagnitude(theme.consolidatedRevenue) ?? "—"} ({theme.segments} segment{theme.segments === 1 ? "" : "s"})
                          </span>
                          <span className="font-bold text-sky-400">
                            {formatPercentFromFraction(theme.themeShare) ?? "Refused / unclassified"}
                          </span>
                        </div>
                      </div>
                      <div className="h-2 w-full rounded-full bg-slate-800">
                        <div
                          className="h-2 rounded-full bg-sky-400 transition-all duration-300"
                          style={{ width: `${sharePct}%` }}
                        />
                      </div>
                    </div>
                  );
                })}
              </div>
            ) : (
              <p className="mt-4 text-sm text-gray-500">
                No theme purity materialization recorded for this issuer.
              </p>
            )}
          </div>

          {/* Timeline Table across Cutoffs */}
          <div className="space-y-3">
            <h2 className="text-base font-semibold text-gray-200">
              Cutoff Timeline &amp; Decision History
            </h2>
            <div className="overflow-x-auto rounded-xl border border-border">
              <table className="w-full text-left text-sm">
                <caption className="sr-only">{issuerId} across cutoffs</caption>
                <thead className="bg-card text-xs uppercase text-gray-500">
                  <tr>
                    <th scope="col" className="px-4 py-3">Cutoff</th>
                    <th scope="col" className="px-4 py-3">Efficiency</th>
                    <th scope="col" className="px-4 py-3">P/S</th>
                    <th scope="col" className="px-4 py-3">Tier</th>
                    <th scope="col" className="px-4 py-3">Valuation Gap</th>
                    <th scope="col" className="px-4 py-3">PEG</th>
                    <th scope="col" className="px-4 py-3">Availability</th>
                    <th scope="col" className="px-4 py-3">Trace</th>
                  </tr>
                </thead>
                <tbody>
                  {sortedRows.map((row) => (
                    <tr key={row.cutoffAt} className="border-t border-border">
                      <th scope="row" className="px-4 py-3 font-medium text-white">
                        {row.cutoffAt}
                      </th>
                      <td className="px-4 py-3 font-mono">
                        {formatUsdMagnitude(row.capitalAdjustedLaborEfficiency) ?? cell(row.capitalAdjustedLaborEfficiency)}
                      </td>
                      <td className="px-4 py-3 font-mono">
                        {formatRatio(row.currentPriceToSales) ?? cell(row.currentPriceToSales)}
                      </td>
                      <td className="px-4 py-3">{tierLabel(row.tier)}</td>
                      <td className={`px-4 py-3 font-mono font-medium ${signColor(row.valuationGap)}`}>
                        {formatSignedRatio(row.valuationGap) ?? cell(row.valuationGap)}
                      </td>
                      <td className="px-4 py-3 font-mono">
                        {formatRatio(row.peg ?? null) ?? cell(row.peg)}
                      </td>
                      <td className="px-4 py-3">
                        <AvailabilityBadge status={row.availability} />
                      </td>
                      <td className="px-4 py-3">
                        <Link
                          href={`/research/trace?issuer=${encodeURIComponent(row.issuerId)}&cutoff=${encodeURIComponent(row.cutoffAt)}`}
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
          </div>
        </>
      )}
    </section>
  );
}
