/**
 * The valuation face of the holdings reader (#63, B-phase step 2): each fund's
 * newest filed weights joined to the governed TOPT valuation run — a join over
 * two materialized planes, never a computation.
 *
 * Fund-level aggregates are NOT computed here, and since #727 that is true of the
 * code and not only of this comment. The weighted valuation gap and the three
 * coverage masses are module-5 factor outputs (`factors.base.etf_virtual_company`)
 * materialized into `mart.fund_virtual_company` by the tick that produced the core
 * rows they weight; this file reads those columns. init.md §1 rule 2: a weight-weighted
 * mean across a fund's holdings spans two tables and aggregates across rows, so it is a
 * metric and metrics live in libs/factors. The previous version computed it in a window
 * function here, against this header — the drift #727 recorded.
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
  /** Coverage masses as the module-5 factor measured them; "0.00" when the tick
   * materialized no consolidation row for this fund and run. */
  valuedWeightPct: string;
  resolvedWeightPct: string;
  totalWeightPct: string;
  /** Weighted mean valuation gap over the valued mass, read from
   * mart.fund_virtual_company. Null when the factor REFUSED the aggregate on coverage
   * (see availabilityStatus/reasonCodes) or when no consolidation row exists. */
  weightedGap: string | null;
  /** The three §8 status dimensions of the consolidation row (#747); null when the
   * tick wrote no row for this fund — an absent aggregate, not a failed one. */
  availabilityStatus: string | null;
  sourceEvidenceStatus: string | null;
  factorValidationStatus: string | null;
  reasonCodes: string[];
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

/** The fund-level row the tick materialized for this run (#727): the aggregate and
 * its coverage masses are read, never recomputed here. `availability_status` says
 * whether the consolidation was published or refused on coverage, and `reason_codes`
 * says which floor refused it — the same §8 dimensions every other factor row carries. */
const FUND_CONSOLIDATION_SQL = `
  select fund_id,
         weighted_valuation_gap::text as weighted_gap,
         valued_weight_pct::text as valued_weight_pct,
         availability_status,
         source_evidence_status,
         factor_validation_status,
         reason_codes
  from mart.fund_virtual_company
  where run_id = $1
`;

/** The filed and listing-resolved mass of each fund's newest vintage. Run-independent:
 * a fund filed its weights whether or not a governed run has valued it, so these render
 * even with no head. The aggregation lives in the view, not in this layer (init.md §1
 * rule 2 governs the App layer; a mart view is the database's own read model). */
const FUND_COVERAGE_SQL = `
  select distinct on (fund_id)
         fund_id,
         total_weight_pct::text as total_weight_pct,
         resolved_weight_pct::text as resolved_weight_pct
  from mart.fund_holdings_coverage
  order by fund_id, transaction_time desc, report_period desc
`;

/** The masses arrive as exact numerics the factor measured and the database stored
 * (review on #699 — a JS float accumulator drifts); formatting is the only conversion. */
function pct(value: unknown): string {
  return typeof value === "string" ? Number(value).toFixed(2) : "0.00";
}

type ConsolidationRow = {
  weightedGap: string | null;
  valuedWeightPct: string;
  availabilityStatus: string | null;
  sourceEvidenceStatus: string | null;
  factorValidationStatus: string | null;
  reasonCodes: string[];
};

export async function loadFundValuation(
  runWithClient: <T>(fn: (client: MartClientLike) => Promise<T>) => Promise<T> = withMartReadonly,
): Promise<FundValuation[]> {
  return runWithClient(async (client) => {
    let head = await client.query(QQQ_POINTER_HEAD_SQL);
    if (head.rows.length === 0) {
      head = await client.query(QQQ_ACCEPTANCE_FALLBACK_HEAD_SQL);
    }
    const rawRunId = head.rows.length > 0 ? head.rows[0].run_id : null;
    // Fail toward "no run" on a malformed head row: String(null) would forge
    // the literal "null" into the join parameter (review on #699).
    const runId = typeof rawRunId === "string" && rawRunId.length > 0 ? rawRunId : null;
    // No governed run yet: the join matches nothing and every valuation column
    // renders as absent — the filed weights still show, honestly unvalued.
    const joinRunId = runId ?? "capture-run:none";
    const result = await client.query(VALUED_LINES_SQL, [joinRunId]);
    // The fund-level aggregate the tick materialized for this run. A fund with no row
    // (the tick predates #727, or it refused before writing) renders as an absent
    // aggregate with zero masses, never as a zero-valued one.
    const consolidations = new Map<string, ConsolidationRow>();
    const consolidated = await client.query(FUND_CONSOLIDATION_SQL, [joinRunId]);
    for (const row of consolidated.rows) {
      consolidations.set(String(row.fund_id), {
        weightedGap: typeof row.weighted_gap === "string" ? Number(row.weighted_gap).toFixed(2) : null,
        valuedWeightPct: pct(row.valued_weight_pct),
        availabilityStatus: row.availability_status === null ? null : String(row.availability_status),
        sourceEvidenceStatus: row.source_evidence_status === null ? null : String(row.source_evidence_status),
        factorValidationStatus:
          row.factor_validation_status === null ? null : String(row.factor_validation_status),
        reasonCodes: Array.isArray(row.reason_codes) ? row.reason_codes.map(String) : [],
      });
    }
    // Filed/resolved mass: the filing's and the KG's, not the run's.
    const coverage = new Map<string, { totalWeightPct: string; resolvedWeightPct: string }>();
    const coverageRows = await client.query(FUND_COVERAGE_SQL);
    for (const row of coverageRows.rows) {
      coverage.set(String(row.fund_id), {
        totalWeightPct: pct(row.total_weight_pct),
        resolvedWeightPct: pct(row.resolved_weight_pct),
      });
    }
    const byFund = new Map<string, FundValuation>();
    for (const row of result.rows) {
      const fundId = String(row.fund_id);
      let fund = byFund.get(fundId);
      if (!fund) {
        const consolidation = consolidations.get(fundId);
        fund = {
          fundId,
          fundName: String(row.fund_name),
          reportPeriod: String(row.report_period),
          runId,
          valuedWeightPct: consolidation?.valuedWeightPct ?? "0.00",
          resolvedWeightPct: coverage.get(fundId)?.resolvedWeightPct ?? "0.00",
          totalWeightPct: coverage.get(fundId)?.totalWeightPct ?? "0.00",
          weightedGap: consolidation?.weightedGap ?? null,
          availabilityStatus: consolidation?.availabilityStatus ?? null,
          sourceEvidenceStatus: consolidation?.sourceEvidenceStatus ?? null,
          factorValidationStatus: consolidation?.factorValidationStatus ?? null,
          reasonCodes: consolidation?.reasonCodes ?? [],
          lines: [],
        };
        byFund.set(fundId, fund);
      }
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
    return [...byFund.values()].sort((a, b) => a.fundName.localeCompare(b.fundName));
  });
}
