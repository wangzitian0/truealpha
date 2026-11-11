import type { ReactNode } from "react";
import Link from "next/link";
import { redirect } from "next/navigation";
import { AvailabilityBadge, ReadStateNotice } from "@/components/read-state";
import { CardExportButton } from "@/components/card-export-button";
import {
  normalizePegConvention,
  PEG_CONVENTION_LABELS,
  PEG_GROWTH_CONVENTIONS,
  projectPegByConvention,
  valuationBasisLabel,
} from "@/contracts/strategyRun";
import { loadEntityDetail } from "@/server/dashboard";
import { getServerPrincipal } from "@/server/auth/request-context";
import { entityLabel, loadEntityDisplayMap } from "@/server/mart/entity-resolution";
import { unvisitedReasonText } from "@/server/mart/theme-purity";
import {
  decodeRouteParam,
  formatCell,
  formatPercentFromFraction,
  formatRatio,
  formatSignedRatio,
  formatTierLabel,
  formatUsdMagnitude,
  signColor,
} from "@/client/format";

export const dynamic = "force-dynamic";

function StatBox({
  label,
  value,
  title,
  testId,
  valueClass = "text-white",
}: {
  label: string;
  value: ReactNode;
  title?: string;
  testId?: string;
  valueClass?: string;
}) {
  return (
    <div className="rounded-lg border border-border/60 bg-slate-900/50 p-3">
      <div className="text-xs text-gray-400">{label}</div>
      <div className={`mt-1 font-mono text-xl font-bold ${valueClass}`} data-testid={testId} title={title}>
        {value}
      </div>
    </div>
  );
}

function ModuleMetaFooter({
  extractor,
  extractorTestId,
  confidence,
  cutoff,
  secondLabel,
  secondValue,
  reasonCodes,
  reasonLabel,
  reasonTestId,
}: {
  extractor: string;
  extractorTestId: string;
  confidence: string | null;
  cutoff: string;
  secondLabel: string;
  secondValue: string;
  reasonCodes: readonly string[];
  reasonLabel: string;
  reasonTestId: string;
}) {
  return (
    <div className="border-t border-border/60 pt-3 text-xs text-gray-400 space-y-1">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span>
          Extractor: <span className="font-mono text-gray-300" data-testid={extractorTestId}>{extractor}</span>
        </span>
        <span>
          Confidence:{" "}
          <span className="font-mono text-gray-300" title={confidence ?? undefined}>
            {formatCell(formatRatio(confidence))}
          </span>
        </span>
      </div>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span>
          Cutoff: <span className="font-mono text-gray-300">{cutoff}</span>
        </span>
        <span>
          {secondLabel}: <span className="font-mono text-gray-300">{secondValue}</span>
        </span>
      </div>
      {reasonCodes.length > 0 && (
        <div className="font-mono text-[11px] text-gray-500" data-testid={reasonTestId}>
          {reasonLabel}: {reasonCodes.join(", ")}
        </div>
      )}
    </div>
  );
}

