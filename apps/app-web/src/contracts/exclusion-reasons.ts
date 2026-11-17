export const EXCLUSION_REASON_LABEL: Record<string, string> = {
  missing_gross_profit_fact:
    "No PIT-valid operating profit fact in SEC XBRL companyfacts (cost of revenue or insurance claims not tagged on consolidated us-gaap concepts before cutoff)",
  missing_total_assets_fact: "Total assets fact not captured yet",
  missing_headcount_disclosure: "Headcount disclosure not captured yet",
  missing_labor_cost_disclosure: "Labor-cost disclosure not captured yet",
  missing_revenue_fact: "Revenue fact not captured yet",
  missing_market_value_input:
    "No PIT-valid consolidated common share count or close price before cutoff (for example, multi-class share counts reported only on dimensional XBRL axes)",
  missing_risk_free_rate_parameter: "Risk-free rate parameter missing for this cutoff",
  missing_tangible_common_equity:
    "Banks: measured tangible common equity not captured yet (stockholders' equity, goodwill, intangibles)",
  missing_preferred_stock_value:
    "Banks: preferred stock value not captured yet (no zero stand-in is used)",
  negative_tangible_common_equity:
    "Banks: measured tangible common equity is negative, so no capital charge applies and no labor-efficiency value is given",
  missing_issuer_class: "Issuer has no operating class, so no labor-efficiency metric applies",
  below_confidence_floor: "Input confidence below the strategy's floor",
  stale_required_input: "A required input is older than the strategy allows",
  stale_period: "A required input's fiscal period ended earlier than the strategy allows",
  nonpositive_headcount: "Reported headcount is zero or negative",
  nonpositive_labor_cost: "Reported labor cost is zero or negative",
  nonpositive_revenue: "Reported revenue is zero or negative",
  nonpositive_gross_profit: "Reported gross profit is zero or negative, so P/GP has no value",
  plausibility_refused_node_sign_policy:
    "Refused by the plausibility gate: the sign of the operating metric breaks its definition",
  registry_unavailable:
    "The issuer registry gave no classification, so the run could not tell which operating profit applies to this issuer",
  primary_listing_undeclared:
    "The issuer has several listings and none is declared as its primary listing, so no listing values it",
};

export function exclusionLabel(reason: string | null): string | null {
  if (reason === null) return null;
  return EXCLUSION_REASON_LABEL[reason] ?? reason;
}

export interface DecisionUsabilityInput {
  readonly issuer_id: string;
  readonly outcome: string;
  readonly eligible: boolean;
  readonly exclusion_reason: string | null;
}

export interface ExcludedIssuerSummary {
  readonly issuer_id: string;
  readonly exclusion_reason: string | null;
  readonly label: string;
}

export interface UniverseUsabilitySummary<
  T extends DecisionUsabilityInput = DecisionUsabilityInput,
> {
  readonly evaluatedCount: number;
  readonly usableCount: number;
  readonly selectedCount: number;
  readonly excludedCount: number;
  readonly usable: readonly T[];
  readonly selections: readonly T[];
  readonly excluded: readonly ExcludedIssuerSummary[];
}

export function summarizeUniverseUsability<T extends DecisionUsabilityInput>(
  decisions: readonly T[],
): UniverseUsabilitySummary<T> {
  const selections = decisions.filter((d) => d.outcome === "selected");
  const usable = decisions.filter((d) => d.eligible && d.exclusion_reason === null);
  const excluded = decisions
    .filter((d) => !d.eligible || d.exclusion_reason !== null || d.outcome === "excluded")
    .map((d) => ({
      issuer_id: d.issuer_id,
      exclusion_reason: d.exclusion_reason,
      label: exclusionLabel(d.exclusion_reason) ?? "Excluded from universe",
    }));
  return {
    evaluatedCount: decisions.length,
    usableCount: usable.length,
    selectedCount: selections.length,
    excludedCount: excluded.length,
    usable,
    selections,
    excluded,
  };
}
