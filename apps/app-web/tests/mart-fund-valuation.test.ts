/**
 * loadFundValuation against a fake mart client (the MartClientLike seam
 * topt-gppe-repository established): the governed head resolves pointer-first, and the
 * fund-level aggregate is READ from mart.fund_virtual_company rather than computed here
 * (#727). The reader must report the factor's masses and its refusal verbatim — a fund
 * whose consolidation was refused shows no gap, and a fund with no consolidation row at
 * all shows an absent aggregate, never a zero.
 */

import { Client } from "pg";

import { hex } from "../scripts/lineage-seed";
import { idPrefixLikeOfRole } from "../src/contracts/universes";
import { loadFundValuation, VALUED_LINES_SQL } from "../src/server/mart/fund-valuation";
import type { MartClientLike } from "../src/server/mart/topt-gppe-repository";
import { requireDb } from "./helpers/db-guard";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

const LINES = [
  {
    fund_id: "etf:series:S1",
    fund_name: "Fund One",
    report_period: "2026-06-30",
    holding_name: "Valued Corp",
    ticker: "VAL",
    weight_pct: "60.0",
    current_ps: "10.5",
    target_ps_midpoint: "12.0",
    valuation_gap: "0.14",
    tier: "tier-1",
    availability: "available",
  },
  {
    fund_id: "etf:series:S1",
    fund_name: "Fund One",
    report_period: "2026-06-30",
    holding_name: "Resolved But Unvalued Corp",
    ticker: "RBU",
    weight_pct: "30.0",
    current_ps: null,
    target_ps_midpoint: null,
    valuation_gap: null,
    tier: null,
    availability: "unavailable",
  },
  {
    fund_id: "etf:series:S1",
    fund_name: "Fund One",
    report_period: "2026-06-30",
    holding_name: "Unresolved Corp",
    ticker: null,
    weight_pct: "9.5",
    current_ps: null,
    target_ps_midpoint: null,
    valuation_gap: null,
    tier: null,
    availability: null,
  },
];

/** What the tick's module-5 factor materialized for the governed run. */
const CONSOLIDATION = {
  fund_id: "etf:series:S1",
  weighted_gap: "0.14",
  valuation_basis: "price_to_gross_profit",
  valued_weight_pct: "60.0",
  availability_status: "available",
  source_evidence_status: "degraded",
  factor_validation_status: "not_evaluated",
  reason_codes: ["partial_valued_mass", "unresolved_holdings"],
};

function fakeRunner(
  headRows: Record<string, unknown>[],
  capture: { runParam?: unknown; headParam?: unknown; fallbackParam?: unknown },
  consolidationRows: Record<string, unknown>[] = [CONSOLIDATION],
) {
  return async <T>(fn: (client: MartClientLike) => Promise<T>): Promise<T> => {
    const client: MartClientLike = {
      query: async (sql: string, params?: readonly unknown[]) => {
        if (sql.includes("current_pointer_head") || sql.includes("served_head")) {
          capture.headParam = params?.[0];
          return { rows: headRows };
        }
        if (sql.includes("topt_capture_status")) {
          capture.fallbackParam = params?.[0];
          return { rows: [] };
        }
        // Filed/resolved mass is the filing's, not the run's: answered whether or not a
        // governed run exists.
        if (sql.includes("fund_holdings_coverage")) {
          return { rows: [{ fund_id: "etf:series:S1", total_weight_pct: "99.5", resolved_weight_pct: "90.0" }] };
        }
        if (sql.includes("fund_virtual_company")) {
          // No governed run means no consolidation row for it — the same empty
          // result the real query returns for "capture-run:none".
          return { rows: params?.[0] === "capture-run:none" ? [] : consolidationRows };
        }
        if (sql.includes("fund_holdings_valuation")) {
          capture.runParam = params?.[0];
          // A non-existent run left-joins to nothing: valuation columns null,
          // exactly what the real SQL produces.
          const rows =
            params?.[0] === "capture-run:none"
              ? LINES.map((row) => ({
                  ...row,
                  current_ps: null,
                  target_ps_midpoint: null,
                  valuation_gap: null,
                  tier: null,
                  availability: null,
                }))
              : LINES;
          return { rows };
        }
        throw new Error(`unexpected SQL: ${sql.slice(0, 60)}`);
      },
    };
    return fn(client);
  };
}

