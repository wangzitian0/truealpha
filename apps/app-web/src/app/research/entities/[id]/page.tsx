import type { ReactNode } from "react";
import Link from "next/link";
import { redirect } from "next/navigation";
import { AvailabilityBadge, ReadStateNotice } from "@/components/read-state";
import { CardExportButton } from "@/components/card-export-button";
import { ModuleMetaFooter, StatBox } from "@/components/entity-stat-card";
import { RatioTd, ResearchTable, SignedRatioTd } from "@/components/research-table";
import {
  normalizePegConvention,
  PEG_CONVENTION_LABELS,
  PEG_GROWTH_CONVENTIONS,
  projectPegByConvention,
  valuationBasisLabel,
} from "@/contracts/strategyRun";
import { loadEntityDetail } from "@/server/dashboard";
import type { Availability } from "@/server/mart/research-read";
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

const ratioOrCell = (v: string | null | undefined) => formatCell(formatRatio(v ?? null));
const usdOrCell = (v: string | null | undefined) => formatUsdMagnitude(v) ?? formatCell(v);
const statBox = (label: string, value: ReactNode, testId: string, title?: string | null, valueClass?: string): Parameters<typeof StatBox>[0] =>
  ({ label, value, title: title ?? undefined, testId, valueClass });

function PrimaryCard({ title, badge, testId, children }: { title: string; badge: ReactNode; testId?: string; children: ReactNode }) {
  return (
    <div className="rounded-xl border border-border bg-card p-5" data-testid={testId}>
      <div className="flex items-center justify-between">
        <span className="text-xs font-bold uppercase tracking-wider text-gray-500">{title}</span>
        {badge}
      </div>
      {children}
    </div>
  );
}

function HeroMetric({ label, value, title, cls = "text-white" }: { label: string; value: ReactNode; title?: string | null; cls?: string }) {
  return (
    <div>
      <div className="text-xs text-gray-400">{label}</div>
      <div className={`mt-1 font-mono text-2xl font-bold ${cls}`} title={title ?? undefined}>{value}</div>
    </div>
  );
}

const moduleMeta = (
  detail: { extractor: string; confidence: string | null; cutoff: string; reasonCodes: string[] },
  extractorTestId: string,
  secondLabel: string,
  secondValue: string,
  reasonLabel: string,
  reasonTestId: string,
): Parameters<typeof ModuleMetaFooter>[0] => ({
  extractor: detail.extractor,
  extractorTestId,
  confidence: detail.confidence,
  cutoff: detail.cutoff,
  secondLabel,
  secondValue,
  reasonCodes: detail.reasonCodes,
  reasonLabel,
  reasonTestId,
});

type BreakdownItem = readonly [label: string, count: number, cls: string, testId?: string];

