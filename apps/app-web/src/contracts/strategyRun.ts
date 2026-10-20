/**
 * Provisional, read-only Core Strategy run reports shared by MCP and the App — see #347.
 *
 * This module intentionally does not implement #41's full seven-module
 * ResearchReadRepository. It mirrors exactly the fields the Python
 * `truealpha_contracts.strategy_run` module and
 * `apps/data-engine/scripts/run_strategy_smoke.py` already produce, reading
 * the same checked-in fixture bytes so the Python and TypeScript adapters
 * agree field-for-field. It performs no new computation.
 *
 * `FixtureStrategyRunRepository` does filesystem I/O and must only be used
 * from server-side code (route loaders, scripts, tests) — never imported
 * into a client component.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { asStrategyRunDefinition, type StrategyRunDefinition } from "./strategyDefinition";

const SHA256_PATTERN = /^[0-9a-f]{64}$/;
const CUTOFF_PATTERN =
	/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/;
// Signed, finite decimal digits only — explicitly excludes "Infinity", "NaN",
// exponential notation ("1e309"), and other Number()-coercible-but-not-decimal
// tokens (see #351's review).
const DECIMAL_PATTERN = /^[+-]?(?:\d+\.?\d*|\.\d+)$/;

/** Structural mirror of `truealpha_contracts.access.AccessContext` — see #347.
 * Fields are intentionally untyped-narrow (plain `string`) so any concrete
 * caller-side context (e.g. apps/app-web/src/server/auth-context.ts's
 * Local-only stand-in) is structurally assignable here without a cast. */
export interface AccessContext {
	contextId: string;
	principalId: string;
	tenantId: string;
	sessionId: string;
	authenticationMethod: string;
	issuedAt: string;
	expiresAt: string;
}

export const STRATEGY_RUN_OUTCOMES = [
	"selected",
	"ranked_beyond_selection_count",
	"rejected_valuation_above_tier_band",
	"excluded",
] as const;
export type StrategyRunOutcome = (typeof STRATEGY_RUN_OUTCOMES)[number];

export const VALUATION_TIERS = [
	"traditional",
	"tech",
	"large_model_native",
] as const;
export type ValuationTier = (typeof VALUATION_TIERS)[number];

/** Module 1 (#284 / #1360): growth-rate conventions selectable on rankings and entity surfaces. */
export const PEG_GROWTH_CONVENTIONS = [
	"historical_cagr",
	"consensus",
	"guidance",
] as const;
export type PegGrowthConvention = (typeof PEG_GROWTH_CONVENTIONS)[number];

export const PEG_CONVENTION_LABELS: Record<PegGrowthConvention, string> = {
	historical_cagr: "Historical CAGR",
	consensus: "Consensus",
	guidance: "Guidance",
};

/** Normalizes query parameters or legacy aliases into the canonical PEG convention union. */
export function normalizePegConvention(
	value: string | null | undefined,
): PegGrowthConvention {
	if (value === "consensus" || value === "analyst_consensus") return "consensus";
	if (value === "guidance" || value === "company_guidance") return "guidance";
	return "historical_cagr";
}

export const GROWTH_CONVENTION_UNSOURCED_PREFIX =
	"growth_convention_unsourced:" as const;

export function growthConventionUnsourcedCode(
	convention: PegGrowthConvention,
): `${typeof GROWTH_CONVENTION_UNSOURCED_PREFIX}${PegGrowthConvention}` {
	return `${GROWTH_CONVENTION_UNSOURCED_PREFIX}${convention}`;
}

export interface PegProjectionInput {
	peg?: string | null;
	pegRank?: number | null;
	pegReasonCodes?: readonly string[] | null;
}

export interface PegProjection {
	peg: string | null;
	pegRank: number | null;
	pegReasonCodes: readonly string[];
}

