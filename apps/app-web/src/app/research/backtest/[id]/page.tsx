import Link from "next/link";
import { notFound } from "next/navigation";
import { getBacktestRun, getBacktestValuations, getBacktestTrades } from "@/server/mart/backtest-repository";
import { decodeRouteParam, formatCell, formatPercentFromFraction, formatRatio, formatUsdMagnitude } from "@/client/format";
import { BacktestBasis, backtestStatusStyle } from "@/components/backtest-basis";
import { BacktestChart } from "@/components/backtest-chart";

export const dynamic = "force-dynamic";

function KpiCard({
  label,
  value,
  tone,
  rawTitle,
  sub,
  subTitle,
}: {
  label: string;
  value: string;
  tone: string;
  rawTitle?: string;
  sub: string;
  subTitle?: string;
}) {
  return (
    <div className="rounded-xl border border-border bg-card p-4">
      <div className="text-xs font-medium text-gray-400">{label}</div>
      <div className={`mt-1 text-2xl font-bold font-mono ${tone}`} title={rawTitle}>
        {value}
      </div>
      <div className="text-[10px] text-gray-500" title={subTitle}>
        {sub}
      </div>
    </div>
  );
}

export default async function BacktestDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const runId = decodeRouteParam(id);
  const run = await getBacktestRun(runId);
  if (!run) notFound();

  const [valM, valD, trades] = await Promise.all([
    getBacktestValuations(runId, "1M"),
    getBacktestValuations(runId, "1D"),
    getBacktestTrades(runId, 100),
  ]);

  return (
    <section aria-labelledby="backtest-detail-heading" className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <div className="flex items-center gap-2 text-xs text-gray-400">
            <Link href="/research/backtest" className="hover:text-white">Backtests</Link>
            <span>/</span>
            <span className="font-mono text-gray-300" title={run.run_id}>{run.run_id.slice(0, 20)}…</span>
          </div>
          <h1 id="backtest-detail-heading" className="mt-1 text-2xl font-bold tracking-tight text-white">
            {run.strategy_key} ({run.strategy_version})
          </h1>
          <p className="text-xs text-gray-400">
            Universe: <span className="font-mono text-gray-300">{run.universe_id}</span> • Period: {run.start_date} to {run.end_date}
          </p>
        </div>
        <span className={`rounded-full px-3 py-1 text-xs font-semibold uppercase ${backtestStatusStyle(run.status)}`}>
          {run.status}
        </span>
      </div>

      <BacktestBasis run={run} />

      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        <KpiCard
          label="CAGR"
          value={formatCell(formatPercentFromFraction(run.cagr_monthly))}
          tone="text-emerald-400"
          rawTitle={run.cagr_monthly !== null ? String(run.cagr_monthly) : undefined}
          sub={`Benchmark ${formatCell(formatPercentFromFraction(run.benchmark_cagr))}`}
          subTitle={run.benchmark_cagr !== null ? String(run.benchmark_cagr) : undefined}
        />
        <KpiCard
          label="Sharpe Ratio (daily)"
          value={formatCell(formatRatio(run.sharpe_daily))}
          tone="text-white"
          rawTitle={run.sharpe_daily !== null ? String(run.sharpe_daily) : undefined}
          sub="Risk-adjusted return (rf=0)"
        />
        <KpiCard
          label="Max Drawdown (daily)"
          value={formatCell(formatPercentFromFraction(run.max_dd_daily))}
          tone="text-rose-400"
          rawTitle={run.max_dd_daily !== null ? String(run.max_dd_daily) : undefined}
          sub={`Benchmark ${formatCell(formatPercentFromFraction(run.benchmark_max_dd))}`}
          subTitle={run.benchmark_max_dd !== null ? String(run.benchmark_max_dd) : undefined}
        />
        <KpiCard
          label="Annualized Volatility"
          value={formatCell(formatPercentFromFraction(run.vol_daily))}
          tone="text-amber-400"
          rawTitle={run.vol_daily !== null ? String(run.vol_daily) : undefined}
          sub="Daily returns, annualized"
        />
      </div>

      <div className="space-y-2">
        <h2 className="text-sm font-semibold uppercase tracking-wider text-gray-400">Strategy and benchmark NAV</h2>
        <BacktestChart valuationsMonthly={valM} valuationsDaily={valD} />
      </div>

      <div className="space-y-2">
        <h2 className="text-sm font-semibold uppercase tracking-wider text-gray-400">
          Rebalance Trades ({trades.length} records)
        </h2>
        <div className="overflow-x-auto rounded-xl border border-border bg-card">
          <table className="w-full text-left text-xs">
            <thead className="border-b border-border bg-muted/40 uppercase text-gray-400">
              <tr>
                {["Date", "Symbol", "Side"].map((h) => (
                  <th key={h} className="px-4 py-2.5">{h}</th>
                ))}
                {["Shares", "Price", "Trade Value", "Target Weight", "Fee"].map((h) => (
                  <th key={h} className="px-4 py-2.5 text-right">{h}</th>
                ))}
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
                      <span className={t.side === "BUY" ? "text-emerald-400" : "text-rose-400"}>{t.side}</span>
                    </td>
                    <td className="px-4 py-2 text-right" title={String(t.shares)}>{formatCell(formatRatio(t.shares))}</td>
                    <td className="px-4 py-2 text-right" title={String(t.execution_price)}>${formatCell(formatRatio(t.execution_price))}</td>
                    <td className="px-4 py-2 text-right" title={String(t.trade_value)}>{formatCell(formatUsdMagnitude(t.trade_value))}</td>
                    <td className="px-4 py-2 text-right" title={String(t.weight_after)}>{formatCell(formatPercentFromFraction(t.weight_after))}</td>
                    <td className="px-4 py-2 text-right text-gray-500" title={String(t.fee_paid)}>${formatCell(formatRatio(t.fee_paid))}</td>
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
