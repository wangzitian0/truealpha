/**
 * #362: Postgres-backed `StrategyRunReadRepositoryLike` for the App
 * `/admin/strategy-runs` page. Reads `mart.strategy_runs`/`strategy_decisions`
 * through the `mart_readonly` role (`./db.ts`), replacing the checked-in
 * `FixtureStrategyRunRepository` as the shipped consumer path.
 *
 * Parity with the Python `PostgresStrategyRunRepository` is not a comment —
 * it is EXECUTED: tests/strategy-run-parity-conformance.test.ts and its Python
 * half seed the same rows on one real schema and assert both serializations
 * equal the frozen canon in libs/contracts/conformance/strategy_run_parity.json
 * (#469). The shared semantics:
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

/** The shared contract report plus the mart row's run identity (#370 AC 3). */
export type MartStrategyRunReport = StrategyRunReport & {
  strategy_run_id: string;
  executed_at: string;
};

const LATEST_RUN_SQL = `
  select strategy_run_id, corpus_sha256, executed_at
  from mart.strategy_runs
  where strategy_key = $1
  order by executed_at desc, created_at desc, strategy_run_id desc
  limit 1
`;

// cutoff_at is formatted in SQL to Python's datetime.isoformat semantics
// ("...:59Z" when microseconds are zero, "...:59.123456Z" otherwise) so the
// serialized decision is byte-identical to the Python twin's pydantic output —
// JS Date.toISOString() always emits milliseconds and silently truncates
// microseconds, which broke trace-ID parity on exactly the mart path (#469).
// ORDER BY names the source column, not the text alias: the two text formats
// do not sort chronologically.
const DECISIONS_SQL = `
  select issuer_id,
         case when to_char(cutoff_at at time zone 'UTC', 'US') = '000000'
              then to_char(cutoff_at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')
              else to_char(cutoff_at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')
         end as cutoff_at,
         capital_adjusted_labor_efficiency, tier,
         current_price_to_sales, target_price_to_sales, valuation_gap,
         eligible, outcome, exclusion_reason, rank, target_weight
  from mart.strategy_decisions
  where strategy_run_id = $1
  order by mart.strategy_decisions.cutoff_at, issuer_id
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
  // DECISIONS_SQL emits Python-isoformat text; a Date here means the SQL-side
  // formatting was lost, which silently reintroduces the millisecond drift.
  if (typeof value === "string") return value;
  throw new SchemaMismatchError("cutoff_at did not arrive as SQL-formatted text");
}

/** Mirrors the Python twin's pydantic Field bounds — a row the MCP side would
 * reject as `schema_mismatch` must not render in the App (#469). */
function boundedDecimalString(value: unknown, field: string): string | null {
  const text = decimalString(value, field);
  if (text !== null) {
    const numeric = Number(text);
    if (!(numeric >= 0 && numeric <= 1)) throw new SchemaMismatchError(`${field} is outside [0, 1]`);
  }
  return text;
}

function decisionFromRow(row: Record<string, unknown>): StrategyRunDecision {
  if (typeof row.issuer_id !== "string" || row.issuer_id.length === 0) {
    throw new SchemaMismatchError("issuer_id is not a non-empty string");
  }
  if (typeof row.eligible !== "boolean") throw new SchemaMismatchError("eligible is not a boolean");
  const rank = row.rank;
  if (rank !== null && typeof rank !== "number") throw new SchemaMismatchError("rank is not an integer");
  if (typeof rank === "number" && rank < 1) throw new SchemaMismatchError("rank is below 1");
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
    target_weight: boundedDecimalString(row.target_weight, "target_weight"),
  };
}

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

    // The Python twin's report type pins strategy_id to the Literal
    // "large_model_value_v0": a run recorded under any other key fails its
    // validation and returns schema_mismatch. Mirror that instead of
    // relabeling foreign rows as the default strategy (#469).
    if (strategyId !== "large_model_value_v0") {
      return { strategy_id: strategyId, reason: "schema_mismatch" };
    }

    const corpusSha256 = runRow.corpus_sha256;
    if (typeof corpusSha256 !== "string") {
      return { strategy_id: strategyId, reason: "schema_mismatch" };
    }

    try {
      return {
        strategy_id: "large_model_value_v0",
        source: "mart",
        corpus_sha256: corpusSha256,
        decisions: decisionRows.map(decisionFromRow),
        golden_mismatches: [],
        // Run identity for the overview (#370 appended AC 3): lets the page prove it
        // renders the same governed run the MCP strategy_run tool serves.
        strategy_run_id: String(runRow.strategy_run_id),
        executed_at: runRow.executed_at instanceof Date ? runRow.executed_at.toISOString() : String(runRow.executed_at),
      };
    } catch (error) {
      if (error instanceof SchemaMismatchError) {
        return { strategy_id: strategyId, reason: "schema_mismatch" };
      }
      throw error;
    }
  }
}
