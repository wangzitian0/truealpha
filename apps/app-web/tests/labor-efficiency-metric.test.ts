/**
 * #1176, #1518: every labor-efficiency number on a research page carries the name of the metric it
 * was computed with. The name is written by the strategy evaluator and read from
 * mart.strategy_decisions. The page never derives it from the issuer class and keeps no name of its own.
 *
 * No database: the row is the shape DECISIONS_SQL returns, mapped by the real decisionFromRow.
 * Run standalone: `bun run tests/labor-efficiency-metric.test.ts`.
 */

import { laborEfficiencyLabels, UNRECORDED_METRIC } from "../src/contracts/laborEfficiency";
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

// 3. Entity page, FINANCIAL issuer, definition 1.1.0 (#1518): the core and the composite evaluate the
// one pinned tree, so the published value and the strategy value agree. The one number names the pin
// that the decision row states: the banking tree, never the uniform metric.
assertSameJson(
  laborEfficiencyLabels({
    publishedGppe: "227476.2018",
    strategyValue: bank.capital_adjusted_labor_efficiency,
    strategyMetric: bank.labor_efficiency_metric ?? null,
  }),
  [{ metric: "gppe_banking_tce_v1", value: "227476.2018" }],
  "a FINANCIAL issuer under 1.1.0 must show its one value under the name of the pinned tree",
);

// 3b. A published value from before 1.1.0 (the core charged total assets) disagrees with the strategy
// value. The row names no metric, so the page says so. It never names the uniform metric for it.
assertSameJson(
  laborEfficiencyLabels({
    publishedGppe: "-422081.43",
    strategyValue: bank.capital_adjusted_labor_efficiency,
    strategyMetric: bank.labor_efficiency_metric ?? null,
  }),
  [
    { metric: UNRECORDED_METRIC, value: "-422081.43" },
    { metric: "gppe_banking_tce_v1", value: "227476.20" },
  ],
  "a published value that disagrees with the strategy value must not borrow a name",
);

// 4. Uniform issuer: the published value and the strategy value are the same metric and number.
const uniform = decisionFromRow(bankRow({ labor_efficiency_metric: "gppe_uniform_charge_v0", capital_adjusted_labor_efficiency: "2563276.38" }));
assertSameJson(
  laborEfficiencyLabels({
    publishedGppe: "2563276.38",
    strategyValue: uniform.capital_adjusted_labor_efficiency,
    strategyMetric: uniform.labor_efficiency_metric ?? null,
  }),
  [{ metric: "gppe_uniform_charge_v0", value: "2563276.38" }],
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

// 7. #1358 AC3: scale-4 publishedGppe and scale-2 strategyValue of the same metric collapse to one entry.
assertSameJson(
  laborEfficiencyLabels({
    publishedGppe: "76328.6381",
    strategyValue: "76328.64",
    strategyMetric: "gppe_uniform_charge_v0",
  }),
  [{ metric: "gppe_uniform_charge_v0", value: "76328.6381" }],
  "scale-4 publishedGppe and scale-2 rounded strategyValue of the same metric collapse to one entry",
);

// 8. Blank/whitespace strings are not parsed as numeric 0 and never falsely deduplicate against "0".
assertSameJson(
  laborEfficiencyLabels({
    publishedGppe: "0",
    strategyValue: "   ",
    strategyMetric: "gppe_uniform_charge_v0",
  }),
  [
    { metric: UNRECORDED_METRIC, value: "0" },
    { metric: "gppe_uniform_charge_v0", value: "   " },
  ],
  "whitespace strategyValue must not coerce to 0 and collapse into publishedGppe 0",
);

// 9. #1518 review: a non-bank excluded after its tree was chosen has a published value and no strategy value. The
// class has one metric, and the decision row names it, so the published value carries that name.
assertSameJson(
  laborEfficiencyLabels({
    publishedGppe: "2563276.38",
    strategyValue: null,
    strategyMetric: "gppe_uniform_charge_v0",
    operatingBranch: "non_financial",
  }),
  [{ metric: "gppe_uniform_charge_v0", value: "2563276.38" }],
  "a non-financial published value with no strategy value takes the metric name of the decision row",
);
assertSameJson(
  laborEfficiencyLabels({ publishedGppe: "2563276.38", strategyValue: null, strategyMetric: null, operatingBranch: "non_financial" }),
  [{ metric: UNRECORDED_METRIC, value: "2563276.38" }],
  "no row names a metric, so the page says so",
);

// 10. #1518 review (second round): a bank published BEFORE definition 1.1.0 holds the old total-assets value
// (JPM -422081.43), while its decision row names the banking metric. With no strategy value to compare with,
// the page cannot show that the published number is the banking one. It must not show it under the banking name.
assertSameJson(
  laborEfficiencyLabels({
    publishedGppe: "-422081.43",
    strategyValue: null,
    strategyMetric: "gppe_banking_tce_v1",
    operatingBranch: "financial",
  }),
  [{ metric: UNRECORDED_METRIC, value: "-422081.43" }],
  "a bank's published value with no strategy value is not named for the banking metric",
);
// The class is unknown: the page cannot say that the class has one metric either.
assertSameJson(
  laborEfficiencyLabels({
    publishedGppe: "-422081.43",
    strategyValue: null,
    strategyMetric: "gppe_banking_tce_v1",
    operatingBranch: null,
  }),
  [{ metric: UNRECORDED_METRIC, value: "-422081.43" }],
  "a published value of an unknown class with no strategy value is not named",
);
// A bank whose published and strategy values agree (definition 1.1.0) takes the banking name.
assertSameJson(
  laborEfficiencyLabels({
    publishedGppe: "227476.20",
    strategyValue: "227476.20",
    strategyMetric: "gppe_banking_tce_v1",
    operatingBranch: "financial",
  }),
  [{ metric: "gppe_banking_tce_v1", value: "227476.20" }],
  "a bank whose two values agree shows one value under the banking name",
);

console.log("labor-efficiency-metric: 14 checks passed");

