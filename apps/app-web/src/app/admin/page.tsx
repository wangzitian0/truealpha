import Link from "next/link";
import { api } from "@/client/api";
import { formatCell, formatRatio, formatShortSha } from "@/client/format";
import { RUNS_EMPTY_MESSAGE, RUNS_UNAVAILABLE_MESSAGE } from "@/client/read-state";
import { HeadRollbackControl } from "@/components/head-rollback-control";
import { ReadStateNotice } from "@/components/read-state";
import { ResearchTable } from "@/components/research-table";
import { TriggerRunButton } from "@/components/trigger-run-button";
import { loadAuthed } from "../_load";

export const dynamic = "force-dynamic";

function scalar(v: unknown): string {
  if (v === null || v === undefined || v === "") return formatCell(null);
  if (typeof v === "string" || typeof v === "number" || typeof v === "boolean") return formatCell(v);
  return formatCell(JSON.stringify(v));
}

function depTone(status: string): string {
  if (status === "present") return "border-emerald-500/40 text-emerald-300";
  if (status === "absent") return "border-red-500/40 text-red-300";
  return "border-amber-500/40 text-amber-300";
}

function verdictLabel(row: Record<string, unknown>): string {
  const state = row.freshness_state;
  if (state === "stale") return "STALE";
  if (state === "missing") return "MISSING";
  if (row.ok === true) return "pass";
  if (row.ok === false) return "fail";
  return "pending";
}

function ageLimitLabel(row: Record<string, unknown>): string {
  const age = row.age_hours;
  const maxAge = row.max_age_hours;
  if (typeof age === "number" && typeof maxAge === "number") return `${age.toFixed(1)} / ${maxAge.toFixed(0)}h`;
  if (typeof maxAge === "number") return `— / ${maxAge.toFixed(0)}h`;
  return "—";
}

