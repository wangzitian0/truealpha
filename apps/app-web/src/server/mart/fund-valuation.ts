/**
 * Fund holdings valuation reader over `mart.fund_holdings_valuation` and `mart.fund_virtual_company`.
 */

import { formatRatio } from "@/client/format";
import type { ValuationBasis } from "@/contracts/strategyRun";
import { idPrefixLikeOfRole } from "@/contracts/universes";
import { nullableString, stringArray } from "@/client/coerce";
import { resolveGovernedHeadRunId, withMartReadonly } from "./db";
import type { MartClientLike } from "./topt-gppe-repository";

export type ValuedHoldingRow = {
  holdingName: string;
  ticker: string | null;
  weightPct: string | null;
  currentPs: string | null;
  targetPsMidpoint: string | null;
  currentPgp: string | null;
  targetPgpMidpoint: string | null;
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
  weightedGap: string | null;
  weightedGapBasis: ValuationBasis | null;
  availabilityStatus: string | null;
  sourceEvidenceStatus: string | null;
  factorValidationStatus: string | null;
  reasonCodes: string[];
  lines: ValuedHoldingRow[];
};

const VALUED_LINES_SQL = `
  select v.fund_id,
         coalesce(v.fund_name, v.fund_id) as fund_name,
         to_char(v.report_period, 'YYYY-MM-DD') as report_period,
         v.holding_name,
         v.ticker,
         v.percent_of_net_assets::text as weight_pct,
         r.current_ps::text as current_ps,
         r.target_ps_midpoint::text as target_ps_midpoint,
         r.current_pgp::text as current_pgp,
         r.target_pgp_midpoint::text as target_pgp_midpoint,
         r.valuation_gap::text as valuation_gap,
         r.tier,
         r.availability
  from mart.fund_holdings_valuation v
  left join (
    select coalesce(ei.listing_id, c.listing_id) as listing_id,
           coalesce(
             lpad(nullif(ltrim(ei_issuer.cik, '0'), ''), 10, '0'),
             lpad(substring(c.issuer_id from '^(?:issuer|company):cik:0*([0-9]+)$'), 10, '0')
           ) as issuer_cik,
           c.run_id,
           c.current_ps,
           c.target_ps_midpoint,
           t.current_pgp,
           t.target_pgp_midpoint,
           c.valuation_gap,
           c.tier,
           c.availability
    from mart.topt_core_result_read c
    join mart.topt_core_results t on t.result_id = c.result_id
    left join mart.entity_identity ei on ei.entity_id::text = c.listing_id
    left join mart.entity_identity ei_issuer on ei_issuer.entity_id::text = c.issuer_id
  ) r
    on r.run_id = $1
   and (
     r.listing_id = v.listing_id
     or (
       r.issuer_cik is not null
       and r.issuer_cik = lpad(substring(v.issuer_entity from '^(?:issuer|company):cik:0*([0-9]+)$'), 10, '0')
     )
   )
  order by v.fund_id, v.percent_of_net_assets desc nulls last, v.holding_name
`;

const FUND_CONSOLIDATION_SQL = `
  select fund_id,
         weighted_valuation_gap::text as weighted_gap,
         valuation_basis,
         valued_weight_pct::text as valued_weight_pct,
         availability_status,
         source_evidence_status,
         factor_validation_status,
         reason_codes
  from mart.fund_virtual_company
  where run_id = $1
`;

const FUND_COVERAGE_SQL = `
  select distinct on (fund_id)
         fund_id,
         total_weight_pct::text as total_weight_pct,
         resolved_weight_pct::text as resolved_weight_pct
  from mart.fund_holdings_coverage
  order by fund_id, transaction_time desc, report_period desc
`;

type ConsolidationRow = {
  weightedGap: string | null;
  weightedGapBasis: ValuationBasis | null;
  valuedWeightPct: string;
  availabilityStatus: string | null;
  sourceEvidenceStatus: string | null;
  factorValidationStatus: string | null;
  reasonCodes: string[];
};

const fmtOrZero = (v: unknown): string => (typeof v === "string" ? formatRatio(v) : null) ?? "0.00";

export async function loadFundValuation(
  runWithClient: <T>(fn: (client: MartClientLike) => Promise<T>) => Promise<T> = withMartReadonly,
): Promise<FundValuation[]> {
  return runWithClient(async (client) => {
    const head = await resolveGovernedHeadRunId(client, idPrefixLikeOfRole("fund_consolidation"));
    const runId = typeof head.runId === "string" && head.runId.length > 0 ? head.runId : null;
    const joinRunId = runId ?? "capture-run:none";
    const result = await client.query(VALUED_LINES_SQL, [joinRunId]);

    const consolidations = new Map<string, ConsolidationRow>();
    const consolidated = await client.query(FUND_CONSOLIDATION_SQL, [joinRunId]);
    for (const row of consolidated.rows) {
      consolidations.set(String(row.fund_id), {
        weightedGap: typeof row.weighted_gap === "string" ? formatRatio(row.weighted_gap) : null,
        weightedGapBasis:
          row.valuation_basis === "price_to_gross_profit" || row.valuation_basis === "price_to_sales"
            ? row.valuation_basis
            : null,
        valuedWeightPct: fmtOrZero(row.valued_weight_pct),
        availabilityStatus: nullableString(row.availability_status),
        sourceEvidenceStatus: nullableString(row.source_evidence_status),
        factorValidationStatus: nullableString(row.factor_validation_status),
        reasonCodes: stringArray(row.reason_codes),
      });
    }

    const coverage = new Map<string, { totalWeightPct: string; resolvedWeightPct: string }>();
    const coverageRows = await client.query(FUND_COVERAGE_SQL);
    for (const row of coverageRows.rows) {
      coverage.set(String(row.fund_id), {
        totalWeightPct: fmtOrZero(row.total_weight_pct),
        resolvedWeightPct: fmtOrZero(row.resolved_weight_pct),
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
          weightedGapBasis: consolidation?.weightedGapBasis ?? null,
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
        ticker: nullableString(row.ticker),
        weightPct: nullableString(row.weight_pct),
        currentPs: nullableString(row.current_ps),
        targetPsMidpoint: nullableString(row.target_ps_midpoint),
        currentPgp: nullableString(row.current_pgp),
        targetPgpMidpoint: nullableString(row.target_pgp_midpoint),
        valuationGap: nullableString(row.valuation_gap),
        tier: nullableString(row.tier),
        availability: nullableString(row.availability),
      });
    }
    return [...byFund.values()].sort((a, b) => a.fundName.localeCompare(b.fundName));
  });
}
