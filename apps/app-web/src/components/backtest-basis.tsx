import type { ReactNode } from "react";
import type { BacktestRunRecord } from "@/server/mart/backtest-repository";
import { formatCell, formatPercentFromFraction } from "@/client/format";

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

const describe = (labels: Record<string, string>, value: string | null): string =>
  value === null ? "Not recorded for this run." : (labels[value] ?? value);

export function backtestStatusStyle(status: string): string {
  if (status === "succeeded") return "bg-emerald-500/10 text-emerald-400 border border-emerald-500/20";
  if (status === "failed") return "bg-rose-500/10 text-rose-400 border border-rose-500/20";
  return "bg-amber-500/10 text-amber-400 border border-amber-500/20";
}

/** The limitations the nightly lane stored with the run (#758). A run before #758 has none. */
export function runLimitations(run: BacktestRunRecord): string[] {
  const stored = (run.metrics_payload ?? {})["limitations"];
  return Array.isArray(stored) ? stored.map(String) : [];
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
function benchmarkNeverFilled(run: BacktestRunRecord): string[] {
  const benchmark = (run.metrics_payload ?? {})["benchmark"];
  const stored = benchmark !== null && typeof benchmark === "object" ? (benchmark as Record<string, unknown>)["never_filled"] : undefined;
  return Array.isArray(stored) ? stored.map(String) : [];
}

function BasisItem({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <dt className="font-semibold uppercase tracking-wider text-gray-400">{label}</dt>
      {children}
    </div>
  );
}

export function BacktestBasis({ run }: { run: BacktestRunRecord }) {
  const neverFilled = benchmarkNeverFilled(run);
  return (
    <div className="rounded-xl border border-amber-500/30 bg-amber-500/5 p-4 text-xs text-gray-300" data-backtest-basis>
      <dl className="grid grid-cols-1 gap-3 md:grid-cols-2">
        <BasisItem label="Price basis">
          <dd data-price-basis={run.price_basis ?? ""}>
            {describe(PRICE_BASIS_LABELS, run.price_basis)}
            {run.price_basis && <span className="ml-1 font-mono text-gray-500">({run.price_basis})</span>}
          </dd>
        </BasisItem>
        <BasisItem label="Claim ceiling">
          <dd data-claim-ceiling={run.claim_ceiling ?? ""}>
            {describe(CLAIM_CEILING_LABELS, run.claim_ceiling)}
            {run.claim_ceiling && <span className="ml-1 font-mono text-gray-500">({run.claim_ceiling})</span>}
          </dd>
        </BasisItem>
        <BasisItem label="Benchmark">
          <dd data-benchmark-rule={run.benchmark_rule ?? ""}>
            {describe(BENCHMARK_LABELS, run.benchmark_rule)} CAGR {formatCell(formatPercentFromFraction(run.benchmark_cagr))}, max drawdown{" "}
            {formatCell(formatPercentFromFraction(run.benchmark_max_dd))}.
            {neverFilled.length > 0 && (
              <span className="mt-1 block text-amber-400" data-benchmark-never-filled={neverFilled.join(",")}>
                Never bought, so held as cash for the whole run: {neverFilled.join(", ")}.
              </span>
            )}
          </dd>
        </BasisItem>
        <BasisItem label="Strategy definition">
          <dd className="font-mono text-gray-400" data-definition-sha256={run.definition_content_sha256 ?? ""}>
            {run.definition_content_sha256 ?? "Not recorded for this run."}
          </dd>
        </BasisItem>
      </dl>
      <div className="mt-3">
        <div className="font-semibold uppercase tracking-wider text-gray-400">Limitations</div>
        <BacktestLimitations run={run} />
      </div>
    </div>
  );
}
