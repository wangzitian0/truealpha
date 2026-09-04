/**
 * #641 D5: the datahub statistics dashboard — governed heads with their quality
 * grades (capture-level AND factor-level availability, #644), source fetch
 * activity, and recent capture runs. The numbers that used to live only in
 * psql sessions, on one page.
 */

import { loadDatahubStats } from "@/server/admin/datahub-stats";
import { getServerPrincipal } from "@/server/auth/request-context";

export const dynamic = "force-dynamic";

function shortRun(id: string): string {
  return id.length > 24 ? `${id.slice(0, 24)}…` : id;
}

export default async function AdminDatahubPage() {
  const principal = await getServerPrincipal();
  if (!principal || principal.principalKind !== "administrator") {
    return <p className="text-amber-400">Access denied.</p>;
  }
  const stats = await loadDatahubStats();

  return (
    <section className="space-y-8">
      <h1 className="text-2xl font-bold tracking-tight">Datahub</h1>
      <p className="text-sm text-gray-500">
        Governed heads, what they serve, and where the bytes came from. Availability is shown twice on
        purpose: the capture-level headline, and the per-factor share of subjects whose required inputs are
        all usable — the number a consumer actually feels.
      </p>

      <div className="space-y-3">
        <h2 className="text-lg font-semibold">Governed heads</h2>
        {stats.heads.length === 0 && (
          <p className="rounded-lg border border-border bg-card p-4 text-gray-400">No pointer heads yet.</p>
        )}
        {stats.heads.map((head) => (
          <div key={head.universe_id} className="rounded-lg border border-border bg-card p-4">
            <div className="flex flex-wrap items-baseline gap-3">
              <span className="font-mono text-accent">{head.universe_id}</span>
              <span className="text-sm text-gray-400">seq {head.sequence}</span>
              <span className="text-sm text-gray-500">{head.advanced_at}</span>
            </div>
            <dl className="mt-2 grid grid-cols-2 gap-2 text-sm sm:grid-cols-4">
              <div>
                <dt className="text-gray-500">target run</dt>
                <dd className="font-mono">{shortRun(head.target_run_id)}</dd>
              </div>
              <div>
                <dt className="text-gray-500">capture availability</dt>
                <dd>{head.availability ?? "—"}</dd>
              </div>
              <div>
                <dt className="text-gray-500">corroborated cells</dt>
                <dd>
                  {head.agreed_cells ?? "—"}/{head.total_cells ?? "—"}
                </dd>
              </div>
            </dl>
            {head.factors.length > 0 && (
              <table className="mt-3 w-full text-left text-sm">
                <thead>
                  <tr className="text-gray-500">
                    <th className="pr-4 font-normal">factor</th>
                    <th className="pr-4 font-normal">complete / universe</th>
                    <th className="font-normal">factor availability</th>
                  </tr>
                </thead>
                <tbody>
                  {head.factors.map((factor) => (
                    <tr key={factor.factor_id}>
                      <td className="pr-4 font-mono">{factor.factor_id}</td>
                      <td className="pr-4">
                        {factor.complete_subjects}/{factor.universe_subjects}
                      </td>
                      <td>{factor.ratio}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        ))}
      </div>

      <div className="space-y-3">
        <h2 className="text-lg font-semibold">Validation</h2>
        <table className="w-full text-left text-sm">
          <thead>
            <tr className="text-gray-500">
              <th className="pr-4 font-normal">check</th>
              <th className="pr-4 font-normal">verdict</th>
              <th className="font-normal">detail</th>
            </tr>
          </thead>
          <tbody>
            {stats.validation.map((row) => (
              <tr key={row.check} className="border-t border-border">
                <td className="pr-4 font-mono">{row.check}</td>
                <td className={`pr-4 ${row.verdict === "fail" ? "text-amber-400" : ""}`}>{row.verdict}</td>
                <td className="text-gray-400">{row.detail}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="space-y-3">
        <h2 className="text-lg font-semibold">Capacity</h2>
        <table className="w-full text-left text-sm">
          <thead>
            <tr className="text-gray-500">
              <th className="pr-4 font-normal">limit</th>
              <th className="pr-4 font-normal">state</th>
              <th className="font-normal">detail</th>
            </tr>
          </thead>
          <tbody>
            {stats.capacity.map((row) => (
              <tr key={row.check} className="border-t border-border">
                <td className="pr-4 font-mono">{row.check}</td>
                <td className={`pr-4 ${["ok", "pass", "active"].includes(row.verdict) ? "" : "text-amber-400"}`}>
                  {row.verdict}
                </td>
                <td className="text-gray-400">{row.detail}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="space-y-3">
        <h2 className="text-lg font-semibold">Traffic (external calls, today UTC)</h2>
        <p className="text-sm text-gray-500">
          One row per vendor from the external call ledger (#729). A request counts whether it succeeded,
          answered an error status, or raised; <span className="font-mono">landed</span> is how many successful
          answers dereference to bytes in raw.fetches. Failed requests carry the vendor&apos;s own error.
        </p>
        {stats.traffic.length === 0 && (
          <p className="rounded-lg border border-border bg-card p-4 text-gray-400">No external calls recorded today.</p>
        )}
        {stats.traffic.length > 0 && (
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="text-gray-500">
                <th className="pr-4 font-normal">source</th>
                <th className="pr-4 font-normal">calls</th>
                <th className="pr-4 font-normal">failed</th>
                <th className="pr-4 font-normal">landed</th>
                <th className="pr-4 font-normal">avg ms</th>
                <th className="pr-4 font-normal">last call</th>
                <th className="font-normal">last error</th>
              </tr>
            </thead>
            <tbody>
              {stats.traffic.map((row) => (
                <tr key={row.source} className="border-t border-border">
                  <td className="pr-4 font-mono">{row.source}</td>
                  <td className="pr-4">{row.calls}</td>
                  <td className={`pr-4 ${row.failed > 0 ? "text-amber-400" : ""}`}>{row.failed}</td>
                  <td className="pr-4">
                    {row.landed}/{row.calls - row.failed}
                  </td>
                  <td className="pr-4">{row.avg_ms ?? "—"}</td>
                  <td className="pr-4 text-gray-400">{row.last_call}</td>
                  <td className="text-gray-400">{row.last_error ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="space-y-3">
        <h2 className="text-lg font-semibold">Recent external calls</h2>
        <p className="text-sm text-gray-500">
          The last 40 requests. <span className="font-mono">landed</span> names the raw.fetches row whose bytes this
          answer became; a failed request shows its status and error instead.
        </p>
        {stats.recentCalls.length === 0 && (
          <p className="rounded-lg border border-border bg-card p-4 text-gray-400">
            No ledger rows yet — the first tick after this deploy writes them.
          </p>
        )}
        {stats.recentCalls.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="text-gray-500">
                  <th className="pr-4 font-normal">at</th>
                  <th className="pr-4 font-normal">source</th>
                  <th className="pr-4 font-normal">endpoint</th>
                  <th className="pr-4 font-normal">status</th>
                  <th className="pr-4 font-normal">ms</th>
                  <th className="pr-4 font-normal">landed</th>
                  <th className="pr-4 font-normal">run</th>
                  <th className="font-normal">error</th>
                </tr>
              </thead>
              <tbody>
                {stats.recentCalls.map((call) => (
                  <tr key={call.id} className="border-t border-border">
                    <td className="pr-4 text-gray-400">{call.called_at}</td>
                    <td className="pr-4 font-mono">{call.source}</td>
                    <td className="pr-4 font-mono">{call.endpoint}</td>
                    <td className={`pr-4 ${call.ok ? "" : "text-amber-400"}`}>
                      {call.ok ? "ok" : "failed"}
                      {call.status_code !== null ? ` ${call.status_code}` : ""}
                    </td>
                    <td className="pr-4">{call.duration_ms ?? "—"}</td>
                    <td className="pr-4 font-mono">
                      {call.landed_fetch_id !== null ? `raw.fetches#${call.landed_fetch_id}` : "—"}
                    </td>
                    <td className="pr-4 font-mono text-gray-400">{call.run_key ? shortRun(call.run_key) : "—"}</td>
                    <td className="text-gray-400">{call.error ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="space-y-3">
        <h2 className="text-lg font-semibold">Sources</h2>
        <table className="w-full text-left text-sm">
          <thead>
            <tr className="text-gray-500">
              <th className="pr-4 font-normal">source</th>
              <th className="pr-4 font-normal">total</th>
              <th className="pr-4 font-normal">last 24h</th>
              <th className="font-normal">last fetch</th>
            </tr>
          </thead>
          <tbody>
            {stats.sources.map((source) => (
              <tr key={source.source} className="border-t border-border">
                <td className="pr-4 font-mono">{source.source}</td>
                <td className="pr-4">{source.fetches_total}</td>
                <td className="pr-4">{source.fetches_24h}</td>
                <td className="text-gray-400">{source.last_fetch}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="space-y-3">
        <h2 className="text-lg font-semibold">Recent capture runs</h2>
        <table className="w-full text-left text-sm">
          <thead>
            <tr className="text-gray-500">
              <th className="pr-4 font-normal">run</th>
              <th className="pr-4 font-normal">universe</th>
              <th className="pr-4 font-normal">cutoff</th>
              <th className="pr-4 font-normal">resolved</th>
              <th className="font-normal">complete</th>
            </tr>
          </thead>
          <tbody>
            {stats.runs.map((run) => (
              <tr key={run.run_id} className="border-t border-border">
                <td className="pr-4 font-mono">{shortRun(run.run_id)}</td>
                <td className="pr-4 font-mono">{run.universe_id}</td>
                <td className="pr-4 text-gray-400">{run.cutoff}</td>
                <td className="pr-4">
                  {run.resolved}/{run.obligations}
                  {run.failed > 0 ? ` (${run.failed} failed)` : ""}
                </td>
                <td>{run.complete ? "yes" : "no"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
