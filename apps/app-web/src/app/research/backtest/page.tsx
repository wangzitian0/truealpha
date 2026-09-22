import Link from "next/link";
import { listBacktestRuns } from "@/server/mart/backtest-repository";
import { formatPercentFromFraction, formatRatio } from "@/client/format";

export const dynamic = "force-dynamic";

export default async function BacktestListPage() {
  let runs: Awaited<ReturnType<typeof listBacktestRuns>> = [];
  let errorMsg: string | null = null;

  try {
    runs = await listBacktestRuns(50);
  } catch (err: unknown) {
    errorMsg = err instanceof Error ? err.message : "Failed to load backtest runs";
  }

  return (
    <section aria-labelledby="backtest-list-heading" className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 id="backtest-list-heading" className="text-2xl font-bold tracking-tight">
            Portfolio Backtests
          </h1>
          <p className="mt-1 text-sm text-gray-400">
            VectorBT simulation on dual-resolution PIT market data (10Y Monthly CAGR + 3Y Daily Sharpe &amp; Drawdown).
          </p>
        </div>
      </div>

      {errorMsg && (
        <div className="rounded-lg border border-red-500/30 bg-red-500/10 p-4 text-sm text-red-400">
          Error loading backtests: {errorMsg}
        </div>
      )}

      {runs.length === 0 && !errorMsg ? (
        <div className="rounded-xl border border-border bg-card p-12 text-center">
          <p className="text-base text-gray-300">No backtest runs found.</p>
          <p className="mt-1 text-sm text-gray-500">
            Execute a backtest via Dagster or the backtest CLI to view results here.
          </p>
        </div>
      ) : (
        <div className="overflow-x-auto rounded-xl border border-border bg-card">
          <table className="w-full text-left text-sm">
            <thead className="border-b border-border bg-muted/40 text-xs font-semibold uppercase text-gray-400">
              <tr>
                <th className="px-4 py-3">Strategy</th>
                <th className="px-4 py-3">Universe</th>
                <th className="px-4 py-3">Time Horizon</th>
                <th className="px-4 py-3 text-right">CAGR (10Y M)</th>
                <th className="px-4 py-3 text-right">Sharpe (3Y D)</th>
                <th className="px-4 py-3 text-right">Max DD (3Y D)</th>
                <th className="px-4 py-3 text-right">Turnover</th>
                <th className="px-4 py-3 text-center">Status</th>
                <th className="px-4 py-3 text-right">Action</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border text-gray-200">
              {runs.map((r) => (
                <tr key={r.run_id} className="hover:bg-muted/20">
                  <td className="px-4 py-3 font-medium text-white">
                    <div>{r.strategy_key}</div>
                    <div className="text-xs text-gray-500">{r.strategy_version}</div>
                  </td>
                  <td className="px-4 py-3 font-mono text-xs text-gray-400">{r.universe_id}</td>
                  <td className="px-4 py-3 text-xs text-gray-400">
                    {r.start_date} ~ {r.end_date}
                  </td>
                  <td className="px-4 py-3 text-right font-mono font-medium text-emerald-400">
                    {formatPercentFromFraction(r.cagr_monthly) ?? "—"}
                  </td>
                  <td className="px-4 py-3 text-right font-mono">
                    {formatRatio(r.sharpe_daily) ?? "—"}
                  </td>
                  <td className="px-4 py-3 text-right font-mono text-rose-400">
                    {formatPercentFromFraction(r.max_dd_daily) ?? "—"}
                  </td>
                  <td className="px-4 py-3 text-right font-mono text-gray-400">
                    {formatRatio(r.turnover_monthly) ?? "—"}
                  </td>
                  <td className="px-4 py-3 text-center">
                    <span
                      className={`inline-flex rounded-full px-2 py-0.5 text-xs font-medium ${
                        r.status === "succeeded"
                          ? "bg-emerald-500/10 text-emerald-400 border border-emerald-500/20"
                          : r.status === "running" || r.status === "pending"
                          ? "bg-amber-500/10 text-amber-400 border border-amber-500/20"
                          : "bg-rose-500/10 text-rose-400 border border-rose-500/20"
                      }`}
                    >
                      {r.status}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-right">
                    <Link
                      href={`/research/backtest/${r.run_id}`}
                      className="rounded bg-accent/20 px-2.5 py-1 text-xs font-semibold text-accent hover:bg-accent/30"
                    >
                      View Report
                    </Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
