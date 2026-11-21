import Link from "next/link";
import { api } from "@/client/api";
import { formatCell, formatPercentFromFraction, formatRatio } from "@/client/format";
import { BacktestLimitations, backtestStatusStyle, runLimitations } from "@/components/backtest-basis";
import { ResearchTable } from "@/components/research-table";
import { loadAuthed } from "../../_load";
import { ReadStateNotice } from "@/components/read-state";

export const dynamic = "force-dynamic";

export default async function BacktestListPage() {
  const { data, error } = await loadAuthed("/research/backtest", (opts) => api.listBacktests(50, opts));
  const runs = data?.runs ?? [];

  return (
    <section aria-labelledby="backtest-list-heading" className="space-y-6">
      <div>
        <h1 id="backtest-list-heading" className="text-2xl font-bold tracking-tight">Portfolio Backtests</h1>
        <p className="mt-1 text-sm text-gray-400">Quarter-end PIT backtests with explicit price basis and claim ceiling.</p>
      </div>
      {error && <ReadStateNotice state={{ kind: "error", message: error }} />}
      {runs.length === 0 && !error ? (
        <div className="rounded-xl border border-border bg-card p-12 text-center text-gray-300">No backtest runs found.</div>
      ) : (
        <ResearchTable caption="Portfolio backtests" headers={["Strategy", "Universe", "Time Horizon", "Basis", "CAGR", "Benchmark CAGR", "Sharpe (rf 0)", "Max DD", "Turnover", "Status", "Action"]}>
          {runs.map((r) => (
            <tr key={r.run_id} data-run-id={r.run_id} className="border-t border-border hover:bg-muted/20">
              <td className="px-4 py-3 font-medium text-white"><div>{r.strategy_key}</div><div className="text-xs text-gray-500">{r.strategy_version}</div></td>
              <td className="px-4 py-3 font-mono text-xs text-gray-400">{r.universe_id}</td>
              <td className="px-4 py-3 text-xs text-gray-400">{r.start_date} ~ {r.end_date}</td>
              <td className="px-4 py-3 font-mono text-xs text-gray-400">
                <div data-price-basis={r.price_basis ?? ""}>{r.price_basis ?? "not recorded"}</div>
                <div className="text-amber-400" data-claim-ceiling={r.claim_ceiling ?? ""}>{r.claim_ceiling ?? "not recorded"}</div>
                <details className="mt-1 font-sans text-gray-400"><summary>Limitations ({runLimitations(r).length})</summary><BacktestLimitations run={r} /></details>
              </td>
              <td className="px-4 py-3 text-right font-mono font-medium text-emerald-400">{formatCell(formatPercentFromFraction(r.cagr_monthly))}</td>
              <td className="px-4 py-3 text-right font-mono text-gray-300">{formatCell(formatPercentFromFraction(r.benchmark_cagr))}</td>
              <td className="px-4 py-3 text-right font-mono">{formatCell(formatRatio(r.sharpe_daily))}</td>
              <td className="px-4 py-3 text-right font-mono text-rose-400">{formatCell(formatPercentFromFraction(r.max_dd_daily))}</td>
              <td className="px-4 py-3 text-right font-mono text-gray-400">{formatCell(formatRatio(r.turnover_monthly))}</td>
              <td className="px-4 py-3 text-center"><span className={`inline-flex rounded-full px-2 py-0.5 text-xs font-medium ${backtestStatusStyle(r.status)}`}>{r.status}</span></td>
              <td className="px-4 py-3 text-right"><Link href={`/research/backtest/${encodeURIComponent(r.run_id)}`} className="text-xs font-semibold text-accent hover:underline">Inspect →</Link></td>
            </tr>
          ))}
        </ResearchTable>
      )}
    </section>
  );
}
