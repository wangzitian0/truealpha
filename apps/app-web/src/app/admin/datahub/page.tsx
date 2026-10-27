/**
 * #641 D5: the datahub statistics dashboard — governed heads with their quality
 * grades (capture-level AND factor-level availability, #644), source fetch
 * activity, and recent capture runs. The numbers that used to live only in
 * psql sessions, on one page.
 *
 * Upgraded in #1219 to provide an Executive Plant Health Overview and a
 * Penetrating Physical Matrix for question coverage, enforcing "Strong Dashboard, Weak Gates".
 */

import Link from "next/link";

import { formatCell } from "@/client/format";
import {
  loadDatahubStats,
  loadVerdictHistory,
  type ListingRatioCell,
  type ListingRatioReport,
  type QuestionCoverageQuestion,
} from "@/server/admin/datahub-stats";
import { getServerPrincipal } from "@/server/auth/request-context";

export const dynamic = "force-dynamic";

/** A pending verdict (`ok = null`) is neither green nor red. */
function verdictLabel(ok: boolean | null): string {
  return ok === null ? "pending" : ok ? "ok" : "fail";
}

function verdictStyle(ok: boolean | null): string {
  return ok === null ? "text-gray-400" : ok ? "text-emerald-400" : "text-rose-400";
}

function shortRun(id: string): string {
  return id.length > 24 ? `${id.slice(0, 24)}…` : id;
}

type PhysicalStatus =
  | "connected"
  | "partial"
  | "disconnected"
  | "not_integrated"
  | "not_applicable";

function getPhysicalStatus(q: QuestionCoverageQuestion): {
  status: PhysicalStatus;
  label: string;
  badgeClass: string;
} {
  if (q.denominator === 0) {
    return {
      status: "not_applicable",
      label: "N/A (out of scope)",
      badgeClass: "bg-zinc-800/80 text-zinc-400 border border-zinc-700",
    };
  }
  if (!q.column || q.missing === q.denominator) {
    return {
      status: "not_integrated",
      label: "Not Integrated",
      badgeClass: "bg-rose-950/60 text-rose-300 border border-rose-800",
    };
  }
  if (q.answered === q.denominator) {
    return {
      status: "connected",
      label: "Connected (100%)",
      badgeClass: "bg-emerald-950/60 text-emerald-400 border border-emerald-800",
    };
  }
  if (q.answered > 0) {
    return {
      status: "partial",
      label: `Partial (${Math.round((q.answered / q.denominator) * 100)}%)`,
      badgeClass: "bg-amber-950/60 text-amber-400 border border-amber-800",
    };
  }
  return {
    status: "disconnected",
    label: "Disconnected (0%)",
    badgeClass: "bg-rose-950/80 text-rose-400 border border-rose-800",
  };
}

/** What one listing's cover read says, in words. The reason and the title text stay visible for `unparsed`. */
function listingRatioText(cell: ListingRatioCell): string {
  if (cell.outcome === "parsed") return `1 unit = ${cell.listing_units_per_share} ordinary shares`;
  if (cell.outcome === "ordinary") return "ordinary share (1)";
  if (cell.outcome === "not_measured") return "not measured";
  return `unparsed: ${cell.reason ?? "no reason"}`;
}

/**
 * #1519 step 1a: the depositary ratio that the cover of each issuer's latest annual filing gave. A measurement for the
 * owner to read before the ratio is enforced: no grade, no gate. The counts come from the data engine.
 */
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
        <details className="mt-1">
          <summary className="cursor-pointer text-gray-500">{report.cells.length} listings, unparsed first</summary>
          <table className="mt-2 w-full text-left">
            <thead>
              <tr className="text-gray-500">
                <th className="pr-4 font-normal">listing</th>
                <th className="pr-4 font-normal">outcome</th>
                <th className="pr-4 font-normal">cover title</th>
                <th className="font-normal">filing</th>
              </tr>
            </thead>
            <tbody>
              {report.cells.map((cell) => (
                <tr key={cell.listing_id} className={cell.outcome === "unparsed" ? "text-amber-400" : ""}>
                  <td className="pr-4 font-mono">{cell.listing_id}</td>
                  <td className="pr-4">{listingRatioText(cell)}</td>
                  <td className="pr-4">{cell.title ?? "—"}</td>
                  <td className="font-mono">
                    {cell.accession === null ? "—" : `${cell.form ?? ""} ${cell.accession} ${cell.filed ?? ""}`.trim()}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      )}
    </div>
  );
}

