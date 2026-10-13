/**
 * #758: what a backtest run can claim. The price basis, the claim ceiling, the strategy
 * definition and the benchmark come from mart.backtest_runs as written; this component only
 * formats them. A run written before #758 has no basis, and the panel says so.
 */

import type { BacktestRunRecord } from "@/server/mart/backtest-repository";
import { formatPercentFromFraction } from "@/client/format";

const PRICE_BASIS_LABELS: Record<string, string> = {
  unadjusted_open_explicit_splits:
    "Unadjusted prices. Orders fill at the next session open. Splits apply once to held shares.",
};

const CLAIM_CEILING_LABELS: Record<string, string> = {
  price_return_no_dividends: "Price return only. Dividends are not included.",
};

const BENCHMARK_LABELS: Record<string, string> = {
  equal_weight_buy_and_hold_first_cutoff_issuers:
    "Equal weight over the issuers with inputs at the first cutoff. Each is bought at the first fill session open. A symbol without an open on that session is bought at its next open. Then all are held, on the same prices and costs.",
};

export function describe(labels: Record<string, string>, value: string | null): string {
  if (value === null) return "Not recorded for this run.";
  return labels[value] ?? value;
}

/** The limitations the nightly lane stored with the run (#758). A run before #758 has none. */
export function runLimitations(run: BacktestRunRecord): string[] {
  const stored = (run.metrics_payload ?? {})["limitations"];
  return Array.isArray(stored) ? stored.map((item) => String(item)) : [];
}

export function BacktestLimitations({ run }: { run: BacktestRunRecord }) {
  const limitations = runLimitations(run);
  return (
    <ul className="list-disc space-y-1 pl-4" data-backtest-limitations={limitations.length}>
      {limitations.length === 0 ? (
        <li>No limitations were recorded for this run.</li>
      ) : (
        limitations.map((item) => <li key={item}>{item}</li>)
      )}
    </ul>
  );
}

/** Benchmark symbols that no open ever bought, so their weight stayed cash (#758). */
export function benchmarkNeverFilled(run: BacktestRunRecord): string[] {
  const benchmark = (run.metrics_payload ?? {})["benchmark"];
  const stored =
    benchmark !== null && typeof benchmark === "object"
      ? (benchmark as Record<string, unknown>)["never_filled"]
      : undefined;
  return Array.isArray(stored) ? stored.map((item) => String(item)) : [];
}

function percent(value: number | null): string {
  return (value !== null ? formatPercentFromFraction(String(value)) : null) ?? "—";
}

export function BacktestBasis({ run }: { run: BacktestRunRecord }) {
  return (
    <div className="rounded-xl border border-amber-500/30 bg-amber-500/5 p-4 text-xs text-gray-300" data-backtest-basis>
      <dl className="grid grid-cols-1 gap-3 md:grid-cols-2">
        <div>
          <dt className="font-semibold uppercase tracking-wider text-gray-400">Price basis</dt>
          <dd data-price-basis={run.price_basis ?? ""}>
            {describe(PRICE_BASIS_LABELS, run.price_basis)}
            {run.price_basis && <span className="ml-1 font-mono text-gray-500">({run.price_basis})</span>}
          </dd>
        </div>
        <div>
          <dt className="font-semibold uppercase tracking-wider text-gray-400">Claim ceiling</dt>
          <dd data-claim-ceiling={run.claim_ceiling ?? ""}>
            {describe(CLAIM_CEILING_LABELS, run.claim_ceiling)}
            {run.claim_ceiling && <span className="ml-1 font-mono text-gray-500">({run.claim_ceiling})</span>}
          </dd>
        </div>
        <div>
          <dt className="font-semibold uppercase tracking-wider text-gray-400">Benchmark</dt>
          <dd data-benchmark-rule={run.benchmark_rule ?? ""}>
            {describe(BENCHMARK_LABELS, run.benchmark_rule)} CAGR {percent(run.benchmark_cagr)}, max drawdown{" "}
            {percent(run.benchmark_max_dd)}.
            {benchmarkNeverFilled(run).length > 0 && (
              <span className="mt-1 block text-amber-400" data-benchmark-never-filled={benchmarkNeverFilled(run).join(",")}>
                Never bought, so held as cash for the whole run: {benchmarkNeverFilled(run).join(", ")}.
              </span>
            )}
          </dd>
        </div>
        <div>
          <dt className="font-semibold uppercase tracking-wider text-gray-400">Strategy definition</dt>
          <dd className="font-mono text-gray-400" data-definition-sha256={run.definition_content_sha256 ?? ""}>
            {run.definition_content_sha256 ?? "Not recorded for this run."}
          </dd>
        </div>
      </dl>
      <div className="mt-3">
        <div className="font-semibold uppercase tracking-wider text-gray-400">Limitations</div>
        <BacktestLimitations run={run} />
      </div>
    </div>
  );
}