/** Projects materialized PEG fields onto the active growth-rate convention (#284 / #1360). */
export function projectPegByConvention(
	input: PegProjectionInput,
	convention: PegGrowthConvention,
): PegProjection {
	if (convention === "historical_cagr") {
		return {
			peg: input.peg ?? null,
			pegRank: input.pegRank ?? null,
			pegReasonCodes: input.pegReasonCodes ?? [],
		};
	}
	return {
		peg: null,
		pegRank: null,
		pegReasonCodes: [growthConventionUnsourcedCode(convention)],
	};
}

/** Universe scopes selectable on rankings (#1360). */
export const RANKING_UNIVERSES = ["topt", "qqq"] as const;
export type RankingUniverse = (typeof RANKING_UNIVERSES)[number];

export const RANKING_UNIVERSE_LABELS: Record<RankingUniverse, string> = {
	topt: "TOPT (Top-100)",
	qqq: "QQQ (Nasdaq-100)",
};

export function normalizeRankingUniverse(value: unknown): RankingUniverse {
	const normalized = typeof value === "string" ? value.toLowerCase().trim() : "";
	return (RANKING_UNIVERSES as readonly string[]).includes(normalized)
		? (normalized as RankingUniverse)
		: "topt";
}

/** Checks whether a decision row's universe_version matches the requested ranking universe (#1360). */
export function matchesRankingUniverse(
	universeVersion: string | null | undefined,
	universe: RankingUniverse,
): boolean {
	const isQqq =
		typeof universeVersion === "string" &&
		universeVersion.toLowerCase().includes("qqq");
	return universe === "qqq" ? isQqq : !isQqq;
}

export interface StrategyRunDecision {
	issuer_id: string;
	cutoff_at: string;
	outcome: StrategyRunOutcome;
	eligible: boolean;
	tier: ValuationTier | null;
	capital_adjusted_labor_efficiency: string | null;
	current_price_to_sales: string | null;
	target_price_to_sales: string | null;
	// #1235: the P/GP pair of strategy definition 1.0.0. When the P/GP target is set,
	// `valuation_gap` is target P/GP / current P/GP - 1 and `target_price_to_sales` is
	// null. Optional so a report produced before definition 1.0.0 still validates.
	current_price_to_gross_profit?: string | null;
	target_price_to_gross_profit?: string | null;
	valuation_gap: string | null;
	confidence: string | null;
	exclusion_reason: string | null;
	rank: number | null;
	target_weight: string | null;
	// Module 1 (#284), recorded but not selecting. Optional so a report produced before it
	// existed still validates, and null when PEG is undefined for the issuer.
	peg?: string | null;
	// Module 1 ordering (#284). Independent of `rank`; PEG does not participate in selection.
	peg_rank?: number | null;
	// #1176: the metric name the strategy used for `capital_adjusted_labor_efficiency`, e.g.
	// gppe_banking_tce_v1 for a FINANCIAL issuer. Null when no metric was recorded.
	labor_efficiency_metric?: string | null;
}

/** The multiple a decision's valuation gap divides (#1235). A definition 1.0.0
 * decision carries a P/GP target; a definition 0.1.0 decision carries a P/S target. */
export type ValuationBasis = "price_to_sales" | "price_to_gross_profit";

export function valuationBasisOf(
	decision: Pick<
		StrategyRunDecision,
		"target_price_to_sales" | "target_price_to_gross_profit"
	>,
): ValuationBasis | null {
	if (decision.target_price_to_gross_profit != null) return "price_to_gross_profit";
	if (decision.target_price_to_sales != null) return "price_to_sales";
	return null;
}

/** The display label of each basis, one place for every page and card. */
export const VALUATION_BASIS_LABEL: Record<ValuationBasis, string> = {
	price_to_sales: "P/S",
	price_to_gross_profit: "P/GP",
};

type MultipleFields = Pick<
	StrategyRunDecision,
	| "current_price_to_sales"
	| "target_price_to_sales"
	| "current_price_to_gross_profit"
	| "target_price_to_gross_profit"
>;

/** The current multiple the decision's gap divides: P/GP under 1.0.0, P/S under 0.1.0. */
export function currentMultipleOf(decision: MultipleFields): string | null {
	const basis = valuationBasisOf(decision);
	if (basis === "price_to_gross_profit") return decision.current_price_to_gross_profit ?? null;
	if (basis === "price_to_sales") return decision.current_price_to_sales;
	return null;
}

