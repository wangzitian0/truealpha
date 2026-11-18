import Link from "next/link";
import { api } from "@/client/api";
import { formatCell, formatMacroValue, formatNumberMagnitude, formatShortSha, formatUsdMagnitude, formatUtcMinute } from "@/client/format";
import { DASHBOARD_STRATEGY_ID, WIDE_PANEL_INPUT_COLUMNS } from "@/client/strategy";
import { DataTable, EmptyNotice } from "@/components/data-table";
import { PanelNodeTd } from "@/components/panel-node-cell";
import { ReadStateNotice } from "@/components/read-state";
import { ResearchTable } from "@/components/research-table";
import { loadAuthed } from "../../_load";

export const dynamic = "force-dynamic";

export default async function CoveragePage() {
  const { data, error } = await loadAuthed("/research/coverage", async (opts) => {
    const [coverage, universes, macro] = await Promise.all([
      api.getPanelCoverage({ strategy_id: DASHBOARD_STRATEGY_ID }, opts),
      api.getPanelUniverses(opts),
      api.getPanelMacro(opts),
    ]);
    return { coverage, universes, macro };
  });

  const cov = data?.coverage ?? null;
  const decisions = cov?.decisions ?? [];
  const excluded = decisions.filter((d) => !d.eligible);
  const widePanel = cov?.wide_panel ?? {};
  const heads = data?.universes?.heads ?? [];
  const macro = data?.macro ?? null;
  const series = macro?.series ?? [];
  const months = macro?.months ?? [];
  const dates = Array.from(new Set(months.map((m) => String(m.observation_date))));
  const valueOf = new Map(months.map((m) => [`${m.observation_date}|${m.series_id}`, String(m.value)]));

  const td = "px-4 py-3";
  return (
    <section aria-labelledby="coverage-heading" className="space-y-8">
      <div>
        <h1 id="coverage-heading" className="text-2xl font-bold tracking-tight">Universe input coverage</h1>
        <p className="mt-2 text-sm text-gray-400">
          Excluded subjects, wide-panel inputs, governed universes, and macro curve from <code className="text-accent">/api/v1/panel/*</code>.
        </p>
      </div>
      {error && <ReadStateNotice state={{ kind: "error", message: error }} />}
      {cov && !cov.available && <ReadStateNotice state={{ kind: "unavailable", reason: cov.reason ?? "unavailable" }} />}
      {cov?.available && (
        <>
          <p className="text-sm text-gray-400">{decisions.length - excluded.length} usable / {decisions.length} total ({excluded.length} excluded)</p>
          <div>
            <h2 className="mb-3 text-lg font-semibold">Wide panel input coverage</h2>
            <ResearchTable caption="Wide panel inputs" headers={["Issuer", ...WIDE_PANEL_INPUT_COLUMNS.map((c) => c.label), "Trace"]}>
              {decisions.map((d) => {
                const cells = widePanel[d.issuer_id] ?? {};
                return (
                  <tr key={d.issuer_id} className="border-t border-border">
                    <th scope="row" className={`${td} font-medium`}>
                      <Link href={`/research/entities/${encodeURIComponent(d.issuer_id)}`} className="text-accent hover:underline" title={d.issuer_id}>{d.label}</Link>
                    </th>
                    {WIDE_PANEL_INPUT_COLUMNS.map((col) => {
                      const node = cells[col.key];
                      const fmt = node?.value ? (col.monetary ? formatUsdMagnitude(node.value) : formatNumberMagnitude(node.value)) : null;
                      return <PanelNodeTd key={col.key} node={node} formatted={fmt} />;
                    })}
                    <td className={td}><Link href={`/research/trace?issuer=${encodeURIComponent(d.issuer_id)}&cutoff=${encodeURIComponent(d.cutoff_at)}`} className="text-accent hover:underline text-xs">trace →</Link></td>
                  </tr>
                );
              })}
            </ResearchTable>
          </div>
          <ResearchTable caption="Excluded issuers" headers={["Issuer", "Outcome", "Reason code", "Explanation", "Cutoff", "Trace"]}>
            {excluded.map((d) => (
              <tr key={d.issuer_id} className="border-t border-border">
                <th scope="row" className={`${td} font-medium`}>
                  <Link href={`/research/entities/${encodeURIComponent(d.issuer_id)}`} className="text-accent hover:underline" title={d.issuer_id}>{d.label}</Link>
                </th>
                <td className={`${td} font-mono text-xs`}>{d.outcome}</td>
                <td className={`${td} font-mono text-xs`}>{formatCell(d.exclusion_reason)}</td>
                <td className={`${td} text-xs text-gray-300`}>{formatCell(d.exclusion_label)}</td>
                <td className={`${td} font-mono text-xs`}>{d.cutoff_at}</td>
                <td className={td}><Link href={`/research/trace?issuer=${encodeURIComponent(d.issuer_id)}&cutoff=${encodeURIComponent(d.cutoff_at)}`} className="text-accent hover:underline text-xs">trace →</Link></td>
              </tr>
            ))}
          </ResearchTable>
        </>
      )}
      <div className="space-y-4">
        <h2 className="text-lg font-semibold">Governed universes</h2>
        {heads.length === 0 && <EmptyNotice>No governed universe heads published yet.</EmptyNotice>}
        {heads.map((head) => {
          const kind = String(head.kind ?? "");
          const rows = data?.universes?.members?.[kind] ?? [];
          return (
            <div key={kind} className="space-y-3">
              <h3 className="font-mono text-base text-accent">{String(head.universe_id ?? "")}</h3>
              <p className="text-xs text-gray-500">
                {kind} · seq {String(head.sequence ?? "")} · {String(head.instrument_count ?? 0)} instruments · mapping {formatShortSha(String(head.mapping_sha256 ?? ""))}
              </p>
              <ResearchTable caption={`Universe ${kind}`} headers={["Ticker", "Company", "CIK", "FIGI", "Market cap", "As of", "Source bytes"]}>
                {rows.map((m) => (
                  <tr key={String(m.ticker ?? "")} className="border-t border-border font-mono text-xs">
                    <th scope="row" className="px-3 py-1.5 font-medium">{String(m.ticker ?? "")}</th>
                    <td className="px-3 py-1.5 font-sans">{String(m.company_name ?? "")}</td>
                    <td className="px-3 py-1.5">{formatCell(m.cik as number | null)}</td>
                    <td className="px-3 py-1.5">{formatCell(m.figi as string | null)}</td>
                    <td className="px-3 py-1.5">{formatCell(formatUsdMagnitude(m.market_cap as string | null))}</td>
                    <td className="px-3 py-1.5">{String(m.as_of ?? "")}</td>
                    <td className="px-3 py-1.5">{String(m.source ?? "")} · {formatShortSha(String(m.fetch_sha256 ?? ""))}</td>
                  </tr>
                ))}
              </ResearchTable>
            </div>
          );
        })}
      </div>
      <div className="space-y-4">
        <h2 className="text-lg font-semibold">Macro base data</h2>
        {macro && !macro.available && <ReadStateNotice state={{ kind: "unavailable", reason: macro.reason ?? "unavailable" }} />}
        {macro?.available && (
          <>
            <div className="grid gap-4 md:grid-cols-3" data-macro-series="cards">
              {series.map((item) => {
                const latest = item.latest as Record<string, unknown> | null | undefined;
                return (
                  <article key={item.series_id} data-series-id={item.series_id} data-fresh={item.is_fresh ? "true" : "false"} className="rounded-xl border border-border bg-card p-5">
                    <h3 className="font-semibold">{item.label}</h3>
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
      </div>
    </section>
  );
}
