import Link from "next/link";
import { api } from "@/client/api";
import { formatCell, formatCompactUtc, formatRatio, formatShortSha } from "@/client/format";
import { RUNS_EMPTY_MESSAGE, RUNS_UNAVAILABLE_MESSAGE } from "@/client/read-state";
import { BacktestTriggerButton } from "@/components/backtest-trigger-button";
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
  if (status === "present") return "border-emerald-500/40 bg-emerald-500/10 text-emerald-300";
  if (status === "absent") return "border-red-500/40 bg-red-500/10 text-red-300";
  return "border-amber-500/40 bg-amber-500/10 text-amber-300";
}

function pillTone(tone: "emerald" | "red" | "amber" | "neutral"): string {
  if (tone === "emerald") return "inline-block whitespace-nowrap rounded-full border border-emerald-500/40 bg-emerald-500/10 px-2 py-0.5 text-emerald-300";
  if (tone === "red") return "inline-block whitespace-nowrap rounded-full border border-red-500/40 bg-red-500/10 px-2 py-0.5 text-red-300";
  if (tone === "amber") return "inline-block whitespace-nowrap rounded-full border border-amber-500/40 bg-amber-500/10 px-2 py-0.5 text-amber-300";
  return "inline-block whitespace-nowrap rounded-full border border-border bg-card px-2 py-0.5 text-gray-300";
}

function verdictLabel(row: Record<string, unknown>): string {
  const state = row.freshness_state;
  if (state === "stale") return "STALE";
  if (state === "missing") return "MISSING";
  if (row.ok === true) return "pass";
  if (row.ok === false) return "fail";
  return "pending";
}

function verdictTone(label: string): "emerald" | "red" | "amber" {
  if (label === "pass") return "emerald";
  if (label === "pending") return "amber";
  return "red";
}