{
  const capture: { runParam?: unknown; headParam?: unknown; fallbackParam?: unknown } = {};
  const funds = await loadFundValuation(fakeRunner([{ run_id: "capture-run:abc" }], capture));
  assert(funds.length === 1, "one fund");
  assert(
    capture.headParam === idPrefixLikeOfRole("fund_consolidation"),
    "the pointer query binds the id prefix of the fund-consolidation universe",
  );
  const fund = funds[0];
  assert(capture.runParam === "capture-run:abc", "the governed head run parameterizes the join");
  assert(fund.runId === "capture-run:abc", "the run id is reported for provenance");
  assert(fund.totalWeightPct === "99.50", `total mass, got ${fund.totalWeightPct}`);
  assert(fund.resolvedWeightPct === "90.00", `resolved mass excludes null tickers, got ${fund.resolvedWeightPct}`);
  assert(fund.valuedWeightPct === "60.00", `valued mass counts only 'available', got ${fund.valuedWeightPct}`);
  assert(fund.weightedGap === "0.14", `weighted gap is read from the factor row, got ${fund.weightedGap}`);
  // #1235: the gap's basis travels with it, so a P/GP run and a P/S run never read the same.
  assert(fund.weightedGapBasis === "price_to_gross_profit", `the gap names its basis, got ${fund.weightedGapBasis}`);
  assert(fund.availabilityStatus === "available", "the consolidation's §8 availability is surfaced");
  assert(fund.sourceEvidenceStatus === "degraded", "partial valued mass reads as degraded evidence");
  assert(fund.factorValidationStatus === "not_evaluated", "module 5 has no sealed holdout verdict (#65)");
  assert(fund.reasonCodes.length === 2, "the factor's flags travel to the reader");
  assert(fund.lines.length === 3 && fund.lines[0].holdingName === "Valued Corp", "row order preserved");
}

{
  // A refused consolidation (coverage below the definition's floors) must not render as
  // a gap of zero: the aggregate is absent and the reason says why.
  const capture: { runParam?: unknown; headParam?: unknown; fallbackParam?: unknown } = {};
  const funds = await loadFundValuation(
    fakeRunner([{ run_id: "capture-run:abc" }], capture, [
      {
        ...CONSOLIDATION,
        weighted_gap: null,
        valuation_basis: null,
        valued_weight_pct: "3.0",
        availability_status: "unavailable",
        reason_codes: ["valued_weight_below_minimum"],
      },
    ]),
  );
  assert(funds[0].weightedGap === null, "a refused aggregate is absent, never 0.00");
  assert(funds[0].weightedGapBasis === null, "a refused aggregate names no basis");
  assert(funds[0].availabilityStatus === "unavailable", "the refusal is visible as unavailable");
  assert(funds[0].reasonCodes[0] === "valued_weight_below_minimum", "the refusing floor is named");
  assert(funds[0].valuedWeightPct === "3.00", "the coverage that caused the refusal is still reported");
}

{
  // A run whose tick predates #727 wrote no consolidation row. The lines still render;
  // the aggregate is absent rather than silently computed here.
  const capture: { runParam?: unknown; headParam?: unknown; fallbackParam?: unknown } = {};
  const funds = await loadFundValuation(fakeRunner([{ run_id: "capture-run:abc" }], capture, []));
  assert(funds[0].weightedGap === null, "no consolidation row means no aggregate");
  assert(funds[0].totalWeightPct === "99.50", "the filed mass is the filing's, not the run's");
  assert(funds[0].valuedWeightPct === "0.00", "nothing is valued without a consolidation row");
  assert(funds[0].availabilityStatus === null, "an absent row carries no status dimensions");
  assert(funds[0].lines.length === 3, "the filed lines still render");
}

{
  const capture: { runParam?: unknown; headParam?: unknown; fallbackParam?: unknown } = {};
  const funds = await loadFundValuation(fakeRunner([], capture));
  assert(capture.runParam === "capture-run:none", "no governed run joins nothing instead of guessing one");
  assert(
    capture.fallbackParam === idPrefixLikeOfRole("fund_consolidation"),
    "the acceptance fallback binds the id prefix of the fund-consolidation universe",
  );
  assert(funds.length === 1 && funds[0].runId === null, "the absence of a run is reported, not hidden");
  assert(funds[0].valuedWeightPct === "0.00", "nothing is valued without a run");
  assert(funds[0].weightedGap === null, "no weighted gap without a run — absent, not zero");
  assert(funds[0].totalWeightPct === "99.50", "the filed mass still reports without a run");
}

