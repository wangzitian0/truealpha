/**
 * #495 (surface 2b): the Operate overview — "is the pipeline healthy today":
 * recent runs, pointer freshness, per-source quota burn, and the manual
 * trigger. The /admin layout already gates the administrator; the loader
 * re-checks anyway (re-authorization per request, house style).
 */

import { TriggerRunButton } from "@/components/trigger-run-button";
import { loadOpsOverview } from "@/server/admin/ops";
import { getServerPrincipal } from "@/server/auth/request-context";

export const dynamic = "force-dynamic";

function hoursSince(iso: string): string {
  const hours = (Date.now() - new Date(iso).getTime()) / 3_600_000;
  return hours < 1 ? `${Math.round(hours * 60)}m` : `${hours.toFixed(1)}h`;
}

export default async function AdminOverviewPage() {
  const principal = await getServerPrincipal();
  const outcome = await loadOpsOverview(
    principal === null ? null : { principalKind: principal.principalKind },
  );

  if (outcome.kind !== "ready") {
    return (
      <section aria-labelledby="ops-heading">
        <h1 id="ops-heading" className="text-2xl font-bold tracking-tight">
          Operate
        </h1>
        <p role="status" className="mt-4 rounded-lg border border-border bg-card p-4 text-amber-400">
          {outcome.kind === "denied"
            ? "Access denied. This area requires a verified administrator identity."
            : `Error loading the ops overview: ${outcome.message}`}
        </p>
      </section>
    );
  }

  const { runs, pointer, quotaToday } = outcome.data;

  return (
    <section aria-labelledby="ops-heading" className="space-y-8">
      <div>
        <h1 id="ops-heading" className="text-2xl font-bold tracking-tight">
          Operate
        </h1>
        <p className="mt-2 text-sm text-gray-400">
          Pipeline health at a glance. Schedule: daily 22:15 UTC (after the US close); the button
          below launches the same job with an explicit timestamp.
        </p>
      </div>

      <dl className="grid grid-cols-2 gap-3 md:grid-cols-3">
        <div className="rounded-lg border border-border bg-card px-4 py-3">
          <dt className="text-xs uppercase tracking-wide text-gray-500">Pointer freshness</dt>
          <dd className="mt-1 text-lg font-semibold tabular-nums">
            {pointer === null ? "—" : `${hoursSince(pointer.advancedAt)} ago`}
          </dd>
          {pointer !== null && (
            <p className="mt-1 truncate text-xs text-gray-500" title={pointer.targetRunId}>
              seq {pointer.sequence} · {pointer.targetRunId}
            </p>
          )}
        </div>
        {quotaToday.map((entry) => (
          <div key={entry.source} className="rounded-lg border border-border bg-card px-4 py-3">
            <dt className="text-xs uppercase tracking-wide text-gray-500">
              {entry.source} · fetches today
            </dt>
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
              <th scope="col" className="px-4 py-3">Run</th>
              <th scope="col" className="px-4 py-3">Job</th>
              <th scope="col" className="px-4 py-3">Status</th>
              <th scope="col" className="px-4 py-3">Created</th>
              <th scope="col" className="px-4 py-3">Duration</th>
            </tr>
          </thead>
          <tbody>
            {runs === "unavailable" ? (
              <tr>
                <td colSpan={5} className="px-4 py-3 text-gray-400">
                  Run history unavailable — Dagster has not bootstrapped its tables on this
                  database.
                </td>
              </tr>
            ) : (
              runs.map((run) => (
                <tr key={run.runId} className="border-t border-border">
                  <td className="px-4 py-3 font-mono text-xs">{run.runId.slice(0, 12)}…</td>
                  <td className="px-4 py-3">{run.jobName}</td>
                  <td className="px-4 py-3">{run.status}</td>
                  <td className="px-4 py-3 tabular-nums">{run.createdAt}</td>
                  <td className="px-4 py-3 tabular-nums">
                    {run.durationSeconds === null ? "—" : `${run.durationSeconds}s`}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}
