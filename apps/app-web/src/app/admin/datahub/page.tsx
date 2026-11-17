import type { ReactNode } from "react";
import Link from "next/link";
import { formatCell } from "@/client/format";
import { BADGE_TONES, Badge, MappedTable, MetricCard, Section, type TableCell } from "@/components/data-table";
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

const verdictLabel = (ok: boolean | null) => (ok === null ? "pending" : ok ? "ok" : "fail");
const verdictStyle = (ok: boolean | null) => (ok === null ? "text-gray-400" : ok ? "text-emerald-400" : "text-rose-400");
const shortRun = (id: string) => (id.length > 24 ? `${id.slice(0, 24)}…` : id);
const amberIf = (content: ReactNode, cond: boolean): TableCell => (cond ? { content, className: "text-amber-400" } : content);

function getPhysicalStatus(q: QuestionCoverageQuestion): { label: string; cls: string } {
  if (q.denominator === 0) return { label: "N/A (out of scope)", cls: BADGE_TONES.muted };
  if (!q.column || q.missing === q.denominator) return { label: "Not Integrated", cls: "bg-rose-950/60 text-rose-300 border border-rose-800" };
  if (q.answered === q.denominator) return { label: "Connected (100%)", cls: BADGE_TONES.ok };
  if (q.answered > 0) return { label: `Partial (${Math.round((q.answered / q.denominator) * 100)}%)`, cls: BADGE_TONES.warn };
  return { label: "Disconnected (0%)", cls: BADGE_TONES.crit };
}

function listingRatioText(cell: ListingRatioCell): string {
  if (cell.outcome === "parsed") return `1 unit = ${cell.listing_units_per_share} ordinary shares`;
  if (cell.outcome === "ordinary") return "ordinary share (1)";
  if (cell.outcome === "not_measured") return "not measured";
  return `unparsed: ${formatCell(cell.reason)}`;
}