export default async function EntityDetailPage({
  params,
  searchParams,
}: {
  params: Promise<{ id: string }>;
  searchParams?: Promise<{ convention?: string }>;
}) {
  const principal = await getServerPrincipal();
  if (!principal) redirect("/login?from=%2Fresearch%2Fentities");
  const { id } = await params;
  const resolvedSearchParams = searchParams ? await searchParams : {};
  const convention = normalizePegConvention(resolvedSearchParams.convention);
  const issuerId = decodeRouteParam(id);
  const state = await loadEntityDetail(principal.context, issuerId);
  const resolvedIssuerId = state.kind === "ready" ? state.data.issuerId : issuerId;
  const names = await loadEntityDisplayMap();
  const displayName = entityLabel(resolvedIssuerId, names);

  const sortedRows =
    state.kind === "ready" ? state.data.rows.slice().sort((a, b) => b.cutoffAt.localeCompare(a.cutoffAt)) : [];
  const latestRow = sortedRows[0];
  const gppeDetail = state.kind === "ready" ? state.data.gppeDetail : null;
  const themes = state.kind === "ready" ? state.data.themes ?? [] : [];
  const analystRatings = state.kind === "ready" ? state.data.analystRatings ?? null : null;
  const supplyChainExposure = state.kind === "ready" ? state.data.supplyChainExposure ?? null : null;

  const laborValue = latestRow?.capitalAdjustedLaborEfficiency ?? gppeDetail?.gppe ?? null;
  const laborMetric = latestRow?.laborEfficiencyMetric ?? "metric not recorded";
  const basisLabel = valuationBasisLabel(latestRow?.valuationBasis ?? null);
  const operatingBranch = gppeDetail?.operatingBranch ?? null;
  const capitalAdjustedGrossProfit = gppeDetail?.capitalAdjustedGrossProfit ?? null;
  const {
    peg: displayPeg,
    pegRank: displayPegRank,
    pegReasonCodes: displayPegReasonCodes,
  } = projectPegByConvention(latestRow ?? {}, convention);

  const timelineHeaders = [
    "Cutoff",
    "Efficiency",
    "P/S",
    "Valuation multiple",
    "Tier",
    "Valuation Gap",
    `PEG (${PEG_CONVENTION_LABELS[convention]})`,
    "Availability",
    "Trace",
  ];

  return (
    <section aria-labelledby="entity-heading" className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="flex flex-wrap items-center gap-3">
            <h1 id="entity-heading" className="text-2xl font-bold tracking-tight text-white md:text-3xl">
              {displayName}
            </h1>
            <span
              className="rounded-md border border-border bg-card px-2.5 py-0.5 font-mono text-xs text-gray-400"
              data-evidence={resolvedIssuerId}
            >
              {resolvedIssuerId}
            </span>
            {latestRow?.tier && (
              <span className="rounded-md border border-sky-800 bg-sky-950/60 px-2.5 py-0.5 text-xs font-semibold text-sky-400">
                {formatTierLabel(latestRow.tier)}
              </span>
            )}
            {latestRow?.valuationGap && (
              <span
                className={`font-mono text-sm font-semibold ${signColor(latestRow.valuationGap)}`}
                title={latestRow.valuationGap}
              >
                Valuation Gap ({basisLabel}): {formatCell(formatSignedRatio(latestRow.valuationGap))}
              </span>
            )}
          </div>
          <p className="mt-2 text-sm text-gray-400">
            Entity 360° deep dive: multi-factor valuation, labor efficiency, growth, analyst consensus, supply-chain exposure, and theme purity.
          </p>
        </div>
        <CardExportButton issuerId={resolvedIssuerId} convention={convention} />
      </div>

      <ReadStateNotice state={state} />

      {state.kind === "ready" && (
        <>
          <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
            <div className="rounded-xl border border-border bg-card p-5">
              <div className="flex items-center justify-between">
                <span className="text-xs font-bold uppercase tracking-wider text-gray-500">
                  Valuation &amp; AI Tier (Module 7)
                </span>
                <span className="rounded bg-slate-800 px-2 py-0.5 text-[11px] font-medium text-sky-400">
                  {formatTierLabel(latestRow?.tier)}
                </span>
              </div>
              <div className="mt-4 grid grid-cols-2 gap-4">
                <div>
                  <div className="text-xs text-gray-400">Current {basisLabel}</div>
                  <div className="mt-1 font-mono text-2xl font-bold text-white" title={latestRow?.currentMultiple ?? undefined}>
                    {formatCell(formatRatio(latestRow?.currentMultiple ?? null))}
                  </div>
                </div>
                <div>
                  <div className="text-xs text-gray-400">Target {basisLabel}</div>
                  <div className="mt-1 font-mono text-2xl font-bold text-gray-300" title={latestRow?.targetMultiple ?? undefined}>
                    {formatCell(formatRatio(latestRow?.targetMultiple ?? null))}
                  </div>
                </div>
              </div>
              {latestRow?.valuationBasis === "price_to_gross_profit" && (
                <div
                  className="mt-2 text-xs text-gray-500"
                  data-testid="recorded-price-to-sales"
                  title={latestRow.currentPriceToSales ?? undefined}
                >
                  Current P/S {formatCell(formatRatio(latestRow.currentPriceToSales))} (recorded; the tier band targets P/GP)
                </div>
              )}
              <div className="mt-4 border-t border-border/60 pt-3 flex items-center justify-between text-sm">
                <span className="text-gray-400">Valuation Gap ({basisLabel})</span>
                <span
                  className={`font-mono font-bold ${signColor(latestRow?.valuationGap ?? null)}`}
                  title={latestRow?.valuationGap ?? undefined}
                >
                  {formatCell(formatSignedRatio(latestRow?.valuationGap ?? null))}
                </span>
              </div>
            </div>

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
                  <span className="text-gray-500 font-mono text-[11px]">{formatCell(null)}</span>
                )}
              </div>
              <div className="mt-4">
                {laborValue === null ? (
                  <div className="font-mono text-2xl font-bold text-white">{formatCell(null)}</div>
                ) : (
                  <div>
                    <div className="font-mono text-xs text-gray-400" data-testid="labor-metric-name">
                      {laborMetric}
                    </div>
                    <div className="mt-1 font-mono text-2xl font-bold text-white" title={laborValue}>
                      {formatUsdMagnitude(laborValue) ?? formatCell(laborValue)}
                    </div>
                  </div>
                )}
              </div>
              <div className="mt-4 border-t border-border/60 pt-3 flex items-center justify-between text-sm">
                <span className="text-gray-400">Cap-Adj Gross Profit</span>
                <span className="font-mono text-gray-300" title={capitalAdjustedGrossProfit ?? undefined}>
                  {formatUsdMagnitude(capitalAdjustedGrossProfit) ?? formatCell(capitalAdjustedGrossProfit)}
                </span>
              </div>
            </div>

            <div className="rounded-xl border border-border bg-card p-5" data-testid="module-1-peg-card">
              <div className="flex items-center justify-between">
                <span className="text-xs font-bold uppercase tracking-wider text-gray-500">
                  Growth / PEG (Module 1)
                </span>
                <span className="rounded bg-slate-800 px-2 py-0.5 text-[11px] font-medium text-gray-400">
                  {PEG_CONVENTION_LABELS[convention]}
                </span>
              </div>
              <div className="mt-3 flex flex-wrap gap-1.5" data-testid="entity-peg-convention-selector">
                {PEG_GROWTH_CONVENTIONS.map((item) => {
                  const active = item === convention;
                  const href =
                    item === "historical_cagr"
                      ? `/research/entities/${encodeURIComponent(resolvedIssuerId)}`
                      : `/research/entities/${encodeURIComponent(resolvedIssuerId)}?convention=${encodeURIComponent(item)}`;
                  return (
                    <Link
                      key={item}
                      href={href}
                      data-peg-convention={item}
                      aria-current={active ? "page" : undefined}
                      className={`rounded px-2 py-0.5 text-[11px] font-medium transition ${
                        active
                          ? "border border-sky-500 bg-sky-950/70 text-sky-300"
                          : "border border-border bg-slate-900 text-gray-400 hover:text-gray-200"
                      }`}
                    >
                      {PEG_CONVENTION_LABELS[item]}
                    </Link>
                  );
                })}
              </div>
              <div className="mt-4 grid grid-cols-2 gap-4">
                <div>
                  <div className="text-xs text-gray-400">PEG Ratio</div>
                  <div className="mt-1 font-mono text-2xl font-bold text-sky-400" title={displayPeg ?? undefined}>
                    {formatCell(formatRatio(displayPeg))}
                  </div>
                </div>
                <div>
                  <div className="text-xs text-gray-400">PEG Rank</div>
                  <div className="mt-1 font-mono text-2xl font-bold text-gray-300">
                    {displayPegRank != null ? `#${displayPegRank}` : formatCell(null)}
                  </div>
                </div>
              </div>
              <div className="mt-4 border-t border-border/60 pt-3 space-y-1">
                <div className="text-xs text-gray-400">
                  Convention: {PEG_CONVENTION_LABELS[convention]} ({convention})
                </div>
                {displayPegReasonCodes.length > 0 && (
                  <div className="font-mono text-[11px] text-amber-400" data-testid="entity-peg-reason-codes">
                    Reason codes: {displayPegReasonCodes.join(", ")}
                  </div>
                )}
              </div>
            </div>
          </div>

          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            <div className="rounded-xl border border-border bg-card p-5" data-testid="module-4-analyst-ratings">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div>
                  <h2 className="text-base font-semibold text-gray-200">Analyst Ratings &amp; Consensus (Module 4)</h2>
                  <p className="mt-0.5 text-xs text-gray-400">
                    Point-in-time consensus from <code className="font-mono text-gray-300">mart.issuer_analyst_ratings</code>.
                  </p>
                </div>
                {analystRatings && <AvailabilityBadge status={analystRatings.availabilityStatus} />}
              </div>

              {analystRatings ? (
                <div className="mt-4 space-y-4">
                  <div className="grid grid-cols-3 gap-3">
                    <StatBox
                      label="Consensus Rating (1–5)"
                      value={formatRatio(analystRatings.consensusRating) ?? formatCell(analystRatings.consensusRating)}
                      title={analystRatings.consensusRating ?? undefined}
                      testId="m4-consensus-rating"
                      valueClass="text-sky-400"
                    />
                    <StatBox
                      label="Target Price"
                      value={
                        analystRatings.targetPrice
                          ? `$${formatRatio(analystRatings.targetPrice) ?? analystRatings.targetPrice}`
                          : "Not disclosed"
                      }
                      title={analystRatings.targetPrice ?? undefined}
                      testId="m4-target-price"
                    />
                    <StatBox
                      label="Analysts Covered"
                      value={analystRatings.analystsCount}
                      testId="m4-analysts-count"
                      valueClass="text-gray-200"
                    />
                  </div>
                  <div className="grid grid-cols-3 gap-3 text-center font-mono text-xs" data-testid="m4-rating-breakdown">
                    <div className="rounded border border-emerald-900/60 bg-emerald-950/30 px-2 py-1.5 text-emerald-300">
                      Buy: <span className="font-bold">{analystRatings.buyCount}</span>
                    </div>
                    <div className="rounded border border-amber-900/60 bg-amber-950/30 px-2 py-1.5 text-amber-300">
                      Hold: <span className="font-bold">{analystRatings.holdCount}</span>
                    </div>
                    <div className="rounded border border-rose-900/60 bg-rose-950/30 px-2 py-1.5 text-rose-300">
                      Sell: <span className="font-bold">{analystRatings.sellCount}</span>
                    </div>
                  </div>
                  <ModuleMetaFooter
                    extractor={analystRatings.extractor}
                    extractorTestId="m4-extractor"
                    confidence={analystRatings.confidence}
                    cutoff={analystRatings.cutoff}
                    secondLabel="Evidence"
                    secondValue={analystRatings.sourceEvidenceStatus}
                    reasonCodes={analystRatings.reasonCodes}
                    reasonLabel="Reason codes"
                    reasonTestId="m4-reason-codes"
                  />
                </div>
              ) : (
                <p className="mt-4 text-sm text-gray-500">No analyst ratings materialization recorded for this issuer.</p>
              )}
            </div>

            <div className="rounded-xl border border-border bg-card p-5" data-testid="module-3-supply-chain">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div>
                  <h2 className="text-base font-semibold text-gray-200">Supply-Chain Exposure &amp; Scenario Graph (Module 3)</h2>
                  <p className="mt-0.5 text-xs text-gray-400">
                    Materialized partner graph from <code className="font-mono text-gray-300">mart.issuer_supply_chain_exposure</code>.
                  </p>
                </div>
                {supplyChainExposure && <AvailabilityBadge status={supplyChainExposure.availabilityStatus} />}
              </div>

              {supplyChainExposure ? (
                <div className="mt-4 space-y-4">
                  <div className="grid grid-cols-3 gap-3">
                    <StatBox
                      label="Scenario Exposure Score"
                      value={formatRatio(supplyChainExposure.exposureScore) ?? formatCell(supplyChainExposure.exposureScore)}
                      title={supplyChainExposure.exposureScore ?? undefined}
                      testId="m3-exposure-score"
                      valueClass="text-sky-400"
                    />
                    <StatBox
                      label="Direct Partners"
                      value={supplyChainExposure.directPartners}
                      testId="m3-direct-partners"
                    />
                    <StatBox
                      label="Max Partner Share"
                      value={formatPercentFromFraction(supplyChainExposure.maxPartnerShare) ?? formatCell(supplyChainExposure.maxPartnerShare)}
                      title={supplyChainExposure.maxPartnerShare ?? undefined}
                      testId="m3-max-partner-share"
                      valueClass="text-gray-200"
                    />
                  </div>
                  <div className="grid grid-cols-2 gap-3 text-center font-mono text-xs" data-testid="m3-partner-edges">
                    <div className="rounded border border-sky-900/60 bg-sky-950/30 px-2 py-1.5 text-sky-300">
                      Upstream Suppliers: <span className="font-bold" data-testid="m3-suppliers-count">{supplyChainExposure.suppliersCount}</span>
                    </div>
                    <div className="rounded border border-indigo-900/60 bg-indigo-950/30 px-2 py-1.5 text-indigo-300">
                      Downstream Customers: <span className="font-bold" data-testid="m3-customers-count">{supplyChainExposure.customersCount}</span>
                    </div>
                  </div>
                  <ModuleMetaFooter
                    extractor={supplyChainExposure.extractor}
                    extractorTestId="m3-extractor"
                    confidence={supplyChainExposure.confidence}
                    cutoff={supplyChainExposure.cutoff}
                    secondLabel="Validation"
                    secondValue={supplyChainExposure.factorValidationStatus}
                    reasonCodes={supplyChainExposure.reasonCodes}
                    reasonLabel="Scenario / edge provenance"
                    reasonTestId="m3-reason-codes"
                  />
                </div>
              ) : (
                <p className="mt-4 text-sm text-gray-500">No supply-chain exposure materialization recorded for this issuer.</p>
              )}
            </div>
          </div>

          <div className="rounded-xl border border-border bg-card p-5">
            <h2 className="text-base font-semibold text-gray-200">Theme Purity &amp; Business Exposure (Module 6)</h2>
            <p className="mt-1 text-xs text-gray-400">Traceable segment revenue classification across active investment themes.</p>
            {themes.length > 0 ? (
              <div className="mt-4 space-y-4">
                {themes.map((theme) => {
                  if (theme.unvisitedReason) {
                    return (
                      <div key={theme.themeId} className="flex flex-wrap items-center justify-between text-sm">
                        <span className="font-medium text-gray-200">{theme.theme}</span>
                        <span className="font-mono text-xs text-gray-400">{unvisitedReasonText(theme.unvisitedReason)}</span>
                      </div>
                    );
                  }
                  const sharePct = theme.themeShare ? Math.min(100, Math.max(0, Number(theme.themeShare) * 100)) : 0;
                  return (
                    <div key={theme.themeId} className="space-y-1.5">
                      <div className="flex flex-wrap items-center justify-between text-sm">
                        <span className="font-medium text-gray-200">{theme.theme}</span>
                        <div className="flex items-center gap-3 font-mono text-xs">
                          <span className="text-gray-400">
                            In-theme: <span title={theme.inThemeRevenue ?? undefined}>{formatCell(formatUsdMagnitude(theme.inThemeRevenue))}</span> / Consolidated:{" "}
                            <span title={theme.consolidatedRevenue ?? undefined}>{formatCell(formatUsdMagnitude(theme.consolidatedRevenue))}</span> ({theme.segments} segment{theme.segments === 1 ? "" : "s"})
                          </span>
                          <span className="font-bold text-sky-400" title={theme.themeShare ?? undefined}>
                            {formatPercentFromFraction(theme.themeShare) ?? "Refused / unclassified"}
                          </span>
                        </div>
                      </div>
                      <div className="h-2 w-full rounded-full bg-slate-800">
                        <div className="h-2 rounded-full bg-sky-400 transition-all duration-300" style={{ width: `${sharePct}%` }} />
                      </div>
                    </div>
                  );
                })}
              </div>
            ) : (
              <p className="mt-4 text-sm text-gray-500">No theme purity materialization recorded for this issuer.</p>
            )}
          </div>

          <div className="space-y-3">
            <h2 className="text-base font-semibold text-gray-200">Cutoff Timeline &amp; Decision History</h2>
            <div className="overflow-x-auto rounded-xl border border-border">
              <table className="w-full text-left text-sm">
                <caption className="sr-only">{displayName} across cutoffs</caption>
                <thead className="bg-card text-xs uppercase text-gray-500">
                  <tr>
                    {timelineHeaders.map((h) => (
                      <th key={h} scope="col" className="px-4 py-3">
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {sortedRows.map((row) => {
                    const rowProjectedPeg = projectPegByConvention(row, convention).peg;
                    return (
                      <tr key={row.cutoffAt} className="border-t border-border">
                        <th scope="row" className="px-4 py-3 font-medium text-white">
                          {row.cutoffAt}
                        </th>
                        <td className="px-4 py-3 font-mono" title={row.capitalAdjustedLaborEfficiency ?? undefined}>
                          {formatUsdMagnitude(row.capitalAdjustedLaborEfficiency) ?? formatCell(row.capitalAdjustedLaborEfficiency)}
                          {row.capitalAdjustedLaborEfficiency !== null && (
                            <div className="font-sans text-[11px] text-gray-500" data-testid="labor-row-metric">
                              {row.laborEfficiencyMetric ?? "metric not recorded"}
                            </div>
                          )}
                        </td>
                        <td className="px-4 py-3 font-mono" title={row.currentPriceToSales ?? undefined}>
                          {formatRatio(row.currentPriceToSales) ?? formatCell(row.currentPriceToSales)}
                        </td>
                        <td className="px-4 py-3 font-mono" title={row.currentMultiple ?? undefined}>
                          {row.currentMultiple
                            ? `${formatRatio(row.currentMultiple)} ${valuationBasisLabel(row.valuationBasis ?? null)}`
                            : formatCell(null)}
                        </td>
                        <td className="px-4 py-3">{formatTierLabel(row.tier)}</td>
                        <td className={`px-4 py-3 font-mono font-medium ${signColor(row.valuationGap)}`} title={row.valuationGap ?? undefined}>
                          {formatSignedRatio(row.valuationGap) ?? formatCell(row.valuationGap)}
                        </td>
                        <td className="px-4 py-3 font-mono" title={rowProjectedPeg ?? undefined}>
                          {formatRatio(rowProjectedPeg) ?? formatCell(rowProjectedPeg)}
                        </td>
                        <td className="px-4 py-3">
                          <AvailabilityBadge status={row.availability} />
                        </td>
                        <td className="px-4 py-3">
                          <Link
                            href={`/research/trace?issuer=${encodeURIComponent(row.issuerId)}&cutoff=${encodeURIComponent(row.cutoffAt)}`}
                            className="font-mono text-xs text-gray-400 hover:text-accent"
                            data-evidence={row.traceId}
                          >
                            {row.traceId}
                          </Link>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}
    </section>
  );
}
