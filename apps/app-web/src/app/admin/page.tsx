import Link from "next/link";
import { api } from "@/client/api";
import { formatAgeHours, formatCell, formatShortSha } from "@/client/format";
import { RUNS_EMPTY_MESSAGE, RUNS_UNAVAILABLE_MESSAGE } from "@/client/read-state";
import { ResearchTable } from "@/components/research-table";
import { TriggerRunButton } from "@/components/trigger-run-button";
import { loadAuthed } from "../_load";

export const dynamic = "force-dynamic";

export default async function AdminOpsPage() {
  const { data: ops, error } = await loadAuthed("/admin", (opts) => api.getAdminOps(opts), { admin: true });
  const card = "rounded-xl border border-border bg-card p-4";
  const td = "px-4 py-2";

  return (
    <section aria-labelledby="admin-heading" className="space-y-8">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 id="admin-heading" className="text-2xl font-bold tracking-tight">Operations</h1>
          <p className="mt-1 text-sm text-gray-400">Dagster runs, governed served heads, and daily vendor fetch volume.</p>
        </div>
        <TriggerRunButton />
      </div>
      {error && <p className="text-sm text-rose-400">{error}</p>}
      {ops && (
        <>
          <div className="grid gap-4 md:grid-cols-3">
            <div className={card}><div className="text-xs text-gray-500">App Git SHA</div><div className="mt-1 font-mono text-sm">{formatShortSha(ops.app_git_sha)}</div></div>
            <div className={card}><div className="text-xs text-gray-500">Data Engine SHA</div><div className="mt-1 font-mono text-sm">{formatShortSha(ops.data_engine?.git_sha)}</div></div>
            <div className={card}>
              <div className="text-xs text-gray-500">Datahub surfaces</div>
              <div className="mt-1 flex gap-3 text-xs text-accent">
                <Link href="/admin/datahub" className="hover:underline">DataHub →</Link>
                <Link href="/admin/quality" className="hover:underline">Quality →</Link>
                <Link href="/admin/pipeline" className="hover:underline">Pipeline →</Link>
              </div>
            </div>
          </div>
          <div className="space-y-2">
            <h2 className="text-lg font-semibold">Governed heads</h2>
            <ResearchTable caption="Governed heads" headers={["Universe", "Seq", "Age", "Freshness", "Availability", "Run ID"]}>
              {(ops.pointers ?? []).map((p) => (
                <tr key={p.universe_id} className="border-t border-border font-mono text-xs">
                  <td className={td}>{p.universe_id}</td>
                  <td className={td}>{p.sequence}</td>
                  <td className={td}>{p.age_hours != null ? formatAgeHours(p.age_hours, 48) : "—"}</td>
                  <td className={td}>{formatCell(p.freshness)}</td>
                  <td className={td}>{formatCell(p.availability)}</td>
                  <td className={`${td} text-gray-400`}>{p.target_run_id}</td>
                </tr>
              ))}
            </ResearchTable>
          </div>
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
        </>
      )}
    </section>
  );
}