function ListingRatioBlock({ report }: { report: ListingRatioReport | null }) {
  if (!report) return null;
  const { counts } = report;
  return (
    <div className="mt-3 text-sm">
      <h3 className="font-semibold">Depositary ratio of the listing (#1519)</h3>
      <p className="text-gray-400">
        {counts.parsed} parsed ratio · {counts.ordinary} ordinary (1) ·{" "}
        <span className={counts.unparsed > 0 ? "text-amber-400 font-medium" : ""}>{counts.unparsed} unparsed</span> · {counts.not_measured} not measured
      </p>
      {report.cells.length > 0 && (
        <details open={counts.unparsed > 0} className="mt-1">
          <summary className="cursor-pointer text-gray-500">{report.cells.length} listings, unparsed first</summary>
          <MappedTable
            headers={["listing", "outcome", "cover title", "filing"]}
            rows={report.cells}
            rowKey={(c) => c.listing_id}
            rowClassName={(c) => (c.outcome === "unparsed" ? "text-amber-400" : "")}
            className="mt-2 w-full text-left"
            colClasses={["font-mono", "", "", "font-mono"]}
            cells={(c) => [
              c.ticker ? `${c.ticker} (${c.listing_id})` : c.listing_id,
              listingRatioText(c),
              formatCell(c.title),
              formatCell(c.accession === null ? null : [c.form, c.accession, c.filed].filter(Boolean).join(" ")),
            ]}
          />
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

function TableSection<T>({ title, id, subtitle, isEmpty, emptyText, overflow, headers, colClasses, rows, rowKey, rowData, cells }: {
  title: ReactNode; id?: string; subtitle?: ReactNode; isEmpty?: boolean; emptyText?: ReactNode; overflow?: boolean;
  headers: string[]; colClasses?: string[]; rows: readonly T[]; rowKey: (item: T) => string | number;
  rowData?: (item: T) => Record<string, string | undefined>; cells: (item: T) => TableCell[];
}) {
  const table = <MappedTable headers={headers} colClasses={colClasses} rows={rows} rowKey={rowKey} rowData={rowData} cells={cells} />;
  return (
    <Section title={title} id={id} subtitle={subtitle} isEmpty={isEmpty} emptyText={emptyText}>
      {overflow ? <div className="overflow-x-auto">{table}</div> : table}
    </Section>
  );
}

const checkTableProps = (headers: [string, string, string], rows: readonly ValidationRow[], okVerdicts: readonly string[]) => ({
  headers,
  rows,
  rowKey: (r: ValidationRow) => r.check,
  colClasses: ["font-mono", "", "text-gray-400"],
  cells: (r: ValidationRow): TableCell[] => [r.check, amberIf(r.verdict, !okVerdicts.includes(r.verdict)), r.detail],
});

function verdictTableProps(rows: readonly NightlyVerdictRow[], showCheckLink?: boolean) {
  const baseHeaders = ["verdict", "ran at (UTC)", "dagster run", "summary"];
  const baseCols = ["font-medium", "text-gray-400", "font-mono text-gray-400", "font-mono text-xs text-gray-300"];
  return {
    headers: showCheckLink ? ["check", ...baseHeaders] : baseHeaders,
    colClasses: showCheckLink ? ["font-mono", ...baseCols] : baseCols,
    rows,
    rowKey: (v: NightlyVerdictRow) => (showCheckLink ? v.check_name : `${v.ran_at}-${v.dagster_run_id ?? ""}`),
    rowData: () => (showCheckLink ? {} : { "data-history-row": "true" }),
    cells: (v: NightlyVerdictRow): TableCell[] => {
      const base: TableCell[] = [
        { content: verdictLabel(v.ok), className: verdictStyle(v.ok) },
        v.ran_at,
        { content: v.dagster_run_id ? shortRun(v.dagster_run_id) : formatCell(null), title: v.dagster_run_id ?? undefined },
        v.summary,
      ];
      return showCheckLink
        ? [<Link key={v.check_name} href={`/admin/datahub?history=${encodeURIComponent(v.check_name)}#verdict-history`} className="underline decoration-dotted hover:text-accent">{v.check_name}</Link>, ...base]
        : base;
    },
  };
}

const statusTag = (text: string, tone: "amber" | "rose") => (
  <span className={`inline-flex items-center rounded bg-${tone}-950/50 px-1.5 py-0.5 text-xs font-mono text-${tone}-300 border border-${tone}-800/60`}>
    {text}
  </span>
);

type OracleCell = Awaited<ReturnType<typeof loadDatahubStats>>["oracleOutcomes"]["rows"][number]["cells"][string];

function OracleCellView({ cell }: { cell: OracleCell }) {
  if (!cell) return <span className="font-mono text-gray-500">no comparator</span>;
  return (
    <div className="space-y-0.5 text-xs">
      <div>
        compared <span className="font-mono">{cell.compared}</span> · agree <span className="font-mono text-emerald-400">{cell.agree}</span> · disagree{" "}
        <span className={cell.disagree > 0 ? "font-mono text-rose-400 font-medium" : "font-mono"}>{cell.disagree}</span> · not_comparable <span className="font-mono">{cell.not_comparable}</span>
        {cell.no_served_value_count > 0 && <> · no_served_value <span className="font-mono text-amber-400 font-medium">{cell.no_served_value_count}</span></>}
      </div>
      <div className="text-gray-400">
        wilson_95_upper_bound <span className="font-mono">{formatCell(cell.wilson_95_upper_bound)}</span> · newest_compared_at <span className="font-mono">{formatCell(cell.newest_compared_at)}</span>
      </div>
    </div>
  );
}

function QuestionRow({ q }: { q: QuestionCoverageQuestion }) {
  const status = getPhysicalStatus(q);
  const percent = q.denominator > 0 ? Math.round((q.answered / q.denominator) * 100) : 0;
  const cols: ReactNode[] = [
    <>
      <div className="flex items-center gap-2">
        <span className="font-mono font-bold text-accent">{q.question.toUpperCase()}</span>
        <span className="inline-flex items-center rounded px-1.5 py-0.5 text-[10px] uppercase font-mono text-zinc-400 bg-zinc-800 border border-zinc-700">{q.scope}</span>
      </div>
      <div className="mt-1 text-xs text-gray-300 max-w-xs">{q.text}</div>
    </>,
    <Badge className={status.cls}>{status.label}</Badge>,
    q.column ? (
      <code className="text-xs font-mono text-emerald-400 bg-emerald-950/30 px-1.5 py-0.5 rounded border border-emerald-900/50">{q.column}</code>
    ) : (
      <div className="space-y-1">
        <span className="text-xs text-rose-400 font-mono">No column</span>
        <div>{renderTrackingIssue(q.tracking_issue)}</div>
      </div>
    ),
    q.denominator > 0 ? (
      <div className="space-y-1">
        <div className="flex items-center justify-between text-xs font-mono">
          <span className="text-gray-200">{q.answered}/{q.denominator}</span>
          <span className="text-gray-400">{percent}%</span>
        </div>
        <div className="flex h-2 w-full overflow-hidden rounded bg-zinc-800">
          {([[q.answered, "bg-emerald-500", "Answered"], [q.unavailable_total, "bg-amber-500", "Unavailable"], [q.missing, "bg-rose-500/80", "Missing"]] as const).map(
            ([count, cls, label]) => count > 0 ? <div key={label} style={{ width: `${(count / q.denominator) * 100}%` }} className={cls} title={`${label}: ${count}`} /> : null,
          )}
        </div>
      </div>
    ) : <span className="text-xs text-gray-500">{formatCell(null)}</span>,
    q.denominator === 0 ? (
      <span className="text-xs text-gray-500">Not applicable to this universe</span>
    ) : q.answered === q.denominator ? (
      <span className="text-xs text-emerald-400/90 flex items-center gap-1 font-mono">✓ Zero defects</span>
    ) : (
      <div className="flex flex-wrap gap-1.5 items-center">
        {q.unavailable_total > 0 && (q.top_reasons ? q.top_reasons.split(", ") : [`unavailable=${q.unavailable_total}`]).map((reason, idx) => <span key={idx}>{statusTag(reason, "amber")}</span>)}
        {q.missing > 0 && statusTag(`missing=${q.missing}`, "rose")}
      </div>
    ),
  ];
  return (
    <tr className="hover:bg-card/40 transition-colors">
      {cols.map((content, idx) => (
        <td key={idx} className={`py-3 align-top${idx < 4 ? " pr-4" : ""}${idx === 3 ? " min-w-[180px]" : ""}`}>{content}</td>
      ))}
    </tr>
  );
}

const QUESTION_HEADERS = ["Question", "Physical Status", "Wiring / Mart Column", "Coverage Progress", "Failure Breakdown & Diagnostics"];
const FRESHNESS_TONES: Record<string, string> = { fresh: BADGE_TONES.ok, manual: BADGE_TONES.muted, stale: BADGE_TONES.crit };

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

  let totalQuestions = 0, totalAnswered = 0, totalDenom = 0, connectedCount = 0, notIntegratedCount = 0;
  for (const report of stats.questionCoverage) {
    for (const q of report.questions) {
      if (q.denominator <= 0) continue;
      totalQuestions++; totalAnswered += q.answered; totalDenom += q.denominator;
      if (q.answered === q.denominator) connectedCount++;
      if (!q.column || q.missing === q.denominator) notIntegratedCount++;
    }
  }
  const connectivityPct = totalDenom > 0 ? ((totalAnswered / totalDenom) * 100).toFixed(1) : null;
  const sumOf = <T,>(arr: readonly T[], fn: (item: T) => number) => arr.reduce((acc, item) => acc + fn(item), 0);
  const totalCalls = sumOf(stats.traffic, (t) => t.calls);
  const totalFailedCalls = sumOf(stats.traffic, (t) => t.failed);
  const totalLanded = sumOf(stats.traffic, (t) => t.landed);
  const totalFetches24h = sumOf(stats.sources, (s) => s.fetches_24h);
  const recentResolved = sumOf(stats.runs, (r) => r.resolved);
  const recentObligations = sumOf(stats.runs, (r) => r.obligations);

  return (
    <section className="space-y-8">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Datahub</h1>
        <p className="mt-1 text-sm text-gray-500">
          Governed heads, what they serve, and where the bytes came from, including capture-level and per-factor availability.
        </p>
      </div>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <MetricCard
          title="Governed Heads"
          badge={totalHeads === 0 ? "empty" : staleHeads > 0 ? `${staleHeads} stale` : anyHeadDegraded ? "degraded" : "all fresh"}
          tone={totalHeads === 0 ? "muted" : anyHeadDegraded ? "warn" : "ok"}
          metric={totalHeads === 0 ? formatCell(null) : `${freshHeads}/${totalHeads}`}
          unit="fresh pointers"
          foot={<>Served heads from <span className="font-mono text-zinc-300">mart.served_head</span>.</>}
        />
        <MetricCard
          title="Question Connectivity"
          badge={totalDenom === 0 ? "no report" : `${connectedCount}/${totalQuestions} connected`}
          tone={totalDenom === 0 ? "muted" : notIntegratedCount > 0 ? "warn" : "ok"}
          metric={connectivityPct ? `${connectivityPct}%` : formatCell(null)}
          unit={`(${totalAnswered}/${totalDenom} cells)`}
          foot={notIntegratedCount > 0 ? `${notIntegratedCount} questions missing underlying mart column` : "All evaluated questions integrated"}
        />
        <MetricCard
          title="Gateway Traffic"
          badge={totalCalls === 0 ? "idle" : totalFailedCalls > 0 ? `${totalFailedCalls} failed` : "healthy"}
          tone={totalFailedCalls > 0 ? "warn" : "ok"}
          metric={totalCalls}
          unit="calls today"
          foot={<>{totalLanded} landed into <span className="font-mono text-zinc-300">raw.fetches</span>.</>}
        />
        <MetricCard
          title="Capture Footprint"
          badge={`${stats.sources.length} sources`}
          tone="muted"
          metric={totalFetches24h}
          unit="fetches (24h)"
          foot={recentObligations > 0 ? `${recentResolved}/${recentObligations} recent obligations resolved` : "No capture obligations logged"}
        />
      </div>

      <Section
        title="Question coverage matrix (init.md §0)"
        badge={<span className="rounded bg-accent/20 px-2 py-0.5 text-xs font-medium text-accent">Penetrating Physical Reality</span>}
        subtitle="Expected from the question registry, observed from the governed head's status dimensions."
        isEmpty={stats.questionCoverage.length === 0}
        emptyText="No coverage report yet."
      >
        {stats.questionCoverage.map((report) => {
          const answered = sumOf(report.questions, (q) => q.answered);
          const denom = sumOf(report.questions, (q) => q.denominator);
          const ratio = denom > 0 ? (answered / denom) * 100 : 0;
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
                  <Badge tone={denom > 0 && answered === denom ? "ok" : "warn"}>{answered}/{denom} cells ({ratio.toFixed(1)}%)</Badge>
                </div>
              </div>
              <div className="overflow-x-auto">
                <table className="w-full text-left text-sm">
                  <thead>
                    <tr className="text-gray-400 border-b border-border/60 text-xs uppercase tracking-wider">
                      {QUESTION_HEADERS.map((h, i) => (
                        <th key={h} className={`py-2 ${i < QUESTION_HEADERS.length - 1 ? "pr-4 " : ""}font-medium`}>{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border/40">
                    {report.questions.map((q) => <QuestionRow key={q.question} q={q} />)}
                  </tbody>
                </table>
              </div>
            </div>
          );
        })}
      </Section>

      <Section title="Confidence & accuracy (nightly)" isEmpty={stats.confidence.length === 0} emptyText="No confidence report yet.">
        {stats.confidence.map((report) => (
          <div key={report.universe_id} className="rounded-lg border border-border bg-card p-4">
            <div className="flex flex-wrap items-baseline gap-3">
              <span className="font-mono">{report.universe_id}</span>
              <span className="text-gray-400">cutoff {report.cutoff}</span>
              <span className="text-gray-400">generated {report.generated_at}</span>
              <span className="text-gray-400">sources connected: {report.sources_connected.length} ({report.sources_connected.join(", ")})</span>
            </div>
            <MappedTable
              headers={["family", "0 origins", "1 origin", "2 origins", "3+ origins", "mean confidence", "agreement (compared)", "origins"]}
              rows={report.families}
              rowKey={(f) => f.family}
              className="mt-3 w-full text-left text-sm"
              colClasses={["font-mono", "", "", "", "", "", "", "font-mono text-gray-400"]}
              cells={(f) => [
                f.family,
                ...(["none", "one", "two", "three_or_more"] as const).map((k) => formatCell(f.origin_counts?.[k] ?? null)),
                formatCell(f.mean_confidence),
                `${formatCell(f.agreement_rate)} (${f.compared}/${f.cells})`,
                f.origins.join(", "),
              ]}
            />
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
      </Section>

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
                {stats.oracleOutcomes.universes.map((u) => <th key={u} className="py-1 pr-4 font-mono">{u}</th>)}
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {stats.oracleOutcomes.rows.map((row) => (
                <tr key={row.metric}>
                  <td className="py-2 pr-4 font-mono align-top">{row.metric}</td>
                  {stats.oracleOutcomes.universes.map((u) => (
                    <td key={u} className="py-2 pr-4 align-top"><OracleCellView cell={row.cells[u] ?? null} /></td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <Section title="Governed heads" isEmpty={stats.heads.length === 0} emptyText="No pointer heads yet.">
        {stats.heads.map((head) => {
          const ageLabel = head.age_hours !== null ? `${head.age_hours.toFixed(1)}h` : null;
          const staleDetails = [ageLabel, head.staleness_reason ?? null].filter(Boolean).join(" — ");
          const freshnessTone = FRESHNESS_TONES[head.freshness] ?? "bg-gray-800 text-gray-400 border border-gray-700";
          const freshnessLabel =
            head.freshness === "fresh" || head.freshness === "manual"
              ? `${head.freshness}${ageLabel ? ` (${ageLabel})` : ""}`
              : head.freshness === "stale" ? `stale${staleDetails ? ` (${staleDetails})` : ""}` : "freshness unknown";
          return (
            <div key={head.universe_id} className="rounded-lg border border-border bg-card p-4">
              <div className="flex flex-wrap items-baseline gap-3">
                <span className="font-mono text-accent">{head.universe_id}</span>
                <span className="text-sm text-gray-400">seq {head.sequence}</span>
                <span className="text-sm text-gray-500">{head.advanced_at}</span>
                <Badge className={freshnessTone}>{freshnessLabel}</Badge>
                <Badge className={head.is_degraded ? BADGE_TONES.warn : "bg-emerald-950/30 text-emerald-500/80 border border-emerald-900/50"}>
                  {head.is_degraded ? `degraded ${head.degraded_reasons.join(" · ")}` : "objectives met"}
                </Badge>
              </div>
              <dl className="mt-2 grid grid-cols-2 gap-2 text-sm sm:grid-cols-4">
                {(
                  [
                    ["target run", shortRun(head.target_run_id), "font-mono", head.target_run_id],
                    ["capture availability", formatCell(head.availability), head.is_avail_degraded ? "text-amber-400 font-medium" : ""],
                    ["corroborated cells", `${formatCell(head.agreed_cells)}/${formatCell(head.total_cells)}`, head.is_corroboration_degraded ? "text-amber-400 font-medium" : ""],
                    ["freshness status", `${head.freshness}${ageLabel ? ` (${ageLabel})` : ""}`, head.freshness === "stale" ? "text-rose-400 font-medium" : ""],
                  ] as const
                ).map(([dt, dd, cls, title]) => (
                  <div key={dt}>
                    <dt className="text-gray-500">{dt}</dt>
                    <dd className={cls} title={title}>{dd}</dd>
                  </div>
                ))}
              </dl>
              {head.factors.length > 0 && (
                <MappedTable
                  headers={["factor", "complete / universe", "factor availability"]}
                  rows={head.factors}
                  rowKey={(f) => f.factor_id}
                  rowClassName={() => ""}
                  className="mt-3 w-full text-left text-sm"
                  colClasses={["font-mono"]}
                  cells={(f) => [f.factor_id, `${f.complete_subjects}/${f.universe_subjects}`, f.ratio]}
                />
              )}
              <ListingRatioBlock report={head.listing_ratio} />
            </div>
          );
        })}
      </Section>

      <TableSection
        title="Standing check verdicts (mart.nightly_verdicts)"
        subtitle="Current verdict per check, newest recorded. Select a check to list its history."
        isEmpty={stats.nightlyVerdicts.length === 0}
        emptyText={<>No standing check verdicts recorded in <span className="font-mono">mart.nightly_verdicts</span> yet.</>}
        {...verdictTableProps(stats.nightlyVerdicts, true)}
      />

      {historyCheck !== null && (
        <TableSection
          id="verdict-history"
          title={<>History of <span className="font-mono">{historyCheck}</span></>}
          isEmpty={history === null || history.length === 0}
          emptyText={history === null ? "That is not the name of a check, so there is no history to list." : "No verdict is recorded for this check."}
          {...verdictTableProps(history ?? [])}
        />
      )}

      <TableSection title="Validation" {...checkTableProps(["check", "verdict", "detail"], stats.validation, ["pass", "active", "idle", "ok"])} />

      <TableSection
        id="vendor-account-shares"
        title="Vendor account shares (#1520)"
        overflow
        headers={["account / seats", "env share (%)", "rows today / daily share", "peak window / share", "429 / 1302", "evidence.kind & unmeasured", "verdict"]}
        rows={stats.vendorAccounts}
        rowKey={(r) => r.account}
        colClasses={["font-mono", "font-mono text-gray-300", "font-mono", "font-mono", "font-mono", "font-mono text-gray-400"]}
        cells={(r) => [
          <>{r.account} <span className="text-xs text-gray-500">({r.seats.join(", ")}; {r.holder})</span></>,
          <>{r.environment} {r.percent}% <span className="text-xs text-gray-500">({Object.values(r.shares_by_environment).map((s) => `${s.environment}:${s.percent}%`).join(" / ")})</span></>,
          `${r.daily_usage_text}${r.daily_ratio_text !== null ? ` (ratio ${r.daily_ratio_text})` : ""}`,
          r.window_usage_text,
          amberIf(`429=${r.http_429_today} / 1302=${r.code_1302_today}`, r.http_429_today > 0 || r.code_1302_today > 0),
          `${r.evidence_kinds.join(", ")}${r.not_known_limits.length > 0 ? ` (${r.not_known_limits.map((l) => `${l.id}:${l.evidence_kind}`).join(", ")})` : ""}`,
          amberIf(r.verdict, r.verdict !== "ok"),
        ]}
      />

      <TableSection
        id="model-seat-1302"
        title="Model seat 1302 answers per environment and day (#1520)"
        isEmpty={stats.model1302ByDay.length === 0}
        emptyText="No model seat calls recorded in the window."
        headers={["environment", "UTC day", "1302 answers", "HTTP 429", "total calls", "sampled pace"]}
        colClasses={["font-mono", "font-mono", "font-mono", "font-mono", "font-mono", "font-mono text-gray-400"]}
        rows={stats.model1302ByDay}
        rowKey={(r) => `${r.environment}:${r.day}`}
        cells={(r) => [
          r.environment,
          r.day,
          amberIf(r.code_1302, r.code_1302 > 0),
          amberIf(r.http_429, r.http_429 > 0),
          r.total_calls,
          r.calls_per_minute_limit !== null ? `${r.calls_per_minute_limit}/min, ${r.max_concurrent_calls} concurrent` : "unpaced",
        ]}
      />

      <TableSection title="Capacity" {...checkTableProps(["limit", "state", "detail"], stats.capacity, ["ok", "pass", "active"])} />

      <TableSection
        title="Traffic (external calls, today UTC)"
        isEmpty={stats.traffic.length === 0}
        emptyText="No external calls recorded today."
        headers={["source", "calls", "failed", "landed", "avg ms", "last call", "last error"]}
        colClasses={["font-mono", "", "", "", "", "text-gray-400", "text-gray-400"]}
        rows={stats.traffic}
        rowKey={(r) => r.source}
        cells={(r) => [
          r.source,
          r.calls,
          amberIf(r.failed, r.failed > 0),
          r.calls - r.failed > 0 ? `${r.landed}/${r.calls - r.failed}` : formatCell(null),
          formatCell(r.avg_ms),
          r.last_call,
          formatCell(r.last_error),
        ]}
      />

      <TableSection
        title="Recent external calls"
        isEmpty={stats.recentCalls.length === 0}
        emptyText="No ledger rows yet — the first tick after this deploy writes them."
        overflow
        headers={["at", "source", "endpoint", "status", "ms", "landed", "run", "error"]}
        rows={stats.recentCalls}
        rowKey={(c) => c.id}
        colClasses={["text-gray-400", "font-mono", "font-mono", "", "", "font-mono", "font-mono text-gray-400", "text-gray-400"]}
        cells={(c) => [
          c.called_at,
          c.source,
          c.endpoint,
          amberIf(`${c.ok ? "ok" : "failed"}${c.status_code !== null ? ` ${c.status_code}` : ""}`, !c.ok),
          formatCell(c.duration_ms),
          c.landed_fetch_id !== null ? `raw.fetches#${c.landed_fetch_id}` : formatCell(null),
          { content: c.run_key ? shortRun(c.run_key) : formatCell(null), title: c.run_key ?? undefined },
          formatCell(c.error),
        ]}
      />

      <Section id="ledger-coverage" title={`Fetch key and run coverage (call ledger, last ${LEDGER_COVERAGE_DAYS} UTC days)`}>
        <LedgerCoverageTable rows={stats.ledgerCoverage} />
      </Section>

      <TableSection
        title="Sources"
        headers={["source", "total", "last 24h", "last fetch"]}
        colClasses={["font-mono", "", "", "text-gray-400"]}
        rows={stats.sources}
        rowKey={(s) => s.source}
        cells={(s) => [s.source, s.fetches_total, s.fetches_24h, s.last_fetch]}
      />

      <TableSection
        title="Recent capture runs"
        headers={["run", "universe", "cutoff", "resolved", "complete"]}
        colClasses={["font-mono", "font-mono", "text-gray-400"]}
        rows={stats.runs}
        rowKey={(r) => r.run_id}
        cells={(r) => [
          { content: shortRun(r.run_id), title: r.run_id },
          r.universe_id,
          r.cutoff,
          `${r.resolved}/${r.obligations}${r.failed > 0 ? ` (${r.failed} failed)` : ""}`,
          r.complete ? "yes" : "no",
        ]}
      />
    </section>
  );
}
