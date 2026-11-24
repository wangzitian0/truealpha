export const DASHBOARD_STRATEGY_ID = "large_model_value_v0";

export type PegGrowthConvention = "historical_cagr" | "consensus" | "guidance";
export const PEG_GROWTH_CONVENTIONS: readonly PegGrowthConvention[] = [
  "historical_cagr",
  "consensus",
  "guidance",
];
export const PEG_CONVENTION_LABELS: Record<PegGrowthConvention, string> = {
  historical_cagr: "Historical CAGR",
  consensus: "Consensus",
  guidance: "Guidance",
};

export function valuationBasisLabel(basis: string | null | undefined): string {
  if (basis === "price_to_gross_profit") return "P/GP";
  if (basis === "price_to_sales") return "P/S";
  return "multiple";
}

export const WIDE_PANEL_INPUT_COLUMNS = [
  { key: "gross_profit", label: "Gross profit", monetary: true },
  { key: "pre_provision_profit", label: "Pre-provision profit", monetary: true },
  { key: "revenue_total", label: "Revenue", monetary: true },
  { key: "total_assets", label: "Total assets", monetary: true },
  { key: "stockholders_equity", label: "Stockholders equity", monetary: true },
  { key: "tangible_common_equity", label: "Tangible common equity", monetary: true },
  { key: "employees_total", label: "Employees", monetary: false },
  { key: "last_close", label: "Market close", monetary: true },
  { key: "shares_outstanding", label: "Shares outstanding", monetary: false },
] as const;

export function classifiedShare(row: {
  in_theme_revenue?: string | null;
  out_of_theme_revenue?: string | null;
  consolidated_revenue?: string | null;
  partition_residual?: string | null;
}): string | null {
  const inRev = Number(row.in_theme_revenue ?? 0);
  const outRev = Number(row.out_of_theme_revenue ?? 0);
  const total = Number(row.consolidated_revenue ?? 0);
  if (!Number.isFinite(total) || total <= 0) return null;
  // Gross segments with an intersegment elimination as the residual count against their own sum (#1851).
  const accounted = total - Math.min(Number(row.partition_residual ?? 0) || 0, 0);
  return (((inRev + outRev) / accounted) * 100).toFixed(1) + "%";
}