function renderTrackingIssue(issue: string) {
  if (!issue) return <span className="text-gray-500">—</span>;
  const match = issue.match(/#?(\d+)/);
  if (match) {
    const issueNum = match[1];
    return (
      <a
        href={`https://github.com/wangzitian0/truealpha/issues/${issueNum}`}
        target="_blank"
        rel="noopener noreferrer"
        className="font-mono text-accent hover:underline inline-flex items-center gap-1"
      >
        <span>#{issueNum}</span>
        <span className="text-xs text-gray-500">↗</span>
      </a>
    );
  }
  return <span className="font-mono text-gray-400">{issue}</span>;
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
  // #1466: `?history=<check>` lists every recorded verdict of one check under the table of current verdicts.
  const requested = (await searchParams).history;
  const historyCheck = typeof requested === "string" ? requested : null;
  const history = historyCheck === null ? null : await loadVerdictHistory(historyCheck, principal);

  // 1. Governed Heads Metrics
  const totalHeads = stats.heads.length;
  const freshHeads = stats.heads.filter(
    (h) => h.freshness === "fresh" || h.freshness === "manual",
  ).length;
  const staleHeads = stats.heads.filter((h) => h.freshness === "stale").length;
  const anyHeadDegraded = stats.heads.some((head) => head.is_degraded);

  // 2. Question Matrix Connectivity Metrics
  let totalQuestionsEvaluated = 0;
  let totalAnsweredCells = 0;
  let totalDenominatorCells = 0;
  let connectedQuestionsCount = 0;
  let notIntegratedQuestionsCount = 0;

  stats.questionCoverage.forEach((report) => {
    report.questions.forEach((q) => {
      if (q.denominator > 0) {
        totalQuestionsEvaluated += 1;
        totalAnsweredCells += q.answered;
        totalDenominatorCells += q.denominator;
        if (q.answered === q.denominator) {
          connectedQuestionsCount += 1;
        }
        if (!q.column || q.missing === q.denominator) {
          notIntegratedQuestionsCount += 1;
        }
      }
    });
  });

  const matrixConnectivityRatio =
    totalDenominatorCells > 0
      ? ((totalAnsweredCells / totalDenominatorCells) * 100).toFixed(1)
      : null;

  // 3. Gateway Traffic Metrics (Today UTC)
  const totalCalls = stats.traffic.reduce((acc, t) => acc + t.calls, 0);
  const totalFailedCalls = stats.traffic.reduce((acc, t) => acc + t.failed, 0);
  const totalLandedFetches = stats.traffic.reduce((acc, t) => acc + t.landed, 0);

  // 4. Ingestion & Runs Footprint Metrics
  const totalFetches24h = stats.sources.reduce((acc, s) => acc + s.fetches_24h, 0);
  const totalSources = stats.sources.length;
  const recentResolved = stats.runs.reduce((acc, r) => acc + r.resolved, 0);
  const recentObligations = stats.runs.reduce((acc, r) => acc + r.obligations, 0);

  return (
    <section className="space-y-8">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Datahub</h1>
        <p className="mt-1 text-sm text-gray-500">
          Governed heads, what they serve, and where the bytes came from. Availability is shown twice on
          purpose: the capture-level headline, and the per-factor share of subjects whose required inputs are
          all usable — the number a consumer actually feels.
        </p>
      </div>

      {/* Executive Plant Health Overview */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {/* Card 1: Governed Heads Freshness */}
        <div className="rounded-xl border border-border bg-card p-4 flex flex-col justify-between">
          <div>
            <div className="flex items-center justify-between text-xs text-gray-400">
              <span className="uppercase tracking-wider font-semibold">Governed Heads</span>
              <span
                className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium border ${
                  totalHeads === 0
                    ? "bg-zinc-800 text-zinc-400 border-zinc-700"
                    : anyHeadDegraded
                      ? "bg-amber-950/60 text-amber-400 border-amber-800"
                      : "bg-emerald-950/60 text-emerald-400 border-emerald-800"
                }`}
              >
                {totalHeads === 0
                  ? "empty"
                  : staleHeads > 0
                    ? `${staleHeads} stale`
                    : anyHeadDegraded
                      ? "degraded"
                      : "all fresh"}
              </span>
            </div>
            <div className="mt-3 text-2xl font-bold tracking-tight">
              {totalHeads === 0 ? "—" : `${freshHeads}/${totalHeads}`}
              <span className="ml-1 text-sm font-normal text-gray-400">fresh pointers</span>
            </div>
          </div>
          <p className="mt-2 text-xs text-gray-400">
            Served heads from <span className="font-mono text-zinc-300">mart.served_head</span>.
          </p>
        </div>

        {/* Card 2: Question Matrix Connectivity */}
        <div className="rounded-xl border border-border bg-card p-4 flex flex-col justify-between">
          <div>
            <div className="flex items-center justify-between text-xs text-gray-400">
              <span className="uppercase tracking-wider font-semibold">Question Connectivity</span>
              <span
                className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium border ${
                  totalDenominatorCells === 0
                    ? "bg-zinc-800 text-zinc-400 border-zinc-700"
                    : notIntegratedQuestionsCount > 0
                      ? "bg-amber-950/60 text-amber-400 border-amber-800"
                      : "bg-emerald-950/60 text-emerald-400 border-emerald-800"
                }`}
              >
                {totalDenominatorCells === 0
                  ? "no report"
                  : `${connectedQuestionsCount}/${totalQuestionsEvaluated} connected`}
              </span>
            </div>
            <div className="mt-3 text-2xl font-bold tracking-tight">
              {matrixConnectivityRatio ? `${matrixConnectivityRatio}%` : "—"}
              <span className="ml-1 text-sm font-normal text-gray-400">
                ({totalAnsweredCells}/{totalDenominatorCells} cells)
              </span>
            </div>
          </div>
          <p className="mt-2 text-xs text-gray-400">
            {notIntegratedQuestionsCount > 0
              ? `${notIntegratedQuestionsCount} questions missing underlying mart column`
              : "All evaluated questions integrated"}
          </p>
        </div>

        {/* Card 3: External Gateway Traffic */}
        <div className="rounded-xl border border-border bg-card p-4 flex flex-col justify-between">
          <div>
            <div className="flex items-center justify-between text-xs text-gray-400">
              <span className="uppercase tracking-wider font-semibold">Gateway Traffic</span>
              <span
                className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium border ${
                  totalFailedCalls > 0
                    ? "bg-amber-950/60 text-amber-400 border-amber-800"
                    : "bg-emerald-950/60 text-emerald-400 border-emerald-800"
                }`}
              >
                {totalCalls === 0 ? "idle" : totalFailedCalls > 0 ? `${totalFailedCalls} failed` : "healthy"}
              </span>
            </div>
            <div className="mt-3 text-2xl font-bold tracking-tight">
              {totalCalls}
              <span className="ml-1 text-sm font-normal text-gray-400">calls today</span>
            </div>
          </div>
          <p className="mt-2 text-xs text-gray-400">
            {totalLandedFetches} landed into <span className="font-mono text-zinc-300">raw.fetches</span>.
          </p>
        </div>

        {/* Card 4: Ingestion & Runs Footprint */}
        <div className="rounded-xl border border-border bg-card p-4 flex flex-col justify-between">
          <div>
            <div className="flex items-center justify-between text-xs text-gray-400">
              <span className="uppercase tracking-wider font-semibold">Capture Footprint</span>
              <span className="inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium bg-zinc-800 text-zinc-300 border border-zinc-700">
                {totalSources} sources
              </span>
            </div>
            <div className="mt-3 text-2xl font-bold tracking-tight">
              {totalFetches24h}
              <span className="ml-1 text-sm font-normal text-gray-400">fetches (24h)</span>
            </div>
          </div>
          <p className="mt-2 text-xs text-gray-400">
            {recentObligations > 0
              ? `${recentResolved}/${recentObligations} recent obligations resolved`
              : "No capture obligations logged"}
          </p>
        </div>
      </div>

      {/* Upgraded Question Coverage Section: Penetrating Physical Matrix */}
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
            No cosmetic green masking: missing columns are classified as <span className="text-rose-400">Not Integrated</span> and
            linked directly to tracking issues.
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
                    className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium border ${
                      isFullConnected
                        ? "bg-emerald-950/60 text-emerald-400 border-emerald-800"
                        : "bg-amber-950/60 text-amber-400 border-amber-800"
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
                      const percent =
                        q.denominator > 0 ? Math.round((q.answered / q.denominator) * 100) : 0;

                      return (
                        <tr key={q.question} className="hover:bg-card/40 transition-colors">
                          {/* Question */}
                          <td className="py-3 pr-4 align-top">
                            <div className="flex items-center gap-2">
                              <span className="font-mono font-bold text-accent">{q.question.toUpperCase()}</span>
                              <span className="inline-flex items-center rounded px-1.5 py-0.5 text-[10px] uppercase font-mono text-zinc-400 bg-zinc-800 border border-zinc-700">
                                {q.scope}
                              </span>
                            </div>
                            <div className="mt-1 text-xs text-gray-300 max-w-xs">{q.text}</div>
                          </td>

                          {/* Physical Status */}
                          <td className="py-3 pr-4 align-top">
                            <span
                              className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ${status.badgeClass}`}
                            >
                              {status.label}
                            </span>
                          </td>

                          {/* Wiring / Mart Column */}
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

                          {/* Coverage Progress */}
                          <td className="py-3 pr-4 align-top min-w-[180px]">
                            {q.denominator > 0 ? (
                              <div className="space-y-1">
                                <div className="flex items-center justify-between text-xs">
                                  <span className="font-mono text-gray-200">
                                    {q.answered}/{q.denominator}
                                  </span>
                                  <span className="text-gray-400 font-mono">{percent}%</span>
                                </div>
                                <div className="flex h-2 w-full overflow-hidden rounded bg-zinc-800">
                                  {q.answered > 0 && (
                                    <div
                                      style={{ width: `${(q.answered / q.denominator) * 100}%` }}
                                      className="bg-emerald-500"
                                      title={`Answered: ${q.answered}`}
                                    />
                                  )}
                                  {q.unavailable_total > 0 && (
                                    <div
                                      style={{ width: `${(q.unavailable_total / q.denominator) * 100}%` }}
                                      className="bg-amber-500"
                                      title={`Unavailable: ${q.unavailable_total}`}
                                    />
                                  )}
                                  {q.missing > 0 && (
                                    <div
                                      style={{ width: `${(q.missing / q.denominator) * 100}%` }}
                                      className="bg-rose-500/80"
                                      title={`Missing: ${q.missing}`}
                                    />
                                  )}
                                </div>
                              </div>
                            ) : (
                              <span className="text-xs text-gray-500">—</span>
                            )}
                          </td>

                          {/* Failure Breakdown & Diagnostics */}
                          <td className="py-3 align-top">
                            {q.denominator === 0 ? (
                              <span className="text-xs text-gray-500">Not applicable to this universe</span>
                            ) : q.answered === q.denominator ? (
                              <span className="text-xs text-emerald-400/90 flex items-center gap-1 font-mono">
                                <span>✓</span>
                                <span>Zero defects</span>
                              </span>
                            ) : (
                              <div className="flex flex-wrap gap-1.5 items-center">
                                {q.unavailable_total > 0 && (
                                  <>
                                    {q.top_reasons ? (
                                      q.top_reasons.split(", ").map((reason, idx) => (
                                        <span
                                          key={idx}
                                          className="inline-flex items-center rounded bg-amber-950/50 px-1.5 py-0.5 text-xs font-mono text-amber-300 border border-amber-800/60"
                                        >
                                          {reason}
                                        </span>
                                      ))
                                    ) : (
                                      <span className="inline-flex items-center rounded bg-amber-950/50 px-1.5 py-0.5 text-xs font-mono text-amber-300 border border-amber-800/60">
                                        unavailable={q.unavailable_total}
                                      </span>
                                    )}
                                  </>
                                )}
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
        <p className="text-sm text-gray-500">
          Every (metric family, subject) cell of the governed head, counted by the independent origins
          that asserted a value, with the mean confidence from 0 to 1 evaluated from what they asserted.
          The stored observation confidence column is a constant and is not read. Agreement is judged only
          over compared cells. A report written before #1517 shows a dash in the origin columns.
        </p>
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
            <table className="mt-3 w-full text-left text-sm">
              <thead>
                <tr className="text-gray-500">
                  <th className="pr-4 font-normal">family</th>
                  <th className="pr-4 font-normal">0 origins</th>
                  <th className="pr-4 font-normal">1 origin</th>
                  <th className="pr-4 font-normal">2 origins</th>
                  <th className="pr-4 font-normal">3+ origins</th>
                  <th className="pr-4 font-normal">mean confidence</th>
                  <th className="pr-4 font-normal">agreement (compared)</th>
                  <th className="font-normal">origins</th>
                </tr>
              </thead>
              <tbody>
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
              </tbody>
            </table>
            <p className="mt-2 text-sm text-gray-400">
              matches the quality report per family:
              {report.quality_report_matches.length === 0 && " —"}
              {report.quality_report_matches.map((match) => (
                <span key={match.family}>
                  {" "}
                  <span className="font-mono">{match.family}</span>{" "}
                  {formatCell(match.matches)}
                  {match.mismatches > 0 ? ` (${match.mismatches} cells)` : ""}
                </span>
              ))}
              {" · "}SEC oracle: {report.oracle_issuers_compared} issuers re-derived
              {report.oracle_fields.map((field) => (
                <span key={field.field}>
                  {" · "}
                  <span className="font-mono">{field.field}</span> {field.agreed}/{field.compared} agree
                </span>
              ))}
              {" · "}stored confidence used for bands: {String(report.stored_confidence_used_for_bands)}
            </p>
          </div>
        ))}
      </div>

      <div className="space-y-3">
        <h2 className="text-lg font-semibold">Governed heads</h2>
        {stats.heads.length === 0 && (
          <p className="rounded-lg border border-border bg-card p-4 text-gray-400">No pointer heads yet.</p>
        )}
        {stats.heads.map((head) => (
          <div key={head.universe_id} className="rounded-lg border border-border bg-card p-4">
            <div className="flex flex-wrap items-baseline gap-3">
              <span className="font-mono text-accent">{head.universe_id}</span>
              <span className="text-sm text-gray-400">seq {head.sequence}</span>
              <span className="text-sm text-gray-500">{head.advanced_at}</span>

              {head.freshness === "fresh" ? (
                <span className="inline-flex items-center rounded-full bg-emerald-950/60 px-2.5 py-0.5 text-xs font-medium text-emerald-400 border border-emerald-800">
                  fresh{head.age_hours !== null ? ` (${head.age_hours.toFixed(1)}h)` : ""}
                </span>
              ) : head.freshness === "manual" ? (
                <span className="inline-flex items-center rounded-full bg-zinc-800 px-2.5 py-0.5 text-xs font-medium text-zinc-300 border border-zinc-700">
                  manual{head.age_hours !== null ? ` (${head.age_hours.toFixed(1)}h)` : ""}
                </span>
              ) : head.freshness === "stale" ? (
                <span className="inline-flex items-center rounded-full bg-rose-950/60 px-2.5 py-0.5 text-xs font-medium text-rose-400 border border-rose-800">
                  stale{(() => {
                    const details = [
                      head.age_hours !== null ? `${head.age_hours.toFixed(1)}h` : null,
                      head.staleness_reason ?? null,
                    ].filter(Boolean).join(" — ");
                    return details ? ` (${details})` : "";
                  })()}
                </span>
              ) : (
                <span className="inline-flex items-center rounded-full bg-gray-800 px-2.5 py-0.5 text-xs font-medium text-gray-400 border border-gray-700">
                  freshness unknown
                </span>
              )}

              {head.is_degraded ? (
                <span className="inline-flex items-center rounded-full bg-amber-950/60 px-2.5 py-0.5 text-xs font-medium text-amber-400 border border-amber-800">
                  degraded {head.degraded_reasons.join(" · ")}
                </span>
              ) : (
                <span className="inline-flex items-center rounded-full bg-emerald-950/30 px-2.5 py-0.5 text-xs font-medium text-emerald-500/80 border border-emerald-900/50">
                  objectives met
                </span>
              )}
            </div>
            <dl className="mt-2 grid grid-cols-2 gap-2 text-sm sm:grid-cols-4">
              <div>
                <dt className="text-gray-500">target run</dt>
                <dd className="font-mono" title={head.target_run_id}>{shortRun(head.target_run_id)}</dd>
              </div>
              <div>
                <dt className="text-gray-500">capture availability</dt>
                <dd className={head.is_avail_degraded ? "text-amber-400 font-medium" : ""}>
                  {formatCell(head.availability)}
                </dd>
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
                    {head.freshness}{head.age_hours !== null ? ` (${head.age_hours.toFixed(1)}h)` : ""}
                  </dd>
                </div>
              </dl>
              {head.factors.length > 0 && (
                <table className="mt-3 w-full text-left text-sm">
                  <thead>
                    <tr className="text-gray-500">
                      <th className="pr-4 font-normal">factor</th>
                      <th className="pr-4 font-normal">complete / universe</th>
                      <th className="font-normal">factor availability</th>
                    </tr>
                  </thead>
                  <tbody>
                    {head.factors.map((factor) => (
                      <tr key={factor.factor_id}>
                        <td className="pr-4 font-mono">{factor.factor_id}</td>
                        <td className="pr-4">
                          {factor.complete_subjects}/{factor.universe_subjects}
                        </td>
                        <td>{factor.ratio}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
              <ListingRatioBlock report={head.listing_ratio} />
          </div>
        ))}
      </div>

      <div className="space-y-3">
        <div>
          <h2 className="text-lg font-semibold">Standing check verdicts (mart.nightly_verdicts)</h2>
          <p className="text-sm text-gray-500">
            One row per check: its current verdict, the newest recorded. Select a check to list its history.{" "}
            In-environment invariants evaluated by Dagster (<span className="font-mono">output_invariants</span> and{" "}
            <span className="font-mono">report_surface_proof</span>) and surfaced through <span className="font-mono">/api/health</span>.
            Re-evaluated automatically within 60s when a governed head advances or a new release boots (#1346).
          </p>
        </div>
        {stats.nightlyVerdicts.length === 0 ? (
          <p className="rounded-lg border border-border bg-card p-4 text-gray-400">
            No standing check verdicts recorded in <span className="font-mono">mart.nightly_verdicts</span> yet.
          </p>
        ) : (
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="text-gray-500">
                <th className="pr-4 font-normal">check</th>
                <th className="pr-4 font-normal">verdict</th>
                <th className="pr-4 font-normal">ran at (UTC)</th>
                <th className="pr-4 font-normal">dagster run</th>
                <th className="font-normal">summary</th>
              </tr>
            </thead>
            <tbody>
              {stats.nightlyVerdicts.map((v) => (
                <tr key={v.check_name} className="border-t border-border">
                  <td className="pr-4 font-mono">
                    <Link
                      href={`/admin/datahub?history=${encodeURIComponent(v.check_name)}#verdict-history`}
                      className="underline decoration-dotted hover:text-accent"
                    >
                      {v.check_name}
                    </Link>
                  </td>
                  <td className={`pr-4 font-medium ${verdictStyle(v.ok)}`}>{verdictLabel(v.ok)}</td>
                  <td className="pr-4 text-gray-400">
                    {v.ran_at}
                  </td>
                  <td className="pr-4 font-mono text-gray-400" title={v.dagster_run_id ?? undefined}>
                    {v.dagster_run_id ? shortRun(v.dagster_run_id) : formatCell(null)}
                  </td>
                  <td className="font-mono text-xs text-gray-300">{v.summary}</td>
                </tr>
              ))}
            </tbody>
          </table>
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
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="text-gray-500">
                  <th className="pr-4 font-normal">verdict</th>
                  <th className="pr-4 font-normal">ran at (UTC)</th>
                  <th className="pr-4 font-normal">dagster run</th>
                  <th className="font-normal">summary</th>
                </tr>
              </thead>
              <tbody>
                {history.map((v) => (
                  <tr key={`${v.ran_at}-${v.dagster_run_id ?? ""}`} className="border-t border-border" data-history-row="true">
                    <td className={`pr-4 font-medium ${verdictStyle(v.ok)}`}>{verdictLabel(v.ok)}</td>
                    <td className="pr-4 text-gray-400">{v.ran_at}</td>
                    <td className="pr-4 font-mono text-gray-400" title={v.dagster_run_id ?? undefined}>
                      {v.dagster_run_id ? shortRun(v.dagster_run_id) : formatCell(null)}
                    </td>
                    <td className="font-mono text-xs text-gray-300">{v.summary}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      <div className="space-y-3">
        <h2 className="text-lg font-semibold">Validation</h2>
        <table className="w-full text-left text-sm">
          <thead>
            <tr className="text-gray-500">
              <th className="pr-4 font-normal">check</th>
              <th className="pr-4 font-normal">verdict</th>
              <th className="font-normal">detail</th>
            </tr>
          </thead>
          <tbody>
            {stats.validation.map((row) => (
              <tr key={row.check} className="border-t border-border">
                <td className="pr-4 font-mono">{row.check}</td>
                <td className={`pr-4 ${row.verdict === "fail" ? "text-amber-400" : ""}`}>{row.verdict}</td>
                <td className="text-gray-400">{row.detail}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="space-y-3">
        <h2 className="text-lg font-semibold">Capacity</h2>
        <table className="w-full text-left text-sm">
          <thead>
            <tr className="text-gray-500">
              <th className="pr-4 font-normal">limit</th>
              <th className="pr-4 font-normal">state</th>
              <th className="font-normal">detail</th>
            </tr>
          </thead>
          <tbody>
            {stats.capacity.map((row) => (
              <tr key={row.check} className="border-t border-border">
                <td className="pr-4 font-mono">{row.check}</td>
                <td className={`pr-4 ${["ok", "pass", "active"].includes(row.verdict) ? "" : "text-amber-400"}`}>
                  {row.verdict}
                </td>
                <td className="text-gray-400">{row.detail}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="space-y-3">
        <h2 className="text-lg font-semibold">Traffic (external calls, today UTC)</h2>
        <p className="text-sm text-gray-500">
          One row per vendor from the external call ledger (#729). A request counts whether it succeeded,
          answered an error status, or raised; <span className="font-mono">landed</span> is how many successful
          answers dereference to bytes in raw.fetches. Failed requests carry the vendor&apos;s own error.
        </p>
        {stats.traffic.length === 0 && (
          <p className="rounded-lg border border-border bg-card p-4 text-gray-400">No external calls recorded today.</p>
        )}
        {stats.traffic.length > 0 && (
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="text-gray-500">
                <th className="pr-4 font-normal">source</th>
                <th className="pr-4 font-normal">calls</th>
                <th className="pr-4 font-normal">failed</th>
                <th className="pr-4 font-normal">landed</th>
                <th className="pr-4 font-normal">avg ms</th>
                <th className="pr-4 font-normal">last call</th>
                <th className="font-normal">last error</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border/40">
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
            </tbody>
          </table>
        )}
      </div>

      <div className="space-y-3">
        <h2 className="text-lg font-semibold">Recent external calls</h2>
        <p className="text-sm text-gray-500">
          The last 40 requests. <span className="font-mono">landed</span> names the raw.fetches row whose bytes this
          answer became; a failed request shows its status and error instead.
        </p>
        {stats.recentCalls.length === 0 && (
          <p className="rounded-lg border border-border bg-card p-4 text-gray-400">
            No ledger rows yet — the first tick after this deploy writes them.
          </p>
        )}
        {stats.recentCalls.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="text-gray-500">
                  <th className="pr-4 font-normal">at</th>
                  <th className="pr-4 font-normal">source</th>
                  <th className="pr-4 font-normal">endpoint</th>
                  <th className="pr-4 font-normal">status</th>
                  <th className="pr-4 font-normal">ms</th>
                  <th className="pr-4 font-normal">landed</th>
                  <th className="pr-4 font-normal">run</th>
                  <th className="font-normal">error</th>
                </tr>
              </thead>
              <tbody>
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
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="space-y-3">
        <h2 className="text-lg font-semibold">Sources</h2>
        <table className="w-full text-left text-sm">
          <thead>
            <tr className="text-gray-500">
              <th className="pr-4 font-normal">source</th>
              <th className="pr-4 font-normal">total</th>
              <th className="pr-4 font-normal">last 24h</th>
              <th className="font-normal">last fetch</th>
            </tr>
          </thead>
          <tbody>
            {stats.sources.map((source) => (
              <tr key={source.source} className="border-t border-border">
                <td className="pr-4 font-mono">{source.source}</td>
                <td className="pr-4">{source.fetches_total}</td>
                <td className="pr-4">{source.fetches_24h}</td>
                <td className="text-gray-400">{source.last_fetch}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="space-y-3">
        <h2 className="text-lg font-semibold">Recent capture runs</h2>
        <table className="w-full text-left text-sm">
          <thead>
            <tr className="text-gray-500">
              <th className="pr-4 font-normal">run</th>
              <th className="pr-4 font-normal">universe</th>
              <th className="pr-4 font-normal">cutoff</th>
              <th className="pr-4 font-normal">resolved</th>
              <th className="font-normal">complete</th>
            </tr>
          </thead>
          <tbody>
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
          </tbody>
        </table>
      </div>
    </section>
  );
}