{
  // An unavailable (withheld, >30d) head joins nothing and renders unvalued with runId: null (#1062)
  const capture: { runParam?: unknown; headParam?: unknown; fallbackParam?: unknown } = {};
  const funds = await loadFundValuation(
    fakeRunner(
      [
        {
          run_id: null,
          availability: "unavailable",
          freshness: "stale",
          staleness_reason: "older_than_30d",
        },
      ],
      capture,
    ),
  );
  assert(capture.runParam === "capture-run:none", "an unavailable head joins nothing (withholding)");
  assert(funds.length === 1 && funds[0].runId === null, "withheld head yields null runId");
  assert(funds[0].valuedWeightPct === "0.00", "nothing valued when head is withheld");
}

console.log("mart fund-valuation coverage arithmetic passed");

type ExplainPlanNode = {
  "Node Type"?: string;
  "Relation Name"?: string;
  "Subplan Name"?: string;
  "Actual Loops"?: number;
  "Actual Rows"?: number;
  Plans?: ExplainPlanNode[];
};

function collectPlanNodes(root: ExplainPlanNode): ExplainPlanNode[] {
  const out: ExplainPlanNode[] = [root];
  for (const child of root.Plans ?? []) {
    out.push(...collectPlanNodes(child));
  }
  return out;
}

export function assertValuedLinesDoesNotRescan(planRoot: ExplainPlanNode): void {
  const nodes = collectPlanNodes(planRoot);
  assert(
    (planRoot["Actual Rows"] ?? 0) >= 4,
    `expected at least 4 holding rows from VALUED_LINES_SQL, got ${planRoot["Actual Rows"]}`,
  );
  const coreSubplan = nodes.find((node) => node["Subplan Name"] === "CTE core");
  assert(coreSubplan !== undefined, "VALUED_LINES_SQL plan must materialize CTE core");
  assert(
    coreSubplan["Actual Loops"] === 1,
    `CTE core must evaluate once (Actual Loops === 1), got ${coreSubplan["Actual Loops"]}`,
  );
  const coreTableScans = nodes.filter((node) => node["Relation Name"] === "topt_core_results");
  assert(coreTableScans.length > 0, "expected a scan on topt_core_results in VALUED_LINES_SQL plan");
  for (const scan of coreTableScans) {
    assert(
      scan["Actual Loops"] === 1,
      `topt_core_results must be scanned once (Actual Loops === 1), got ${scan["Actual Loops"]}`,
    );
  }
}

{
  // Falsifiability check: assertValuedLinesDoesNotRescan must fail on missing CTE core or per-holding rescan.
  for (const badPlan of [
    {
      "Node Type": "Sort",
      "Actual Rows": 4,
      Plans: [{ "Relation Name": "topt_core_results", "Actual Loops": 4 }],
    },
    {
      "Node Type": "Sort",
      "Actual Rows": 4,
      Plans: [
        { "Subplan Name": "CTE core", "Actual Loops": 1 },
        { "Relation Name": "topt_core_results", "Actual Loops": 4 },
      ],
    },
  ] satisfies ExplainPlanNode[]) {
    let threw = false;
    try {
      assertValuedLinesDoesNotRescan(badPlan);
    } catch {
      threw = true;
    }
    assert(threw, "assertValuedLinesDoesNotRescan must reject non-materialized or rescanned plans");
  }
}

