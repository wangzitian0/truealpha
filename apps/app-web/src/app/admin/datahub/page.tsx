import type { ReactNode } from "react";
import Link from "next/link";

import { formatCell } from "@/client/format";
import {
  LEDGER_COVERAGE_DAYS,
  loadDatahubStats,
  loadVerdictHistory,
  type ListingRatioCell,
  type ListingRatioReport,
  type NightlyVerdictRow,
  type QuestionCoverageQuestion,
  type ValidationRow,
} from "@/server/admin/datahub-stats";
import { getServerPrincipal } from "@/server/auth/request-context";

import { LedgerCoverageTable } from "./ledger-coverage";

export const dynamic = "force-dynamic";

const verdictLabel = (ok: boolean | null): string => (ok === null ? "pending" : ok ? "ok" : "fail");
const verdictStyle = (ok: boolean | null): string =>
  ok === null ? "text-gray-400" : ok ? "text-emerald-400" : "text-rose-400";
const shortRun = (id: string): string => (id.length > 24 ? `${id.slice(0, 24)}…` : id);

const BADGE_TONES = {
  ok: "bg-emerald-950/60 text-emerald-400 border border-emerald-800",
  warn: "bg-amber-950/60 text-amber-400 border border-amber-800",
  crit: "bg-rose-950/80 text-rose-400 border border-rose-800",
  muted: "bg-zinc-800 text-zinc-300 border border-zinc-700",
} as const;

function getPhysicalStatus(q: QuestionCoverageQuestion): { label: string; badgeClass: string } {
  if (q.denominator === 0) return { label: "N/A (out of scope)", badgeClass: BADGE_TONES.muted };
  if (!q.column || q.missing === q.denominator) {
    return { label: "Not Integrated", badgeClass: "bg-rose-950/60 text-rose-300 border border-rose-800" };
  }
  if (q.answered === q.denominator) return { label: "Connected (100%)", badgeClass: BADGE_TONES.ok };
  if (q.answered > 0) {
    return { label: `Partial (${Math.round((q.answered / q.denominator) * 100)}%)`, badgeClass: BADGE_TONES.warn };
  }
  return { label: "Disconnected (0%)", badgeClass: BADGE_TONES.crit };
}

function listingRatioText(cell: ListingRatioCell): string {
  if (cell.outcome === "parsed") return `1 unit = ${cell.listing_units_per_share} ordinary shares`;
  if (cell.outcome === "ordinary") return "ordinary share (1)";
  if (cell.outcome === "not_measured") return "not measured";
  return `unparsed: ${formatCell(cell.reason)}`;
}

function listingRatioFiling(cell: ListingRatioCell): string | null {
  return cell.accession === null ? null : [cell.form, cell.accession, cell.filed].filter(Boolean).join(" ");
}

