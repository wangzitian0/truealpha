import { formatShortSha } from "@/client/format";
import { formatAgeHours } from "@/client/coerce";
import { SummaryTile } from "@/components/data-table";
import { ReadStateNotice } from "@/components/read-state";
import { ResearchTable } from "@/components/research-table";
import type { PipelineOutcome, PipelineVerdict } from "@/server/admin/pipeline";

const ago = (iso: string, now: number) => `${formatAgeHours((now - new Date(iso).getTime()) / 3_600_000, 48)} ago`;

function formatDuration(seconds: number | null): string {
  if (seconds === null) return "—";
  if (seconds < 60) return `${seconds}s`;
  const minutes = seconds / 60;
  return minutes < 60 ? `${minutes.toFixed(1)}m` : `${(minutes / 60).toFixed(1)}h`;
}

function verdictLabel(verdict: PipelineVerdict | null): "ok" | "red" | "pending" | "none" {
  if (verdict === null) return "none";
  if (verdict.ok === null) return "pending";
  return verdict.ok ? "ok" : "red";
}

const VERDICT_STYLE = { ok: "text-emerald-400", red: "text-rose-400", pending: "text-gray-400", none: "text-gray-400" } as const;

export function PipelineHealth({ outcome, now }: { outcome: PipelineOutcome; now: number }) {
  if (outcome.kind !== "ready") {
    return (
      <section aria-labelledby="pipeline-heading" className="space-y-4">
        <h1 id="pipeline-heading" className="text-2xl font-bold tracking-tight">Pipeline</h1>
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

  const { windowDays, unreadable, jobs, jobRunHealth, release, appGitSha } = outcome.data;
  const vLabel = verdictLabel(jobRunHealth);

  return (
    <section aria-labelledby="pipeline-heading" className="space-y-6">
      <div>
        <h1 id="pipeline-heading" className="text-2xl font-bold tracking-tight">Pipeline</h1>
        <p className="mt-2 text-sm text-gray-500">
          Finished runs (success or failure) of every Dagster job in the last {windowDays} days, from{" "}
          <span className="font-mono">dagster.runs</span>. The nightly verdict{" "}
          <span className="font-mono">job_run_health</span> judges these runs: red when the last two scheduled runs of a
          job (of the last 30 days) failed and its newest run failed, or when its failure rate over {windowDays} days is
          above that of the other jobs. This page shows the counts and the error line; it does not judge.
        </p>
      </div>

      <dl className="grid grid-cols-1 gap-3 md:grid-cols-2">
        <SummaryTile
          label="Verdict · job run health"
          value={vLabel === "none" ? (unreadable.verdicts !== null ? "not read" : "no verdict recorded") : vLabel}
          valueClass={VERDICT_STYLE[vLabel]}
          valueData={{ "data-verdict": "job_run_health" }}
          footClass="break-words font-mono text-gray-400"
          foot={jobRunHealth === null ? "—" : `${jobRunHealth.ranAt} · ${jobRunHealth.summary}`}
        />
        <SummaryTile
          label="Release"
          value={release === null ? (unreadable.release === null ? "unknown" : "not read") : formatAgeHours(release.ageHours, 48)}
          valueData={{ "data-release-age": "true" }}
          footClass="break-words text-gray-500"
          foot={
            release === null
              ? unreadable.release === null
                ? "no run has recorded its build yet"
                : "see the notice above"
              : `data engine ${formatShortSha(release.gitSha)} since ${release.since} · app ${formatShortSha(appGitSha)}`
          }
        />
      </dl>

      {(
        [
          ["event-log", "The Dagster event log could not be read, so the error lines are missing", unreadable.eventLog],
          ["verdicts", "The verdicts could not be read", unreadable.verdicts],
          ["release", "The run plans could not be read, so the release age is missing", unreadable.release],
        ] as const
      ).map(
        ([part, words, reason]) =>
          reason !== null && (
            <p key={part} role="status" className="rounded-lg border border-amber-500/40 bg-card p-3 text-sm text-amber-300" data-unreadable={part}>
              {words}: {reason}
            </p>
          ),
      )}

      <ResearchTable
        caption={`Run health per Dagster job, last ${windowDays} days`}
        headers={["Job", "Success rate", "Runs", "Derived bound", "Longest success", "Ended by bound", "Last success", "Last failure", "First error line of the last failure"]}
      >
        {jobs.map((job) => (
          <tr key={job.job} className="border-t border-border/60" data-job={job.job}>
            <td className="px-4 py-2 font-mono">{job.job}</td>
            <td className={`px-4 py-2 tabular-nums ${job.failures > 0 ? "text-amber-400" : ""}`}>
              {job.successRate === null ? "no run" : `${Math.round(job.successRate * 100)}%`}
            </td>
            <td className="px-4 py-2 tabular-nums">{job.successes} ok · {job.failures} failed</td>
            <td className="px-4 py-2 font-mono text-xs tabular-nums text-gray-300">
              {job.derivedBoundSeconds === null ? "instance limit (3h)" : formatDuration(job.derivedBoundSeconds)}
            </td>
            <td className="px-4 py-2 font-mono text-xs tabular-nums text-gray-300">{formatDuration(job.longestSuccessSeconds)}</td>
            <td className={`px-4 py-2 tabular-nums ${job.runsEndedByBound > 0 ? "font-semibold text-rose-400" : "text-gray-400"}`}>
              {job.runsEndedByBound}
            </td>
            <td className="px-4 py-2 text-gray-400" title={job.lastSuccessAt ?? undefined}>
              {job.lastSuccessAt === null ? "never" : ago(job.lastSuccessAt, now)}
            </td>
            <td className="px-4 py-2 text-gray-400" title={job.lastFailure?.at}>
              {job.lastFailure === null ? "never" : ago(job.lastFailure.at, now)}
            </td>
            <td className="px-4 py-2 font-mono text-xs text-gray-300" title={job.lastFailure?.runId}>
              {job.lastFailure === null
                ? "—"
                : (job.lastFailure.firstError ?? (unreadable.eventLog === null ? "no error text in the event log" : "—"))}
            </td>
          </tr>
        ))}
      </ResearchTable>
    </section>
  );
}
