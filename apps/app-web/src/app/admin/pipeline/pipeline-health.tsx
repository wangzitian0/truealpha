import type { PipelineOverviewResponse } from "@/client/api";
import { formatCell, formatShortSha } from "@/client/format";
import { ReadStateNotice, type ReadState } from "@/components/read-state";

function fmtDur(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds < 60) return `${seconds}s`;
  const mins = seconds / 60;
  return mins < 60 ? `${mins.toFixed(1)}m` : `${(mins / 60).toFixed(1)}h`;
}

export function PipelineHealth({ outcome }: { outcome: ReadState<PipelineOverviewResponse> }) {
  if (outcome.kind !== "ready") {
    return (
      <section aria-labelledby="pipeline-heading" className="space-y-4">
        <h1 id="pipeline-heading" className="text-2xl font-bold tracking-tight">Pipeline</h1>
        <ReadStateNotice
          state={outcome}
          overrides={{
            denied: "Access denied. This area requires a verified administrator identity.",
            empty: "No finished run in the Dagster history of this database yet.",
            unavailable: "reason" in outcome ? `Run history unavailable (${outcome.reason}).` : "Run history unavailable.",
          }}
        />
      </section>
    );
  }

  const { window_days = 7, jobs = [], job_run_health, release, app_git_sha } = outcome.data;
  if (outcome.data.available === false) {
    return (
      <section aria-labelledby="pipeline-heading" className="space-y-4">
        <h1 id="pipeline-heading" className="text-2xl font-bold tracking-tight">Pipeline</h1>
        <ReadStateNotice
          state={{ kind: "unavailable", reason: outcome.data.reason ?? "unavailable" }}
          overrides={{ unavailable: `Run history unavailable (${outcome.data.reason ?? "unavailable"}).` }}
        />
      </section>
    );
  }

  const verdictLabel = job_run_health ? (job_run_health.ok ? "green" : "red") : "no verdict recorded";
  const ageHours = typeof release?.age_hours === "number" ? release.age_hours : null;
  const ageLabel = ageHours !== null ? `${(ageHours / 24).toFixed(1)}d` : "no build recorded";

  return (
    <section aria-labelledby="pipeline-heading" className="space-y-6">
      <div>
        <h1 id="pipeline-heading" className="text-2xl font-bold tracking-tight">Pipeline</h1>
        <p className="mt-1 text-sm text-gray-400">
          Finished Dagster runs over the last {window_days} days · App {formatShortSha(app_git_sha)}
        </p>
      </div>
      <div className="grid gap-4 sm:grid-cols-2">
        <div className="rounded-xl border border-border p-4">
          <div className="text-xs uppercase text-gray-400">Job run health</div>
          <div className="mt-1 text-lg font-semibold" data-verdict="job_run_health">{verdictLabel}</div>
          {job_run_health?.summary ? <p className="mt-1 text-xs text-gray-400">{String(job_run_health.summary)}</p> : null}
        </div>
        <div className="rounded-xl border border-border p-4">
          <div className="text-xs uppercase text-gray-400">Release age</div>
          <div className="mt-1 text-lg font-semibold" data-release-age="true">{ageLabel}</div>
          {release?.git_sha ? <p className="mt-1 text-xs font-mono text-gray-400">{formatShortSha(String(release.git_sha))}</p> : null}
        </div>
      </div>
      <div className="overflow-x-auto rounded-xl border border-border">
        <table className="w-full text-left text-xs">
          <thead className="border-b border-border uppercase text-gray-400">
            <tr>
              {["Job", "Success rate", "Runs", "Derived bound", "Longest", "Timeouts", "Last success", "Last failure"].map((h) => (
                <th key={h} className="px-3 py-2">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-border font-mono">
            {(jobs ?? []).map((j) => (
              <tr key={j.job} data-job={j.job}>
                <td className="px-3 py-2 text-white">{j.job}</td>
                <td className="px-3 py-2">{j.runs === 0 ? "no run" : j.success_rate !== null && j.success_rate !== undefined ? `${(j.success_rate * 100).toFixed(0)}%` : "—"}</td>
                <td className="px-3 py-2">{j.successes} ok · {j.failures} failed</td>
                <td className="px-3 py-2">{fmtDur(j.derived_bound_seconds)}</td>
                <td className="px-3 py-2">{fmtDur(j.longest_success_seconds)}</td>
                <td className="px-3 py-2">{j.runs_ended_by_bound}</td>
                <td className="px-3 py-2">{formatCell(j.last_success_at)}</td>
                <td className="px-3 py-2 text-rose-300">{j.last_failure ? `${j.last_failure.at}: ${j.last_failure.first_error ?? ""}` : "never"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