function DetailModuleCard({
  testId, title, table, prefix, availabilityStatus, emptyText, stats, breakdownTestId, breakdownCols, breakdownItems, meta,
}: {
  testId: string;
  title: string;
  table: string;
  prefix: string;
  availabilityStatus: Availability | undefined;
  emptyText: string;
  stats: Parameters<typeof StatBox>[0][] | null;
  breakdownTestId: string;
  breakdownCols: string;
  breakdownItems: readonly BreakdownItem[] | null;
  meta: Parameters<typeof ModuleMetaFooter>[0] | null;
}) {
  return (
    <div className="rounded-xl border border-border bg-card p-5" data-testid={testId}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 className="text-base font-semibold text-gray-200">{title}</h2>
          <p className="mt-0.5 text-xs text-gray-400">{prefix} <code className="font-mono text-gray-300">{table}</code>.</p>
        </div>
        {availabilityStatus && <AvailabilityBadge status={availabilityStatus} />}
      </div>
      {stats && breakdownItems && meta ? (
        <div className="mt-4 space-y-4">
          <div className="grid grid-cols-3 gap-3">{stats.map((s) => <StatBox key={s.testId} {...s} />)}</div>
          <div className={`grid ${breakdownCols} gap-3 text-center font-mono text-xs`} data-testid={breakdownTestId}>
            {breakdownItems.map(([label, count, cls, itemTestId]) => (
              <div key={itemTestId ?? label} className={`rounded border px-2 py-1.5 ${cls}`}>
                {label}: <span className="font-bold" data-testid={itemTestId}>{count}</span>
              </div>
            ))}
          </div>
          <ModuleMetaFooter {...meta} />
        </div>
      ) : (
        <p className="mt-4 text-sm text-gray-500">{emptyText}</p>
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
  const convention = normalizePegConvention((searchParams ? await searchParams : {}).convention);
  const issuerId = decodeRouteParam(id);
  const state = await loadEntityDetail(principal.context, issuerId);
  const resolvedIssuerId = state.kind === "ready" ? state.data.issuerId : issuerId;
  const names = await loadEntityDisplayMap();
  const displayName = entityLabel(resolvedIssuerId, names);

  const sortedRows = state.kind === "ready" ? state.data.rows.slice().sort((a, b) => b.cutoffAt.localeCompare(a.cutoffAt)) : [];
  const latestRow = sortedRows[0];
  const gppeDetail = state.kind === "ready" ? state.data.gppeDetail : null;
  const themes = state.kind === "ready" ? state.data.themes ?? [] : [];
  const analystRatings = state.kind === "ready" ? state.data.analystRatings ?? null : null;
  const supplyChainExposure = state.kind === "ready" ? state.data.supplyChainExposure ?? null : null;

  const laborValue = latestRow?.capitalAdjustedLaborEfficiency ?? gppeDetail?.gppe ?? null;
  const laborMetric = latestRow?.laborEfficiencyMetric ?? "metric not recorded";
  const basisLabel = valuationBasisLabel(latestRow?.valuationBasis ?? null);
  const operatingBranch = gppeDetail?.operatingBranch ?? null;
  const capAdjGrossProfit = gppeDetail?.capitalAdjustedGrossProfit ?? null;
  const { peg: displayPeg, pegRank: displayPegRank, pegReasonCodes: displayPegReasonCodes } = projectPegByConvention(
    latestRow ?? {},
    convention,
  );

  return (
    <section aria-labelledby="entity-heading" className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="flex flex-wrap items-center gap-3">
            <h1 id="entity-heading" className="text-2xl font-bold tracking-tight text-white md:text-3xl">{displayName}</h1>
            <span className="rounded-md border border-border bg-card px-2.5 py-0.5 font-mono text-xs text-gray-400" data-evidence={resolvedIssuerId}>
              {resolvedIssuerId}
            </span>
            {latestRow?.tier && (
              <span className="rounded-md border border-sky-800 bg-sky-950/60 px-2.5 py-0.5 text-xs font-semibold text-sky-400">
                {formatTierLabel(latestRow.tier)}
              </span>
            )}
            {latestRow?.valuationGap && (
              <span className={`font-mono text-sm font-semibold ${signColor(latestRow.valuationGap)}`} title={latestRow.valuationGap}>
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
            <PrimaryCard
              title="Valuation & AI Tier (Module 7)"
              badge={<span className="rounded bg-slate-800 px-2 py-0.5 text-[11px] font-medium text-sky-400">{formatTierLabel(latestRow?.tier)}</span>}
            >
              <div className="mt-4 grid grid-cols-2 gap-4">
                <HeroMetric label={`Current ${basisLabel}`} value={ratioOrCell(latestRow?.currentMultiple)} title={latestRow?.currentMultiple} />
                <HeroMetric label={`Target ${basisLabel}`} value={ratioOrCell(latestRow?.targetMultiple)} title={latestRow?.targetMultiple} cls="text-gray-300" />
              </div>
              {latestRow?.valuationBasis === "price_to_gross_profit" && (
                <div className="mt-2 text-xs text-gray-500" data-testid="recorded-price-to-sales" title={latestRow.currentPriceToSales ?? undefined}>
                  Current P/S {ratioOrCell(latestRow.currentPriceToSales)} (recorded; the tier band targets P/GP)
                </div>
              )}
              <div className="mt-4 border-t border-border/60 pt-3 flex items-center justify-between text-sm">
                <span className="text-gray-400">Valuation Gap ({basisLabel})</span>
                <span className={`font-mono font-bold ${signColor(latestRow?.valuationGap ?? null)}`} title={latestRow?.valuationGap ?? undefined}>
                  {formatCell(formatSignedRatio(latestRow?.valuationGap ?? null))}
                </span>
              </div>
            </PrimaryCard>

            <PrimaryCard
              title="Labor Efficiency / GPPE (Module 2)"
              badge={operatingBranch ? <span className="rounded bg-slate-800 px-2 py-0.5 font-mono text-[11px] font-medium text-sky-400">{operatingBranch}</span> : <span className="text-gray-500 font-mono text-[11px]">{formatCell(null)}</span>}
            >
              <div className="mt-4">
                {laborValue === null ? (
                  <div className="font-mono text-2xl font-bold text-white">{formatCell(null)}</div>
                ) : (
                  <div>
                    <div className="font-mono text-xs text-gray-400" data-testid="labor-metric-name">{laborMetric}</div>
                    <div className="mt-1 font-mono text-2xl font-bold text-white" title={laborValue}>{usdOrCell(laborValue)}</div>
                  </div>
                )}
              </div>
              <div className="mt-4 border-t border-border/60 pt-3 flex items-center justify-between text-sm">
                <span className="text-gray-400">Cap-Adj Gross Profit</span>
                <span className="font-mono text-gray-300" title={capAdjGrossProfit ?? undefined}>{usdOrCell(capAdjGrossProfit)}</span>
              </div>
            </PrimaryCard>

            <PrimaryCard
              testId="module-1-peg-card"
              title="Growth / PEG (Module 1)"
              badge={<span className="rounded bg-slate-800 px-2 py-0.5 text-[11px] font-medium text-gray-400">{PEG_CONVENTION_LABELS[convention]}</span>}
            >
              <div className="mt-3 flex flex-wrap gap-1.5" data-testid="entity-peg-convention-selector">
                {PEG_GROWTH_CONVENTIONS.map((item) => {
                  const active = item === convention;
                  const base = `/research/entities/${encodeURIComponent(resolvedIssuerId)}`;
                  return (
                    <Link
                      key={item}
                      href={item === "historical_cagr" ? base : `${base}?convention=${encodeURIComponent(item)}`}
                      data-peg-convention={item}
                      aria-current={active ? "page" : undefined}
                      className={`rounded px-2 py-0.5 text-[11px] font-medium transition ${
                        active ? "border border-sky-500 bg-sky-950/70 text-sky-300" : "border border-border bg-slate-900 text-gray-400 hover:text-gray-200"
                      }`}
                    >
                      {PEG_CONVENTION_LABELS[item]}
                    </Link>
                  );
                })}
              </div>
              <div className="mt-4 grid grid-cols-2 gap-4">
                <HeroMetric label="PEG Ratio" value={ratioOrCell(displayPeg)} title={displayPeg} cls="text-sky-400" />
                <HeroMetric label="PEG Rank" value={displayPegRank != null ? `#${displayPegRank}` : formatCell(null)} cls="text-gray-300" />
              </div>
              <div className="mt-4 border-t border-border/60 pt-3 space-y-1">
                <div className="text-xs text-gray-400">Convention: {PEG_CONVENTION_LABELS[convention]} ({convention})</div>
                {displayPegReasonCodes.length > 0 && (
                  <div className="font-mono text-[11px] text-amber-400" data-testid="entity-peg-reason-codes">
                    Reason codes: {displayPegReasonCodes.join(", ")}
                  </div>
                )}
              </div>
            </PrimaryCard>
          </div>

          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            <DetailModuleCard
              testId="module-4-analyst-ratings"
              title="Analyst Ratings & Consensus (Module 4)"
              prefix="Point-in-time consensus from"
              table="mart.issuer_analyst_ratings"
              availabilityStatus={analystRatings?.availabilityStatus}
              emptyText="No analyst ratings materialization recorded for this issuer."
              stats={
                analystRatings && [
                  statBox("Consensus Rating (1–5)", ratioOrCell(analystRatings.consensusRating), "m4-consensus-rating", analystRatings.consensusRating, "text-sky-400"),
                  statBox("Target Price", analystRatings.targetPrice ? `$${formatRatio(analystRatings.targetPrice) ?? analystRatings.targetPrice}` : "Not disclosed", "m4-target-price", analystRatings.targetPrice),
                  statBox("Analysts Covered", analystRatings.analystsCount, "m4-analysts-count", undefined, "text-gray-200"),
                ]
              }
              breakdownTestId="m4-rating-breakdown"
              breakdownCols="grid-cols-3"
              breakdownItems={
                analystRatings && [
                  ["Buy", analystRatings.buyCount, "border-emerald-900/60 bg-emerald-950/30 text-emerald-300"],
                  ["Hold", analystRatings.holdCount, "border-amber-900/60 bg-amber-950/30 text-amber-300"],
                  ["Sell", analystRatings.sellCount, "border-rose-900/60 bg-rose-950/30 text-rose-300"],
                ]
              }
              meta={analystRatings && moduleMeta(analystRatings, "m4-extractor", "Evidence", analystRatings.sourceEvidenceStatus, "Reason codes", "m4-reason-codes")}
            />

            <DetailModuleCard
              testId="module-3-supply-chain"
              title="Supply-Chain Exposure & Scenario Graph (Module 3)"
              prefix="Materialized partner graph from"
              table="mart.issuer_supply_chain_exposure"
              availabilityStatus={supplyChainExposure?.availabilityStatus}
              emptyText="No supply-chain exposure materialization recorded for this issuer."
              stats={
                supplyChainExposure && [
                  statBox("Scenario Exposure Score", ratioOrCell(supplyChainExposure.exposureScore), "m3-exposure-score", supplyChainExposure.exposureScore, "text-sky-400"),
                  statBox("Direct Partners", supplyChainExposure.directPartners, "m3-direct-partners"),
                  statBox("Max Partner Share", formatPercentFromFraction(supplyChainExposure.maxPartnerShare) ?? formatCell(supplyChainExposure.maxPartnerShare), "m3-max-partner-share", supplyChainExposure.maxPartnerShare, "text-gray-200"),
                ]
              }
              breakdownTestId="m3-partner-edges"
              breakdownCols="grid-cols-2"
              breakdownItems={
                supplyChainExposure && [
                  ["Upstream Suppliers", supplyChainExposure.suppliersCount, "border-sky-900/60 bg-sky-950/30 text-sky-300", "m3-suppliers-count"],
                  ["Downstream Customers", supplyChainExposure.customersCount, "border-indigo-900/60 bg-indigo-950/30 text-indigo-300", "m3-customers-count"],
                ]
              }
              meta={supplyChainExposure && moduleMeta(supplyChainExposure, "m3-extractor", "Validation", supplyChainExposure.factorValidationStatus, "Scenario / edge provenance", "m3-reason-codes")}
            />
          </div>

          <div className="rounded-xl border border-border bg-card p-5">
            <h2 className="text-base font-semibold text-gray-200">Theme Purity &amp; Business Exposure (Module 6)</h2>
            <p className="mt-1 text-xs text-gray-400">Traceable segment revenue classification across active investment themes.</p>
            {themes.length > 0 ? (
              <div className="mt-4 space-y-4">
                {themes.map((theme) =>
                  theme.unvisitedReason ? (
                    <div key={theme.themeId} className="flex flex-wrap items-center justify-between text-sm">
                      <span className="font-medium text-gray-200">{theme.theme}</span>
                      <span className="font-mono text-xs text-gray-400">{unvisitedReasonText(theme.unvisitedReason)}</span>
                    </div>
                  ) : (
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
                        <div
                          className="h-2 rounded-full bg-sky-400 transition-all duration-300"
                          style={{ width: `${theme.themeShare ? Math.min(100, Math.max(0, Number(theme.themeShare) * 100)) : 0}%` }}
                        />
                      </div>
                    </div>
                  ),
                )}
              </div>
            ) : (
              <p className="mt-4 text-sm text-gray-500">No theme purity materialization recorded for this issuer.</p>
            )}
          </div>

          <div className="space-y-3">
            <h2 className="text-base font-semibold text-gray-200">Cutoff Timeline &amp; Decision History</h2>
            <ResearchTable
              caption={`${displayName} across cutoffs`}
              headers={["Cutoff", "Efficiency", "P/S", "Valuation multiple", "Tier", "Valuation Gap", `PEG (${PEG_CONVENTION_LABELS[convention]})`, "Availability", "Trace"]}
            >
              {sortedRows.map((row) => {
                const rowPeg = projectPegByConvention(row, convention).peg;
                return (
                  <tr key={row.cutoffAt} className="border-t border-border">
                    <th scope="row" className="px-4 py-3 font-medium text-white">{row.cutoffAt}</th>
                    <td className="px-4 py-3 font-mono" title={row.capitalAdjustedLaborEfficiency ?? undefined}>
                      {usdOrCell(row.capitalAdjustedLaborEfficiency)}
                      {row.capitalAdjustedLaborEfficiency !== null && (
                        <div className="font-sans text-[11px] text-gray-500" data-testid="labor-row-metric">
                          {row.laborEfficiencyMetric ?? "metric not recorded"}
                        </div>
                      )}
                    </td>
                    <RatioTd value={row.currentPriceToSales} mono />
                    <td className="px-4 py-3 font-mono" title={row.currentMultiple ?? undefined}>
                      {row.currentMultiple ? `${formatRatio(row.currentMultiple)} ${valuationBasisLabel(row.valuationBasis ?? null)}` : formatCell(null)}
                    </td>
                    <td className="px-4 py-3">{formatTierLabel(row.tier)}</td>
                    <SignedRatioTd value={row.valuationGap} />
                    <RatioTd value={rowPeg} mono />
                    <td className="px-4 py-3"><AvailabilityBadge status={row.availability} /></td>
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
            </ResearchTable>
          </div>
        </>
      )}
    </section>
  );
}
