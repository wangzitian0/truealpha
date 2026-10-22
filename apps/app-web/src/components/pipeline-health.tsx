/**
 * #1466: the /admin/pipeline view. A pure function of the loader outcome and the clock, so a test renders
 * every state, and the route walk (e2e/walk-tree.mjs) is held to the markers and sentences that this file
 * prints (tests/admin-pipeline-page.test.ts).
 */

import { formatShortSha } from "@/client/format";
import { ReadStateNotice } from "@/components/read-state";
import type { PipelineOutcome, PipelineVerdict } from "@/server/admin/pipeline";

function formatAge(hours: number): string {
  if (hours < 1) return `${Math.round(hours * 60)}m`;
  return hours < 48 ? `${hours.toFixed(1)}h` : `${(hours / 24).toFixed(1)}d`;
}

function ago(iso: string, now: number): string {
  return `${formatAge((now - new Date(iso).getTime()) / 3_600_000)} ago`;
}

function rate(value: number | null): string {
  return value === null ? "no run" : `${Math.round(value * 100)}%`;
}

function verdictLabel(verdict: PipelineVerdict | null): "ok" | "red" | "pending" | "none" {
  if (verdict === null) return "none";
  if (verdict.ok === null) return "pending";
  return verdict.ok ? "ok" : "red";
}

const VERDICT_STYLE = {
  ok: "text-emerald-400",
  red: "text-rose-400",
  pending: "text-gray-400",
  none: "text-gray-400",
} as const;

function VerdictCard({ title, check, verdict }: { title: string; check: string; verdict: PipelineVerdict | null }) {
  const label = verdictLabel(verdict);
  return (
    <div className="rounded-lg border border-border bg-card px-4 py-3">
      <dt className="text-xs uppercase tracking-wide text-gray-500">{title}</dt>
      <dd className={`mt-1 text-lg font-semibold ${VERDICT_STYLE[label]}`} data-verdict={check}>
        {label === "none" ? "no verdict recorded" : label}
      </dd>
      <p className="mt-1 break-words font-mono text-xs text-gray-400">
        {verdict === null ? "—" : `${verdict.ranAt} · ${verdict.summary}`}
      </p>
    </div>
  );
}

export function PipelineHealth({ outcome, now }: { outcome: PipelineOutcome; now: number }) {
  if (outcome.kind !== "ready") {
    return (
      <section aria-labelledby="pipeline-heading" className="space-y-4">
        <h1 id="pipeline-heading" className="text-2xl font-bold tracking-tight">
          Pipeline
        </h1>
        <ReadStateNotice
          state={outcome}
          overrides={{
            denied: "Access denied. This area requires a verified administrator identity.",
            empty: "No finished run in the Dagster history of this database yet.",
            error: outcome.kind === "error" ? `Error loading the pipeline health: ${outcome.message}` : undefined,
          }}
        />
      </section>
    );
  }

  const { windowDays, jobs, jobRunHealth, runtimeShape, release, appGitSha } = outcome.data;

  return (
    <section aria-labelledby="pipeline-heading" className="space-y-6">
      <div>
        <h1 id="pipeline-heading" className="text-2xl font-bold tracking-tight">
          Pipeline
        </h1>
        <p className="mt-2 text-sm text-gray-500">
          Finished runs (success or failure) of every Dagster job in the last {windowDays} days, from{" "}
          <span className="font-mono">dagster.runs</span>. The nightly verdict{" "}
          <span className="font-mono">job_run_health</span> judges the same window: red when the last two scheduled
          runs of a job failed, or when its failure rate is above that of the other jobs. This page shows the counts and
          the error line; it does not judge.
        </p>
      </div>

      <dl className="grid grid-cols-1 gap-3 md:grid-cols-3">
        <VerdictCard title="Verdict · job run health" check="job_run_health" verdict={jobRunHealth} />
        <VerdictCard title="Verdict · runtime shape (memory)" check="runtime_shape" verdict={runtimeShape} />
        <div className="rounded-lg border border-border bg-card px-4 py-3">
          <dt className="text-xs uppercase tracking-wide text-gray-500">Release</dt>
          <dd className="mt-1 text-lg font-semibold tabular-nums" data-release-age="true">
            {release === null ? "unknown" : formatAge(release.ageHours)}
          </dd>
          <p className="mt-1 break-words text-xs text-gray-500">
            {release === null
              ? "no run has recorded its build yet"
              : `data engine ${formatShortSha(release.gitSha)} first ran ${release.since} · app ${formatShortSha(appGitSha)}`}
          </p>
        </div>
      </dl>

      <div className="overflow-x-auto rounded-xl border border-border">
        <table className="w-full text-left text-sm">
          <caption className="sr-only">Run health per Dagster job, last {windowDays} days</caption>
          <thead className="bg-card text-xs uppercase text-gray-500">
            <tr>
              <th scope="col" className="px-4 py-3">Job</th>
              <th scope="col" className="px-4 py-3">Success rate</th>
              <th scope="col" className="px-4 py-3">Runs</th>
              <th scope="col" className="px-4 py-3">Last success</th>
              <th scope="col" className="px-4 py-3">Last failure</th>
              <th scope="col" className="px-4 py-3">First error line of the last failure</th>
            </tr>
          </thead>
          <tbody>
            {jobs.map((job) => (
              <tr key={job.job} className="border-t border-border/60" data-job={job.job}>
                <td className="px-4 py-2 font-mono">{job.job}</td>
                <td className={`px-4 py-2 tabular-nums ${job.failures > 0 ? "text-amber-400" : ""}`}>
                  {rate(job.successRate)}
                </td>
                <td className="px-4 py-2 tabular-nums">
                  {job.successes} ok · {job.failures} failed
                </td>
                <td className="px-4 py-2 text-gray-400" title={job.lastSuccessAt ?? undefined}>
                  {job.lastSuccessAt === null ? "never" : ago(job.lastSuccessAt, now)}
                </td>
                <td className="px-4 py-2 text-gray-400" title={job.lastFailure?.at}>
                  {job.lastFailure === null ? "never" : ago(job.lastFailure.at, now)}
                </td>
                <td className="px-4 py-2 font-mono text-xs text-gray-300" title={job.lastFailure?.runId}>
                  {job.lastFailure === null ? "—" : (job.lastFailure.firstError ?? "no error text in the event log")}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
