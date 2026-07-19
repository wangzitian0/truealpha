/**
 * #362: Postgres-backed `StrategyRunReadRepositoryLike` for the App
 * `/admin/strategy-runs` page. Reads `mart.strategy_runs`/`strategy_decisions`
 * through the `mart_readonly` role (`./db.ts`), replacing the checked-in
 * `FixtureStrategyRunRepository` as the shipped consumer path.
 *
 * The SQL and the row→DTO mapping mirror the Python
 * `truealpha_contracts.strategy_run_postgres.PostgresStrategyRunRepository`
 * exactly, so the App and MCP return semantically identical responses from the
 * same mart contract:
 *  - latest run per `strategy_key` by `executed_at desc, created_at desc, strategy_run_id desc`;
 *  - decisions ordered by `cutoff_at, issuer_id`;
 *  - `confidence` is always null (mart.strategy_decisions has no such column yet, #355);
 *  - a query error → `database_unavailable`; no run → `no_runs_recorded`;
 *    a row that no longer matches the DTO shape → `schema_mismatch`.
 *
 * Server-only; never import into a client component.
 */

import type { PoolClient } from "pg";

import {
  STRATEGY_RUN_OUTCOMES,
  VALUATION_TIERS,
  type AccessContext,
  type StrategyRunDecision,
  type StrategyRunOutcome,
  type StrategyRunReport,
  type StrategyRunUnavailable,
  type ValuationTier,
} from "@/contracts/strategyRun";

import { withMartReadonly } from "./db";

const LATEST_RUN_SQL = `
  select strategy_run_id, corpus_sha256, executed_at
  from mart.strategy_runs
  where strategy_key = $1
  order by executed_at desc, created_at desc, strategy_run_id desc
  limit 1
`;

const DECISIONS_SQL = `
  select issuer_id, cutoff_at, capital_adjusted_labor_efficiency, tier,
         current_price_to_sales, target_price_to_sales, valuation_gap,
         eligible, outcome, exclusion_reason, rank, target_weight
  from mart.strategy_decisions
  where strategy_run_id = $1
  order by cutoff_at, issuer_id
`;

class SchemaMismatchError extends Error {}

/** `numeric` comes back from node-pg as a precision-preserving string; keep it
 * verbatim (never coerce through a JS number). timestamptz comes back as a Date. */
function decimalString(value: unknown, field: string): string | null {
  if (value === null || value === undefined) return null;
  if (typeof value === "string") return value;
  throw new SchemaMismatchError(`${field} is not a numeric string`);
}

function outcomeOf(value: unknown): StrategyRunOutcome {
  if (typeof value === "string" && (STRATEGY_RUN_OUTCOMES as readonly string[]).includes(value)) {
    return value as StrategyRunOutcome;
  }
  throw new SchemaMismatchError(`unknown outcome ${String(value)}`);
}

function tierOf(value: unknown): ValuationTier | null {
  if (value === null || value === undefined) return null;
  if (typeof value === "string" && (VALUATION_TIERS as readonly string[]).includes(value)) {
    return value as ValuationTier;
  }
  throw new SchemaMismatchError(`unknown tier ${String(value)}`);
}

function cutoffIso(value: unknown): string {
  if (value instanceof Date) return value.toISOString();
  if (typeof value === "string") return value;
  throw new SchemaMismatchError("cutoff_at is neither a timestamp nor a string");
}

function decisionFromRow(row: Record<string, unknown>): StrategyRunDecision {
  if (typeof row.issuer_id !== "string") throw new SchemaMismatchError("issuer_id is not a string");
  if (typeof row.eligible !== "boolean") throw new SchemaMismatchError("eligible is not a boolean");
  const rank = row.rank;
  if (rank !== null && typeof rank !== "number") throw new SchemaMismatchError("rank is not an integer");
  return {
    issuer_id: row.issuer_id,
    cutoff_at: cutoffIso(row.cutoff_at),
    outcome: outcomeOf(row.outcome),
    eligible: row.eligible,
    tier: tierOf(row.tier),
    capital_adjusted_labor_efficiency: decimalString(
      row.capital_adjusted_labor_efficiency,
      "capital_adjusted_labor_efficiency",
    ),
    current_price_to_sales: decimalString(row.current_price_to_sales, "current_price_to_sales"),
    target_price_to_sales: decimalString(row.target_price_to_sales, "target_price_to_sales"),
    valuation_gap: decimalString(row.valuation_gap, "valuation_gap"),
    // #355's mart.strategy_decisions has no confidence column yet.
    confidence: null,
    exclusion_reason: typeof row.exclusion_reason === "string" ? row.exclusion_reason : null,
    rank: (rank as number | null) ?? null,
    target_weight: decimalString(row.target_weight, "target_weight"),
  };
}

/** The shared report enriched with the mart run row's identity, so consumers (the
 * `/research` overview, #370's appended acceptance) can display exactly which governed
 * run they rendered and compare it with the MCP `strategy_run` tool output. The fixture
 * repository has no run row, hence a separate widened type rather than a contract change. */
export type MartStrategyRunReport = StrategyRunReport & {
  strategy_run_id: string | null;
  executed_at: string | null;
};

export class MartStrategyRunRepository {
  async getLatest(
    strategyId: string,
    _context: AccessContext,
  ): Promise<MartStrategyRunReport | StrategyRunUnavailable> {
    let runRow: Record<string, unknown> | undefined;
    let decisionRows: Record<string, unknown>[];
    try {
      [runRow, decisionRows] = await withMartReadonly(async (client: PoolClient) => {
        const run = await client.query(LATEST_RUN_SQL, [strategyId]);
        if (run.rows.length === 0) return [undefined, []] as const;
        const decisions = await client.query(DECISIONS_SQL, [run.rows[0].strategy_run_id]);
        return [run.rows[0] as Record<string, unknown>, decisions.rows as Record<string, unknown>[]] as const;
      });
    } catch {
      return { strategy_id: strategyId, reason: "database_unavailable" };
    }

    if (runRow === undefined) {
      return { strategy_id: strategyId, reason: "no_runs_recorded" };
    }

    const corpusSha256 = runRow.corpus_sha256;
    if (typeof corpusSha256 !== "string") {
      return { strategy_id: strategyId, reason: "schema_mismatch" };
    }

    const executedAtRaw = runRow.executed_at;
    try {
      return {
        strategy_id: "large_model_value_v0",
        source: "mart",
        corpus_sha256: corpusSha256,
        strategy_run_id: typeof runRow.strategy_run_id === "string" ? runRow.strategy_run_id : null,
        executed_at:
          executedAtRaw instanceof Date
            ? executedAtRaw.toISOString()
            : typeof executedAtRaw === "string"
              ? executedAtRaw
              : null,
        decisions: decisionRows.map(decisionFromRow),
        golden_mismatches: [],
      };
    } catch (error) {
      if (error instanceof SchemaMismatchError) {
        return { strategy_id: strategyId, reason: "schema_mismatch" };
      }
      throw error;
    }
  }
}