/** The band-midpoint target of the decision's own basis. */
export function targetMultipleOf(decision: MultipleFields): string | null {
	const basis = valuationBasisOf(decision);
	if (basis === "price_to_gross_profit") return decision.target_price_to_gross_profit ?? null;
	if (basis === "price_to_sales") return decision.target_price_to_sales;
	return null;
}

/** One run evaluates one definition, so its decisions share one basis. Null when no
 * decision carries a target (every issuer excluded). */
export function reportValuationBasis(
	decisions: readonly MultipleFields[],
): ValuationBasis | null {
	for (const decision of decisions) {
		const basis = valuationBasisOf(decision);
		if (basis !== null) return basis;
	}
	return null;
}

/** "P/GP", "P/S", or a neutral word when no decision names a basis. */
export function valuationBasisLabel(basis: ValuationBasis | null): string {
	return basis === null ? "multiple" : VALUATION_BASIS_LABEL[basis];
}

export interface StrategyRunReport {
	strategy_id: "large_model_value_v0";
	// `strategy_smoke_fixture` is the checked-in preview; `mart` is the real
	// read from mart.strategy_runs/strategy_decisions (#362). Both carry the same
	// decision shape, so the admin page renders either.
	source: "strategy_smoke_fixture" | "mart";
	corpus_sha256: string;
	decisions: readonly StrategyRunDecision[];
	golden_mismatches: readonly string[];
	strategy_run_id?: string;
	governed?: boolean;
	// #1235: the definition the run evaluated and its review state. Absent on a report
	// that records no definition (the preview fixture), as the Python twin's None.
	definition?: StrategyRunDefinition;
}

export interface StrategyRunUnavailable {
	strategy_id: string;
	// The first three are fixture-path reasons; the last three mirror the Python
	// PostgresStrategyRunRepository so the App and MCP mart reads are semantically
	// identical (#362).
	reason:
		| "unknown_strategy_id"
		| "fixture_missing"
		| "fixture_hash_mismatch"
		| "no_runs_recorded"
		| "database_unavailable"
		| "schema_mismatch"
		| "empty_decisions";
}

export class StrategyRunContractError extends Error {
	constructor(message: string) {
		super(message);
		this.name = "StrategyRunContractError";
	}
}

function fail(path: string, message: string): never {
	throw new StrategyRunContractError(`${path}: ${message}`);
}

function isObject(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}

function asObject(value: unknown, path: string): Record<string, unknown> {
	if (!isObject(value)) fail(path, "expected an object");
	return value;
}

/** `optional` exists because the TypeScript interface already said these fields
 *  were optional and this function did not honour it. `peg?` carries the comment
 *  "optional so a report produced before it existed still validates", which was
 *  true of the type and false of the runtime check — the committed fixture only
 *  passed because it had been regenerated to carry the field.
 *
 *  Unknown keys are still rejected. The relaxation is one-directional: a report
 *  written before a field existed validates; a report carrying a field nobody
 *  declared does not. */
function assertExactKeys(
	value: Record<string, unknown>,
	expected: readonly string[],
	path: string,
	optional: readonly string[] = [],
): void {
	const expectedSet = new Set(expected);
	const optionalSet = new Set(optional);
	const unknown = Object.keys(value).filter((key) => !expectedSet.has(key));
	const missing = expected.filter(
		(key) => !(key in value) && !optionalSet.has(key),
	);
	if (unknown.length > 0)
		fail(path, `unknown fields: ${unknown.sort().join(", ")}`);
	if (missing.length > 0)
		fail(path, `missing fields: ${missing.sort().join(", ")}`);
}

/**
 * Compares two finite decimal-string tokens (as matched by DECIMAL_PATTERN)
 * without going through `Number()`, so an arbitrarily precise value near a
 * bound (e.g. "1.000000000000000001") can't round to exactly the bound and
 * slip past a strict comparison (see #356's review).
 */
