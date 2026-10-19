import Link from "next/link";
import { redirect } from "next/navigation";
import { AvailabilityBadge, ReadStateNotice } from "@/components/read-state";
import { CardExportButton } from "@/components/card-export-button";
import { laborEfficiencyLabels, UNRECORDED_METRIC } from "@/contracts/laborEfficiency";
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

  const sortedRows = state.kind === "ready"
    ? state.data.rows.slice().sort((a, b) => b.cutoffAt.localeCompare(a.cutoffAt))
    : [];
  const latestRow = sortedRows[0];
  const gppeDetail = state.kind === "ready" ? state.data.gppeDetail : null;
  const themes = state.kind === "ready" ? state.data.themes ?? [] : [];
  const analystRatings = state.kind === "ready" ? state.data.analystRatings ?? null : null;
  const supplyChainExposure = state.kind === "ready" ? state.data.supplyChainExposure ?? null : null;

  // #1176: the published `gppe` column is the uniform metric; the strategy value carries the name the
  // strategy used. Both are shown, each with its name, and neither is shown bare.
  const laborLabels = laborEfficiencyLabels({
    publishedGppe: gppeDetail?.gppe ?? null,
    strategyValue: latestRow?.capitalAdjustedLaborEfficiency ?? null,
    strategyMetric: latestRow?.laborEfficiencyMetric ?? null,
  });
  // #1235: the gap divides the multiple of the row's definition — P/GP under 1.0.0.
  const basisLabel = valuationBasisLabel(latestRow?.valuationBasis ?? null);
  const operatingBranch = gppeDetail?.operatingBranch ?? null;
  const capitalAdjustedGrossProfit = gppeDetail?.capitalAdjustedGrossProfit ?? null;
  const {
    peg: displayPeg,
    pegRank: displayPegRank,
    pegReasonCodes: displayPegReasonCodes,
  } = projectPegByConvention(latestRow ?? {}, convention);

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

        <CardExportButton issuerId={resolvedIssuerId} />
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
                  {formatTierLabel(latestRow?.tier)}
                </span>
              </div>
              <div className="mt-4 grid grid-cols-2 gap-4">
                <div>
                  <div className="text-xs text-gray-400">Current {basisLabel}</div>
                  <div
                    className="mt-1 font-mono text-2xl font-bold text-white"
                    title={latestRow?.currentMultiple ?? undefined}
                  >
                    {formatCell(formatRatio(latestRow?.currentMultiple ?? null))}
                  </div>
                </div>
                <div>
                  <div className="text-xs text-gray-400">Target {basisLabel}</div>
                  <div
                    className="mt-1 font-mono text-2xl font-bold text-gray-300"
                    title={latestRow?.targetMultiple ?? undefined}
                  >
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
              <div className="mt-4 border-t border-border/60 pt-3">
                <div className="flex items-center justify-between text-sm">
                  <span className="text-gray-400">Valuation Gap ({basisLabel})</span>
                  <span
                    className={`font-mono font-bold ${signColor(latestRow?.valuationGap ?? null)}`}
                    title={latestRow?.valuationGap ?? undefined}
                  >
                    {formatCell(formatSignedRatio(latestRow?.valuationGap ?? null))}
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
                  <span className="text-gray-500 font-mono text-[11px]">{formatCell(null)}</span>
                )}
              </div>
              <div className="mt-4">
                {laborLabels.length === 0 ? (
                  <div className="font-mono text-2xl font-bold text-white">{formatCell(null)}</div>
                ) : (
                  laborLabels.map((label) => (
                    <div key={`${label.metric}:${label.value}`} className="mt-2 first:mt-0">
                      <div className="font-mono text-xs text-gray-400" data-testid="labor-metric-name">
                        {label.metric}
                      </div>
                      <div className="mt-1 font-mono text-2xl font-bold text-white" title={label.value}>
                        {formatUsdMagnitude(label.value) ?? formatCell(label.value)}
                      </div>
                    </div>
                  ))
                )}
              </div>
              <div className="mt-4 border-t border-border/60 pt-3">
                <div className="flex items-center justify-between text-sm">
                  <span className="text-gray-400">Cap-Adj Gross Profit</span>
                  <span className="font-mono text-gray-300" title={capitalAdjustedGrossProfit ?? undefined}>
                    {formatUsdMagnitude(capitalAdjustedGrossProfit) ?? formatCell(capitalAdjustedGrossProfit)}
                  </span>
                </div>
              </div>
            </div>

            {/* Card 3: Growth (Module 1) */}
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
                  <div
                    className="mt-1 font-mono text-2xl font-bold text-sky-400"
                    title={displayPeg ?? undefined}
                  >
                    {formatCell(formatRatio(displayPeg))}
                  </div>
                </div>
                <div>
                  <div className="text-xs text-gray-400">PEG Rank</div>
                  <div className="mt-1 font-mono text-2xl font-bold text-gray-300">
                    {displayPegRank !== null && displayPegRank !== undefined ? `#${displayPegRank}` : formatCell(null)}
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

          {/* Module 4 & Module 3 Two-Column Reader Section */}
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            {/* Module 4: Analyst Ratings & Consensus */}
            <div className="rounded-xl border border-border bg-card p-5" data-testid="module-4-analyst-ratings">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div>
                  <h2 className="text-base font-semibold text-gray-200">
                    Analyst Ratings &amp; Consensus (Module 4)
                  </h2>
                  <p className="mt-0.5 text-xs text-gray-400">
                    Point-in-time consensus rating, target price, and analyst rating breakdown from{" "}
                    <code className="font-mono text-gray-300">mart.issuer_analyst_ratings</code>.
                  </p>
                </div>
                {analystRatings && (
                  <AvailabilityBadge status={analystRatings.availabilityStatus} />
                )}
              </div>

              {analystRatings ? (
                <div className="mt-4 space-y-4">
                  <div className="grid grid-cols-3 gap-3">
                    <div className="rounded-lg border border-border/60 bg-slate-900/50 p-3">
                      <div className="text-xs text-gray-400">Consensus Rating (1–5)</div>
                      <div
                        className="mt-1 font-mono text-xl font-bold text-sky-400"
                        data-testid="m4-consensus-rating"
                        title={analystRatings.consensusRating ?? undefined}
                      >
                        {formatRatio(analystRatings.consensusRating) ?? formatCell(analystRatings.consensusRating)}
                      </div>
                    </div>
                    <div className="rounded-lg border border-border/60 bg-slate-900/50 p-3">
                      <div className="text-xs text-gray-400">Target Price</div>
                      <div
                        className="mt-1 font-mono text-xl font-bold text-white"
                        data-testid="m4-target-price"
                        title={analystRatings.targetPrice ?? undefined}
                      >
                        {analystRatings.targetPrice
                          ? `$${formatRatio(analystRatings.targetPrice) ?? analystRatings.targetPrice}`
                          : "Not disclosed"}
                      </div>
                    </div>
                    <div className="rounded-lg border border-border/60 bg-slate-900/50 p-3">
                      <div className="text-xs text-gray-400">Analysts Covered</div>
                      <div className="mt-1 font-mono text-xl font-bold text-gray-200" data-testid="m4-analysts-count">
                        {analystRatings.analystsCount}
                      </div>
                    </div>
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

                  <div className="border-t border-border/60 pt-3 text-xs text-gray-400 space-y-1">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <span>
                        Extractor: <span className="font-mono text-gray-300" data-testid="m4-extractor">{analystRatings.extractor}</span>
                      </span>
                      <span>
                        Confidence:{" "}
                        <span className="font-mono text-gray-300" title={analystRatings.confidence ?? undefined}>
                          {formatCell(formatRatio(analystRatings.confidence))}
                        </span>
                      </span>
                    </div>
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <span>
                        Cutoff: <span className="font-mono text-gray-300">{analystRatings.cutoff}</span>
                      </span>
                      <span>
                        Evidence: <span className="font-mono text-gray-300">{analystRatings.sourceEvidenceStatus}</span>
                      </span>
                    </div>
                    {analystRatings.reasonCodes.length > 0 && (
                      <div className="font-mono text-[11px] text-gray-500" data-testid="m4-reason-codes">
                        Reason codes: {analystRatings.reasonCodes.join(", ")}
                      </div>
                    )}
                  </div>
                </div>
              ) : (
                <p className="mt-4 text-sm text-gray-500">
                  No analyst ratings materialization recorded for this issuer.
                </p>
              )}
            </div>

            {/* Module 3: Supply-Chain Exposure & Scenario Graph */}
            <div className="rounded-xl border border-border bg-card p-5" data-testid="module-3-supply-chain">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div>
                  <h2 className="text-base font-semibold text-gray-200">
                    Supply-Chain Exposure &amp; Scenario Graph (Module 3)
                  </h2>
                  <p className="mt-0.5 text-xs text-gray-400">
                    Materialized supply-chain partner graph and scenario exposure from{" "}
                    <code className="font-mono text-gray-300">mart.issuer_supply_chain_exposure</code>.
                  </p>
                </div>
                {supplyChainExposure && (
                  <AvailabilityBadge status={supplyChainExposure.availabilityStatus} />
                )}
              </div>

              {supplyChainExposure ? (
                <div className="mt-4 space-y-4">
                  <div className="grid grid-cols-3 gap-3">
                    <div className="rounded-lg border border-border/60 bg-slate-900/50 p-3">
                      <div className="text-xs text-gray-400">Scenario Exposure Score</div>
                      <div
                        className="mt-1 font-mono text-xl font-bold text-sky-400"
                        data-testid="m3-exposure-score"
                        title={supplyChainExposure.exposureScore ?? undefined}
                      >
                        {formatRatio(supplyChainExposure.exposureScore) ?? formatCell(supplyChainExposure.exposureScore)}
                      </div>
                    </div>
                    <div className="rounded-lg border border-border/60 bg-slate-900/50 p-3">
                      <div className="text-xs text-gray-400">Direct Partners</div>
                      <div className="mt-1 font-mono text-xl font-bold text-white" data-testid="m3-direct-partners">
                        {supplyChainExposure.directPartners}
                      </div>
                    </div>
                    <div className="rounded-lg border border-border/60 bg-slate-900/50 p-3">
                      <div className="text-xs text-gray-400">Max Partner Share</div>
                      <div
                        className="mt-1 font-mono text-xl font-bold text-gray-200"
                        data-testid="m3-max-partner-share"
                        title={supplyChainExposure.maxPartnerShare ?? undefined}
                      >
                        {formatPercentFromFraction(supplyChainExposure.maxPartnerShare) ?? formatCell(supplyChainExposure.maxPartnerShare)}
                      </div>
                    </div>
                  </div>

                  <div className="grid grid-cols-2 gap-3 text-center font-mono text-xs" data-testid="m3-partner-edges">
                    <div className="rounded border border-sky-900/60 bg-sky-950/30 px-2 py-1.5 text-sky-300">
                      Upstream Suppliers: <span className="font-bold" data-testid="m3-suppliers-count">{supplyChainExposure.suppliersCount}</span>
                    </div>
                    <div className="rounded border border-indigo-900/60 bg-indigo-950/30 px-2 py-1.5 text-indigo-300">
                      Downstream Customers: <span className="font-bold" data-testid="m3-customers-count">{supplyChainExposure.customersCount}</span>
                    </div>
                  </div>

                  <div className="border-t border-border/60 pt-3 text-xs text-gray-400 space-y-1">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <span>
                        Extractor: <span className="font-mono text-gray-300" data-testid="m3-extractor">{supplyChainExposure.extractor}</span>
                      </span>
                      <span>
                        Confidence:{" "}
                        <span className="font-mono text-gray-300" title={supplyChainExposure.confidence ?? undefined}>
                          {formatCell(formatRatio(supplyChainExposure.confidence))}
                        </span>
                      </span>
                    </div>
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <span>
                        Cutoff: <span className="font-mono text-gray-300">{supplyChainExposure.cutoff}</span>
                      </span>
                      <span>
                        Validation: <span className="font-mono text-gray-300">{supplyChainExposure.factorValidationStatus}</span>
                      </span>
                    </div>
                    {supplyChainExposure.reasonCodes.length > 0 && (
                      <div className="font-mono text-[11px] text-gray-500" data-testid="m3-reason-codes">
                        Scenario / edge provenance: {supplyChainExposure.reasonCodes.join(", ")}
                      </div>
                    )}
                  </div>
                </div>
              ) : (
                <p className="mt-4 text-sm text-gray-500">
                  No supply-chain exposure materialization recorded for this issuer.
                </p>
              )}
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
                  if (theme.unvisitedReason) {
                    // A fill row: the issuer has no segment partition (#1117). The page says why, in words.
                    return (
                      <div key={theme.themeId} className="flex flex-wrap items-center justify-between text-sm">
                        <span className="font-medium text-gray-200">{theme.theme}</span>
                        <span className="font-mono text-xs text-gray-400">
                          {unvisitedReasonText(theme.unvisitedReason)}
                        </span>
                      </div>
                    );
                  }
                  const sharePct = theme.themeShare
                    ? Math.min(100, Math.max(0, Number(theme.themeShare) * 100))
                    : 0;
                  return (
                    <div key={theme.themeId} className="space-y-1.5">
                      <div className="flex flex-wrap items-center justify-between text-sm">
                        <span className="font-medium text-gray-200">{theme.theme}</span>
                        <div className="flex items-center gap-3 font-mono text-xs">
                          <span className="text-gray-400">
                            In-theme:{" "}
                            <span title={theme.inThemeRevenue ?? undefined}>
                              {formatCell(formatUsdMagnitude(theme.inThemeRevenue))}
                            </span>{" "}
                            / Consolidated:{" "}
                            <span title={theme.consolidatedRevenue ?? undefined}>
                              {formatCell(formatUsdMagnitude(theme.consolidatedRevenue))}
                            </span>{" "}
                            ({theme.segments} segment{theme.segments === 1 ? "" : "s"})
                          </span>
                          <span className="font-bold text-sky-400" title={theme.themeShare ?? undefined}>
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
                <caption className="sr-only">{displayName} across cutoffs</caption>
                <thead className="bg-card text-xs uppercase text-gray-500">
                  <tr>
                    <th scope="col" className="px-4 py-3">Cutoff</th>
                    <th scope="col" className="px-4 py-3">Efficiency</th>
                    <th scope="col" className="px-4 py-3">P/S</th>
                    <th scope="col" className="px-4 py-3">Valuation multiple</th>
                    <th scope="col" className="px-4 py-3">Tier</th>
                    <th scope="col" className="px-4 py-3">Valuation Gap</th>
                    <th scope="col" className="px-4 py-3">PEG ({PEG_CONVENTION_LABELS[convention]})</th>
                    <th scope="col" className="px-4 py-3">Availability</th>
                    <th scope="col" className="px-4 py-3">Trace</th>
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
                              {row.laborEfficiencyMetric ?? UNRECORDED_METRIC}
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
                        <td
                          className={`px-4 py-3 font-mono font-medium ${signColor(row.valuationGap)}`}
                          title={row.valuationGap ?? undefined}
                        >
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
