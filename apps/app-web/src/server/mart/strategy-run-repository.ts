/**
 * Postgres-backed `StrategyRunReadRepositoryLike` over `mart.strategy_runs` and `mart.strategy_decisions`.
 */

import type { PoolClient } from "pg";

import {
	type AccessContext,
	STRATEGY_RUN_OUTCOMES,
	type StrategyRunDecision,
	type StrategyRunOutcome,
	type StrategyRunReport,
	type StrategyRunUnavailable,
	VALUATION_TIERS,
	type ValuationTier,
} from "@/contracts/strategyRun";
import { strategyRunDefinitionOf } from "@/contracts/strategyDefinition";
import { idPrefixLikeOfRole } from "@/contracts/universes";
import {
	type RegisteredStrategyDefinition,
	STRATEGY_DEFINITION_REGISTRY,
} from "@/contracts/strategyDefinitions.generated";

import { withMartReadonly } from "./db";

export type MartStrategyRunReport = StrategyRunReport & {
	strategy_run_id: string;
	executed_at: string;
	governed: boolean;
	provenance: ReadonlyMap<string, DecisionProvenance>;
};

export const LATEST_RUN_SQL = `
    select r.strategy_run_id, r.corpus_sha256, r.executed_at,
           exists (
               select 1 from mart.served_head h
               join mart.strategy_run_capture b on b.capture_run_id = h.run_id
               where b.strategy_run_id = r.strategy_run_id
                 and h.factor_id = 'gross_profit_per_employee'
                 and h.universe_id like $1
           ) as is_governed,
           r.strategy_version, r.definition_content_sha256
    from mart.strategy_runs r
    where r.strategy_key = $2
    order by is_governed desc, r.executed_at desc, r.created_at desc, r.strategy_run_id desc
    limit 1
`;

export const DECISIONS_SQL = `
  select coalesce(ei.legacy_id, d.issuer_id) as issuer_id,
         case when to_char(d.cutoff_at at time zone 'UTC', 'US') = '000000'
              then to_char(d.cutoff_at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')
              else to_char(d.cutoff_at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')
         end as cutoff_at,
         d.capital_adjusted_labor_efficiency, d.tier,
         d.current_price_to_sales, d.target_price_to_sales,
         d.current_price_to_gross_profit, d.target_price_to_gross_profit, d.valuation_gap,
         d.eligible, d.outcome, d.exclusion_reason, d.rank, d.target_weight, d.peg, d.peg_rank,
         d.peg_reason_codes,
         d.labor_efficiency_metric,
         t.confidence,
         t.operating_period_end, t.revenue_period_end, t.shares_period_end,
         t.universe_id, t.universe_version, t.universe_sha256,
         t.gppe_definition_sha256, t.tier_definition_sha256
  from mart.strategy_decisions d
  left join mart.strategy_run_capture scope on scope.strategy_run_id = d.strategy_run_id
  left join mart.topt_core_results t
    on t.run_id = scope.capture_run_id and t.issuer_id = d.issuer_id and t.cutoff = d.cutoff_at
  left join lateral (
    select ei.legacy_id
    from mart.entity_identity ei
    where ei.entity_id::text = d.issuer_id
    limit 1
  ) ei on true
  where d.strategy_run_id = $1
  order by d.cutoff_at, coalesce(ei.legacy_id, d.issuer_id)
`;

class SchemaMismatchError extends Error {}

function decimalString(value: unknown, field: string): string | null {
	if (value === null || value === undefined) return null;
	if (typeof value === "string") return value;
	throw new SchemaMismatchError(`${field} is not a numeric string`);
}

function dateString(value: unknown): string | null {
	if (value === null || value === undefined) return null;
	if (value instanceof Date) return value.toISOString().slice(0, 10);
	if (typeof value === "string") return value.slice(0, 10);
	throw new SchemaMismatchError("period end is neither a date nor a string");
}

function textOrNull(value: unknown): string | null {
	if (value === null || value === undefined) return null;
	if (typeof value === "string") return value;
	throw new SchemaMismatchError("expected a text column");
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
	if (typeof value === "string") return value;
	throw new SchemaMismatchError("cutoff_at did not arrive as SQL-formatted text");
}

function outsideUnitInterval(text: string): boolean {
	const negative = text.startsWith("-");
	const magnitude = negative ? text.slice(1) : text;
	const [whole = "", fraction = ""] = magnitude.split(".");
	const wholeStripped = whole.replace(/^0+/, "");
	const fractionHasValue = /[1-9]/.test(fraction);
	if (negative) return wholeStripped !== "" || fractionHasValue;
	if (wholeStripped === "") return false;
	if (wholeStripped === "1") return fractionHasValue;
	return true;
}

function boundedDecimalString(value: unknown, field: string): string | null {
	const text = decimalString(value, field);
	if (text !== null && outsideUnitInterval(text)) {
		throw new SchemaMismatchError(`${field} is outside [0, 1]`);
	}
	return text;
}