const dbUrl = requireDb("mart-fund-valuation", { write: true });
if (dbUrl) {
  const client = new Client({ connectionString: dbUrl, connectionTimeoutMillis: 3000 });
  let connected = false;
  try {
    await client.connect();
    connected = true;
  } catch (error) {
    await client.end().catch(() => {});
    if (process.env.TRUEALPHA_REQUIRE_RUNTIME) {
      throw new Error(`configured Postgres is unreachable: ${String(error)}`);
    }
    console.log("mart-fund-valuation: no local Postgres and TRUEALPHA_REQUIRE_RUNTIME unset — SKIP");
  }
  if (connected) {
    try {
      await client.query("begin");
      await client.query("set local session_replication_role = replica");

      const fundId = "etf:series:TEST-VALUATION-PLAN";
      const issuerId = "issuer:cik:0008880001";
      const runId = `capture-run:${hex()}`;
      const decoyRunId = `capture-run:${hex()}`;
      const isins = ["TESTVAL00001", "TESTVAL00002", "TESTVAL00003", "TESTVAL00004"];
      const tickers = ["TVA", "TVB", "TVC", "TVD"];

      for (const [id, kind, name] of [
        [fundId, "etf", "Test Valuation Plan Fund"],
        [issuerId, "company", "Alpha Valuation Corp"],
        ...isins.map((isin, idx) => [`company:isin:${isin}`, "company", `Holding ${idx + 1}`]),
      ]) {
        await client.query(
          "insert into staging.kg_entities (id, entity_type, display_name) values ($1, $2, $3) on conflict (id) do nothing",
          [id, kind, name],
        );
      }

      for (let i = 0; i < isins.length; i += 1) {
        await client.query(
          `insert into staging.fund_holding_facts
             (fund_id, holding_id, holding_name, report_period, transaction_time,
              cusip, isin, lei, balance, value_usd, percent_of_net_assets, confidence, raw_ref)
           values ($1, $2, $3, '2026-06-30', '2026-08-01T00:00:00Z', null, $4, null, 10, 100, 25, 1.0, 'raw.fetches:0')
           on conflict do nothing`,
          [fundId, `company:isin:${isins[i]}`, `Holding ${i + 1}`, isins[i]],
        );
        await client.query(
          `insert into staging.kg_identifiers
             (entity_id, source, identifier_type, identifier_value, valid_time, transaction_time, confidence, raw_ref)
           values ($1, 'test', 'ticker', $2, daterange('2026-08-01', null, '[)'), '2026-08-01T00:00:00Z', 0.98, 'raw.fetches:0')
           on conflict (source, identifier_type, identifier_value, transaction_time) do nothing`,
          [i === 0 ? issuerId : `company:isin:${isins[i]}`, tickers[i]],
        );
      }
      await client.query(
        `insert into staging.kg_identifiers
           (entity_id, source, identifier_type, identifier_value, valid_time, transaction_time, confidence, raw_ref)
         values ($1, 'test', 'isin', $2, daterange('2026-08-01', null, '[)'), '2026-08-01T00:00:00Z', 0.98, 'raw.fetches:0')
         on conflict (source, identifier_type, identifier_value, transaction_time) do nothing`,
        [issuerId, isins[0]],
      );

      for (const [targetRunId, targetIssuer, targetListing] of [
        [runId, issuerId, "listing:xnas:tva"],
        [decoyRunId, "issuer:cik:0008880002", "listing:xnas:decoy"],
      ]) {
        const sha = hex();
        await client.query(
          `insert into mart.topt_core_results
             (result_id, content_sha256, invocation_id, snapshot_id, run_id, release_manifest_id, universe_id,
              universe_version, universe_sha256, cutoff, issuer_id, instrument_id, listing_id, operating_branch,
              operating_metric, availability, operating_efficiency, capital_adjusted_gross_profit, gppe, tier,
              target_ps_lower, target_ps_upper, target_ps_midpoint, current_ps, valuation_gap, confidence,
              freshness, reason_codes, input_observation_ids, gppe_invocation_id, gppe_result_id,
              gppe_definition_id, gppe_definition_sha256, tier_definition_id, tier_definition_sha256, payload,
              created_at, availability_status, source_evidence_status, factor_validation_status)
           values ($1, $2, $3, $4, $5, $6, 'universe:topt-us-2026-03-31', 'v1', $7, '2026-03-31T00:00:00Z',
                   $8, $9, $10, 'non_financial', 'capital_adjusted_gppe', 'available', 12.5, 1, 12.5, 'tech',
                   15, 20, 18.75, 20, 0.5, 0.85, 'fresh', '{}', array['o1','o2','o3','o4'],
                   $11, $12, 'gppe-def:test', $13, 'tier-def:test', $14, '{}'::jsonb,
                   '2026-04-01T00:00:00Z', 'available', 'verified', 'not_evaluated')`,
          [
            `topt-core-result:${sha}`,
            sha,
            `topt-core-invocation:${hex()}`,
            `topt-core-snapshot:${hex()}`,
            targetRunId,
            `release-manifest:${hex()}`,
            hex(),
            targetIssuer,
            `instrument:${hex(4)}`,
            targetListing,
            `topt-gppe-invocation:${hex()}`,
            `topt-gppe-result:${hex()}`,
            hex(),
            hex(),
          ],
        );
      }

      const explain = await client.query<{ "QUERY PLAN": Array<{ Plan: ExplainPlanNode }> }>(
        `EXPLAIN (ANALYZE, COSTS OFF, FORMAT JSON) ${VALUED_LINES_SQL}`,
        [runId],
      );
      const planRoot = explain.rows[0]["QUERY PLAN"][0].Plan;
      assertValuedLinesDoesNotRescan(planRoot);
      console.log("mart fund-valuation EXPLAIN plan assertion passed");
    } finally {
      await client.query("rollback").catch(() => {});
      await client.end().catch(() => {});
    }
  }
}
