import { formatShortSha } from "@/client/format";
import { formatAgeHours } from "@/client/coerce";
import { BADGE_TONES, SummaryTile } from "@/components/data-table";
import { ReadStateNotice } from "@/components/read-state";
import { ResearchTable } from "@/components/research-table";
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
          Pipeline health at a glance. Schedule: daily 22:15 UTC (after the US close); the button below launches the same job
          with an explicit timestamp. A scheduled tick reuses observations committed in the last 12 hours; tick &ldquo;Force
          a fresh vendor fetch&rdquo; to fetch everything again (recorded as <code>forced_fetch</code>).
        </p>
      </div>

      <dl className="grid grid-cols-2 gap-3 md:grid-cols-3">
        {pointers.length === 0 && <SummaryTile label="Pointer freshness" value="—" foot="no governed head on this database" />}
        {pointers.map((pointer) => {
          const badgeTone =
            pointer.availability === "unavailable" || pointer.freshness === "stale"
              ? BADGE_TONES.crit
              : pointer.freshness === "manual"
                ? BADGE_TONES.muted
                : BADGE_TONES.ok;
          return (
            <SummaryTile
              key={pointer.universeId}
              label={`Pointer · ${pointer.universeId}`}
              badge={
                (pointer.freshness || pointer.availability === "unavailable") && (
                  <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${badgeTone}`}>
                    {pointer.availability === "unavailable" ? "unavailable" : pointer.freshness}
                  </span>
                )
              }
              value={pointer.ageHours != null ? `${formatAgeHours(pointer.ageHours)} ago` : "—"}
              footClass="truncate text-gray-500"
              footTitle={pointer.targetRunId}
              foot={`seq ${pointer.sequence} · ${pointer.targetRunId}${pointer.stalenessReason ? ` · ${pointer.stalenessReason}` : ""}`}
            />
          );
        })}
        <SummaryTile
          label="Data engine build"
          value={dataEngine === null ? "unknown" : formatShortSha(dataEngine.gitSha)}
          valueClass="truncate tabular-nums"
          valueTitle={dataEngine?.imageDigest}
          footClass="truncate text-gray-500"
          footTitle={dataEngine?.runId}
          foot={
            dataEngine === null
              ? "no run has recorded its build yet"
              : `newest run ${formatAgeHours((Date.now() - new Date(dataEngine.createdAt).getTime()) / 3_600_000)} ago · app ${formatShortSha(appGitSha)}`
          }
        />
        {quotaToday.map((entry) => (
          <SummaryTile key={entry.source} label={`${entry.source} · fetches today`} value={entry.fetches} />
        ))}
      </dl>

      <TriggerRunButton />

      <ResearchTable caption="Most recent Dagster runs" headers={["Run", "Job", "Status", "Created", "Duration"]}>
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
      </ResearchTable>
    </section>
  );
}