function compareDecimalStrings(a: string, b: string): number {
	const parse = (token: string) => {
		const negative = token.startsWith("-");
		const unsigned = token.replace(/^[+-]/, "");
		const [integerPart = "", fractionalPart = ""] = unsigned.split(".");
		return {
			negative,
			integerPart: integerPart.replace(/^0+(?=\d)/, "") || "0",
			fractionalPart: fractionalPart.replace(/0+$/, ""),
		};
	};
	const left = parse(a);
	const right = parse(b);
	if (left.negative !== right.negative) return left.negative ? -1 : 1;
	const sign = left.negative ? -1 : 1;
	if (left.integerPart.length !== right.integerPart.length) {
		return sign * (left.integerPart.length - right.integerPart.length);
	}
	if (left.integerPart !== right.integerPart)
		return sign * (left.integerPart < right.integerPart ? -1 : 1);
	const width = Math.max(
		left.fractionalPart.length,
		right.fractionalPart.length,
	);
	const leftFraction = left.fractionalPart.padEnd(width, "0");
	const rightFraction = right.fractionalPart.padEnd(width, "0");
	if (leftFraction === rightFraction) return 0;
	return sign * (leftFraction < rightFraction ? -1 : 1);
}

function asDecimalString(
	value: unknown,
	path: string,
	bounds?: readonly [number, number],
): string | null {
	if (value === null) return null;
	if (typeof value !== "string" || !DECIMAL_PATTERN.test(value)) {
		fail(path, "expected a decimal string");
	}
	if (bounds) {
		const [min, max] = bounds;
		if (
			compareDecimalStrings(value, String(min)) < 0 ||
			compareDecimalStrings(value, String(max)) > 0
		) {
			fail(path, `decimal is outside [${min}, ${max}]`);
		}
	}
	return value;
}