function ListingRatioBlock({ report }: { report: ListingRatioReport | null }) {
  if (report === null) return null;
  const { counts } = report;
  return (
    <div className="mt-3 text-sm">
      <h3 className="font-semibold">Depositary ratio of the listing (#1519)</h3>
      <p className="text-gray-400">
        {counts.parsed} parsed ratio · {counts.ordinary} ordinary (1) ·{" "}
        <span className={counts.unparsed > 0 ? "text-amber-400 font-medium" : ""}>{counts.unparsed} unparsed</span> ·{" "}
        {counts.not_measured} not measured
      </p>
      {report.cells.length > 0 && (
        <details open={counts.unparsed > 0} className="mt-1">
          <summary className="cursor-pointer text-gray-500">{report.cells.length} listings, unparsed first</summary>
          <DataTable headers={["listing", "outcome", "cover title", "filing"]} className="mt-2 w-full text-left">
            {report.cells.map((cell) => (
              <tr key={cell.listing_id} className={cell.outcome === "unparsed" ? "text-amber-400" : ""}>
                <td className="pr-4 font-mono">{cell.ticker ? `${cell.ticker} (${cell.listing_id})` : cell.listing_id}</td>
                <td className="pr-4">{listingRatioText(cell)}</td>
                <td className="pr-4">{formatCell(cell.title)}</td>
                <td className="font-mono">{formatCell(listingRatioFiling(cell))}</td>
              </tr>
            ))}
          </DataTable>
        </details>
      )}
    </div>
  );
}

function renderTrackingIssue(issue: string) {
  if (!issue) return <span className="text-gray-500">{formatCell(null)}</span>;
  const match = issue.match(/#?(\d+)/);
  if (!match) return <span className="font-mono text-gray-400">{issue}</span>;
  return (
    <a
      href={`https://github.com/wangzitian0/truealpha/issues/${match[1]}`}
      target="_blank"
      rel="noopener noreferrer"
      className="font-mono text-accent hover:underline inline-flex items-center gap-1"
    >
      <span>#{match[1]}</span>
      <span className="text-xs text-gray-500">↗</span>
    </a>
  );
}

function OverviewCard({
  title,
  badge,
  badgeTone,
  metric,
  unit,
  foot,
}: {
  title: string;
  badge: string;
  badgeTone: keyof typeof BADGE_TONES;
  metric: ReactNode;
  unit?: string;
  foot: ReactNode;
}) {
  return (
    <div className="rounded-xl border border-border bg-card p-4 flex flex-col justify-between">
      <div>
        <div className="flex items-center justify-between text-xs text-gray-400">
          <span className="uppercase tracking-wider font-semibold">{title}</span>
          <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium border ${BADGE_TONES[badgeTone]}`}>
            {badge}
          </span>
        </div>
        <div className="mt-3 text-2xl font-bold tracking-tight">
          {metric}
          {unit && <span className="ml-1 text-sm font-normal text-gray-400">{unit}</span>}
        </div>
      </div>
      <p className="mt-2 text-xs text-gray-400">{foot}</p>
    </div>
  );
}

function DataTable({
  headers,
  children,
  className = "w-full text-left text-sm",
}: {
  headers: readonly string[];
  children: ReactNode;
  className?: string;
}) {
  return (
    <table className={className}>
      <thead>
        <tr className="text-gray-500">
          {headers.map((h, i) => (
            <th key={h} className={i < headers.length - 1 ? "pr-4 font-normal" : "font-normal"}>
              {h}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>{children}</tbody>
    </table>
  );
}

function CheckVerdictTable({
  headers,
  rows,
  okVerdicts,
}: {
  headers: readonly [string, string, string];
  rows: readonly ValidationRow[];
  okVerdicts: readonly string[];
}) {
  return (
    <DataTable headers={headers}>
      {rows.map((row) => (
        <tr key={row.check} className="border-t border-border">
          <td className="pr-4 font-mono">{row.check}</td>
          <td className={`pr-4 ${okVerdicts.includes(row.verdict) ? "" : "text-amber-400"}`}>{row.verdict}</td>
          <td className="text-gray-400">{row.detail}</td>
        </tr>
      ))}
    </DataTable>
  );
}

function VerdictRows({ rows, showCheckLink }: { rows: readonly NightlyVerdictRow[]; showCheckLink?: boolean }) {
  return (
    <>
      {rows.map((v) => (
        <tr
          key={showCheckLink ? v.check_name : `${v.ran_at}-${v.dagster_run_id ?? ""}`}
          className="border-t border-border"
          data-history-row={showCheckLink ? undefined : "true"}
        >
          {showCheckLink && (
            <td className="pr-4 font-mono">
              <Link
                href={`/admin/datahub?history=${encodeURIComponent(v.check_name)}#verdict-history`}
                className="underline decoration-dotted hover:text-accent"
              >
                {v.check_name}
              </Link>
            </td>
          )}
          <td className={`pr-4 font-medium ${verdictStyle(v.ok)}`}>{verdictLabel(v.ok)}</td>
          <td className="pr-4 text-gray-400">{v.ran_at}</td>
          <td className="pr-4 font-mono text-gray-400" title={v.dagster_run_id ?? undefined}>
            {v.dagster_run_id ? shortRun(v.dagster_run_id) : formatCell(null)}
          </td>
          <td className="font-mono text-xs text-gray-300">{v.summary}</td>
        </tr>
      ))}
    </>
  );
}

