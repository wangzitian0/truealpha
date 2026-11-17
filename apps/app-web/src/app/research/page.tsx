import Link from "next/link";
import { api } from "@/client/api";
import { formatCell, formatPercentFromFraction, formatRatio, formatSignedRatio, formatUsdMagnitude, signColor } from "@/client/format";
import { DASHBOARD_STRATEGY_ID, summarizeUniverseUsability, valuationBasisLabel } from "@/client/strategy";
import { CardExportButton } from "@/components/card-export-button";
import { AvailabilityBadge, ReadStateNotice } from "@/components/read-state";
import { ResearchTable } from "@/components/research-table";
import { loadAuthed } from "../_load";
import { describeServedRunAge } from "./served-run-age";

export const dynamic = "force-dynamic";

export default async function ResearchOverviewPage() {
  const { data: loaded, error } = await loadAuthed("/research", async (opts) => {
    const [overview, strategy] = await Promise.all([api.getOverview(opts), api.getStrategy({ strategy_id: DASHBOARD_STRATEGY_ID }, opts)]);
    return { overview, strategy };
  });
  const overview = loaded?.overview ?? null;
  const age = overview?.latest_cutoff ? describeServedRunAge(overview.latest_cutoff) : null;
  const decisions = loaded?.strategy?.decisions ?? [];
  const usable = summarizeUniverseUsability(decisions);
  const topRanked = decisions.filter((d) => d.rank !== null).slice(0, 5);
  const basisLabel = valuationBasisLabel(topRanked[0]?.valuation_basis);
  const td = "px-4 py-3";

  return (
    <section aria-labelledby="research-heading" className="space-y-8">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 id="research-heading" className="text-2xl font-bold tracking-tight">Research overview</h1>
          <p className="mt-1 text-sm text-gray-400">Seven factor modules from governed <code className="text-accent">mart</code> snapshots via unified OpenAPI.</p>
        </div>
        <div className="flex items-center gap-3">
          {overview?.latest_cutoff && (
            <span data-served-run-age={age ? String(age.hours) : ""} className={`rounded-full border px-3 py-1 font-mono text-xs ${age?.stale ? "border-amber-500/40 text-amber-300" : "border-emerald-500/40 text-emerald-300"}`}>
              as of {overview.latest_cutoff.slice(0, 10)} ({age?.label ?? "unknown"})
            </span>
          )}
          <CardExportButton cutoff={overview?.latest_cutoff ?? undefined} />
        </div>
      </div>
      {error && <ReadStateNotice state={{ kind: "error", message: error }} />}
      {overview && !overview.available && <ReadStateNotice state={{ kind: "unavailable", reason: overview.reason ?? "unavailable" }} />}
      {overview?.available && (
        <>
          <div className="grid gap-3 md:grid-cols-4">
            {[
              ["Served run", formatCell(overview.strategy_run_id), "font-mono text-sm text-gray-200"],
              ["Universe usability", `${usable.usable} / ${usable.total}`, "text-xl font-bold text-white"],
              ["Selected", String(decisions.filter((d) => d.outcome === "selected").length), "text-xl font-bold text-emerald-400"],
              ["Corpus prefix", formatCell(overview.corpus_prefix || null), "font-mono text-sm text-accent"],
            ].map(([label, val, cls]) => (
              <div key={label} className="rounded-xl border border-border bg-card p-4">
                <div className="text-xs text-gray-500">{label}</div>
                <div className={`mt-1 ${cls}`}>{val}</div>
              </div>
            ))}
          </div>
          <div>
            <h2 className="mb-3 text-lg font-semibold">Factor modules</h2>
            <ResearchTable caption="Seven research modules" headers={["#", "Module", "Gate", "Availability", "Coverage", "Source"]}>
              {(overview.modules ?? []).map((m) => (
                <tr key={m.module} data-module={m.module} className="border-t border-border">
                  <td className={`${td} font-mono`}>{m.module}</td>
                  <td className={td}><div className="font-medium text-white">{m.name}</div><div className="text-xs text-gray-400">{m.note}</div></td>
                  <td className={`${td} text-xs`}>{m.gate}</td>
                  <td className={td}><AvailabilityBadge status={m.availability} /></td>
                  <td className={`${td} tabular-nums`}>{m.with_value !== null && m.total !== null ? `${m.with_value} / ${m.total}` : "—"}</td>
                  <td className={`${td} font-mono text-xs text-gray-400`}>{m.source}</td>
                </tr>
              ))}
            </ResearchTable>
          </div>
          {topRanked.length > 0 && (
            <div>
              <div className="mb-3 flex items-center justify-between">
                <h2 className="text-lg font-semibold">Top ranked issuers</h2>
                <Link href="/research/rankings" className="text-xs text-accent hover:underline">View full rankings →</Link>
              </div>
              <ResearchTable caption="Top ranked issuers" headers={["Rank", "Issuer", "Tier", `Current ${basisLabel}`, `Gap (${basisLabel})`, "GPPE", "Weight", "Trace"]}>
                {topRanked.map((row) => (
                  <tr key={row.issuer_id} className="border-t border-border">
                    <td className={`${td} font-mono`}>{row.rank}</td>
                    <th scope="row" className={`${td} font-medium`}><Link href={`/research/entities/${encodeURIComponent(row.issuer_id)}`} className="text-accent hover:underline">{row.label}</Link></th>
                    <td className={td}>{formatCell(row.tier)}</td>
                    <td className={`${td} tabular-nums`}>{formatCell(formatRatio(row.current_multiple))}</td>
                    <td className={`${td} tabular-nums ${signColor(row.valuation_gap)}`}>{formatCell(formatSignedRatio(row.valuation_gap))}</td>
                    <td className={`${td} tabular-nums`}>{formatCell(formatUsdMagnitude(row.capital_adjusted_labor_efficiency))}</td>
                    <td className={`${td} tabular-nums`}>{formatCell(formatPercentFromFraction(row.target_weight))}</td>
                    <td className={td}><Link href={`/research/trace?issuer=${encodeURIComponent(row.issuer_id)}&cutoff=${encodeURIComponent(row.cutoff_at)}`} className="text-accent hover:underline">trace</Link></td>
                  </tr>
                ))}
              </ResearchTable>
            </div>
          )}
        </>
      )}
    </section>
  );
}
