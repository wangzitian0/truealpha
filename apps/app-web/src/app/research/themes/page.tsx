import Link from "next/link";
import { api } from "@/client/api";
import { formatCell, formatPercentFromFraction, formatRatio, formatShortSha } from "@/client/format";
import { classifiedShare } from "@/client/strategy";
import { ResearchTable } from "@/components/research-table";
import { loadAuthed } from "../../_load";
import { ReadStateNotice } from "@/components/read-state";

export const dynamic = "force-dynamic";

function millions(value: string): string {
  const n = value.trim() ? Number(value) : NaN;
  return Number.isFinite(n) ? `${(n / 1_000_000).toLocaleString("en-US", { maximumFractionDigits: 0 })}` : "—";
}

export default async function ThemesPage() {
  const { data, error } = await loadAuthed("/research/themes", (opts) => api.getThemes(opts));
  const groups = data?.groups ?? [];
  const unvisitedCount = Number(data?.unvisited_count ?? 0);
  const headers = ["Rank", "Issuer", "Theme share", "Classified", "In theme (m)", "Consolidated (m)", "Segments", "Period", "Confidence", "Status"];

  return (
    <section aria-labelledby="themes-heading" className="space-y-8">
      <div>
        <h1 id="themes-heading" className="text-2xl font-bold tracking-tight">Theme purity</h1>
        <p className="mt-2 text-sm text-gray-400">Revenue share per theme over consolidated revenue from <code className="mx-1 rounded bg-card px-1">mart.issuer_theme_purity</code>.</p>
      </div>
      {error && <ReadStateNotice state={{ kind: "error", message: error }} />}
      {unvisitedCount > 0 && (
        <p role="note" data-testid="themes-unvisited-notice" className="rounded-lg border border-amber-400/40 bg-amber-400/10 px-4 py-2 text-xs text-amber-300">
          Unvisited theme rows (no segment partition): <strong className="font-mono">{unvisitedCount}</strong>
        </p>
      )}
      {groups.length === 0 && !error ? (
        <p className="text-sm text-gray-400">No theme purity materialized yet.</p>
      ) : (
        groups.map((group) => (
          <div key={group.theme_id} className="space-y-3">
            <div>
              <h2 className="text-lg font-semibold">{group.theme}</h2>
              <p className="text-xs text-gray-500">
                Definition {group.definition_version} (<span className="font-mono" title={group.definition_sha256}>{formatShortSha(group.definition_sha256)}</span>) · run {group.run_id} · cutoff {group.cutoff}
              </p>
            </div>
            <ResearchTable caption={`Issuers ranked by ${group.theme}`} headers={headers}>
              {(group.rows ?? []).map((row, index) => (
                <tr key={row.issuer_id} className="border-t border-border">
                  <td className="px-4 py-3">{row.theme_share === null ? "—" : index + 1}</td>
                  <th scope="row" className="px-4 py-3 font-medium">
                    <Link href={`/research/entities/${encodeURIComponent(row.issuer_id)}`} className="text-accent hover:underline" title={row.issuer_id}>{row.label}</Link>
                  </th>
                  <td className="px-4 py-3 tabular-nums">{row.theme_share === null ? <span className="text-gray-500">refused</span> : formatCell(formatPercentFromFraction(row.theme_share))}</td>
                  <td className="px-4 py-3 tabular-nums">{formatCell(formatPercentFromFraction(classifiedShare(row)))}</td>
                  <td className="px-4 py-3 tabular-nums">{millions(row.in_theme_revenue)}</td>
                  <td className="px-4 py-3 tabular-nums">{millions(row.consolidated_revenue)}</td>
                  <td className="px-4 py-3 tabular-nums">{formatCell(row.segments)}</td>
                  <td className="px-4 py-3">{row.period_end}</td>
                  <td className="px-4 py-3 tabular-nums">{formatCell(formatRatio(row.confidence))}</td>
                  <td className="px-4 py-3 text-xs">
                    <div>{row.availability_status}</div>
                    {(row.source_evidence_status || row.factor_validation_status) && (
                      <div className="font-mono text-[10px] text-gray-500">
                        {[row.source_evidence_status, row.factor_validation_status].filter(Boolean).join(" / ")}
                      </div>
                    )}
                  </td>
                </tr>
              ))}
            </ResearchTable>
          </div>
        ))
      )}
    </section>
  );
}
