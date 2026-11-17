import { api } from "@/client/api";
import { formatCell, formatMacroValue, formatUtcMinute } from "@/client/format";
import { DataTable } from "@/components/data-table";
import { ReadStateNotice } from "@/components/read-state";
import { loadAuthed } from "../../_load";

export const dynamic = "force-dynamic";

export default async function AdminMacroPage() {
  const { data: macro, error } = await loadAuthed("/admin/macro", (opts) => api.getAdminMacro(opts), { admin: true });
  const series = macro?.series ?? [];
  const months = macro?.months ?? [];
  const dates = Array.from(new Set(months.map((m) => String(m.observation_date))));
  const valueOf = new Map(months.map((m) => [`${m.observation_date}|${m.series_id}`, String(m.value)]));

  return (
    <section aria-labelledby="macro-heading" className="space-y-8">
      <div>
        <h1 id="macro-heading" className="text-2xl font-bold tracking-tight">Macro base data</h1>
        <p className="mt-1 text-sm text-gray-400">U.S. Treasury par yield curve freshness and 12-month history.</p>
      </div>
      {error && <ReadStateNotice state={{ kind: "error", message: error }} />}
      {macro && !macro.available && <ReadStateNotice state={{ kind: "unavailable", reason: macro.reason ?? "unavailable" }} />}
      {macro?.available && (
        <>
          <div className="grid gap-4 md:grid-cols-3" data-macro-series="cards">
            {series.map((item) => {
              const latest = item.latest as Record<string, unknown> | null | undefined;
              return (
                <article key={item.series_id} data-series-id={item.series_id} data-fresh={item.is_fresh ? "true" : "false"} className="rounded-xl border border-border bg-card p-5">
                  <h2 className="font-semibold">{item.label}</h2>
                  <p className="text-xs font-mono text-gray-500">{item.series_id}</p>
                  <p className="mt-3 text-3xl font-bold">{formatCell(latest ? formatMacroValue(item.unit, String(latest.value ?? "")) : null)}</p>
                  <p className="mt-1 text-xs text-gray-400">
                    {latest ? `${latest.observation_date} · ${latest.source} · ${formatUtcMinute(String(latest.fetched_at ?? ""))}` : "no observation"}
                  </p>
                </article>
              );
            })}
          </div>
          {dates.length > 0 && (
            <div className="overflow-x-auto rounded-xl border border-border bg-card p-4" data-macro-series="months">
              <DataTable headers={["Date", ...series.map((s) => s.series_id)]} className="w-full text-left text-xs font-mono">
                {dates.slice(0, 24).map((d) => (
                  <tr key={d} className="border-t border-border">
                    <td className="py-1.5 pr-4">{d}</td>
                    {series.map((s) => <td key={s.series_id} className="py-1.5 pr-4">{formatCell(valueOf.get(`${d}|${s.series_id}`) ?? null)}</td>)}
                  </tr>
                ))}
              </DataTable>
            </div>
          )}
        </>
      )}
    </section>
  );
}