export default async function AdminOpsPage() {
  const { data: ops, error } = await loadAuthed("/admin", (opts) => api.getAdminOps(opts), { admin: true });
  const card = "rounded-xl border border-border bg-card p-4";
  const td = "px-4 py-2";
  const dependencies = (ops?.dependencies ?? []) as Record<string, unknown>[];
  const servedHeads = (ops?.served_heads ?? []) as Record<string, unknown>[];
  const nightlyVerdicts = (ops?.nightly_verdicts ?? []) as Record<string, unknown>[];
  const failedJobErrors = (ops?.failed_job_errors ?? []) as Record<string, unknown>[];
  const triggerQueue = (ops?.trigger_queue ?? []) as Record<string, unknown>[];
  const rollbackRequests = (ops?.rollback_requests ?? []) as Record<string, unknown>[];

  return (
    <section aria-labelledby="admin-heading" className="space-y-8">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 id="admin-heading" className="text-2xl font-bold tracking-tight">Operations</h1>
          <p className="mt-1 text-sm text-gray-400">Dagster runs, dependencies, served heads, nightly verdicts, and manual controls.</p>
        </div>
        <TriggerRunButton />
      </div>
      {error && <ReadStateNotice state={{ kind: "error", message: error }} />}
      {ops && (
        <>
          <div className="grid gap-4 md:grid-cols-3">
            <div className={card}><div className="text-xs text-gray-500">App Git SHA</div><div className="mt-1 font-mono text-sm">{formatShortSha(ops.app_git_sha)}</div></div>
            <div className={card}><div className="text-xs text-gray-500">Data Engine SHA</div><div className="mt-1 font-mono text-sm">{formatShortSha(ops.data_engine?.git_sha)}</div></div>
            <div className={card}>
              <div className="text-xs text-gray-500">Datahub surface</div>
              <div className="mt-1 flex gap-3 text-xs text-accent">
                <Link href="/admin/datahub" className="hover:underline">DataHub →</Link>
              </div>
            </div>
          </div>

          {dependencies.length > 0 && (
            <div className="space-y-2">
              <h2 className="text-lg font-semibold">Dependencies</h2>
              <div className="grid gap-4 md:grid-cols-3">
                {dependencies.map((d) => {
                  const status = String(d.status ?? "unknown");
                  return (
                    <div key={String(d.name ?? "")} className={card}>
                      <div className="flex items-center justify-between gap-2">
                        <span className="font-mono text-sm font-semibold">{scalar(d.name)}</span>
                        <span className={`rounded-full border px-2 py-0.5 text-xs ${depTone(status)}`}>{status}</span>
                      </div>
                      <div className="mt-1 font-mono text-xs text-gray-400">
                        {d.elapsed_ms !== undefined && d.elapsed_ms !== null ? `${String(d.elapsed_ms)} ms` : formatCell(null)}
                      </div>
                      <div className="mt-1 text-xs text-gray-400">{scalar(d.detail)}</div>
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          <div className="space-y-3">
            <h2 className="text-lg font-semibold">Served heads</h2>
            {servedHeads.length === 0 ? (
              <p className="text-sm text-gray-400">No served heads recorded in mart.served_head yet.</p>
            ) : (
              <ResearchTable
                caption="Served heads"
                headers={["Universe", "Version", "Factor", "Seq", "Freshness", "Availability", "Age / Limit (h)", "Served run", "Head run", "Withhold / Staleness"]}
              >
                {servedHeads.map((h, i) => (
                  <tr key={`${String(h.universe_id ?? "")}-${String(h.factor_id ?? "")}-${i}`} className="border-t border-border font-mono text-xs">
                    <td className={td}>{scalar(h.universe_id)}</td>
                    <td className={td}>{scalar(h.universe_version)}</td>
                    <td className={td}>{scalar(h.factor_id)}</td>
                    <td className={td}>{scalar(h.sequence)}</td>
                    <td className={td}>{scalar(h.freshness)}</td>
                    <td className={td}>{scalar(h.availability)}</td>
                    <td className={td}>
                      {formatCell(formatRatio(h.age_hours as number | string | null | undefined))} /{" "}
                      {formatCell(formatRatio(h.limit_hours as number | string | null | undefined))}
                    </td>
                    <td className={td} title={String(h.run_id ?? "")}>{formatCell(h.run_id ? formatShortSha(String(h.run_id)) : "withheld")}</td>
                    <td className={td} title={String(h.head_run_id ?? "")}>{formatShortSha(h.head_run_id as string | null | undefined)}</td>
                    <td className={td}>{scalar(h.staleness_reason)}</td>
                  </tr>
                ))}
              </ResearchTable>
            )}
            <HeadRollbackControl heads={servedHeads} />
          </div>

          <div className="space-y-2">
            {(() => {
              const cp = (ops as Record<string, unknown> | null)?.construction_progress as Record<string, unknown> | null | undefined;
              if (!cp) return null;
              const universes = Array.isArray(cp.universes) ? (cp.universes as Record<string, unknown>[]) : [];
              const strategyRuns = Array.isArray(cp.strategy_runs) ? (cp.strategy_runs as Record<string, unknown>[]) : [];
              const backtests = Array.isArray(cp.backtest_runs) ? (cp.backtest_runs as Record<string, unknown>[]) : [];
              return (
                <div className="space-y-4">
                  <h2 className="text-lg font-semibold">Construction &amp; 6-Scenario Progress Matrix</h2>
                  <p className="text-xs font-mono text-gray-300">
                    Criteria ({scalar(cp.environment)}): verified {scalar(cp.criteria_verified)}/{scalar(cp.criteria_total)} · partial{" "}
                    {scalar(cp.criteria_partial)} · pending {scalar(cp.criteria_pending)} · failed/stale{" "}
                    {scalar(cp.criteria_failed_or_stale)}
                  </p>
                  {universes.length > 0 && (
                    <ResearchTable
                      caption="6-Scenario Actionable Question Coverage"
                      headers={["Universe", "Scheduled", "Freshness", "Q1–Q6 actionable coverage"]}
                    >
                      {universes.map((u) => {
                        const qs = Array.isArray(u.questions) ? (u.questions as Record<string, unknown>[]) : [];
                        const qSummary =
                          qs.length > 0
                            ? qs
                                .map((q) => {
                                  const pct = q.actionable_coverage_pct ?? "—";
                                  return `${scalar(q.question_id)}:${scalar(q.answered)}/${scalar(q.actionable_denominator)}(${pct}%,gap=${scalar(q.pipeline_gap)})`;
                                })
                                .join(" · ")
                            : "—";
                        return (
                          <tr key={String(u.universe_key ?? "")} className="border-t border-border font-mono text-xs">
                            <td className={td}>{scalar(u.universe_key)}</td>
                            <td className={td}>{u.scheduled ? "yes" : "no"}</td>
                            <td className={td}>{scalar(u.freshness)}</td>
                            <td className={td}>{qSummary}</td>
                          </tr>
                        );
                      })}
                    </ResearchTable>
                  )}
                  {(strategyRuns.length > 0 || backtests.length > 0) && (
                    <ResearchTable
                      caption="Strategy & Backtest Reality"
                      headers={["Kind", "Strategy", "Universe / Run", "Status / Counts", "Executed at"]}
                    >
                      {strategyRuns.map((s) => (
                        <tr key={`sr-${String(s.strategy_run_id ?? "")}`} className="border-t border-border font-mono text-xs">
                          <td className={td}>strategy</td>
                          <td className={td}>{scalar(s.strategy_key)}</td>
                          <td className={td}>{scalar(s.strategy_run_id)}</td>
                          <td className={td}>
                            selected {scalar(s.selected_count)} / eligible {scalar(s.eligible_count)} / total {scalar(s.total_count)}
                          </td>
                          <td className={td}>{scalar(s.executed_at)}</td>
                        </tr>
                      ))}
                      {backtests.map((b) => (
                        <tr
                          key={`bt-${String(b.strategy_key ?? "")}-${String(b.universe_id ?? "")}`}
                          className="border-t border-border font-mono text-xs"
                        >
                          <td className={td}>backtest</td>
                          <td className={td}>{scalar(b.strategy_key)}</td>
                          <td className={td}>{scalar(b.universe_id)}</td>
                          <td className={td}>
                            {scalar(b.status)} · cagr={scalar(b.cagr_monthly)} · sharpe={scalar(b.sharpe_daily)}
                          </td>
                          <td className={td}>{scalar(b.executed_at)}</td>
                        </tr>
                      ))}
                    </ResearchTable>
                  )}
                </div>
              );
            })()}
            <h2 className="text-lg font-semibold">Nightly verdicts</h2>
            {nightlyVerdicts.length === 0 ? (
              <p className="text-sm text-gray-400">No nightly verdicts recorded yet.</p>
            ) : (
              <ResearchTable
                caption="Nightly verdicts"
                headers={["Check", "Verdict", "Age / Max", "Ran at", "Summary", "Dagster run"]}
              >
                {nightlyVerdicts.map((v) => (
                  <tr key={String(v.check_name ?? "")} className="border-t border-border font-mono text-xs">
                    <td className={td}>{scalar(v.check_name)}</td>
                    <td className={td}>{verdictLabel(v)}</td>
                    <td className={td}>{ageLimitLabel(v)}</td>
                    <td className={td}>{scalar(v.ran_at)}</td>
                    <td className={`${td} font-sans`}>{scalar(v.summary)}</td>
                    <td className={`${td} text-gray-400`}>{scalar(v.dagster_run_id)}</td>
                  </tr>
                ))}
              </ResearchTable>
            )}
          </div>

          {failedJobErrors.length > 0 && (
            <div className="space-y-2">
              <h2 className="text-lg font-semibold">Failed job errors</h2>
              <ResearchTable caption="Failed job errors" headers={["Dagster run ID", "Logged at", "First-line error"]}>
                {failedJobErrors.map((e) => (
                  <tr key={String(e.run_id ?? "")} className="border-t border-border font-mono text-xs">
                    <td className={td}>{scalar(e.run_id)}</td>
                    <td className={td}>{scalar(e.logged_at)}</td>
                    <td className={`${td} text-red-300`}>{scalar(e.first_line_error)}</td>
                  </tr>
                ))}
              </ResearchTable>
            </div>
          )}

          <div className="space-y-2">
            <h2 className="text-lg font-semibold">Recent runs</h2>
            {typeof ops.runs === "string" ? <p className="text-sm text-gray-400">{RUNS_UNAVAILABLE_MESSAGE}</p>
              : (ops.runs ?? []).length === 0 ? <p className="text-sm text-gray-400">{RUNS_EMPTY_MESSAGE}</p> : (
              <ResearchTable caption="Recent runs" headers={["Job", "Status", "Created", "Duration (s)", "Run ID"]}>
                {ops.runs.map((r) => (
                  <tr key={r.run_id} className="border-t border-border font-mono text-xs">
                    <td className={td}>{r.job_name}</td>
                    <td className={td}>{r.status}</td>
                    <td className={td}>{r.created_at}</td>
                    <td className={td}>{formatCell(r.duration_seconds)}</td>
                    <td className={`${td} text-gray-400`}>{r.run_id}</td>
                  </tr>
                ))}
              </ResearchTable>
            )}
          </div>

          {(triggerQueue.length > 0 || rollbackRequests.length > 0) && (
            <div className="grid gap-6 lg:grid-cols-2">
              <div className="space-y-2">
                <h2 className="text-lg font-semibold">Trigger queue</h2>
                {triggerQueue.length === 0 ? (
                  <p className="text-sm text-gray-400">No trigger requests queued.</p>
                ) : (
                  <ResearchTable caption="Trigger queue" headers={["ID", "Job", "Force fetch", "Requested at", "Consumed at", "Run key"]}>
                    {triggerQueue.map((t) => (
                      <tr key={String(t.request_id ?? "")} className="border-t border-border font-mono text-xs">
                        <td className={td}>{scalar(t.request_id)}</td>
                        <td className={td}>{scalar(t.job_name)}</td>
                        <td className={td}>{scalar(t.force_fetch)}</td>
                        <td className={td}>{scalar(t.requested_at)}</td>
                        <td className={td}>{t.consumed_at ? scalar(t.consumed_at) : "pending"}</td>
                        <td className={td}>{scalar(t.launched_run_key)}</td>
                      </tr>
                    ))}
                  </ResearchTable>
                )}
              </div>
              <div className="space-y-2">
                <h2 className="text-lg font-semibold">Rollback queue</h2>
                {rollbackRequests.length === 0 ? (
                  <p className="text-sm text-gray-400">No rollback requests queued.</p>
                ) : (
                  <ResearchTable caption="Rollback queue" headers={["ID", "Universe", "Target run", "Reason", "Consumed at", "Pointer"]}>
                    {rollbackRequests.map((rb) => (
                      <tr key={String(rb.request_id ?? "")} className="border-t border-border font-mono text-xs">
                        <td className={td}>{scalar(rb.request_id)}</td>
                        <td className={td}>{scalar(rb.universe_id)}</td>
                        <td className={td}>{formatShortSha(rb.target_run_id as string | null | undefined)}</td>
                        <td className={`${td} font-sans`}>{scalar(rb.reason)}</td>
                        <td className={td}>{rb.consumed_at ? scalar(rb.consumed_at) : "pending"}</td>
                        <td className={td}>{formatShortSha(rb.result_pointer_id as string | null | undefined)}</td>
                      </tr>
                    ))}
                  </ResearchTable>
                )}
              </div>
            </div>
          )}
        </>
      )}
    </section>
  );
}