function parseDecision(value: unknown, path: string): StrategyRunDecision {
	const object = asObject(value, path);
	assertExactKeys(
		object,
		[
			"issuer_id",
			"cutoff_at",
			"outcome",
			"eligible",
			"tier",
			"capital_adjusted_labor_efficiency",
			"current_price_to_sales",
			"target_price_to_sales",
			"current_price_to_gross_profit",
			"target_price_to_gross_profit",
			"valuation_gap",
			"confidence",
			"exclusion_reason",
			"rank",
			"target_weight",
			"peg",
			"peg_rank",
			"labor_efficiency_metric",
		],
		path,
		// Optional exactly where the interface says optional: PEG (#284), the input
		// provenance joined from mart.topt_core_results, the labor-efficiency metric
		// name (#1176) and the P/GP pair (#1235). A report produced before any of them
		// existed still validates.
		[
			"peg",
			"peg_rank",
			"labor_efficiency_metric",
			"current_price_to_gross_profit",
			"target_price_to_gross_profit",
		],
	);

	const issuerId = object.issuer_id;
	if (typeof issuerId !== "string" || issuerId.length === 0)
		fail(`${path}.issuer_id`, "expected a non-empty string");

	const cutoffAt = object.cutoff_at;
	if (
		typeof cutoffAt !== "string" ||
		!CUTOFF_PATTERN.test(cutoffAt) ||
		Number.isNaN(Date.parse(cutoffAt))
	) {
		fail(`${path}.cutoff_at`, "expected an aware ISO date-time");
	}

	const outcome = object.outcome;
	if (
		typeof outcome !== "string" ||
		!(STRATEGY_RUN_OUTCOMES as readonly string[]).includes(outcome)
	) {
		fail(`${path}.outcome`, "unknown outcome");
	}

	if (typeof object.eligible !== "boolean")
		fail(`${path}.eligible`, "expected a boolean");

	const tier = object.tier;
	if (
		tier !== null &&
		!(VALUATION_TIERS as readonly string[]).includes(tier as string)
	) {
		fail(`${path}.tier`, "unknown tier");
	}

	if (
		object.exclusion_reason !== null &&
		typeof object.exclusion_reason !== "string"
	) {
		fail(`${path}.exclusion_reason`, "expected a string or null");
	}

	const rank = object.rank;
	if (
		rank !== null &&
		(typeof rank !== "number" || !Number.isInteger(rank) || rank < 1)
	) {
		fail(`${path}.rank`, "expected a positive integer or null");
	}

	// Module 1's ordering, validated the same way and kept separate from `rank` (#284).
	const pegRank = object.peg_rank ?? null;
	if (
		pegRank !== null &&
		(typeof pegRank !== "number" || !Number.isInteger(pegRank) || pegRank < 1)
	) {
		fail(`${path}.peg_rank`, "expected a positive integer or null");
	}

	const laborEfficiencyMetric = object.labor_efficiency_metric ?? null;
	if (
		laborEfficiencyMetric !== null &&
		(typeof laborEfficiencyMetric !== "string" || laborEfficiencyMetric.length === 0)
	) {
		fail(`${path}.labor_efficiency_metric`, "expected a non-empty metric name or null");
	}

	return {
		issuer_id: issuerId,
		cutoff_at: cutoffAt,
		outcome: outcome as StrategyRunOutcome,
		eligible: object.eligible,
		tier: tier as ValuationTier | null,
		capital_adjusted_labor_efficiency: asDecimalString(
			object.capital_adjusted_labor_efficiency,
			`${path}.capital_adjusted_labor_efficiency`,
		),
		current_price_to_sales: asDecimalString(
			object.current_price_to_sales,
			`${path}.current_price_to_sales`,
		),
		target_price_to_sales: asDecimalString(
			object.target_price_to_sales,
			`${path}.target_price_to_sales`,
		),
		current_price_to_gross_profit:
			object.current_price_to_gross_profit === undefined
				? null
				: asDecimalString(
						object.current_price_to_gross_profit,
						`${path}.current_price_to_gross_profit`,
					),
		target_price_to_gross_profit:
			object.target_price_to_gross_profit === undefined
				? null
				: asDecimalString(
						object.target_price_to_gross_profit,
						`${path}.target_price_to_gross_profit`,
					),
		valuation_gap: asDecimalString(
			object.valuation_gap,
			`${path}.valuation_gap`,
		),
		confidence: asDecimalString(
			object.confidence,
			`${path}.confidence`,
			[0, 1],
		),
		exclusion_reason: object.exclusion_reason as string | null,
		rank: rank as number | null,
		target_weight: asDecimalString(
			object.target_weight,
			`${path}.target_weight`,
			[0, 1],
		),
		peg:
			object.peg === undefined
				? null
				: asDecimalString(object.peg, `${path}.peg`),
		peg_rank: pegRank as number | null,
		labor_efficiency_metric: laborEfficiencyMetric as string | null,
	};
}

/** Parses and strictly validates a `StrategyRunReport`, rejecting unknown fields. */
export function asStrategyRunReport(value: unknown): StrategyRunReport {
	const object = asObject(value, "$");
	assertExactKeys(
		object,
		[
			"strategy_id",
			"source",
			"corpus_sha256",
			"decisions",
			"golden_mismatches",
			"strategy_run_id",
			"governed",
			"definition",
		],
		"$",
		["strategy_run_id", "governed", "definition"],
	);

	if (object.strategy_id !== "large_model_value_v0")
		fail("$.strategy_id", "unknown strategy_id");
	if (object.source !== "strategy_smoke_fixture")
		fail("$.source", "unknown source");

	const corpusSha256 = object.corpus_sha256;
	if (typeof corpusSha256 !== "string" || !SHA256_PATTERN.test(corpusSha256)) {
		fail("$.corpus_sha256", "expected a sha256 hex digest");
	}

	if (!Array.isArray(object.decisions))
		fail("$.decisions", "expected an array");
	const decisions = object.decisions.map((decision, index) =>
		parseDecision(decision, `$.decisions[${index}]`),
	);

	if (
		!Array.isArray(object.golden_mismatches) ||
		object.golden_mismatches.some((item) => typeof item !== "string")
	) {
		fail("$.golden_mismatches", "expected a string array");
	}

	if (
		object.strategy_run_id !== undefined &&
		typeof object.strategy_run_id !== "string"
	) {
		fail("$.strategy_run_id", "expected a string");
	}

	if (
		object.governed !== undefined &&
		typeof object.governed !== "boolean"
	) {
		fail("$.governed", "expected a boolean");
	}

	const report: StrategyRunReport = {
		strategy_id: "large_model_value_v0",
		source: "strategy_smoke_fixture",
		corpus_sha256: corpusSha256,
		decisions,
		golden_mismatches: object.golden_mismatches as string[],
		governed: typeof object.governed === "boolean" ? object.governed : false,
	};
	if (typeof object.strategy_run_id === "string") {
		report.strategy_run_id = object.strategy_run_id;
	}
	// #1235: null is the Python twin's serialized "no definition".
	if (object.definition !== undefined && object.definition !== null) {
		const definition = asStrategyRunDefinition(object.definition);
		if (definition === null)
			fail("$.definition", "expected a definition marker that the definition registry confirms");
		report.definition = definition;
	}
	return report;
}