export function decisionFromRow(row: Record<string, unknown>): StrategyRunDecision {
	if (typeof row.issuer_id !== "string" || row.issuer_id.length === 0) {
		throw new SchemaMismatchError("issuer_id is not a non-empty string");
	}
	if (typeof row.eligible !== "boolean") {
		throw new SchemaMismatchError("eligible is not a boolean");
	}
	const rank = row.rank;
	if (rank !== null && typeof rank !== "number") {
		throw new SchemaMismatchError("rank is not an integer");
	}
	if (typeof rank === "number" && (!Number.isInteger(rank) || rank < 1)) {
		throw new SchemaMismatchError("rank is not an integer >= 1");
	}
	if (row.target_price_to_sales != null && row.target_price_to_gross_profit != null) {
		throw new SchemaMismatchError("a decision targets the multiple of one valuation basis, not two");
	}
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
		current_price_to_gross_profit: decimalString(
			row.current_price_to_gross_profit,
			"current_price_to_gross_profit",
		),
		target_price_to_gross_profit: decimalString(
			row.target_price_to_gross_profit,
			"target_price_to_gross_profit",
		),
		valuation_gap: decimalString(row.valuation_gap, "valuation_gap"),
		confidence: decimalString(row.confidence, "confidence"),
		exclusion_reason: typeof row.exclusion_reason === "string" ? row.exclusion_reason : null,
		rank: (rank as number | null) ?? null,
		target_weight: boundedDecimalString(row.target_weight, "target_weight"),
		peg: decimalString(row.peg, "peg"),
		peg_rank: typeof row.peg_rank === "number" ? row.peg_rank : null,
		labor_efficiency_metric:
			typeof row.labor_efficiency_metric === "string" ? row.labor_efficiency_metric : null,
	};
}

function provenanceMarker(): void {}

export interface DecisionProvenance {
	operating_period_end: string | null;
	revenue_period_end: string | null;
	shares_period_end: string | null;
	universe_id: string | null;
	universe_version: string | null;
	universe_sha256: string | null;
	gppe_definition_sha256: string | null;
	tier_definition_sha256: string | null;
	peg_reason_codes?: readonly string[];
}

export function provenanceFromRow(row: Record<string, unknown>): DecisionProvenance {
	void provenanceMarker;
	return {
		operating_period_end: dateString(row.operating_period_end),
		revenue_period_end: dateString(row.revenue_period_end),
		shares_period_end: dateString(row.shares_period_end),
		universe_id: textOrNull(row.universe_id),
		universe_version: textOrNull(row.universe_version),
		universe_sha256: textOrNull(row.universe_sha256),
		gppe_definition_sha256: textOrNull(row.gppe_definition_sha256),
		tier_definition_sha256: textOrNull(row.tier_definition_sha256),
		peg_reason_codes: Array.isArray(row.peg_reason_codes) ? row.peg_reason_codes.map(String) : [],
	};
}

export class MartStrategyRunRepository {
	constructor(
		private readonly registry: readonly RegisteredStrategyDefinition[] = STRATEGY_DEFINITION_REGISTRY,
	) {}

	async getLatest(
		strategyId: string,
		_context: AccessContext,
	): Promise<MartStrategyRunReport | StrategyRunUnavailable> {
		let runRow: Record<string, unknown> | undefined;
		let decisionRows: Record<string, unknown>[];
		try {
			[runRow, decisionRows] = await withMartReadonly(async (client: PoolClient) => {
				const run = await client.query(LATEST_RUN_SQL, [idPrefixLikeOfRole("strategy"), strategyId]);
				if (run.rows.length === 0) return [undefined, []] as const;
				const decisions = await client.query(DECISIONS_SQL, [run.rows[0].strategy_run_id]);
				return [
					run.rows[0] as Record<string, unknown>,
					decisions.rows as Record<string, unknown>[],
				] as const;
			});
		} catch {
			return { strategy_id: strategyId, reason: "database_unavailable" };
		}

		if (runRow === undefined) return { strategy_id: strategyId, reason: "no_runs_recorded" };
		if (
			strategyId !== "large_model_value_v0" ||
			typeof runRow.corpus_sha256 !== "string" ||
			typeof runRow.strategy_run_id !== "string"
		) {
			return { strategy_id: strategyId, reason: "schema_mismatch" };
		}

		const definition = strategyRunDefinitionOf(
			runRow.strategy_version,
			runRow.definition_content_sha256,
			this.registry,
		);
		if (definition === null) return { strategy_id: strategyId, reason: "schema_mismatch" };

		try {
			return {
				strategy_id: "large_model_value_v0",
				source: "mart",
				corpus_sha256: runRow.corpus_sha256,
				decisions: decisionRows.map(decisionFromRow),
				provenance: new Map(
					decisionRows.map((row) => [String(row.issuer_id), provenanceFromRow(row)]),
				),
				golden_mismatches: [],
				strategy_run_id: runRow.strategy_run_id,
				executed_at:
					runRow.executed_at instanceof Date
						? runRow.executed_at.toISOString()
						: String(runRow.executed_at),
				governed: Boolean(runRow.is_governed),
				definition,
			};
		} catch (error) {
			if (error instanceof SchemaMismatchError) {
				return { strategy_id: strategyId, reason: "schema_mismatch" };
			}
			throw error;
		}
	}
}
