import { api } from "@/client/api";
import { formatCell, formatShortSha, formatUsdMagnitude } from "@/client/format";
import { EmptyNotice } from "@/components/data-table";
import { ResearchTable } from "@/components/research-table";
import { loadAuthed } from "../../_load";

export const dynamic = "force-dynamic";

export default async function AdminUniversesPage() {
  const { data } = await loadAuthed("/admin/universes", (opts) => api.getAdminUniverses(opts), { admin: true });
  const heads = data?.heads ?? [];

  return (
    <section className="space-y-6">
      <h1 className="text-2xl font-bold tracking-tight">Universes</h1>
      {heads.length === 0 && <EmptyNotice>No governed universe heads published yet.</EmptyNotice>}
      {heads.map((head) => {
        const kind = String(head.kind ?? "");
        const rows = data?.members?.[kind] ?? [];
        return (
          <div key={kind} className="space-y-3">
            <h2 className="font-mono text-lg text-accent">{String(head.universe_id ?? "")}</h2>
            <p className="text-xs text-gray-500">
              {kind} · seq {String(head.sequence ?? "")} · {String(head.instrument_count ?? 0)} instruments · mapping {formatShortSha(String(head.mapping_sha256 ?? ""))}
            </p>
            <ResearchTable caption={`Universe ${kind}`} headers={["Ticker", "Company", "CIK", "FIGI", "Market cap", "As of", "Source bytes"]}>
              {rows.map((m) => (
                <tr key={String(m.ticker ?? "")} className="border-t border-border font-mono text-xs">
                  <td className="px-3 py-1.5">{String(m.ticker ?? "")}</td>
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
    </section>
  );
}