function verdictPriority(row: Record<string, unknown>): number {
  const label = verdictLabel(row);
  if (label === "STALE" || label === "MISSING" || label === "fail") return 0;
  if (label === "pending") return 1;
  return 2;
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

  const sortedVerdicts = [...nightlyVerdicts].sort(
    (a, b) => verdictPriority(a) - verdictPriority(b) || String(a.check_name ?? "").localeCompare(String(b.check_name ?? "")),
  );
  const presentDeps = dependencies.filter((d) => d.status === "present").length;
  const freshHeads = servedHeads.filter((h) => h.freshness === "fresh" && h.availability === "available" && h.run_id).length;
  const passVerdicts = nightlyVerdicts.filter((v) => verdictLabel(v) === "pass").length;
  const badVerdicts = nightlyVerdicts.filter((v) => {
    const l = verdictLabel(v);
    return l === "fail" || l === "STALE" || l === "MISSING";
  }).length;
  const shaMismatch = Boolean(ops?.app_git_sha && ops?.data_engine?.git_sha && ops.app_git_sha !== ops.data_engine.git_sha);
  const pendingTriggers = triggerQueue.filter((t) => !t.consumed_at).length;
  const pendingRollbacks = rollbackRequests.filter((rb) => !rb.consumed_at).length;

  const groupedFailedErrors = Array.from(
    failedJobErrors
      .reduce((acc, row) => {
        const msg = String(row.first_line_error ?? "").trim() || "Unknown error";
        const existing = acc.get(msg);
        if (existing) {
          existing.count += 1;
        } else {
          acc.set(msg, {
            msg,
            latestRunId: String(row.run_id ?? ""),
            latestLoggedAt: String(row.logged_at ?? ""),
            count: 1,
          });
        }
        return acc;
      }, new Map<string, { msg: string; latestRunId: string; latestLoggedAt: string; count: number }>())
      .values(),
  );

  return (
    <section aria-labelledby="admin-heading" className="space-y-8">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 id="admin-heading" className="text-2xl font-bold tracking-tight">Operations</h1>
          <p className="mt-1 text-sm text-gray-400">Dagster runs, dependencies, served heads, nightly verdicts, and manual controls.</p>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <TriggerRunButton />
          <BacktestTriggerButton />
        </div>
      </div>
      {error && <ReadStateNotice state={{ kind: "error", message: error }} />}
      {ops && (
        <>
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5" data-testid="admin-ops-summary">
            <div className={card}>
              <div className="flex items-center justify-between text-xs text-gray-500">
                <span>Release</span>
                <span className={pillTone(shaMismatch ? "amber" : "emerald")}>{shaMismatch ? "sha drift" : "aligned"}</span>
              </div>
              <div className="mt-2 font-mono text-xs text-gray-200">app: {formatShortSha(ops.app_git_sha)}</div>
              <div className="mt-0.5 font-mono text-xs text-gray-400">engine: {formatShortSha(ops.data_engine?.git_sha)}</div>
            </div>
            <div className={card}>
              <div className="flex items-center justify-between text-xs text-gray-500">
                <span>Dependencies</span>
                <span className={pillTone(presentDeps === dependencies.length && dependencies.length > 0 ? "emerald" : "amber")}>
                  {presentDeps}/{dependencies.length}
                </span>
              </div>
              <div className="mt-2 font-mono text-lg font-semibold">
                {presentDeps}/{dependencies.length} present
              </div>
            </div>
            <div className={card}>
              <div className="flex items-center justify-between text-xs text-gray-500">
                <span>Served Heads</span>
                <span className={pillTone(freshHeads === servedHeads.length && servedHeads.length > 0 ? "emerald" : "red")}>
                  {freshHeads}/{servedHeads.length}
                </span>
              </div>
              <div className="mt-2 font-mono text-lg font-semibold">
                {freshHeads}/{servedHeads.length} fresh
              </div>
            </div>
            <div className={card}>
              <div className="flex items-center justify-between text-xs text-gray-500">
                <span>Nightly Verdicts</span>
                <span className={pillTone(badVerdicts > 0 ? "red" : "emerald")}>
                  {badVerdicts > 0 ? `${badVerdicts} red` : "all green"}
                </span>
              </div>
              <div className="mt-2 font-mono text-lg font-semibold">
                {passVerdicts}/{nightlyVerdicts.length} pass
              </div>
            </div>
            <div className={card}>
              <div className="flex items-center justify-between text-xs text-gray-500">
                <span>Failed Jobs</span>
                <span className={pillTone(failedJobErrors.length > 0 ? "red" : "emerald")}>
                  {failedJobErrors.length > 0 ? `${failedJobErrors.length} recent` : "clean"}
                </span>
              </div>
              <div className="mt-2 font-mono text-lg font-semibold">{failedJobErrors.length} errors</div>
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
                {servedHeads.map((h, i) => {
                  const freshOk = h.freshness === "fresh";
                  const availOk = h.availability === "available" && Boolean(h.run_id);
                  return (
                    <tr key={`${String(h.universe_id ?? "")}-${String(h.factor_id ?? "")}-${i}`} className="border-t border-border font-mono text-xs">
                      <td className={td}>{scalar(h.universe_id)}</td>
                      <td className={td}>{scalar(h.universe_version)}</td>
                      <td className={td}>{scalar(h.factor_id)}</td>
                      <td className={td}>{scalar(h.sequence)}</td>
                      <td className={td}><span className={pillTone(freshOk ? "emerald" : "amber")}>{scalar(h.freshness)}</span></td>
                      <td className={td}><span className={pillTone(availOk ? "emerald" : "red")}>{scalar(h.availability)}</span></td>
                      <td className={td}>
                        {formatCell(formatRatio(h.age_hours as number | string | null | undefined))} /{" "}
                        {formatCell(formatRatio(h.limit_hours as number | string | null | undefined))}
                      </td>
                      <td className={td} title={String(h.run_id ?? "")}>{formatCell(h.run_id ? formatShortSha(String(h.run_id)) : "withheld")}</td>
                      <td className={td} title={String(h.head_run_id ?? "")}>{formatShortSha(h.head_run_id as string | null | undefined)}</td>
                      <td className={td}>{scalar(h.staleness_reason)}</td>
                    </tr>
                  );
                })}
              </ResearchTable>
            )}
            <HeadRollbackControl heads={servedHeads} />
          </div>

          <div className="space-y-2">
            <h2 className="text-lg font-semibold">Nightly verdicts</h2>
            {sortedVerdicts.length === 0 ? (
              <p className="text-sm text-gray-400">No nightly verdicts recorded yet.</p>
            ) : (
              <ResearchTable
                caption="Nightly verdicts"
                headers={["Check", "Verdict", "Age / Max", "Ran at", "Summary", "Dagster run"]}
              >
                {sortedVerdicts.map((v) => {
                  const label = verdictLabel(v);
                  return (
                    <tr key={String(v.check_name ?? "")} className="border-t border-border font-mono text-xs">
                      <td className={td}>{scalar(v.check_name)}</td>
                      <td className={td}><span className={pillTone(verdictTone(label))}>{label}</span></td>
                      <td className={td}>{ageLimitLabel(v)}</td>
                      <td className={`${td} whitespace-nowrap`}>{formatCompactUtc(v.ran_at as string | null | undefined)}</td>
                      <td className={`${td} font-sans`}>{scalar(v.summary)}</td>
                      <td className={`${td} text-gray-400`} title={String(v.dagster_run_id ?? "")}>{formatShortSha(v.dagster_run_id as string | null | undefined)}</td>
                    </tr>
                  );
                })}
              </ResearchTable>
            )}
          </div>

          {groupedFailedErrors.length > 0 && (
            <div className="space-y-2">
              <h2 className="text-lg font-semibold">
                Failed job errors ({failedJobErrors.length} recent · {groupedFailedErrors.length} unique)
              </h2>
              <ResearchTable caption="Failed job errors" headers={["Count", "Latest run", "Latest logged", "First-line error"]}>
                {groupedFailedErrors.map((g) => (
                  <tr key={`${g.latestRunId}-${g.msg.slice(0, 32)}`} className="border-t border-border font-mono text-xs">
                    <td className={td}><span className={pillTone(g.count > 1 ? "red" : "amber")}>×{g.count}</span></td>
                    <td className={td} title={g.latestRunId}>{formatShortSha(g.latestRunId)}</td>
                    <td className={`${td} whitespace-nowrap`}>{formatCompactUtc(g.latestLoggedAt)}</td>
                    <td className={`${td} text-red-300`}>{g.msg}</td>
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
                    <td className={td}><span className={pillTone(r.status === "SUCCESS" ? "emerald" : r.status === "FAILURE" ? "red" : "amber")}>{r.status}</span></td>
                    <td className={`${td} whitespace-nowrap`}>{formatCompactUtc(r.created_at)}</td>
                    <td className={td}>{formatCell(r.duration_seconds)}</td>
                    <td className={`${td} text-gray-400`} title={r.run_id}>{formatShortSha(r.run_id)}</td>
                  </tr>
                ))}
              </ResearchTable>
            )}
          </div>

          {(triggerQueue.length > 0 || rollbackRequests.length > 0) && (
            <details open={pendingTriggers > 0 || pendingRollbacks > 0} className="rounded-xl border border-border bg-card/40 p-4">
              <summary className="cursor-pointer select-none text-sm font-semibold">
                Queues (triggers: {triggerQueue.length}, rollbacks: {rollbackRequests.length}
                {pendingTriggers + pendingRollbacks > 0 ? ` · ${pendingTriggers + pendingRollbacks} pending` : ""})
              </summary>
              <div className="mt-4 grid gap-6 lg:grid-cols-2">
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
            </details>
          )}
        </>
      )}
    </section>
  );
}
