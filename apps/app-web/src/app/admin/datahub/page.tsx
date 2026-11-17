import { api } from "@/client/api";
import { formatCell } from "@/client/format";
import { ResearchTable } from "@/components/research-table";
import { TriggerRunButton } from "@/components/trigger-run-button";
import { UNIVERSES } from "@/contracts/environmentContract.generated";
import { loadAuthed } from "../../_load";
import { LedgerCoverageTable, type LedgerCoverageRow } from "./ledger-coverage";

export const dynamic = "force-dynamic";
const METRICS = ["listed_units", "shares_outstanding", "revenue", "gross_profit", "total_assets", "employees_total", "operating_branch", "gross_profit_per_employee", "last_close"];

function DataSection({ title, headers, rows }: { title: string; headers: string[]; rows: (string | number)[][] }) {
  return (
    <div className="space-y-2">
      <h2 className="text-lg font-semibold">{title}</h2>
      <ResearchTable caption={title} headers={headers}>
        {rows.map((cells, i) => <tr key={`${i}-${cells[0] ?? ""}`} className="border-t border-border font-mono text-xs">{cells.map((v, j) => <td key={j} className="px-3 py-2">{String(v)}</td>)}</tr>)}
      </ResearchTable>
    </div>
  );
}

export default async function AdminDatahubPage() {
  const { data: stats, error } = await loadAuthed("/admin/datahub", (opts) => api.getAdminDatahub(opts), { admin: true });
  const coverage = (stats?.ledger_coverage ?? []) as unknown as LedgerCoverageRow[];
  const rawOutcomes = (stats?.oracle_outcomes ?? []) as Record<string, unknown>[];
  const uSet = new Set<string>();
  for (const h of [...(stats?.heads ?? []), ...rawOutcomes]) {
    const uid = String(h.universe_id ?? "");
    if (uid && !uid.includes("canary")) uSet.add(uid);
  }
  if (uSet.size === 0) for (const u of UNIVERSES) if (u.scheduled_somewhere && !u.roles.includes("sentinel")) uSet.add(u.id_prefix);
  const oUniverses = Array.from(uSet).sort();
  const oMap = new Map(rawOutcomes.map((r) => [`${r.universe_id}\0${r.field}`, r]));

  return (
    <section aria-labelledby="datahub-heading" className="space-y-8">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div><h1 id="datahub-heading" className="text-2xl font-bold tracking-tight">Datahub</h1><p className="mt-1 text-sm text-gray-400">Governed heads, oracle outcomes, verdicts, and API ledger.</p></div>
        <TriggerRunButton />
      </div>
      {error && <p className="text-sm text-rose-400">{error}</p>}
      <DataSection title="Governed heads" headers={["Universe", "Freshness", "Availability", "Agreed cells", "Run ID"]} rows={(stats?.heads ?? []).map((h) => [String(h.universe_id), formatCell(h.freshness as string), formatCell(h.availability as string), `${h.agreed_cells ?? 0}/${h.total_cells ?? 0}`, String(h.target_run_id ?? "")])} />
      <DataSection title="Question coverage matrix" headers={["Universe", "Cutoff", "Created"]} rows={(stats?.question_coverage ?? []).map((q) => [String(q.universe_id ?? ""), String(q.cutoff ?? ""), String(q.created_at ?? "")])} />
      <div className="space-y-2">
        <h2 className="text-lg font-semibold">Oracle outcomes (mart.oracle_outcomes)</h2>
        <div className="overflow-x-auto rounded-lg border border-border bg-card p-4">
          <table className="w-full text-left text-sm" data-testid="oracle-outcomes-table">
            <thead className="text-gray-400"><tr><th className="py-1 pr-4">metric</th>{oUniverses.map((u) => <th key={u} className="py-1 pr-4 font-mono">{u}</th>)}</tr></thead>
            <tbody className="divide-y divide-border font-mono text-xs">
              {METRICS.map((m) => (
                <tr key={m}>
                  <td className="py-2 pr-4">{m}</td>
                  {oUniverses.map((u) => { const c = oMap.get(`${u}\0${m}`); return <td key={u} className="py-2 pr-4">{c && Number(c.compared ?? 0) + Number(c.not_comparable ?? 0) > 0 ? `${c.compared ?? 0} compared (wilson_95_upper_bound=${c.wilson_95_upper_bound ?? "n/a"})` : "no comparator"}</td>; })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
      <DataSection title="Standing check verdicts" headers={["Check", "Status", "Ran at", "Summary"]} rows={(stats?.nightly_verdicts ?? []).map((v) => [String(v.check_name), v.ok === true ? "PASS" : v.ok === false ? "FAIL" : "PENDING", String(v.ran_at ?? ""), String(v.summary ?? "")])} />
      <DataSection title="Traffic (external calls, today UTC)" headers={["Source", "Calls", "Failed", "Avg ms", "Last call"]} rows={(stats?.traffic ?? []).map((t) => [String(t.source ?? ""), String(t.calls ?? 0), String(t.failed ?? 0), formatCell(t.avg_ms as string), String(t.last_call ?? "")])} />
      <div className="space-y-2"><h2 className="text-lg font-semibold">Fetch key and run coverage</h2><LedgerCoverageTable rows={coverage} /></div>
      <DataSection title="Sources" headers={["Source", "Total", "24h", "Last fetch"]} rows={(stats?.sources ?? []).map((s) => [String(s.source ?? ""), String(s.fetches_total ?? 0), String(s.fetches_24h ?? 0), String(s.last_fetch ?? "")])} />
      <DataSection title="Recent capture runs" headers={["Universe", "Cutoff", "Resolved", "Failed", "Complete", "Run ID"]} rows={(stats?.runs ?? []).map((r) => [String(r.universe_id ?? ""), String(r.cutoff ?? ""), `${r.resolved ?? 0}/${r.obligations ?? 0}`, String(r.failed ?? 0), r.complete ? "yes" : "no", String(r.run_id ?? "")])} />
    </section>
  );
}
