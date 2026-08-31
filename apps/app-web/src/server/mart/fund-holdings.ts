/**
 * Reader for mart.fund_holdings (#63 first tranche): each fund's NEWEST filed
 * vintage, weights as the fund filed them. Older vintages stay queryable in the
 * view; this loader deliberately answers only "what does the fund say now" —
 * the longitudinal reader is a later tranche.
 *
 * mart-only by construction (#362): the view projects staging on the reader's
 * behalf, this module never names a staging table.
 */

import { withMartReadonly } from "./db";

export type FundHoldingRow = {
  holdingName: string;
  isin: string | null;
  weightPct: string | null;
  valueUsd: string | null;
};

export type FundHoldingsVintage = {
  fundId: string;
  fundName: string;
  reportPeriod: string;
  filedOn: string;
  weightSumPct: string;
  lines: FundHoldingRow[];
};

const NEWEST_VINTAGE_SQL = `
  with newest as (
    select fund_id, max(report_period) as report_period
    from mart.fund_holdings
    group by fund_id
  )
  select h.fund_id,
         coalesce(h.fund_name, h.fund_id) as fund_name,
         to_char(h.report_period, 'YYYY-MM-DD') as report_period,
         to_char(max(h.transaction_time) over (partition by h.fund_id), 'YYYY-MM-DD') as filed_on,
         h.holding_name,
         h.isin,
         h.percent_of_net_assets::text as weight_pct,
         h.value_usd::text as value_usd
  from mart.fund_holdings h
  join newest using (fund_id, report_period)
  order by h.fund_id, h.percent_of_net_assets desc nulls last, h.holding_name
`;

export async function loadFundHoldings(): Promise<FundHoldingsVintage[]> {
  const rows = await withMartReadonly(async (client) => {
    const result = await client.query<{
      fund_id: string;
      fund_name: string;
      report_period: string;
      filed_on: string;
      holding_name: string;
      isin: string | null;
      weight_pct: string | null;
      value_usd: string | null;
    }>(NEWEST_VINTAGE_SQL);
    return result.rows;
  });

  const byFund = new Map<string, FundHoldingsVintage>();
  for (const row of rows) {
    let vintage = byFund.get(row.fund_id);
    if (!vintage) {
      vintage = {
        fundId: row.fund_id,
        fundName: row.fund_name,
        reportPeriod: row.report_period,
        filedOn: row.filed_on,
        weightSumPct: "0",
        lines: [],
      };
      byFund.set(row.fund_id, vintage);
    }
    vintage.lines.push({
      holdingName: row.holding_name,
      isin: row.isin,
      weightPct: row.weight_pct,
      valueUsd: row.value_usd,
    });
  }
  for (const vintage of byFund.values()) {
    const sum = vintage.lines.reduce((total, line) => total + (line.weightPct ? Number(line.weightPct) : 0), 0);
    vintage.weightSumPct = sum.toFixed(2);
  }
  return [...byFund.values()].sort((a, b) => a.fundName.localeCompare(b.fundName));
}
