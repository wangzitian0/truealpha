/**
 * Test-only fixture parser and repository for `strategy_run_preview.v1.json`.
 * Relocated out of `src/contracts/strategyRun.ts` under AGENTS.md Rule 6.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { asStrategyRunDefinition } from "../../src/contracts/strategyDefinition";
import {
	type AccessContext,
	STRATEGY_RUN_OUTCOMES,
	type StrategyRunDecision,
	type StrategyRunOutcome,
	type StrategyRunReport,
	type StrategyRunUnavailable,
	VALUATION_TIERS,
	type ValuationTier,
} from "../../src/contracts/strategyRun";

const SHA256_PATTERN = /^[0-9a-f]{64}$/;
const CUTOFF_PATTERN =
	/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/;
const DECIMAL_PATTERN = /^[+-]?(?:\d+\.?\d*|\.\d+)$/;

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
	if (object.definition !== undefined && object.definition !== null) {
		const definition = asStrategyRunDefinition(object.definition);
		if (definition === null)
			fail("$.definition", "expected a definition marker that the definition registry confirms");
		report.definition = definition;
	}
	return report;
}

export const parseStrategyRunReport = asStrategyRunReport;

const FIXTURE_RELATIVE =
	"common/contracts/src/truealpha_contracts/data/strategy_run_preview.v1.json";
const FIXTURE_PATH = join(process.cwd(), "..", "..", FIXTURE_RELATIVE);
const REPO_ROOT_FIXTURE_PATH = join(process.cwd(), FIXTURE_RELATIVE);

export class FixtureStrategyRunRepository {
	private readonly fixturePath: string | undefined;

	constructor(fixturePath?: string) {
		this.fixturePath = fixturePath;
	}

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