export default async function AdminDatahubPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const principal = await getServerPrincipal();
  if (!principal || principal.principalKind !== "administrator") {
    return <p className="text-amber-400">Access denied.</p>;
  }
  const stats = await loadDatahubStats();
  const requested = (await searchParams).history;
  const historyCheck = typeof requested === "string" ? requested : null;
  const history = historyCheck === null ? null : await loadVerdictHistory(historyCheck, principal);

  const totalHeads = stats.heads.length;
  const freshHeads = stats.heads.filter((h) => h.freshness === "fresh" || h.freshness === "manual").length;
  const staleHeads = stats.heads.filter((h) => h.freshness === "stale").length;
  const anyHeadDegraded = stats.heads.some((head) => head.is_degraded);

  let totalQuestionsEvaluated = 0;
  let totalAnsweredCells = 0;
  let totalDenominatorCells = 0;
  let connectedQuestionsCount = 0;
  let notIntegratedQuestionsCount = 0;
  for (const report of stats.questionCoverage) {
    for (const q of report.questions) {
      if (q.denominator <= 0) continue;
      totalQuestionsEvaluated += 1;
      totalAnsweredCells += q.answered;
      totalDenominatorCells += q.denominator;
      if (q.answered === q.denominator) connectedQuestionsCount += 1;
      if (!q.column || q.missing === q.denominator) notIntegratedQuestionsCount += 1;
    }
  }
  const matrixConnectivityRatio =
    totalDenominatorCells > 0 ? ((totalAnsweredCells / totalDenominatorCells) * 100).toFixed(1) : null;

  const totalCalls = stats.traffic.reduce((acc, t) => acc + t.calls, 0);
  const totalFailedCalls = stats.traffic.reduce((acc, t) => acc + t.failed, 0);
  const totalLandedFetches = stats.traffic.reduce((acc, t) => acc + t.landed, 0);
  const totalFetches24h = stats.sources.reduce((acc, s) => acc + s.fetches_24h, 0);
  const recentResolved = stats.runs.reduce((acc, r) => acc + r.resolved, 0);
  const recentObligations = stats.runs.reduce((acc, r) => acc + r.obligations, 0);

  return (
    <section className="space-y-8">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Datahub</h1>
        <p className="mt-1 text-sm text-gray-500">
          Governed heads, what they serve, and where the bytes came from, including capture-level and per-factor availability.
        </p>
      </div>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <OverviewCard
          title="Governed Heads"
          badge={
            totalHeads === 0
              ? "empty"
              : staleHeads > 0
                ? `${staleHeads} stale`
                : anyHeadDegraded
                  ? "degraded"
                  : "all fresh"
          }
          badgeTone={totalHeads === 0 ? "muted" : anyHeadDegraded ? "warn" : "ok"}
          metric={totalHeads === 0 ? formatCell(null) : `${freshHeads}/${totalHeads}`}
          unit="fresh pointers"
          foot={<>Served heads from <span className="font-mono text-zinc-300">mart.served_head</span>.</>}
        />
        <OverviewCard
          title="Question Connectivity"
          badge={
            totalDenominatorCells === 0
              ? "no report"
              : `${connectedQuestionsCount}/${totalQuestionsEvaluated} connected`
          }
          badgeTone={totalDenominatorCells === 0 ? "muted" : notIntegratedQuestionsCount > 0 ? "warn" : "ok"}
          metric={matrixConnectivityRatio ? `${matrixConnectivityRatio}%` : formatCell(null)}
          unit={`(${totalAnsweredCells}/${totalDenominatorCells} cells)`}
          foot={
            notIntegratedQuestionsCount > 0
              ? `${notIntegratedQuestionsCount} questions missing underlying mart column`
              : "All evaluated questions integrated"
          }
        />
        <OverviewCard
          title="Gateway Traffic"
          badge={totalCalls === 0 ? "idle" : totalFailedCalls > 0 ? `${totalFailedCalls} failed` : "healthy"}
          badgeTone={totalFailedCalls > 0 ? "warn" : "ok"}
          metric={totalCalls}
          unit="calls today"
          foot={<>{totalLandedFetches} landed into <span className="font-mono text-zinc-300">raw.fetches</span>.</>}
        />
        <OverviewCard
          title="Capture Footprint"
          badge={`${stats.sources.length} sources`}
          badgeTone="muted"
          metric={totalFetches24h}
          unit="fetches (24h)"
          foot={
            recentObligations > 0
              ? `${recentResolved}/${recentObligations} recent obligations resolved`
              : "No capture obligations logged"
          }
        />
      </div>

      <div className="space-y-4">
        <div>
          <div className="flex items-center gap-3">
            <h2 className="text-lg font-semibold">Question coverage matrix (init.md §0)</h2>
            <span className="rounded bg-accent/20 px-2 py-0.5 text-xs font-medium text-accent">
              Penetrating Physical Reality
            </span>
          </div>
          <p className="mt-1 text-sm text-gray-500">
            Expected from the question registry, observed from the governed head&apos;s status dimensions.
          </p>
        </div>

        {stats.questionCoverage.length === 0 && (
          <p className="rounded-lg border border-border bg-card p-4 text-gray-400">No coverage report yet.</p>
        )}

        {stats.questionCoverage.map((report) => {
          const universeAnswered = report.questions.reduce((acc, q) => acc + q.answered, 0);
          const universeDenominator = report.questions.reduce((acc, q) => acc + q.denominator, 0);
          const universeRatio = universeDenominator > 0 ? (universeAnswered / universeDenominator) * 100 : 0;
          const isFullConnected = universeDenominator > 0 && universeAnswered === universeDenominator;

          return (
            <div key={report.universe_id} className="rounded-xl border border-border bg-card p-5 space-y-4">
              <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border/80 pb-3">
                <div className="flex flex-wrap items-baseline gap-3">
                  <span className="font-mono font-semibold text-foreground">{report.universe_id}</span>
                  <span className="text-xs text-gray-400">cutoff {report.cutoff}</span>
                  <span className="text-xs text-gray-500">generated {report.generated_at}</span>
                </div>
                <div className="flex items-center gap-2">
                  <span className="text-xs text-gray-400">Universe coverage:</span>
                  <span
                    className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ${
                      isFullConnected ? BADGE_TONES.ok : BADGE_TONES.warn
                    }`}
                  >
                    {universeAnswered}/{universeDenominator} cells ({universeRatio.toFixed(1)}%)
                  </span>
                </div>
              </div>

              <div className="overflow-x-auto">
                <table className="w-full text-left text-sm">
                  <thead>
                    <tr className="text-gray-400 border-b border-border/60 text-xs uppercase tracking-wider">
                      <th className="py-2 pr-4 font-medium">Question</th>
                      <th className="py-2 pr-4 font-medium">Physical Status</th>
                      <th className="py-2 pr-4 font-medium">Wiring / Mart Column</th>
                      <th className="py-2 pr-4 font-medium">Coverage Progress</th>
                      <th className="py-2 font-medium">Failure Breakdown &amp; Diagnostics</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border/40">
                    {report.questions.map((q) => {
                      const status = getPhysicalStatus(q);
                      const percent = q.denominator > 0 ? Math.round((q.answered / q.denominator) * 100) : 0;
                      return (
                        <tr key={q.question} className="hover:bg-card/40 transition-colors">
                          <td className="py-3 pr-4 align-top">
                            <div className="flex items-center gap-2">
                              <span className="font-mono font-bold text-accent">{q.question.toUpperCase()}</span>
                              <span className="inline-flex items-center rounded px-1.5 py-0.5 text-[10px] uppercase font-mono text-zinc-400 bg-zinc-800 border border-zinc-700">
                                {q.scope}
                              </span>
                            </div>
                            <div className="mt-1 text-xs text-gray-300 max-w-xs">{q.text}</div>
                          </td>
                          <td className="py-3 pr-4 align-top">
                            <span className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ${status.badgeClass}`}>
                              {status.label}
                            </span>
                          </td>
                          <td className="py-3 pr-4 align-top">
                            {q.column ? (
                              <code className="text-xs font-mono text-emerald-400 bg-emerald-950/30 px-1.5 py-0.5 rounded border border-emerald-900/50">
                                {q.column}
                              </code>
                            ) : (
                              <div className="space-y-1">
                                <span className="text-xs text-rose-400 font-mono">No column</span>
                                <div>{renderTrackingIssue(q.tracking_issue)}</div>
                              </div>
                            )}
                          </td>
                          <td className="py-3 pr-4 align-top min-w-[180px]">
                            {q.denominator > 0 ? (
                              <div className="space-y-1">
                                <div className="flex items-center justify-between text-xs">
                                  <span className="font-mono text-gray-200">{q.answered}/{q.denominator}</span>
                                  <span className="text-gray-400 font-mono">{percent}%</span>
                                </div>
                                <div className="flex h-2 w-full overflow-hidden rounded bg-zinc-800">
                                  {q.answered > 0 && (
                                    <div style={{ width: `${(q.answered / q.denominator) * 100}%` }} className="bg-emerald-500" title={`Answered: ${q.answered}`} />
                                  )}
                                  {q.unavailable_total > 0 && (
                                    <div style={{ width: `${(q.unavailable_total / q.denominator) * 100}%` }} className="bg-amber-500" title={`Unavailable: ${q.unavailable_total}`} />
                                  )}
                                  {q.missing > 0 && (
                                    <div style={{ width: `${(q.missing / q.denominator) * 100}%` }} className="bg-rose-500/80" title={`Missing: ${q.missing}`} />
                                  )}
                                </div>
                              </div>
                            ) : (
                              <span className="text-xs text-gray-500">{formatCell(null)}</span>
                            )}
                          </td>
                          <td className="py-3 align-top">
                            {q.denominator === 0 ? (
                              <span className="text-xs text-gray-500">Not applicable to this universe</span>
                            ) : q.answered === q.denominator ? (
                              <span className="text-xs text-emerald-400/90 flex items-center gap-1 font-mono">✓ Zero defects</span>
                            ) : (
                              <div className="flex flex-wrap gap-1.5 items-center">
                                {q.unavailable_total > 0 &&
                                  (q.top_reasons ? q.top_reasons.split(", ") : [`unavailable=${q.unavailable_total}`]).map((reason, idx) => (
                                    <span key={idx} className="inline-flex items-center rounded bg-amber-950/50 px-1.5 py-0.5 text-xs font-mono text-amber-300 border border-amber-800/60">
                                      {reason}
                                    </span>
                                  ))}
                                {q.missing > 0 && (
                                  <span className="inline-flex items-center rounded bg-rose-950/50 px-1.5 py-0.5 text-xs font-mono text-rose-300 border border-rose-800/60">
                                    missing={q.missing}
                                  </span>
                                )}
                              </div>
                            )}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          );
        })}
      </div>

      <div className="space-y-3">
        <h2 className="text-lg font-semibold">Confidence &amp; accuracy (nightly)</h2>
        {stats.confidence.length === 0 && (
          <p className="rounded-lg border border-border bg-card p-4 text-gray-400">No confidence report yet.</p>
        )}
        {stats.confidence.map((report) => (
          <div key={report.universe_id} className="rounded-lg border border-border bg-card p-4">
            <div className="flex flex-wrap items-baseline gap-3">
              <span className="font-mono">{report.universe_id}</span>
              <span className="text-gray-400">cutoff {report.cutoff}</span>
              <span className="text-gray-400">generated {report.generated_at}</span>
              <span className="text-gray-400">
                sources connected: {report.sources_connected.length} ({report.sources_connected.join(", ")})
              </span>
            </div>
            <DataTable
              headers={["family", "0 origins", "1 origin", "2 origins", "3+ origins", "mean confidence", "agreement (compared)", "origins"]}
              className="mt-3 w-full text-left text-sm"
            >
              {report.families.map((family) => (
                <tr key={family.family} className="border-t border-border">
                  <td className="pr-4 font-mono">{family.family}</td>
                  <td className="pr-4">{formatCell(family.origin_counts?.none ?? null)}</td>
                  <td className="pr-4">{formatCell(family.origin_counts?.one ?? null)}</td>
                  <td className="pr-4">{formatCell(family.origin_counts?.two ?? null)}</td>
                  <td className="pr-4">{formatCell(family.origin_counts?.three_or_more ?? null)}</td>
                  <td className="pr-4">{formatCell(family.mean_confidence)}</td>
                  <td className="pr-4">
                    {formatCell(family.agreement_rate)} ({family.compared}/{family.cells})
                  </td>
                  <td className="font-mono text-gray-400">{family.origins.join(", ")}</td>
                </tr>
              ))}
            </DataTable>
            <p className="mt-2 text-sm text-gray-400">
              matches the quality report per family:
              {report.quality_report_matches.length === 0 && ` ${formatCell(null)}`}
              {report.quality_report_matches.map((match) => (
                <span key={match.family}>
                  {" "}
                  <span className="font-mono">{match.family}</span> {formatCell(match.matches)}
                  {match.mismatches > 0 ? ` (${match.mismatches} cells)` : ""}
                </span>
              ))}
              {" · "}stored confidence used for bands: {String(report.stored_confidence_used_for_bands)}
            </p>
          </div>
        ))}
      </div>

      <div className="space-y-3" data-testid="oracle-outcomes-section">
        <div>
          <h2 className="text-lg font-semibold">Oracle outcomes (mart.oracle_outcomes)</h2>
          <p className="text-sm text-gray-400">
            Outside comparisons per metric across active governed universes; metrics without outcome rows show{" "}
            <span className="font-mono">no comparator</span>.
          </p>
        </div>
        <div className="overflow-x-auto rounded-lg border border-border bg-card p-4">
          <table className="w-full text-left text-sm" data-testid="oracle-outcomes-table">
            <thead className="text-gray-400">
              <tr>
                <th className="py-1 pr-4">metric</th>
                {stats.oracleOutcomes.universes.map((universeId) => (
                  <th key={universeId} className="py-1 pr-4 font-mono">
                    {universeId}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {stats.oracleOutcomes.rows.map((row) => (
                <tr key={row.metric}>
                  <td className="py-2 pr-4 font-mono align-top">{row.metric}</td>
                  {stats.oracleOutcomes.universes.map((universeId) => {
                    const c = row.cells[universeId] ?? null;
                    return (
                      <td key={universeId} className="py-2 pr-4 align-top">
                        {c === null ? (
                          <span className="font-mono text-gray-500">no comparator</span>
                        ) : (
                          <div className="space-y-0.5 text-xs">
                            <div>
                              compared <span className="font-mono">{c.compared}</span> · agree{" "}
                              <span className="font-mono text-emerald-400">{c.agree}</span> · disagree{" "}
                              <span className={c.disagree > 0 ? "font-mono text-rose-400 font-medium" : "font-mono"}>
                                {c.disagree}
                              </span>{" "}
                              · not_comparable <span className="font-mono">{c.not_comparable}</span>
                              {c.no_served_value_count > 0 && (
                                <>
                                  {" "}· no_served_value{" "}
                                  <span className="font-mono text-amber-400 font-medium">{c.no_served_value_count}</span>
                                </>
                              )}
                            </div>
                            <div className="text-gray-400">
                              wilson_95_upper_bound <span className="font-mono">{formatCell(c.wilson_95_upper_bound)}</span> ·
                              newest_compared_at <span className="font-mono">{formatCell(c.newest_compared_at)}</span>
                            </div>
                          </div>
                        )}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div className="space-y-3">
        <h2 className="text-lg font-semibold">Governed heads</h2>
        {stats.heads.length === 0 && (
          <p className="rounded-lg border border-border bg-card p-4 text-gray-400">No pointer heads yet.</p>
        )}
        {stats.heads.map((head) => {
          const ageLabel = head.age_hours !== null ? `${head.age_hours.toFixed(1)}h` : null;
          const staleDetails = [ageLabel, head.staleness_reason ?? null].filter(Boolean).join(" — ");
          return (
            <div key={head.universe_id} className="rounded-lg border border-border bg-card p-4">
              <div className="flex flex-wrap items-baseline gap-3">
                <span className="font-mono text-accent">{head.universe_id}</span>
                <span className="text-sm text-gray-400">seq {head.sequence}</span>
                <span className="text-sm text-gray-500">{head.advanced_at}</span>
                <span
                  className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ${
                    head.freshness === "fresh"
                      ? BADGE_TONES.ok
                      : head.freshness === "manual"
                        ? BADGE_TONES.muted
                        : head.freshness === "stale"
                          ? BADGE_TONES.crit
                          : "bg-gray-800 text-gray-400 border border-gray-700"
                  }`}
                >
                  {head.freshness === "fresh" || head.freshness === "manual"
                    ? `${head.freshness}${ageLabel ? ` (${ageLabel})` : ""}`
                    : head.freshness === "stale"
                      ? `stale${staleDetails ? ` (${staleDetails})` : ""}`
                      : "freshness unknown"}
                </span>
                <span
                  className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ${
                    head.is_degraded
                      ? BADGE_TONES.warn
                      : "bg-emerald-950/30 text-emerald-500/80 border border-emerald-900/50"
                  }`}
                >
                  {head.is_degraded ? `degraded ${head.degraded_reasons.join(" · ")}` : "objectives met"}
                </span>
              </div>
              <dl className="mt-2 grid grid-cols-2 gap-2 text-sm sm:grid-cols-4">
                <div>
                  <dt className="text-gray-500">target run</dt>
                  <dd className="font-mono" title={head.target_run_id}>{shortRun(head.target_run_id)}</dd>
                </div>
                <div>
                  <dt className="text-gray-500">capture availability</dt>
                  <dd className={head.is_avail_degraded ? "text-amber-400 font-medium" : ""}>{formatCell(head.availability)}</dd>
                </div>
                <div>
                  <dt className="text-gray-500">corroborated cells</dt>
                  <dd className={head.is_corroboration_degraded ? "text-amber-400 font-medium" : ""}>
                    {formatCell(head.agreed_cells)}/{formatCell(head.total_cells)}
                  </dd>
                </div>
                <div>
                  <dt className="text-gray-500">freshness status</dt>
                  <dd className={head.freshness === "stale" ? "text-rose-400 font-medium" : ""}>
                    {head.freshness}{ageLabel ? ` (${ageLabel})` : ""}
                  </dd>
                </div>
              </dl>
              {head.factors.length > 0 && (
                <DataTable headers={["factor", "complete / universe", "factor availability"]} className="mt-3 w-full text-left text-sm">
                  {head.factors.map((factor) => (
                    <tr key={factor.factor_id}>
                      <td className="pr-4 font-mono">{factor.factor_id}</td>
                      <td className="pr-4">{factor.complete_subjects}/{factor.universe_subjects}</td>
                      <td>{factor.ratio}</td>
                    </tr>
                  ))}
                </DataTable>
              )}
              <ListingRatioBlock report={head.listing_ratio} />
            </div>
          );
        })}
      </div>

      <div className="space-y-3">
        <div>
          <h2 className="text-lg font-semibold">Standing check verdicts (mart.nightly_verdicts)</h2>
          <p className="text-sm text-gray-500">
            Current verdict per check, newest recorded. Select a check to list its history.
          </p>
        </div>
        {stats.nightlyVerdicts.length === 0 ? (
          <p className="rounded-lg border border-border bg-card p-4 text-gray-400">
            No standing check verdicts recorded in <span className="font-mono">mart.nightly_verdicts</span> yet.
          </p>
        ) : (
          <DataTable headers={["check", "verdict", "ran at (UTC)", "dagster run", "summary"]}>
            <VerdictRows rows={stats.nightlyVerdicts} showCheckLink />
          </DataTable>
        )}
      </div>

      {historyCheck !== null && (
        <div id="verdict-history" className="space-y-3">
          <h2 className="text-lg font-semibold">
            History of <span className="font-mono">{historyCheck}</span>
          </h2>
          {history === null ? (
            <p className="rounded-lg border border-border bg-card p-4 text-gray-400">
              That is not the name of a check, so there is no history to list.
            </p>
          ) : history.length === 0 ? (
            <p className="rounded-lg border border-border bg-card p-4 text-gray-400">
              No verdict is recorded for this check.
            </p>
          ) : (
            <DataTable headers={["verdict", "ran at (UTC)", "dagster run", "summary"]}>
              <VerdictRows rows={history} />
            </DataTable>
          )}
        </div>
      )}

      <div className="space-y-3">
        <h2 className="text-lg font-semibold">Validation</h2>
        <CheckVerdictTable headers={["check", "verdict", "detail"]} rows={stats.validation} okVerdicts={["pass", "active", "idle", "ok"]} />
      </div>

      <div className="space-y-3" id="vendor-account-shares">
        <h2 className="text-lg font-semibold">Vendor account shares (#1520)</h2>
        <div className="overflow-x-auto">
          <DataTable
            headers={[
              "account / seats",
              "env share (%)",
              "rows today / daily share",
              "peak window / share",
              "429 / 1302",
              "evidence.kind & unmeasured",
              "verdict",
            ]}
          >
            {stats.vendorAccounts.map((row) => (
              <tr key={row.account} className="border-t border-border">
                <td className="pr-4 font-mono">
                  {row.account} <span className="text-xs text-gray-500">({row.seats.join(", ")}; {row.holder})</span>
                </td>
                <td className="pr-4 font-mono text-gray-300">
                  {row.environment} {row.percent}%{" "}
                  <span className="text-xs text-gray-500">
                    ({Object.values(row.shares_by_environment).map((s) => `${s.environment}:${s.percent}%`).join(" / ")})
                  </span>
                </td>
                <td className="pr-4 font-mono">
                  {row.daily_usage_text}
                  {row.daily_ratio_text !== null ? ` (ratio ${row.daily_ratio_text})` : ""}
                </td>
                <td className="pr-4 font-mono">{row.window_usage_text}</td>
                <td className={`pr-4 font-mono ${row.http_429_today > 0 || row.code_1302_today > 0 ? "text-amber-400" : ""}`}>
                  429={row.http_429_today} / 1302={row.code_1302_today}
                </td>
                <td className="pr-4 font-mono text-gray-400">
                  {row.evidence_kinds.join(", ")}
                  {row.not_known_limits.length > 0
                    ? ` (${row.not_known_limits.map((l) => `${l.id}:${l.evidence_kind}`).join(", ")})`
                    : ""}
                </td>
                <td className={row.verdict === "ok" ? "" : "text-amber-400"}>{row.verdict}</td>
              </tr>
            ))}
          </DataTable>
        </div>
      </div>

      <div className="space-y-3" id="model-seat-1302">
        <h2 className="text-lg font-semibold">Model seat 1302 answers per environment and day (#1520)</h2>
        {stats.model1302ByDay.length === 0 ? (
          <p className="rounded-lg border border-border bg-card p-4 text-gray-400">No model seat calls recorded in the window.</p>
        ) : (
          <DataTable headers={["environment", "UTC day", "1302 answers", "HTTP 429", "total calls", "sampled pace"]}>
            {stats.model1302ByDay.map((row) => (
              <tr key={`${row.environment}:${row.day}`} className="border-t border-border">
                <td className="pr-4 font-mono">{row.environment}</td>
                <td className="pr-4 font-mono">{row.day}</td>
                <td className={`pr-4 font-mono ${row.code_1302 > 0 ? "text-amber-400" : ""}`}>{row.code_1302}</td>
                <td className={`pr-4 font-mono ${row.http_429 > 0 ? "text-amber-400" : ""}`}>{row.http_429}</td>
                <td className="pr-4 font-mono">{row.total_calls}</td>
                <td className="font-mono text-gray-400">
                  {row.calls_per_minute_limit !== null
                    ? `${row.calls_per_minute_limit}/min, ${row.max_concurrent_calls} concurrent`
                    : "unpaced"}
                </td>
              </tr>
            ))}
          </DataTable>
        )}
      </div>

      <div className="space-y-3">
        <h2 className="text-lg font-semibold">Capacity</h2>
        <CheckVerdictTable headers={["limit", "state", "detail"]} rows={stats.capacity} okVerdicts={["ok", "pass", "active"]} />
      </div>

      <div className="space-y-3">
        <h2 className="text-lg font-semibold">Traffic (external calls, today UTC)</h2>
        {stats.traffic.length === 0 ? (
          <p className="rounded-lg border border-border bg-card p-4 text-gray-400">No external calls recorded today.</p>
        ) : (
          <DataTable headers={["source", "calls", "failed", "landed", "avg ms", "last call", "last error"]}>
            {stats.traffic.map((row) => (
              <tr key={row.source} className="border-t border-border">
                <td className="pr-4 font-mono">{row.source}</td>
                <td className="pr-4">{row.calls}</td>
                <td className={`pr-4 ${row.failed > 0 ? "text-amber-400" : ""}`}>{row.failed}</td>
                <td className="pr-4">
                  {row.calls - row.failed > 0 ? `${row.landed}/${row.calls - row.failed}` : formatCell(null)}
                </td>
                <td className="pr-4">{formatCell(row.avg_ms)}</td>
                <td className="pr-4 text-gray-400">{row.last_call}</td>
                <td className="text-gray-400">{formatCell(row.last_error)}</td>
              </tr>
            ))}
          </DataTable>
        )}
      </div>

      <div className="space-y-3">
        <h2 className="text-lg font-semibold">Recent external calls</h2>
        {stats.recentCalls.length === 0 ? (
          <p className="rounded-lg border border-border bg-card p-4 text-gray-400">
            No ledger rows yet — the first tick after this deploy writes them.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <DataTable headers={["at", "source", "endpoint", "status", "ms", "landed", "run", "error"]}>
              {stats.recentCalls.map((call) => (
                <tr key={call.id} className="border-t border-border">
                  <td className="pr-4 text-gray-400">{call.called_at}</td>
                  <td className="pr-4 font-mono">{call.source}</td>
                  <td className="pr-4 font-mono">{call.endpoint}</td>
                  <td className={`pr-4 ${call.ok ? "" : "text-amber-400"}`}>
                    {call.ok ? "ok" : "failed"}
                    {call.status_code !== null ? ` ${call.status_code}` : ""}
                  </td>
                  <td className="pr-4">{formatCell(call.duration_ms)}</td>
                  <td className="pr-4 font-mono">
                    {call.landed_fetch_id !== null ? `raw.fetches#${call.landed_fetch_id}` : formatCell(null)}
                  </td>
                  <td className="pr-4 font-mono text-gray-400" title={call.run_key ?? undefined}>
                    {call.run_key ? shortRun(call.run_key) : formatCell(null)}
                  </td>
                  <td className="text-gray-400">{formatCell(call.error)}</td>
                </tr>
              ))}
            </DataTable>
          </div>
        )}
      </div>

      <div className="space-y-3" id="ledger-coverage">
        <div>
          <h2 className="text-lg font-semibold">Fetch key and run coverage (call ledger, last {LEDGER_COVERAGE_DAYS} UTC days)</h2>
        </div>
        <LedgerCoverageTable rows={stats.ledgerCoverage} />
      </div>

      <div className="space-y-3">
        <h2 className="text-lg font-semibold">Sources</h2>
        <DataTable headers={["source", "total", "last 24h", "last fetch"]}>
          {stats.sources.map((source) => (
            <tr key={source.source} className="border-t border-border">
              <td className="pr-4 font-mono">{source.source}</td>
              <td className="pr-4">{source.fetches_total}</td>
              <td className="pr-4">{source.fetches_24h}</td>
              <td className="text-gray-400">{source.last_fetch}</td>
            </tr>
          ))}
        </DataTable>
      </div>

      <div className="space-y-3">
        <h2 className="text-lg font-semibold">Recent capture runs</h2>
        <DataTable headers={["run", "universe", "cutoff", "resolved", "complete"]}>
          {stats.runs.map((run) => (
            <tr key={run.run_id} className="border-t border-border">
              <td className="pr-4 font-mono" title={run.run_id}>{shortRun(run.run_id)}</td>
              <td className="pr-4 font-mono">{run.universe_id}</td>
              <td className="pr-4 text-gray-400">{run.cutoff}</td>
              <td className="pr-4">
                {run.resolved}/{run.obligations}
                {run.failed > 0 ? ` (${run.failed} failed)` : ""}
              </td>
              <td>{run.complete ? "yes" : "no"}</td>
            </tr>
          ))}
        </DataTable>
      </div>
    </section>
  );
}
