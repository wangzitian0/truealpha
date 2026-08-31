/**
 * The valuation face of the holdings reader (#63, B-phase step 2): each fund's
 * newest filed weights joined to the governed TOPT valuation run — a join over
 * two materialized planes, never a computation. Fund-level weighted aggregates
 * are deliberately NOT computed here: a weighted valuation is a metric, and
 * metrics live in libs/factors (stated on #63); this surface reports the
 * per-holding join plus explicit coverage mass so nothing reads as complete
 * that is not.
 *
 * Head resolution mirrors topt-gppe-repository.ts / truealpha_contracts.topt_read
 * (pointer first, acceptance fallback), additionally scoped to the QQQ universe:
 * this surface prices QQQ's holdings, and the newest pointer across ALL
 * universes can belong to the canary. A fund->universe registry is the
 * multi-fund tranche's problem.
 */

import { withMartReadonly } from "./db";
import type { MartClientLike } from "./topt-gppe-repository";

export type ValuedHoldingRow = {
  holdingName: string;
  ticker: string | null;
  weightPct: string | null;
  currentPs: string | null;
  targetPsMidpoint: string | null;
  valuationGap: string | null;
  tier: string | null;
  availability: string | null;
};

export type FundValuation = {
  fundId: string;
  fundName: string;
  reportPeriod: string;
  runId: string | null;
  valuedWeightPct: string;
  resolvedWeightPct: string;
  totalWeightPct: string;
  lines: ValuedHoldingRow[];
};

const QQQ_POINTER_HEAD_SQL = `
  select target_run_id as run_id from mart.current_pointer_head
  where environment = 'production' and factor_id = 'gross_profit_per_employee'
    and universe_id like 'universe:qqq-us-%'
  order by advanced_at desc limit 1
`;

const QQQ_ACCEPTANCE_FALLBACK_HEAD_SQL = `
  select s.run_id
  from mart.topt_capture_status s
  join mart.datahub_quality_report q on q.run_id = s.run_id
  where s.environment = 'production' and s.complete
    and s.universe_id like 'universe:qqq-us-%'
  order by q.created_at desc, q.report_id desc limit 1
`;

const VALUED_LINES_SQL = `
  select v.fund_id,
         coalesce(v.fund_name, v.fund_id) as fund_name,
         to_char(v.report_period, 'YYYY-MM-DD') as report_period,
         v.holding_name,
         v.ticker,
         v.percent_of_net_assets::text as weight_pct,
         r.current_ps::text as current_ps,
         r.target_ps_midpoint::text as target_ps_midpoint,
         r.valuation_gap::text as valuation_gap,
         r.tier,
         r.availability
  from mart.fund_holdings_valuation v
  left join mart.topt_core_result_read r
    on r.listing_id = v.listing_id and r.run_id = $1
  order by v.fund_id, v.percent_of_net_assets desc nulls last, v.holding_name
`;

function pct(total: number): string {
  return total.toFixed(2);
}

export async function loadFundValuation(
  runWithClient: <T>(fn: (client: MartClientLike) => Promise<T>) => Promise<T> = withMartReadonly,
): Promise<FundValuation[]> {
  return runWithClient(async (client) => {
    let head = await client.query(QQQ_POINTER_HEAD_SQL);
    if (head.rows.length === 0) {
      head = await client.query(QQQ_ACCEPTANCE_FALLBACK_HEAD_SQL);
    }
    const runId = head.rows.length > 0 ? String(head.rows[0].run_id) : null;
    // No governed run yet: the join matches nothing and every valuation column
    // renders as absent — the filed weights still show, honestly unvalued.
    const result = await client.query(VALUED_LINES_SQL, [runId ?? "capture-run:none"]);
    const byFund = new Map<string, FundValuation & { valued: number; resolved: number; total: number }>();
    for (const row of result.rows) {
      const fundId = String(row.fund_id);
      let fund = byFund.get(fundId);
      if (!fund) {
        fund = {
          fundId,
          fundName: String(row.fund_name),
          reportPeriod: String(row.report_period),
          runId,
          valuedWeightPct: "0",
          resolvedWeightPct: "0",
          totalWeightPct: "0",
          lines: [],
          valued: 0,
          resolved: 0,
          total: 0,
        };
        byFund.set(fundId, fund);
      }
      const weight = row.weight_pct === null ? 0 : Number(row.weight_pct);
      fund.total += weight;
      if (row.ticker !== null) fund.resolved += weight;
      if (row.availability === "available") fund.valued += weight;
      fund.lines.push({
        holdingName: String(row.holding_name),
        ticker: row.ticker === null ? null : String(row.ticker),
        weightPct: row.weight_pct === null ? null : String(row.weight_pct),
        currentPs: row.current_ps === null ? null : String(row.current_ps),
        targetPsMidpoint: row.target_ps_midpoint === null ? null : String(row.target_ps_midpoint),
        valuationGap: row.valuation_gap === null ? null : String(row.valuation_gap),
        tier: row.tier === null ? null : String(row.tier),
        availability: row.availability === null ? null : String(row.availability),
      });
    }
    return [...byFund.values()]
      .map(({ valued, resolved, total, ...fund }) => ({
        ...fund,
        valuedWeightPct: pct(valued),
        resolvedWeightPct: pct(resolved),
        totalWeightPct: pct(total),
      }))
      .sort((a, b) => a.fundName.localeCompare(b.fundName));
  });
}
