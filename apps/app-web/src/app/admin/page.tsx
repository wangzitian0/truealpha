import { formatShortSha } from "@/client/format";
import { formatAgeHours } from "@/client/coerce";
import { ReadStateNotice } from "@/components/read-state";
import { TriggerRunButton } from "@/components/trigger-run-button";
import { loadOpsOverview, RUNS_EMPTY_MESSAGE, RUNS_UNAVAILABLE_MESSAGE } from "@/server/admin/ops";
import { getServerPrincipal } from "@/server/auth/request-context";

export const dynamic = "force-dynamic";

export default async function AdminOverviewPage() {
  const principal = await getServerPrincipal();
  const outcome = await loadOpsOverview(principal === null ? null : { principalKind: principal.principalKind });

  if (outcome.kind !== "ready") {
    return (
      <section aria-labelledby="ops-heading">
        <h1 id="ops-heading" className="text-2xl font-bold tracking-tight">Operate</h1>
        <ReadStateNotice
          state={outcome}
          overrides={{
            denied: "Access denied. This area requires a verified administrator identity.",
            error: outcome.kind === "error" ? `Error loading the ops overview: ${outcome.message}` : undefined,
          }}
        />
      </section>
    );
  }

  const { runs, pointers, dataEngine, appGitSha, quotaToday } = outcome.data;

  return (
    <section aria-labelledby="ops-heading" className="space-y-8">
      <div>
        <h1 id="ops-heading" className="text-2xl font-bold tracking-tight">Operate</h1>
        <p className="mt-2 text-sm text-gray-400">
          Pipeline health at a glance. Schedule: daily 22:15 UTC (after the US close); the button
          below launches the same job with an explicit timestamp. A scheduled tick reuses
          observations committed in the last 12 hours; tick &ldquo;Force a fresh vendor fetch&rdquo;
          to fetch everything again (recorded as <code>forced_fetch</code>).
        </p>
      </div>

      <dl className="grid grid-cols-2 gap-3 md:grid-cols-3">
        {pointers.length === 0 && (
          <div className="rounded-lg border border-border bg-card px-4 py-3">
            <dt className="text-xs uppercase tracking-wide text-gray-500">Pointer freshness</dt>
            <dd className="mt-1 text-lg font-semibold tabular-nums">—</dd>
            <p className="mt-1 text-xs text-gray-500">no governed head on this database</p>
          </div>
        )}
        {pointers.map((pointer) => (
          <div key={pointer.universeId} className="rounded-lg border border-border bg-card px-4 py-3">
            <div className="flex items-center justify-between gap-2">
              <dt className="text-xs uppercase tracking-wide text-gray-500">Pointer · {pointer.universeId}</dt>
              {(pointer.freshness || pointer.availability === "unavailable") && (
                <span
                  className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium border ${
                    pointer.availability === "unavailable" || pointer.freshness === "stale"
                      ? "bg-rose-950/60 text-rose-400 border-rose-800"
                      : pointer.freshness === "manual"
                        ? "bg-zinc-800 text-zinc-300 border-zinc-700"
                        : "bg-emerald-950/60 text-emerald-400 border-emerald-800"
                  }`}
                >
                  {pointer.availability === "unavailable" ? "unavailable" : pointer.freshness}
                </span>
              )}
            </div>
            <dd className="mt-1 text-lg font-semibold tabular-nums">
              {pointer.ageHours !== null && pointer.ageHours !== undefined ? `${formatAgeHours(pointer.ageHours)} ago` : "—"}
            </dd>
            <p className="mt-1 truncate text-xs text-gray-500" title={pointer.targetRunId}>
              seq {pointer.sequence} · {pointer.targetRunId}
              {pointer.stalenessReason ? ` · ${pointer.stalenessReason}` : ""}
            </p>
          </div>
        ))}
        <div className="rounded-lg border border-border bg-card px-4 py-3">
          <dt className="text-xs uppercase tracking-wide text-gray-500">Data engine build</dt>
          <dd className="mt-1 truncate text-lg font-semibold tabular-nums" title={dataEngine?.imageDigest}>
            {dataEngine === null ? "unknown" : formatShortSha(dataEngine.gitSha)}
          </dd>
          <p className="mt-1 truncate text-xs text-gray-500" title={dataEngine?.runId}>
            {dataEngine === null
              ? "no run has recorded its build yet"
              : `newest run ${formatAgeHours((Date.now() - new Date(dataEngine.createdAt).getTime()) / 3_600_000)} ago · app ${formatShortSha(appGitSha)}`}
          </p>
        </div>
        {quotaToday.map((entry) => (
          <div key={entry.source} className="rounded-lg border border-border bg-card px-4 py-3">
            <dt className="text-xs uppercase tracking-wide text-gray-500">{entry.source} · fetches today</dt>
            <dd className="mt-1 text-lg font-semibold tabular-nums">{entry.fetches}</dd>
          </div>
        ))}
      </dl>

      <TriggerRunButton />

      <div className="overflow-x-auto rounded-xl border border-border">
        <table className="w-full text-left text-sm">
          <caption className="sr-only">Most recent Dagster runs</caption>
          <thead className="bg-card text-xs uppercase text-gray-500">
            <tr>
              {["Run", "Job", "Status", "Created", "Duration"].map((h) => (
                <th key={h} scope="col" className="px-4 py-3">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {runs === "unavailable" || runs.length === 0 ? (
              <tr>
                <td colSpan={5} className="px-4 py-3 text-gray-400">
                  {runs === "unavailable" ? RUNS_UNAVAILABLE_MESSAGE : RUNS_EMPTY_MESSAGE}
                </td>
              </tr>
            ) : (
              runs.map((run) => (
                <tr key={run.runId} className="border-t border-border">
                  <td className="px-4 py-3 font-mono text-xs" title={run.runId}>{formatShortSha(run.runId)}</td>
                  <td className="px-4 py-3">{run.jobName}</td>
                  <td className="px-4 py-3">{run.status}</td>
                  <td className="px-4 py-3 tabular-nums">{run.createdAt}</td>
                  <td className="px-4 py-3 tabular-nums">{run.durationSeconds === null ? "—" : `${run.durationSeconds}s`}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}
