import Link from "next/link";
import { listBacktestRuns } from "@/server/mart/backtest-repository";
import { formatCell, formatPercentFromFraction, formatRatio } from "@/client/format";
import { BacktestLimitations, backtestStatusStyle, runLimitations } from "@/components/backtest-basis";
import { ResearchTable } from "@/components/research-table";

export const dynamic = "force-dynamic";

function MetricTd({ raw, text, cls }: { raw: number | null; text: string | null; cls: string }) {
  return (
    <td className={`px-4 py-3 text-right font-mono ${cls}`} title={raw !== null ? String(raw) : undefined}>
      {formatCell(text)}
    </td>
  );
}

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
      <div>
        <h1 id="backtest-list-heading" className="text-2xl font-bold tracking-tight">Portfolio Backtests</h1>
        <p className="mt-1 text-sm text-gray-400">
          Quarter-end backtests of the core strategy on point-in-time inputs. Each run states its price basis and its claim ceiling.
        </p>
      </div>

      {errorMsg && (
        <div className="rounded-lg border border-red-500/30 bg-red-500/10 p-4 text-sm text-red-400">
          Error loading backtests: {errorMsg}
        </div>
      )}

      {runs.length === 0 && !errorMsg ? (
        <div className="rounded-xl border border-border bg-card p-12 text-center">
          <p className="text-base text-gray-300">No backtest runs found.</p>
          <p className="mt-1 text-sm text-gray-500">Execute a backtest via Dagster or the backtest CLI to view results here.</p>
        </div>
      ) : (
        <ResearchTable
          className="overflow-x-auto rounded-xl border border-border bg-card"
          theadClassName="border-b border-border bg-muted/40 text-xs font-semibold uppercase text-gray-400"
          tableClassName="w-full text-left text-sm"
          headers={[
            "Strategy",
            "Universe",
            "Time Horizon",
            "Basis",
            ...["CAGR", "Benchmark CAGR", "Sharpe (rf 0)", "Max DD", "Turnover"].map((label) => ({ label, className: "text-right" })),
            { label: "Status", className: "text-center" },
            { label: "Action", className: "text-right" },
          ]}
        >
          {runs.map((r) => (
            <tr key={r.run_id} data-run-id={r.run_id} className="border-t border-border text-gray-200 hover:bg-muted/20">
              <td className="px-4 py-3 font-medium text-white">
                <div>{r.strategy_key}</div>
                <div className="text-xs text-gray-500">{r.strategy_version}</div>
              </td>
              <td className="px-4 py-3 font-mono text-xs text-gray-400">{r.universe_id}</td>
              <td className="px-4 py-3 text-xs text-gray-400">{r.start_date} ~ {r.end_date}</td>
              <td className="px-4 py-3 font-mono text-xs text-gray-400">
                <div data-price-basis={r.price_basis ?? ""}>{r.price_basis ?? "not recorded"}</div>
                <div className="text-amber-400" data-claim-ceiling={r.claim_ceiling ?? ""}>{r.claim_ceiling ?? "not recorded"}</div>
                <details className="mt-1 font-sans text-gray-400">
                  <summary>Limitations ({runLimitations(r).length})</summary>
                  <BacktestLimitations run={r} />
                </details>
              </td>
              <MetricTd raw={r.cagr_monthly} text={formatPercentFromFraction(r.cagr_monthly)} cls="font-medium text-emerald-400" />
              <MetricTd raw={r.benchmark_cagr} text={formatPercentFromFraction(r.benchmark_cagr)} cls="text-gray-300" />
              <MetricTd raw={r.sharpe_daily} text={formatRatio(r.sharpe_daily)} cls="" />
              <MetricTd raw={r.max_dd_daily} text={formatPercentFromFraction(r.max_dd_daily)} cls="text-rose-400" />
              <MetricTd raw={r.turnover_monthly} text={formatRatio(r.turnover_monthly)} cls="text-gray-400" />
              <td className="px-4 py-3 text-center">
                <span className={`inline-flex rounded-full px-2 py-0.5 text-xs font-medium ${backtestStatusStyle(r.status)}`}>{r.status}</span>
              </td>
              <td className="px-4 py-3 text-right">
                <Link href={`/research/backtest/${encodeURIComponent(r.run_id)}`} className="rounded bg-accent/20 px-2.5 py-1 text-xs font-semibold text-accent hover:bg-accent/30">
                  View Report
                </Link>
              </td>
            </tr>
          ))}
        </ResearchTable>
      )}
    </section>
  );
}
