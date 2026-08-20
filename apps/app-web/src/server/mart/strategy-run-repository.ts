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
 *  - `confidence` and the input vintages come from mart.topt_core_results,
 *    joined on (issuer_id, cutoff) — mart.strategy_decisions records the verdict
 *    and nothing about what it was reached from;
 *  - a query error → `database_unavailable`; no run → `no_runs_recorded`;
 *    a row that no longer matches the DTO shape → `schema_mismatch`.
 *
 * Server-only; never import into a client component.
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

import { withMartReadonly } from "./db";

/** The shared contract report plus the mart row's run identity (#370 AC 3) and
 * the inputs each decision was reached from.
 *
 * `provenance` lives HERE and not on `StrategyRunDecision` on purpose. That type
 * is serialized byte-for-byte against the Python twin
 * (`strategy-run-parity-conformance`: "two languages, one schema, one expected
 * byte shape"), and the first cut of this change put display provenance inside
 * it and diverged from the frozen canon. The extension type is where mart-only
 * reads already belong. */
export type MartStrategyRunReport = StrategyRunReport & {
	strategy_run_id: string;
	executed_at: string;
	provenance: ReadonlyMap<string, DecisionProvenance>;
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
  select d.issuer_id,
         case when to_char(d.cutoff_at at time zone 'UTC', 'US') = '000000'
              then to_char(d.cutoff_at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')
              else to_char(d.cutoff_at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')
         end as cutoff_at,
         d.capital_adjusted_labor_efficiency, d.tier,
         d.current_price_to_sales, d.target_price_to_sales, d.valuation_gap,
         d.eligible, d.outcome, d.exclusion_reason, d.rank, d.target_weight, d.peg, d.peg_rank,
         -- #615-adjacent, and init.md's reason for point-in-time in the first
         -- place: "every step downstream has to be traceable back to the
         -- original raw material". The decision row records what was decided
         -- and nothing about what it was decided FROM. The core result carries
         -- all of it and was never joined, so the App declared a confidence
         -- field that mart.strategy_decisions does not even have a column for
         -- — always null, rendered as an em dash in every row of /research/rankings
         -- since the column shipped.
         --
         -- Measured before joining, not assumed: 20 of 20 decisions in the
         -- served run match a core result on (issuer_id, cutoff), and the
         -- confidences line up with the exclusions — 0 for the two
         -- missing_gross_profit_fact rows, 0.80 for missing_market_value_input,
         -- 0.85 for everything available.
         --
         -- A join is a read, not a computation: init.md principle 2 keeps
         -- metric computation in libs/factors, and this adds no metric.
         t.confidence,
         t.operating_period_end, t.revenue_period_end, t.shares_period_end,
         t.universe_version, t.universe_sha256,
         t.gppe_definition_sha256, t.tier_definition_sha256
  from mart.strategy_decisions d
  left join mart.topt_core_results t
    on t.issuer_id = d.issuer_id and t.cutoff = d.cutoff_at
  where d.strategy_run_id = $1
  order by d.cutoff_at, d.issuer_id
`;

class SchemaMismatchError extends Error {}

/** `numeric` comes back from node-pg as a precision-preserving string; keep it
 * verbatim (never coerce through a JS number). timestamptz comes back as a Date. */
function decimalString(value: unknown, field: string): string | null {
	if (value === null || value === undefined) return null;
	if (typeof value === "string") return value;
	throw new SchemaMismatchError(`${field} is not a numeric string`);
}

/** A `date` column arrives from node-pg as a Date at UTC midnight. Rendered as
 * the calendar day it names, never as an instant: these are period ends —
 * "the quarter this figure describes" — and a timezone-shifted 2025-12-31 that
 * displays as 2025-12-30 would be a lie about the vintage. */
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
	if (
		typeof value === "string" &&
		(STRATEGY_RUN_OUTCOMES as readonly string[]).includes(value)
	) {
		return value as StrategyRunOutcome;
	}
	throw new SchemaMismatchError(`unknown outcome ${String(value)}`);
}

function tierOf(value: unknown): ValuationTier | null {
	if (value === null || value === undefined) return null;
	if (
		typeof value === "string" &&
		(VALUATION_TIERS as readonly string[]).includes(value)
	) {
		return value as ValuationTier;
	}
	throw new SchemaMismatchError(`unknown tier ${String(value)}`);
}

function cutoffIso(value: unknown): string {
	// DECISIONS_SQL emits Python-isoformat text; a Date here means the SQL-side
	// formatting was lost, which silently reintroduces the millisecond drift.
	if (typeof value === "string") return value;
	throw new SchemaMismatchError(
		"cutoff_at did not arrive as SQL-formatted text",
	);
}

/** Exact [0, 1] membership test on a Postgres numeric string — string
 * arithmetic only, because coercing through a JS number reintroduces the
 * float rounding this module's own rule forbids (#482 review): an edge value
 * like "1.0000000000000000001" must be rejected exactly as the Python twin's
 * Decimal bound rejects it, not rounded into acceptance. */
function outsideUnitInterval(text: string): boolean {
	const negative = text.startsWith("-");
	const magnitude = negative ? text.slice(1) : text;
	const [whole = "", fraction = ""] = magnitude.split(".");
	const wholeStripped = whole.replace(/^0+/, "");
	const fractionHasValue = /[1-9]/.test(fraction);
	if (negative) return wholeStripped !== "" || fractionHasValue; // any negative non-zero
	if (wholeStripped === "") return false; // 0 <= value < 1
	if (wholeStripped === "1") return fractionHasValue; // exactly 1 passes; 1.0…01 fails
	return true; // integer part >= 2
}

/** Mirrors the Python twin's pydantic Field bounds — a row the MCP side would
 * reject as `schema_mismatch` must not render in the App (#469). */
function boundedDecimalString(value: unknown, field: string): string | null {
	const text = decimalString(value, field);
	if (text !== null && outsideUnitInterval(text)) {
		throw new SchemaMismatchError(`${field} is outside [0, 1]`);
	}
	return text;
}

function decisionFromRow(row: Record<string, unknown>): StrategyRunDecision {
	if (typeof row.issuer_id !== "string" || row.issuer_id.length === 0) {
		throw new SchemaMismatchError("issuer_id is not a non-empty string");
	}
	if (typeof row.eligible !== "boolean")
		throw new SchemaMismatchError("eligible is not a boolean");
	const rank = row.rank;
	if (rank !== null && typeof rank !== "number")
		throw new SchemaMismatchError("rank is not an integer");
	if (typeof rank === "number" && (!Number.isInteger(rank) || rank < 1)) {
		throw new SchemaMismatchError("rank is not an integer >= 1");
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
		current_price_to_sales: decimalString(
			row.current_price_to_sales,
			"current_price_to_sales",
		),
		target_price_to_sales: decimalString(
			row.target_price_to_sales,
			"target_price_to_sales",
		),
		valuation_gap: decimalString(row.valuation_gap, "valuation_gap"),
		// Joined from mart.topt_core_results, because mart.strategy_decisions
		// still has no confidence column (#355). This mapper hard-coded null,
		// so the first cut of this change selected the column and then threw it
		// away: the SQL was right, the page still rendered an em dash, and the
		// guard I added passed because it only checked the SELECT (review).
		confidence: decimalString(row.confidence, "confidence"),
		exclusion_reason:
			typeof row.exclusion_reason === "string" ? row.exclusion_reason : null,
		rank: (rank as number | null) ?? null,
		target_weight: boundedDecimalString(row.target_weight, "target_weight"),
		// Module 1 (#284). Nullable by design: PEG is undefined for non-positive earnings or
		// growth and the factor returns no value rather than a signed one, so the read model
		// must carry the absence rather than coercing it to a number.
		peg: decimalString(row.peg, "peg"),
		// Module 1's own ordering (#284). Independent of `rank`: PEG does not select.
		peg_rank: typeof row.peg_rank === "number" ? row.peg_rank : null,
	};
}

/** The inputs a decision was reached from, keyed by issuer.
 *
 * Deliberately NOT on `StrategyRunDecision`. That type is the frozen,
 * content-addressed contract the Python shipping consumer serializes
 * byte-for-byte — `strategy-run-parity-conformance` compares the two, and the
 * first cut of this change put display provenance inside it and diverged from
 * the canon. "Two languages, one schema, one expected byte shape" is the
 * contract; a read-model concern does not belong in it.
 *
 * The join stays in one query — this is the same rows, projected twice. */
export interface DecisionProvenance {
	operating_period_end: string | null;
	revenue_period_end: string | null;
	shares_period_end: string | null;
	universe_version: string | null;
	universe_sha256: string | null;
	gppe_definition_sha256: string | null;
	tier_definition_sha256: string | null;
}

export function provenanceFromRow(
	row: Record<string, unknown>,
): DecisionProvenance {
	return {
		operating_period_end: dateString(row.operating_period_end),
		revenue_period_end: dateString(row.revenue_period_end),
		shares_period_end: dateString(row.shares_period_end),
		universe_version: textOrNull(row.universe_version),
		universe_sha256: textOrNull(row.universe_sha256),
		gppe_definition_sha256: textOrNull(row.gppe_definition_sha256),
		tier_definition_sha256: textOrNull(row.tier_definition_sha256),
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
			[runRow, decisionRows] = await withMartReadonly(
				async (client: PoolClient) => {
					const run = await client.query(LATEST_RUN_SQL, [strategyId]);
					if (run.rows.length === 0) return [undefined, []] as const;
					const decisions = await client.query(DECISIONS_SQL, [
						run.rows[0].strategy_run_id,
					]);
					return [
						run.rows[0] as Record<string, unknown>,
						decisions.rows as Record<string, unknown>[],
					] as const;
				},
			);
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
				provenance: new Map(
					decisionRows.map((row) => [
						String(row.issuer_id),
						provenanceFromRow(row),
					]),
				),
				golden_mismatches: [],
				// Run identity for the overview (#370 appended AC 3): lets the page prove it
				// renders the same governed run the MCP strategy_run tool serves.
				strategy_run_id: String(runRow.strategy_run_id),
				executed_at:
					runRow.executed_at instanceof Date
						? runRow.executed_at.toISOString()
						: String(runRow.executed_at),
			};
		} catch (error) {
			if (error instanceof SchemaMismatchError) {
				return { strategy_id: strategyId, reason: "schema_mismatch" };
			}
			throw error;
		}
	}
}
