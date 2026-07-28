/**
 * #494: THE single human-readable translation of strategy exclusion reasons
 * (mirrors truealpha_contracts.strategy.ExclusionReason, which the mart rows
 * carry verbatim). One map, reused by /research/coverage rows, strategy
 * decision labels, and tooltips — tests/exclusion-reasons.test.ts asserts
 * every reason the deployed strategy can emit has an entry, so a new enum
 * member fails the test, not the page.
 */

export const EXCLUSION_REASON_LABEL: Record<string, string> = {
  missing_gross_profit_fact:
    "Gross profit not in the warehouse yet (financial-sector SEC tags need their own mapping)",
  missing_total_assets_fact: "Total assets fact not captured yet",
  missing_headcount_disclosure: "Headcount disclosure not captured yet",
  missing_labor_cost_disclosure: "Labor-cost disclosure not captured yet",
  missing_revenue_fact: "Revenue fact not captured yet",
  missing_market_value_input: "Market-value inputs (shares × last close) incomplete",
  missing_risk_free_rate_parameter: "Risk-free rate parameter missing for this cutoff",
  below_confidence_floor: "Input confidence below the strategy's floor",
  stale_required_input: "A required input is older than the strategy allows",
  nonpositive_headcount: "Reported headcount is zero or negative",
  nonpositive_labor_cost: "Reported labor cost is zero or negative",
  nonpositive_revenue: "Reported revenue is zero or negative",
};

export function exclusionLabel(reason: string | null): string | null {
  if (reason === null) return null;
  return EXCLUSION_REASON_LABEL[reason] ?? reason;
}
