/**
 * #727: the App side of the fund-consolidation conformance case.
 *
 * `libs/contracts/conformance/fund_virtual_company.json` pins one fund's filed lines, the
 * consolidation the module-5 factor computes, and the strings this reader must render.
 * `libs/contracts/tests/test_fund_virtual_company_conformance.py` asserts the factor
 * reproduces the numbers; this asserts `loadFundValuation` renders exactly those numbers
 * from the materialized row — which is what "the App value equals the mart value" means
 * now that the App reads a column instead of computing one.
 *
 * The mart row is built HERE from the fixture's expected values, so a reader that quietly
 * went back to aggregating the lines itself would not reproduce them: the lines and the
 * aggregate deliberately disagree unless the aggregate is read.
 */

import { readFileSync } from "node:fs";

import { loadFundValuation } from "../src/server/mart/fund-valuation";
import type { MartClientLike } from "../src/server/mart/topt-gppe-repository";

const fixtureUrl = new URL("../../../libs/contracts/conformance/fund_virtual_company.json", import.meta.url);

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

type Fixture = {
  fund_id: string;
  lines: {
    holding_name: string;
    weight: string;
    listing_id: string | null;
    valuation_gap: string | null;
    availability: string | null;
  }[];
  expected: {
    weighted_valuation_gap: string;
    total_weight_pct: string;
    resolved_weight_pct: string;
    valued_weight_pct: string;
  };
  expected_rendered: {
    weightedGap: string;
    totalWeightPct: string;
    resolvedWeightPct: string;
    valuedWeightPct: string;
  };
};

const fixture = JSON.parse(readFileSync(fixtureUrl, "utf8")) as Fixture;
const RUN_ID = "capture-run:conformance";

function runner() {
  return async <T>(fn: (client: MartClientLike) => Promise<T>): Promise<T> => {
    const client: MartClientLike = {
      query: async (sql: string) => {
        if (sql.includes("current_pointer_head")) return { rows: [{ run_id: RUN_ID }] };
        if (sql.includes("topt_capture_status")) return { rows: [] };
        if (sql.includes("fund_holdings_coverage")) {
          return {
            rows: [
              {
                fund_id: fixture.fund_id,
                total_weight_pct: fixture.expected.total_weight_pct,
                resolved_weight_pct: fixture.expected.resolved_weight_pct,
              },
            ],
          };
        }
        if (sql.includes("fund_virtual_company")) {
          return {
            rows: [
              {
                fund_id: fixture.fund_id,
                weighted_gap: fixture.expected.weighted_valuation_gap,
                valued_weight_pct: fixture.expected.valued_weight_pct,
                availability_status: "available",
                source_evidence_status: "degraded",
                factor_validation_status: "not_evaluated",
                reason_codes: ["partial_valued_mass", "unresolved_holdings"],
              },
            ],
          };
        }
        if (sql.includes("fund_holdings_valuation")) {
          return {
            rows: fixture.lines.map((line) => ({
              fund_id: fixture.fund_id,
              fund_name: "Conformance Fund",
              report_period: "2026-03-31",
              holding_name: line.holding_name,
              ticker: line.listing_id === null ? null : line.listing_id.split(":").pop(),
              weight_pct: line.weight,
              current_ps: null,
              target_ps_midpoint: null,
              valuation_gap: line.valuation_gap,
              tier: null,
              availability: line.availability,
            })),
          };
        }
        throw new Error(`unexpected SQL: ${sql.slice(0, 60)}`);
      },
    };
    return fn(client);
  };
}

{
  const funds = await loadFundValuation(runner());
  assert(funds.length === 1, "one fund in the fixture");
  const fund = funds[0];
  const want = fixture.expected_rendered;

  assert(
    fund.weightedGap === want.weightedGap,
    `weighted gap: rendered ${fund.weightedGap}, the factor's value is ${want.weightedGap}`,
  );
  assert(fund.valuedWeightPct === want.valuedWeightPct, `valued mass, got ${fund.valuedWeightPct}`);
  assert(fund.resolvedWeightPct === want.resolvedWeightPct, `resolved mass, got ${fund.resolvedWeightPct}`);
  assert(fund.totalWeightPct === want.totalWeightPct, `filed mass, got ${fund.totalWeightPct}`);
  assert(fund.lines.length === fixture.lines.length, "every filed line still renders");
  assert(fund.availabilityStatus === "available", "the consolidation's §8 dimensions reach the reader");
  assert(fund.reasonCodes.length === 2, "the factor's flags reach the reader");
}

console.log("fund virtual-company conformance passed");