export const parseStrategyRunReport = asStrategyRunReport;

// Resolved from process.cwd(), not import.meta.url: Next.js's webpack RSC
// bundling substitutes a URL implementation that node:fs's readFileSync does
// not recognize as `instanceof URL` (verified — both a raw URL and
// fileURLToPath(url) throw ERR_INVALID_ARG_TYPE at request time, though both
// work fine under a bare `bun run` or `tsc`). Every invocation of this
// package (bun test, `next dev`/`build`/`start`) runs with cwd == apps/app-web
// (see the Makefile), so this stays stable across both.
const FIXTURE_RELATIVE =
	"common/contracts/src/truealpha_contracts/data/strategy_run_preview.v1.json";
const FIXTURE_PATH = join(process.cwd(), "..", "..", FIXTURE_RELATIVE);
const REPO_ROOT_FIXTURE_PATH = join(process.cwd(), FIXTURE_RELATIVE);

/**
 * Loads the one checked-in `large_model_value_v0` preview fixture — server-side only.
 * Mirrors `truealpha_contracts.strategy_run_fixture.FixtureStrategyRunRepository`.
 */
export class FixtureStrategyRunRepository {
	private readonly fixturePath: string | undefined;

	constructor(fixturePath?: string) {
		this.fixturePath = fixturePath;
	}

	/** `context` is reserved for a future authorization decision; unused today. */
	getLatest(
		strategyId: string,
		_context: AccessContext,
	): StrategyRunReport | StrategyRunUnavailable {
		let raw: string;
		try {
			raw = readFileSync(this.fixturePath ?? FIXTURE_PATH, "utf8");
		} catch (error) {
			if ((error as NodeJS.ErrnoException).code === "ENOENT" && !this.fixturePath) {
				try {
					raw = readFileSync(REPO_ROOT_FIXTURE_PATH, "utf8");
				} catch (fallbackError) {
					if ((fallbackError as NodeJS.ErrnoException).code === "ENOENT") {
						return { strategy_id: strategyId, reason: "fixture_missing" };
					}
					throw fallbackError;
				}
			} else if ((error as NodeJS.ErrnoException).code === "ENOENT") {
				return { strategy_id: strategyId, reason: "fixture_missing" };
			} else {
				throw error;
			}
		}

		let report: StrategyRunReport;
		try {
			report = parseStrategyRunReport(JSON.parse(raw));
		} catch (error) {
			if (
				error instanceof StrategyRunContractError ||
				error instanceof SyntaxError
			) {
				return { strategy_id: strategyId, reason: "fixture_hash_mismatch" };
			}
			throw error;
		}

		if (strategyId !== report.strategy_id) {
			return { strategy_id: strategyId, reason: "unknown_strategy_id" };
		}
		return {
			...report,
			strategy_run_id: "strategy_smoke_fixture",
			governed: false,
		};
	}
}
