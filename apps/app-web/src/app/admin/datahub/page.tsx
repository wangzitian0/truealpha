import Link from "next/link";
import { api } from "@/client/api";
import { formatCell, formatRatio, formatShortSha } from "@/client/format";
import { ReadStateNotice } from "@/components/read-state";
import { TriggerRunButton } from "@/components/trigger-run-button";
import { UNIVERSES } from "@/contracts/environmentContract.generated";
import { loadAuthed } from "../../_load";
import { DataSection, DatahubFactPanels, QuestionCoverageDetails, badgeTone, safeCell } from "./fact-panels";
import { LedgerCoverageTable, type LedgerCoverageRow } from "./ledger-coverage";

export const dynamic = "force-dynamic";
const BASE_METRICS = ["listed_units", "shares_outstanding", "revenue", "gross_profit", "total_assets", "employees_total", "operating_branch", "gross_profit_per_employee", "last_close"];

function formatOracleCell(c: Record<string, unknown> | undefined): string {
  if (!c) return "no comparator";
  const compared = Number(c.compared ?? 0);
  const notComp = Number(c.not_comparable ?? 0);
  if (!Number.isFinite(compared + notComp) || compared + notComp <= 0) return "no comparator";
  const agree = Number(c.agree ?? 0);
  const disagree = Number(c.disagree ?? 0);
  const noServed = Number(c.no_served_value_count ?? 0);
  const bound = safeCell(c.wilson_95_upper_bound ?? "n/a");
  const newest = c.newest_compared_at ? ` · ${String(c.newest_compared_at)}` : "";
  return `${compared} compared (${agree}/${disagree} disagree · nc=${notComp} no_served=${noServed} · wilson_95_upper_bound=${bound}${newest})`;
}

