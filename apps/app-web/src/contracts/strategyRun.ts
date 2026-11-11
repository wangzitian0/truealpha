/**
 * Read-only Core Strategy run report contracts shared by MCP and the App (#347, #1235).
 */

import { UNIVERSES, findUniverseOfId } from "./environmentContract.generated";
import type { StrategyRunDefinition } from "./strategyDefinition";
import { universeWithRole } from "./universes";

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

export const RANKING_UNIVERSES: readonly string[] = UNIVERSES.filter(
	(universe) => !universe.roles.includes("sentinel") && universe.scheduled_somewhere,
).map((universe) => universe.key);

export function normalizeRankingUniverse(value: unknown): string | null {
	const normalized = typeof value === "string" ? value.toLowerCase().trim() : "";
	if (normalized === "") return universeWithRole("strategy");
	return UNIVERSES.some((universe) => universe.key === normalized) ? normalized : null;
}

export function matchesRankingUniverse(
	universeId: string | null | undefined,
	universe: string,
): boolean {
	return typeof universeId === "string" && findUniverseOfId(universeId) === universe;
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
	current_price_to_gross_profit?: string | null;
	target_price_to_gross_profit?: string | null;
	valuation_gap: string | null;
	confidence: string | null;
	exclusion_reason: string | null;
	rank: number | null;
	target_weight: string | null;
	peg?: string | null;
	peg_rank?: number | null;
	labor_efficiency_metric?: string | null;
}

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

export function currentMultipleOf(decision: MultipleFields): string | null {
	const basis = valuationBasisOf(decision);
	if (basis === "price_to_gross_profit") return decision.current_price_to_gross_profit ?? null;
	if (basis === "price_to_sales") return decision.current_price_to_sales;
	return null;
}

export function targetMultipleOf(decision: MultipleFields): string | null {
	const basis = valuationBasisOf(decision);
	if (basis === "price_to_gross_profit") return decision.target_price_to_gross_profit ?? null;
	if (basis === "price_to_sales") return decision.target_price_to_sales;
	return null;
}

export function reportValuationBasis(
	decisions: readonly MultipleFields[],
): ValuationBasis | null {
	for (const decision of decisions) {
		const basis = valuationBasisOf(decision);
		if (basis !== null) return basis;
	}
	return null;
}

export function valuationBasisLabel(basis: ValuationBasis | null): string {
	return basis === null ? "multiple" : VALUATION_BASIS_LABEL[basis];
}

export interface StrategyRunReport {
	strategy_id: "large_model_value_v0";
	source: "strategy_smoke_fixture" | "mart";
	corpus_sha256: string;
	decisions: readonly StrategyRunDecision[];
	golden_mismatches: readonly string[];
	strategy_run_id?: string;
	governed?: boolean;
	definition?: StrategyRunDefinition;
}

export interface StrategyRunUnavailable {
	strategy_id: string;
	reason:
		| "unknown_strategy_id"
		| "fixture_missing"
		| "fixture_hash_mismatch"
		| "no_runs_recorded"
		| "database_unavailable"
		| "schema_mismatch"
		| "empty_decisions";
}
