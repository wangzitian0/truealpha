import Link from "next/link";
import { api } from "@/client/api";
import { decodeRouteParam, formatCell, formatPercentFromFraction, formatRatio, formatUsdMagnitude } from "@/client/format";
import { BacktestBasis, backtestStatusStyle } from "@/components/backtest-basis";
import { BacktestChart } from "@/components/backtest-chart";
import { ResearchTable } from "@/components/research-table";
import { loadAuthed } from "../../../_load";

export const dynamic = "force-dynamic";

export default async function BacktestDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const runId = decodeRouteParam(id);
  const { data: detail, error } = await loadAuthed("/research/backtest", (opts) => api.getBacktest(runId, opts), { notFoundOn404: true });
  if (!detail) return <p className="text-sm text-red-400">{error ?? "Failed to load backtest."}</p>;
  const { run, valuations = [], trades = [] } = detail;

  return (
    <section aria-labelledby="backtest-detail-heading" className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <div className="flex items-center gap-2 text-xs text-gray-400">
            <Link href="/research/backtest" className="hover:text-white">Backtests</Link>
            <span>/</span>
            <span className="font-mono text-gray-300" title={run.run_id}>{run.run_id.slice(0, 20)}…</span>
          </div>
          <h1 id="backtest-detail-heading" className="mt-1 text-2xl font-bold tracking-tight text-white">{run.strategy_key} ({run.strategy_version})</h1>
          <p className="text-xs text-gray-400">Universe: <span className="font-mono text-gray-300">{run.universe_id}</span> • {run.start_date} to {run.end_date}</p>
        </div>
        <span className={`rounded-full px-3 py-1 text-xs font-semibold uppercase ${backtestStatusStyle(run.status)}`}>{run.status}</span>
      </div>
      <BacktestBasis run={run} />
      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        {[
          ["CAGR", formatCell(formatPercentFromFraction(run.cagr_monthly)), `Benchmark ${formatCell(formatPercentFromFraction(run.benchmark_cagr))}`, "text-emerald-400"],
          ["Sharpe (daily)", formatCell(formatRatio(run.sharpe_daily)), "Risk-adjusted (rf=0)", "text-white"],
          ["Max Drawdown", formatCell(formatPercentFromFraction(run.max_dd_daily)), `Benchmark ${formatCell(formatPercentFromFraction(run.benchmark_max_dd))}`, "text-rose-400"],
          ["Turnover", formatCell(formatRatio(run.turnover_monthly)), "Monthly", "text-gray-200"],
        ].map(([label, val, sub, cls]) => (
          <div key={label} className="rounded-xl border border-border bg-card p-4">
            <div className="text-xs text-gray-400">{label}</div>
            <div className={`mt-1 text-2xl font-bold font-mono ${cls}`}>{val}</div>
            <div className="text-[10px] text-gray-500">{sub}</div>
          </div>
        ))}
      </div>
      <BacktestChart valuationsMonthly={valuations.filter((v) => v.resolution === "1M")} valuationsDaily={valuations.filter((v) => v.resolution === "1D")} />
      {trades.length > 0 && (
        <ResearchTable caption="Backtest trades" headers={["Date", "Symbol", "Side", "Shares", "Price", "Value", "Weight after"]}>
          {trades.map((t) => (
            <tr key={t.trade_id} className="border-t border-border font-mono text-xs">
              <td className="px-3 py-2">{t.trade_date}</td>
              <td className="px-3 py-2 text-white">{t.symbol}</td>
              <td className="px-3 py-2">{t.side}</td>
              <td className="px-3 py-2">{formatRatio(t.shares)}</td>
              <td className="px-3 py-2">{formatRatio(t.execution_price)}</td>
              <td className="px-3 py-2">{formatUsdMagnitude(t.trade_value)}</td>
              <td className="px-3 py-2">{formatPercentFromFraction(t.weight_after)}</td>
            </tr>
          ))}
        </ResearchTable>
      )}
    </section>
  );
}