export default async function AdminDatahubPage() {
  const { data: stats, error } = await loadAuthed("/admin/datahub", (opts) => api.getAdminDatahub(opts), { admin: true });
  const coverage = (stats?.ledger_coverage ?? []) as unknown as LedgerCoverageRow[];
  const rawOutcomes = (stats?.oracle_outcomes ?? []) as Record<string, unknown>[];
  const questionCoverage = (stats?.question_coverage ?? []) as Record<string, unknown>[];
  const oracleDisagreements = (stats?.oracle_disagreements ?? []) as Record<string, unknown>[];
  const outputInvariants = (stats?.output_invariants ?? []) as Record<string, unknown>[];
  const capacity = (stats?.capacity ?? []) as Record<string, unknown>[];
  const llmUsage = (stats?.llm_usage ?? []) as Record<string, unknown>[];
  const captureRuns = (stats?.capture_runs ?? []) as Record<string, unknown>[];
  const dbHygiene = (stats?.db_hygiene ?? {}) as Record<string, unknown>;
  const zeroScanCount = Array.isArray(dbHygiene.zero_scan_indexes) ? dbHygiene.zero_scan_indexes.length : 0;
  const idleTablesCount = Array.isArray(dbHygiene.idle_tables) ? dbHygiene.idle_tables.length : 0;

  let qAnswered = 0;
  let qDenom = 0;
  let qNegDelta = 0;
  let qUnaligned = 0;
  for (const row of questionCoverage) {
    if (row.matches_served_head === false) qUnaligned += 1;
    const questions = ((row.payload as Record<string, unknown> | undefined)?.questions ?? {}) as Record<string, Record<string, unknown>>;
    for (const qVal of Object.values(questions)) {
      qAnswered += Number(qVal?.answered ?? 0);
      qDenom += Number(qVal?.denominator ?? 0);
      const chg = Number((qVal?.change as Record<string, unknown> | undefined)?.answered ?? 0);
      if (chg < 0) qNegDelta += 1;
    }
  }
  const totalCompared = rawOutcomes.reduce((s, r) => s + Number(r.compared ?? 0), 0);
  const totalAgree = rawOutcomes.reduce((s, r) => s + Number(r.agree ?? 0), 0);
  const agreePct = totalCompared > 0 ? formatRatio((totalAgree * 100) / totalCompared) : "—";
  const invViolations = outputInvariants.reduce((s, r) => s + Number(r.violation_count ?? 0), 0);
  const invHeld = outputInvariants.filter((r) => Number(r.violation_count ?? 0) === 0).length;
  const maxQuotaPct = capacity.reduce((mx, c) => {
    const val = Number(c.utilization_pct);
    return Number.isFinite(val) && val > mx ? val : mx;
  }, 0);
  const llmCalls = llmUsage.reduce((s, u) => s + Number(u.calls ?? 0), 0);
  const llmErrors = llmUsage.reduce((s, u) => s + Number(u.error_calls ?? 0) + Number(u.refusal_calls ?? 0), 0);
  const hasFailedCapture = captureRuns.some((r) => Number(r.failed ?? 0) > 0);

  const uSet = new Set<string>();
  const extraMetrics = new Set<string>();
  for (const h of rawOutcomes) {
    const uid = String(h.universe_id ?? "");
    const field = String(h.field ?? "");
    if (uid && !uid.includes("canary")) uSet.add(uid);
    if (field && !BASE_METRICS.includes(field)) extraMetrics.add(field);
  }
  if (uSet.size === 0) for (const u of UNIVERSES) if (u.scheduled_somewhere && !u.roles.includes("sentinel")) uSet.add(u.id_prefix);
  const oUniverses = Array.from(uSet).sort();
  const metrics = [...BASE_METRICS, ...Array.from(extraMetrics).sort()];
  const oMap = new Map(rawOutcomes.map((r) => [`${String(r.universe_id ?? "")}\0${String(r.field ?? "")}`, r]));
  const card = "rounded-xl border border-border bg-card p-4";

  return (
    <section aria-labelledby="datahub-heading" className="space-y-8">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 id="datahub-heading" className="text-2xl font-bold tracking-tight">Datahub</h1>
          <p className="mt-1 text-sm text-gray-400">
            Question coverage, oracle outcomes, served heads, invariants, quotas, LLM usage, capture runs, and API ledger.
          </p>
        </div>
        <div className="flex items-center gap-3">
          <Link href="/admin" className="rounded-lg border border-border bg-card px-3 py-2 text-xs text-accent hover:border-accent">
            ← Operations
          </Link>
          <TriggerRunButton />
        </div>
      </div>
      {error && <ReadStateNotice state={{ kind: "error", message: error }} />}

      {stats && (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-6" data-testid="datahub-summary-strip">
          <div className={card}>
            <div className="flex items-center justify-between text-xs text-gray-500">
              <span>Question Coverage</span>
              <span className={badgeTone(qUnaligned > 0 ? "red" : qNegDelta > 0 ? "amber" : "emerald")}>
                {qUnaligned > 0 ? `${qUnaligned} unaligned` : qNegDelta > 0 ? `${qNegDelta} drops` : "aligned"}
              </span>
            </div>
            <div className="mt-2 font-mono text-lg font-semibold">
              {qDenom > 0 ? `${qAnswered}/${qDenom}` : "0/0"}
            </div>
          </div>
          <div className={card}>
            <div className="flex items-center justify-between text-xs text-gray-500">
              <span>Oracle Truth</span>
              <span className={badgeTone(oracleDisagreements.length > 0 ? "red" : "emerald")}>
                {oracleDisagreements.length} disagree
              </span>
            </div>
            <div className="mt-2 font-mono text-lg font-semibold">
              {agreePct}% ({totalCompared})
            </div>
          </div>
          <div className={card}>
            <div className="flex items-center justify-between text-xs text-gray-500">
              <span>Output Invariants</span>
              <span className={badgeTone(invViolations > 0 ? "red" : "emerald")}>
                {invViolations} violations
              </span>
            </div>
            <div className="mt-2 font-mono text-lg font-semibold">
              {invHeld}/{outputInvariants.length} held
            </div>
          </div>
          <div className={card}>
            <div className="flex items-center justify-between text-xs text-gray-500">
              <span>Vendor Quotas</span>
              <span className={badgeTone(maxQuotaPct >= 100 ? "red" : maxQuotaPct >= 70 ? "amber" : "emerald")}>
                {maxQuotaPct >= 100 ? "over" : maxQuotaPct >= 70 ? "high" : "ok"}
              </span>
            </div>
            <div className="mt-2 font-mono text-lg font-semibold">
              {formatCell(formatRatio(maxQuotaPct))}% max
            </div>
          </div>
          <div className={card}>
            <div className="flex items-center justify-between text-xs text-gray-500">
              <span>LLM Usage</span>
              <span className={badgeTone(llmErrors > 0 ? "amber" : "emerald")}>
                {llmErrors} err/ref
              </span>
            </div>
            <div className="mt-2 font-mono text-lg font-semibold">{llmCalls} calls</div>
          </div>
          <div className={card}>
            <div className="flex items-center justify-between text-xs text-gray-500">
              <span>DB Hygiene</span>
              <span className={badgeTone(zeroScanCount > 0 || idleTablesCount > 0 ? "amber" : "emerald")}>
                {idleTablesCount} idle
              </span>
            </div>
            <div className="mt-2 font-mono text-lg font-semibold">{zeroScanCount} 0-scan idx</div>
          </div>
        </div>
      )}

      <DataSection
        title="Question coverage matrix"
        headers={["Universe", "Report run", "Served head", "Aligned", "Cutoff", "Created"]}
        rows={questionCoverage.map((q) => [
          safeCell(q.universe_id),
          formatShortSha((q.report_run_id as string | null | undefined) ?? null),
          formatShortSha((q.served_head_run_id as string | null | undefined) ?? null),
          q.matches_served_head === true ? "yes" : q.matches_served_head === false ? "no" : formatCell(null),
          safeCell(q.cutoff),
          safeCell(q.created_at),
        ])}
      />
      <QuestionCoverageDetails questionCoverage={questionCoverage} />
      <div className="space-y-2">
        <h2 className="text-lg font-semibold">Oracle outcomes (mart.oracle_outcomes)</h2>
        <div className="overflow-x-auto rounded-lg border border-border bg-card p-4">
          <table className="w-full text-left text-sm" data-testid="oracle-outcomes-table">
            <thead className="text-gray-400">
              <tr>
                <th className="py-1 pr-4">metric</th>
                {oUniverses.map((u) => (
                  <th key={u} className="py-1 pr-4 font-mono">{u}</th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-border font-mono text-xs">
              {metrics.map((m) => (
                <tr key={m}>
                  <td className="py-2 pr-4">{m}</td>
                  {oUniverses.map((u) => (
                    <td key={u} className="py-2 pr-4">{formatOracleCell(oMap.get(`${u}\0${m}`))}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {stats && <DatahubFactPanels stats={stats as unknown as Record<string, unknown>} />}

      <details open={hasFailedCapture} className="rounded-xl border border-border bg-card/40 p-4">
        <summary className="cursor-pointer select-none text-sm font-semibold">
          Traffic, Sources &amp; Capture runs (sources: {(stats?.sources ?? []).length}, runs: {captureRuns.length}
          {hasFailedCapture ? " · has failures" : ""})
        </summary>
        <div className="mt-4 space-y-6">
          <DataSection
            title="Traffic (external calls, today UTC)"
            headers={["Source", "Calls", "Failed", "Avg ms", "Last call"]}
            rows={(stats?.traffic ?? []).map((t) => [
              safeCell(t.source),
              String(t.calls ?? 0),
              String(t.failed ?? 0),
              formatCell(t.avg_ms as string | number | null | undefined),
              safeCell(t.last_call),
            ])}
          />
          <div className="space-y-2">
            <h2 className="text-lg font-semibold">Fetch key and run coverage</h2>
            <LedgerCoverageTable rows={coverage} />
          </div>
          <DataSection
            title="Sources"
            headers={["Source", "Total", "24h", "Last fetch"]}
            rows={(stats?.sources ?? []).map((s) => [
              safeCell(s.source),
              String(s.fetches_total ?? 0),
              String(s.fetches_24h ?? 0),
              safeCell(s.last_fetch),
            ])}
          />
          <DataSection
            title="Recent capture runs"
            headers={["Universe", "Cutoff", "Resolved", "Failed", "Complete", "Run ID"]}
            rows={captureRuns.map((r) => [
              safeCell(r.universe_id),
              safeCell(r.cutoff),
              `${String(r.resolved ?? 0)}/${String(r.obligations ?? 0)}`,
              String(r.failed ?? 0),
              r.complete ? "yes" : "no",
              safeCell(r.run_id),
            ])}
          />
        </div>
      </details>
    </section>
  );
}
