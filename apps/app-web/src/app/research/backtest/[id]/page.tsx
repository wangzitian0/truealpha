import Link from "next/link";
import { notFound } from "next/navigation";
import {
  getBacktestRun,
  getBacktestValuations,
  getBacktestTrades,
} from "@/server/mart/backtest-repository";
import { formatPercentFromFraction } from "@/client/format";
import { BacktestChart } from "@/components/backtest-chart";

export const dynamic = "force-dynamic";

interface PageProps {
  params: Promise<{ id: string }>;
}

export default async function BacktestDetailPage({ params }: PageProps) {
  const { id } = await params;
  const run = await getBacktestRun(id);

  if (!run) {
    notFound();
  }

  const [valM, valD, trades] = await Promise.all([
    getBacktestValuations(id, "1M"),
    getBacktestValuations(id, "1D"),
    getBacktestTrades(id, 100),
  ]);

  return (
    <section aria-labelledby="backtest-detail-heading" className="space-y-6">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <div className="flex items-center gap-2 text-xs text-gray-400">
            <Link href="/research/backtest" className="hover:text-white">
              Backtests
            </Link>
            <span>/</span>
            <span className="font-mono text-gray-300">{run.run_id.slice(0, 20)}...</span>
          </div>
          <h1 id="backtest-detail-heading" className="mt-1 text-2xl font-bold tracking-tight text-white">
            {run.strategy_key} ({run.strategy_version})
          </h1>
          <p className="text-xs text-gray-400">
            Universe: <span className="font-mono text-gray-300">{run.universe_id}</span> • Period: {run.start_date} to {run.end_date}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <span
            className={`rounded-full px-3 py-1 text-xs font-semibold uppercase ${
              run.status === "succeeded"
                ? "bg-emerald-500/10 text-emerald-400 border border-emerald-500/20"
                : "bg-rose-500/10 text-rose-400 border border-rose-500/20"
            }`}
          >
            {run.status}
          </span>
        </div>
      </div>

      {/* KPI Cards */}
      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        <div className="rounded-xl border border-border bg-card p-4">
          <div className="text-xs font-medium text-gray-400">CAGR (10Y Monthly)</div>
          <div className="mt-1 text-2xl font-bold font-mono text-emerald-400">
            {run.cagr_monthly !== null ? formatPercentFromFraction(String(run.cagr_monthly)) : "—"}
          </div>
          <div className="text-[10px] text-gray-500">Long-term compound annual growth</div>
        </div>

        <div className="rounded-xl border border-border bg-card p-4">
          <div className="text-xs font-medium text-gray-400">Sharpe Ratio (3Y Daily)</div>
          <div className="mt-1 text-2xl font-bold font-mono text-white">
            {run.sharpe_daily !== null ? run.sharpe_daily.toFixed(2) : "—"}
          </div>
          <div className="text-[10px] text-gray-500">Risk-adjusted return (rf=0)</div>
        </div>

        <div className="rounded-xl border border-border bg-card p-4">
          <div className="text-xs font-medium text-gray-400">Max Drawdown (3Y Daily)</div>
          <div className="mt-1 text-2xl font-bold font-mono text-rose-400">
            {run.max_dd_daily !== null ? formatPercentFromFraction(String(run.max_dd_daily)) : "—"}
          </div>
          <div className="text-[10px] text-gray-500">Peak-to-trough high watermark drop</div>
        </div>

        <div className="rounded-xl border border-border bg-card p-4">
          <div className="text-xs font-medium text-gray-400">Annualized Volatility</div>
          <div className="mt-1 text-2xl font-bold font-mono text-amber-400">
            {run.vol_daily !== null ? formatPercentFromFraction(String(run.vol_daily)) : "—"}
          </div>
          <div className="text-[10px] text-gray-500">3-Year daily annualized volatility</div>
        </div>
      </div>

      {/* Performance Charts */}
      <div className="space-y-2">
        <h2 className="text-sm font-semibold uppercase tracking-wider text-gray-400">
          Dual-Resolution Equity Performance
        </h2>
        <BacktestChart valuationsMonthly={valM} valuationsDaily={valD} />
      </div>

      {/* Trades Table */}
      <div className="space-y-2">
        <h2 className="text-sm font-semibold uppercase tracking-wider text-gray-400">
          Rebalance Trades ({trades.length} records)
        </h2>
        <div className="overflow-x-auto rounded-xl border border-border bg-card">
          <table className="w-full text-left text-xs">
            <thead className="border-b border-border bg-muted/40 uppercase text-gray-400">
              <tr>
                <th className="px-4 py-2.5">Date</th>
                <th className="px-4 py-2.5">Symbol</th>
                <th className="px-4 py-2.5">Side</th>
                <th className="px-4 py-2.5 text-right">Shares</th>
                <th className="px-4 py-2.5 text-right">Price</th>
                <th className="px-4 py-2.5 text-right">Trade Value</th>
                <th className="px-4 py-2.5 text-right">Target Weight</th>
                <th className="px-4 py-2.5 text-right">Fee</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border text-gray-300">
              {trades.length === 0 ? (
                <tr>
                  <td colSpan={8} className="px-4 py-6 text-center text-gray-500">
                    No rebalance trades recorded for this run.
                  </td>
                </tr>
              ) : (
                trades.map((t) => (
                  <tr key={t.trade_id} className="hover:bg-muted/20 font-mono">
                    <td className="px-4 py-2 text-gray-400">{t.trade_date}</td>
                    <td className="px-4 py-2 font-bold text-white">{t.symbol}</td>
                    <td className="px-4 py-2">
                      <span className={t.side === "BUY" ? "text-emerald-400" : "text-rose-400"}>
                        {t.side}
                      </span>
                    </td>
                    <td className="px-4 py-2 text-right">{t.shares.toFixed(2)}</td>
                    <td className="px-4 py-2 text-right">${t.execution_price.toFixed(2)}</td>
                    <td className="px-4 py-2 text-right">${t.trade_value.toLocaleString()}</td>
                    <td className="px-4 py-2 text-right">{(t.weight_after * 100).toFixed(1)}%</td>
                    <td className="px-4 py-2 text-right text-gray-500">${t.fee_paid.toFixed(2)}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}
