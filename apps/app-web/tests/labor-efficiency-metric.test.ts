/**
 * #1176: every labor-efficiency number on a research page carries the name of the metric it
 * was computed with. The name is written by the strategy evaluator and read from
 * mart.strategy_decisions. The page never derives it from the issuer class.
 *
 * No database: the row is the shape DECISIONS_SQL returns, mapped by the real decisionFromRow.
 * Run standalone: `bun run tests/labor-efficiency-metric.test.ts`.
 */

import { laborEfficiencyLabels, PUBLISHED_GPPE_METRIC, UNRECORDED_METRIC } from "../src/contracts/laborEfficiency";
import { decisionFromRow } from "../src/server/mart/strategy-run-repository";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

function assertSameJson(actual: unknown, expected: unknown, message: string): void {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  assert(a === e, `${message}: expected ${e}, got ${a}`);
}

// A FINANCIAL bank row as DECISIONS_SQL returns it after #1176. JPM FY2025 banking v1 value.
function bankRow(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    issuer_id: "issuer:jpm",
    cutoff_at: "2026-06-30T23:59:59Z",
    outcome: "rejected_valuation_above_tier_band",
    eligible: true,
    tier: "traditional",
    capital_adjusted_labor_efficiency: "227476.20",
    current_price_to_sales: "4.8388",
    target_price_to_sales: "1.1500",
    valuation_gap: "-0.7623",
    confidence: "0.9",
    exclusion_reason: null,
    rank: null,
    target_weight: null,
    peg: null,
    peg_rank: null,
    labor_efficiency_metric: "gppe_banking_tce_v1",
    ...overrides,
  };
}

// 1. The read model carries the name the writer stored, for a FINANCIAL decision row.
const bank = decisionFromRow(bankRow());
assert(
  bank.labor_efficiency_metric === "gppe_banking_tce_v1",
  `read model dropped the metric name: got ${String(bank.labor_efficiency_metric)}`,
);

// 2. A row written before #1176 has no name. The read model says so with null, not a guess.
const legacy = decisionFromRow(bankRow({ labor_efficiency_metric: undefined }));
assert(legacy.labor_efficiency_metric === null, "a row without the column must read as null");

// 3. Entity page, FINANCIAL issuer: the published uniform value and the strategy value, each named.
assertSameJson(
  laborEfficiencyLabels({
    publishedGppe: "-422081.43",
    strategyValue: bank.capital_adjusted_labor_efficiency,
    strategyMetric: bank.labor_efficiency_metric ?? null,
  }),
  [
    { metric: "gppe_uniform_charge_v0", value: "-422081.43" },
    { metric: "gppe_banking_tce_v1", value: "227476.20" },
  ],
  "a FINANCIAL issuer must show both named values when they differ",
);

// 4. Uniform issuer: the published value and the strategy value are the same metric and number.
const uniform = decisionFromRow(bankRow({ labor_efficiency_metric: "gppe_uniform_charge_v0", capital_adjusted_labor_efficiency: "2563276.38" }));
assertSameJson(
  laborEfficiencyLabels({
    publishedGppe: "2563276.38",
    strategyValue: uniform.capital_adjusted_labor_efficiency,
    strategyMetric: uniform.labor_efficiency_metric ?? null,
  }),
  [{ metric: PUBLISHED_GPPE_METRIC, value: "2563276.38" }],
  "equal metric and value collapse to one labelled number",
);

// 5. A strategy value with no recorded name is labelled as unrecorded, never shown bare.
assertSameJson(
  laborEfficiencyLabels({ publishedGppe: null, strategyValue: "227476.20", strategyMetric: legacy.labor_efficiency_metric }),
  [{ metric: UNRECORDED_METRIC, value: "227476.20" }],
  "a strategy value without a recorded metric name must say so",
);

// 6. Nothing is shown without a name.
for (const label of laborEfficiencyLabels({ publishedGppe: "1", strategyValue: "2", strategyMetric: null })) {
  assert(label.metric.length > 0, "every displayed number needs a metric name");
}

console.log("labor-efficiency-metric: 6 checks passed");
