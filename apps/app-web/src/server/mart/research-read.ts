/**
 * TypeScript mart read adapter for the research dashboard — see #370.
 *
 * Conforms to the provisional read models the App and MCP already share (`#347`'s
 * `strategyRun.ts`), pending #41's stable seven-module `ResearchReadRepository`. Defaults
 * to `MartStrategyRunRepository` (real `mart.strategy_runs`/`strategy_decisions` reads via
 * `mart_readonly`, #362) so the App reproduces the MCP comparison/ranking values exactly
 * for the same governed run. `FixtureStrategyRunRepository` is an injection point for
 * tests only — production callers never construct this class with it (#429/AGENTS.md
 * rule 6: fixtures live in tests, never on a deployed route).
 *
 * Boundary: this module performs NO cross-factor computation. It selects, labels, sorts,
 * and copies already-materialized values through byte-exact. It never joins two factors or
 * two time points into a new metric (init.md Section 1, rule 2). A boundary scan in
 * `tests/dashboard-boundary.test.ts` statically forbids metric arithmetic here.
 *
 * Server-only; never import into a client component.
 */

import {
	type AccessContext,
	currentMultipleOf,
	type StrategyRunDecision,
	type StrategyRunOutcome,
	type StrategyRunReport,
	type StrategyRunUnavailable,
	targetMultipleOf,
	type ValuationBasis,
	valuationBasisOf,
} from "@/contracts/strategyRun";
import {
	type DecisionProvenance,
	MartStrategyRunRepository,
} from "./strategy-run-repository";
import { withMartReadonly } from "./db";
import type { MartClientLike } from "./topt-gppe-repository";

export type Availability =
	| "available"
	| "unavailable"
	| "stale"
	| "excluded"
	| "low_confidence"
	| "error";

/** The strategy run whose materialized decisions back the current dashboard surfaces. */
export const DASHBOARD_STRATEGY_ID = "large_model_value_v0";

/**
 * #1062 / Rule B: consumer queries on run-addressed mart relations read
 * through the governed head exposed by mart.served_head.
 */
export const SERVED_HEAD_SQL = `select run_id, freshness, availability from mart.served_head`;

export interface RunIdentity {
	strategyRunId: string | null;
	executedAt: string | null;
	source: string;
	corpusPrefix: string;
}

export interface ModuleOverviewRow {
	module: number;
	name: string;
	note: string;
	gate: string;
	availability: Availability;
	/** How many of this run's decisions actually carry the module's value.
	 *
	 * `availability` was decided by `decisions.some(...)`: ONE non-null row out
	 * of 660 made a module read "available". PEG carries 28 of 660 — 4% — and
	 * the card said available, which is the #537 shape (a report that cannot
	 * report a problem). A status word cannot distinguish 4% from 100%, so the
	 * count travels with it.
	 *
	 * Counting non-null values over rows already loaded for this page is
	 * filtering, not metric arithmetic — the same thing /research/coverage
	 * already does ("17 of 20 companies have every input"). No factor is
	 * combined with another and nothing new is computed (init.md principle 2).
	 */
	coverage: { withValue: number; total: number } | null;
	/** The table this module's value is read from: the decision table, or its own mart output. */
	source: string;
	/** Output counts for modules 3 to 6, read at read time. Null for decision modules. */
	output: ModuleOutputCount | null;
}

/** The mart tables that hold the output of modules 3 to 6. */
export type MartOutputTable =
	| "mart.issuer_supply_chain_exposure"
	| "mart.issuer_analyst_ratings"
	| "mart.fund_virtual_company"
	| "mart.issuer_theme_purity";

/** Modules 1, 2 and 7 read their values from the strategy decision table. */
const DECISION_SOURCE = "mart.strategy_decisions";

/** What one output table holds for its newest governed run, counted at read time. */
export interface ModuleOutputCount {
	table: MartOutputTable;
	/** The run whose rows were counted: the run with the newest cutoff. Null when the table is empty. */
	runId: string | null;
	/** Rows the run wrote to the table. Zero when the table is empty. */
	rows: number;
	/** Rows whose availability_status is 'available'. Fill rows and refusals are not counted here. */
	availableRows: number;
	/** The newest cutoff of that run, ISO-8601 UTC to the second. Null when the table is empty. */
	latestCutoff: string | null;
}

export interface RankingRow {
	rank: number | null;
	issuerId: string;
	cutoffAt: string;
	outcome: StrategyRunOutcome;
	tier: string | null;
	currentPriceToSales: string | null;
	targetPriceToSales: string | null;
	// #1235: the P/GP pair of definition 1.0.0, and the multiple the gap divides.
	currentPriceToGrossProfit: string | null;
	targetPriceToGrossProfit: string | null;
	valuationBasis: ValuationBasis | null;
	currentMultiple: string | null;
	targetMultiple: string | null;
	valuationGap: string | null;
	targetWeight: string | null;
	// Module 1 (#284). `pegRank` is PEG's own ordering, deliberately separate from `rank`:
	// PEG does not participate in selection, so a page must never present the two as one
	// ranking.
	peg: string | null;
	pegRank: number | null;
	pegReasonCodes?: readonly string[];
	confidence: string | null;
	availability: Availability;
	traceId: string;
	// What the number was computed FROM, so drift is visible on the surface
	// instead of found by audit. init.md states the reason for point-in-time is
	// that "every step downstream has to be traceable back to the original raw
	// material"; the decision row carries the verdict and the core result carries
	// the inputs, and they were never joined.
	//
	// The period ends span 2025-08-31 to 2026-01-25 in the run serving /research
	// today — five months behind one "current P/S" column, previously invisible.
	provenance: Provenance;
}

/** The inputs behind one published number. Every field is nullable on purpose:
 *  an absent vintage is information ("we do not know when this was true"), and
 *  hiding it behind a dash is what let #529 stand. */
/** An absent entry is information, not an error: a decision whose core result
 *  was not joined has no known vintage, and the surface must say so rather than
 *  imply freshness. */
function provenanceOf(inputs: DecisionProvenance | undefined): Provenance {
	return {
		operatingPeriodEnd: inputs?.operating_period_end ?? null,
		revenuePeriodEnd: inputs?.revenue_period_end ?? null,
		sharesPeriodEnd: inputs?.shares_period_end ?? null,
		universeVersion: inputs?.universe_version ?? null,
		universeSha256: inputs?.universe_sha256 ?? null,
		gppeDefinitionSha256: inputs?.gppe_definition_sha256 ?? null,
		tierDefinitionSha256: inputs?.tier_definition_sha256 ?? null,
	};
}

export interface Provenance {
	operatingPeriodEnd: string | null;
	revenuePeriodEnd: string | null;
	sharesPeriodEnd: string | null;
	universeVersion: string | null;
	universeSha256: string | null;
	gppeDefinitionSha256: string | null;
	tierDefinitionSha256: string | null;
}

export interface ComparisonRow {
	issuerId: string;
	cutoffAt: string;
	capitalAdjustedLaborEfficiency: string | null;
	/** The metric name the strategy used for `capitalAdjustedLaborEfficiency` (#1176). */
	laborEfficiencyMetric?: string | null;
	currentPriceToSales: string | null;
	targetPriceToSales?: string | null;
	// #1235: the P/GP pair of definition 1.0.0, and the multiple the gap divides.
	currentPriceToGrossProfit?: string | null;
	targetPriceToGrossProfit?: string | null;
	valuationBasis?: ValuationBasis | null;
	currentMultiple?: string | null;
	targetMultiple?: string | null;
	tier: string | null;
	valuationGap: string | null;
	peg?: string | null;
	pegRank?: number | null;
	pegReasonCodes?: readonly string[];
	confidence: string | null;
	availability: Availability;
	traceId: string;
	// What the number was computed FROM, so drift is visible on the surface
	// instead of found by audit. init.md states the reason for point-in-time is
	// that "every step downstream has to be traceable back to the original raw
	// material"; the decision row carries the verdict and the core result carries
	// the inputs, and they were never joined.
	//
	// The period ends span 2025-08-31 to 2026-01-25 in the run serving /research
	// today — five months behind one "current P/S" column, previously invisible.
	provenance: Provenance;
}

export interface AnalystRatingsDetail {
	runId: string;
	cutoff: string;
	consensusRating: string | null;
	targetPrice: string | null;
	analystsCount: number;
	buyCount: number;
	holdCount: number;
	sellCount: number;
	confidence: string | null;
	reasonCodes: string[];
	extractor: string;
	availabilityStatus: string;
	sourceEvidenceStatus: string;
	factorValidationStatus: string;
}

export interface SupplyChainExposureDetail {
	runId: string;
	cutoff: string;
	exposureScore: string | null;
	directPartners: number;
	suppliersCount: number;
	customersCount: number;
	maxPartnerShare: string | null;
	confidence: string | null;
	reasonCodes: string[];
	extractor: string;
	availabilityStatus: string;
	sourceEvidenceStatus: string;
	factorValidationStatus: string;
}

export interface EntityDetail {
	issuerId: string;
	rows: readonly ComparisonRow[];
	themes?: Array<{
		themeId: string;
		theme: string;
		themeShare: string | null;
		inThemeRevenue: string | null;
		consolidatedRevenue: string | null;
		/** Null for a fill row: it has no segments. */
		segments: number | null;
		confidence?: string | null;
		availabilityStatus: string;
		/** The reason code of a fill row (an issuer without a segment partition, #1117), else null.
		 * A fill row is "no answer, and why". It is not a refused share. */
		unvisitedReason?: string | null;
	}>;
	gppeDetail?: {
		gppe: string | null;
		operatingBranch: string | null;
		capitalAdjustedGrossProfit: string | null;
		availabilityStatus: string;
		reasonCodes: string[];
	} | null;
	analystRatings?: AnalystRatingsDetail | null;
	supplyChainExposure?: SupplyChainExposureDetail | null;
}

export interface TraceLink {
	kind: string;
	label: string;
	reference: string | null;
}

/** The text the surface prints for a lineage field that the lineage does not carry (#1241).
 * A missing field is shown as missing. It is never replaced by a default or a guess. */
export const NOT_RECORDED = "not recorded";

/** The six inputs of one TOPT decision, in display order. `key` is the payload key that
 * `mart.topt_core_meta_info` copies into `inputs` (20261012T1130). `semanticType` names the
 * lineage item that carries the input. `monetary` marks a value that the item's `currency` prices. */
export const TRACE_INPUT_SPECS = [
	{ key: "gross_profit", label: "Gross profit", semanticType: "financial-fact", monetary: true },
	{ key: "total_assets", label: "Total assets", semanticType: "financial-fact", monetary: true },
	{ key: "headcount", label: "Headcount", semanticType: "financial-fact", monetary: false },
	{ key: "shares_outstanding", label: "Shares outstanding", semanticType: "financial-fact", monetary: false },
	{ key: "revenue", label: "Revenue", semanticType: "financial-fact", monetary: true },
	{ key: "close", label: "Price (close)", semanticType: "market-price", monetary: true },
] as const;

/** Which time a `knowableAt` cell shows. A filing date is the most exact. A headcount fact has its own
 * time. The observation time is the latest of every input in the observation, so it is the least exact. */
export type KnowableAtBasis = "filed" | "input" | "observation";

/** What a `source` cell names. A headcount never takes the source vintage of the filing facts. */
export type SourceKind = "source vintage" | "raw pointer" | "evidence ref";

/** One input of a decision, as its lineage item records it. Every field except `key` and
 * `label` is null when the lineage does not carry it. */
export interface TraceInput {
	key: string;
	label: string;
	/** The value as the payload stores it: a base-10 string, copied verbatim. */
	value: string | null;
	/** The currency of a monetary value. Null for a count, and when the payload names none. */
	currency: string | null;
	/** True when `currency` applies to this input. */
	monetary: boolean;
	/** The period the figure describes: the filing's period end. */
	period: string | null;
	/** The filing date of this input when the vintage names it, else the time of the input or of its observation. */
	knowableAt: string | null;
	knowableAtBasis: KnowableAtBasis | null;
	/** The filing accession. Null for a price, and for a headcount without a filing. */
	accession: string | null;
	/** The source of this input: its own vintage id or pointer. */
	source: string | null;
	sourceKind: SourceKind | null;
}

/** The inputs of one listing. An issuer with two listings (dual-class) has one set for each (#1241). */
export interface TraceListing {
	/** The listing that owns these inputs, from the lineage payload. Null when no input names one. */
	listingId: string | null;
	/** True for the listing that the core result itself names. */
	isResultListing: boolean;
	/** Always the six in `TRACE_INPUT_SPECS`. */
	inputs: readonly TraceInput[];
}

export interface TraceView {
	traceId: string;
	strategyId: string;
	source: string;
	issuerId: string;
	cutoffAt: string;
	corpusSha256: string;
	links: readonly TraceLink[];
	/** The inputs of the decision (#1241), one set per listing. */
	listings: readonly TraceListing[];
	/** The core result whose lineage supplied `listings`. Null when no lineage was read. */
	lineageResultId: string | null;
	/** True when the governed head resolves this run. False when the page shows the newest recorded run
	 * because no head resolves one. Null when the report does not say (the checked-in fixture). */
	governed: boolean | null;
}

export class MartReadUnavailable extends Error {
	readonly reason: string;
	constructor(reason: string) {
		super(`mart read unavailable: ${reason}`);
		this.name = "MartReadUnavailable";
		this.reason = reason;
	}
}

export interface ModuleCatalogEntry {
	module: number;
	name: string;
	note: string;
	gate: string;
	/** The decision key whose presence proves a decision module. Null for an output-table module. */
	field: keyof StrategyRunDecision | null;
	/** The mart table a module's output is read from. Null for a decision module. */
	outputTable: MartOutputTable | null;
}

// The seven modules and the strategy composite. `field` names the decision field whose
// presence proves the module is materialized in the strategy run. `outputTable` names the
// mart table a module writes its own output to; modules 3 to 6 have one.
export const MODULE_CATALOG: readonly ModuleCatalogEntry[] = [
	// `field` must name the decision key the module writes, or the badge reports the
	// catalog's opinion instead of the data (Copilot review on #603: PEG read "unavailable"
	// while `peg` values were present and rendering on /research/rankings).
	{
		module: 1,
		name: "PEG",
		note: "recency-weighted historical growth",
		gate: "Gate 2",
		field: "peg",
		outputTable: null,
	},
	{
		module: 2,
		name: "Labor efficiency",
		note: "capital-adjusted labor efficiency, named per issuer class (#1176)",
		gate: "Gate 1",
		field: "capital_adjusted_labor_efficiency",
		outputTable: null,
	},
	{
		module: 3,
		name: "Supply-chain graph",
		note: "confidence-gated scenario exposure",
		gate: "Gate 2",
		field: null,
		outputTable: "mart.issuer_supply_chain_exposure",
	},
	{
		module: 4,
		name: "Analyst backtesting",
		note: "PIT event eligibility and outcomes",
		gate: "Gate 2",
		field: null,
		outputTable: "mart.issuer_analyst_ratings",
	},
	{
		module: 5,
		name: "ETF virtual company",
		note: "delayed N-PORT holdings",
		gate: "Gate 2",
		field: null,
		outputTable: "mart.fund_virtual_company",
	},
	{
		module: 6,
		name: "Pure-blood screening",
		note: "traceable segment classification",
		gate: "Gate 2",
		field: null,
		outputTable: "mart.issuer_theme_purity",
	},
	{
		module: 7,
		name: "Three-tier valuation",
		note: "materialized composite factor",
		gate: "Gate 1",
		field: "tier",
		outputTable: null,
	},
];

/** The table a module's value comes from: its own output table, or the decision table. */
export function moduleSourceTable(entry: ModuleCatalogEntry): string {
	return entry.outputTable ?? DECISION_SOURCE;
}

/**
 * Counts the newest run in one output table, in one statement. The newest run has the newest
 * cutoff. The theme-purity reader uses the same rule. `table` is a typed constant, never input.
 */
export function moduleOutputCountSql(table: MartOutputTable): string {
	return `
		with newest as (
			select run_id
			from ${table}
			group by run_id
			order by max(cutoff) desc
			limit 1
		)
		select (select run_id from newest) as run_id,
		       count(*)::int as row_count,
		       count(*) filter (where availability_status = 'available')::int as available_count,
		       to_char(max(cutoff) at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') as latest_cutoff
		from ${table}
		where run_id = (select run_id from newest)
	`;
}

function countOf(value: unknown, column: string): number {
	if (typeof value !== "number" || !Number.isInteger(value)) {
		throw new MartReadUnavailable(`${column} is not an integer count`);
	}
	return value;
}

async function readOutputCount(
	client: MartClientLike,
	table: MartOutputTable,
): Promise<ModuleOutputCount> {
	const result = await client.query(moduleOutputCountSql(table));
	const row = result.rows[0];
	if (row === undefined) {
		throw new MartReadUnavailable(`${table} count returned no row`);
	}
	const runId = row.run_id;
	const latestCutoff = row.latest_cutoff;
	return {
		table,
		runId: typeof runId === "string" ? runId : null,
		rows: countOf(row.row_count, `${table}.row_count`),
		availableRows: countOf(row.available_count, `${table}.available_count`),
		latestCutoff: typeof latestCutoff === "string" ? latestCutoff : null,
	};
}

/** Exported for `tests/dashboard-read.test.ts`'s fixture-independent hard-excluded case —
 * see #370's rebase note: the shared fixture no longer contains a naturally-occurring
 * non-confidence-floor exclusion, so that branch is tested with a synthetic decision. */
export function decisionAvailability(
	decision: StrategyRunDecision,
): Availability {
	if (decision.outcome === "excluded") {
		if (decision.exclusion_reason === "below_confidence_floor")
			return "low_confidence";
		return "excluded";
	}
	return "available";
}

/**
 * #1241: the lineage of one strategy decision. The decision joins the core result of its capture
 * run on issuer and cutoff, the same join DECISIONS_SQL makes. The capture run comes from
 * `mart.strategy_run_capture`, which prefers the governed head's run. So the lineage read here
 * belongs to the run that the page serves, and never to the newest row. `$2` is the issuer id the
 * page shows, which is the legacy id when `mart.entity_identity` has one. Every core result of one
 * capture run reads the same frozen snapshot, so any of them names the same observations.
 * `listing_id` is the listing that the core result itself names.
 */
export const TRACE_LINEAGE_SQL = `
	select m.result_id, m.listing_id, m.lineage
	from mart.strategy_decisions d
	join mart.strategy_run_capture scope on scope.strategy_run_id = d.strategy_run_id
	join mart.topt_core_meta_info m
	  on m.run_id = scope.capture_run_id and m.issuer_id = d.issuer_id and m.cutoff = d.cutoff_at
	left join lateral (
	    select ei.legacy_id
	    from mart.entity_identity ei
	    where ei.entity_id::text = d.issuer_id
	    order by ei.legacy_id
	    limit 1
	) ei on true
	where d.strategy_run_id = $1
	  and (d.issuer_id = $2 or coalesce(ei.legacy_id, d.issuer_id) = $2)
	  and d.cutoff_at = $3::timestamptz
	order by m.result_id
	limit 1
`;

function recordOf(value: unknown): Record<string, unknown> | null {
	return typeof value === "object" && value !== null && !Array.isArray(value)
		? (value as Record<string, unknown>)
		: null;
}

/** A lineage field is text, or it is not recorded. Any other type means the view changed shape. */
function lineageText(value: unknown, field: string): string | null {
	if (value === null || value === undefined) return null;
	if (typeof value === "string") return value.length > 0 ? value : null;
	throw new MartReadUnavailable(`lineage ${field} is not text`);
}

type TraceSpec = (typeof TRACE_INPUT_SPECS)[number];

function traceInputOf(
	spec: TraceSpec,
	item: Record<string, unknown> | undefined,
): TraceInput {
	const inputs = recordOf(item?.inputs);
	const filing = recordOf(recordOf(item?.vintage)?.[spec.key]);
	const value = lineageText(inputs?.[spec.key], `${spec.key} value`);
	const observationAt = lineageText(item?.knowable_at, "knowable_at");
	const filed = lineageText(filing?.filed, `${spec.key} filed`);
	// A headcount fact carries its own knowable_at. No other vintage entry does.
	const inputAt =
		spec.key === "headcount" ? lineageText(filing?.knowable_at, "headcount knowable_at") : null;
	const [knowableAt, knowableAtBasis]: [string | null, KnowableAtBasis | null] =
		filed !== null
			? [filed, "filed"]
			: inputAt !== null
				? [inputAt, "input"]
				: observationAt !== null
					? [observationAt, "observation"]
					: [null, null];
	// The headcount comes from another source than the filing facts, so it names its own pointer.
	// The observation's source vintage is the filing facts' vintage and never labels a headcount.
	const headcountRaw = spec.key === "headcount" ? lineageText(filing?.raw, "headcount raw") : null;
	const headcountRef =
		spec.key === "headcount" ? lineageText(filing?.evidence_ref, "headcount evidence_ref") : null;
	const [source, sourceKind]: [string | null, SourceKind | null] =
		spec.key !== "headcount"
			? [lineageText(item?.source_vintage_id, "source_vintage_id"), "source vintage"]
			: headcountRaw !== null
				? [headcountRaw, "raw pointer"]
				: headcountRef !== null
					? [headcountRef, "evidence ref"]
					: [null, null];
	return {
		key: spec.key,
		label: spec.label,
		value,
		currency: spec.monetary ? lineageText(inputs?.currency, "currency") : null,
		monetary: spec.monetary,
		period: lineageText(filing?.period_end, `${spec.key} period_end`),
		knowableAt,
		knowableAtBasis,
		accession: lineageText(filing?.accession, `${spec.key} accession`),
		source,
		sourceKind: source === null ? null : sourceKind,
	};
}

/**
 * Maps the `lineage` array of one `mart.topt_core_meta_info` row to the inputs of the decision, one
 * set per listing. A core result covers an issuer. An issuer with two listings has two financial-fact
 * and two market-price items, and `inputs.listing_id` says which item belongs to which listing. It
 * copies fields and computes nothing. A field that the lineage does not carry stays null.
 */
export function traceInputsFromLineage(
	lineage: unknown,
	resultListingId: string | null = null,
): TraceListing[] {
	if (!Array.isArray(lineage)) {
		throw new MartReadUnavailable("lineage is not an array");
	}
	const items = lineage.map((item) => {
		const record = recordOf(item);
		if (record === null) throw new MartReadUnavailable("a lineage item is not an object");
		return record;
	});
	const listingOf = (item: Record<string, unknown>): string => {
		const listingId = lineageText(recordOf(item.inputs)?.listing_id, "inputs listing_id");
		if (listingId === null) {
			throw new MartReadUnavailable("a lineage item names no listing");
		}
		return listingId;
	};
	const carriers = items.filter(
		(item) => item.semantic_type === "financial-fact" || item.semantic_type === "market-price",
	);
	const listingIds = [...new Set(carriers.map(listingOf))].sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
	if (listingIds.length === 0) {
		// Nothing was read: show the six inputs as not recorded, under the listing the result names.
		return [
			{
				listingId: resultListingId,
				isResultListing: resultListingId !== null,
				inputs: TRACE_INPUT_SPECS.map((spec) => traceInputOf(spec, undefined)),
			},
		];
	}
	// The listing that the result names comes first, then the others in id order.
	const ordered = [
		...listingIds.filter((listingId) => listingId === resultListingId),
		...listingIds.filter((listingId) => listingId !== resultListingId),
	];
	return ordered.map((listingId) => ({
		listingId,
		isResultListing: listingId === resultListingId,
		inputs: TRACE_INPUT_SPECS.map((spec) =>
			traceInputOf(
				spec,
				carriers.find((item) => item.semantic_type === spec.semanticType && listingOf(item) === listingId),
			),
		),
	}));
}

function traceId(
	source: string,
	issuerId: string,
	cutoffAt: string,
	corpusSha256: string,
): string {
	// Full cutoffAt, not just its date: truncating to YYYY-MM-DD would collide across
	// multiple same-day cutoffs (Copilot review on #387). `source` (not a hardcoded
	// "strategy_smoke_fixture" literal, #370) mirrors research_report_fixture.py's
	// _trace() (#369/#383) so both sides stay byte-identical, and stays honest once this
	// adapter's default is mart-backed instead of fixture-backed.
	const corpusPrefix = corpusSha256.slice(0, 12);
	return `${source}:${corpusPrefix}:${issuerId}:${cutoffAt}`;
}

/** Orders ranked members first (ascending rank), then unranked members by issuer id. */
function rankingOrder(a: StrategyRunDecision, b: StrategyRunDecision): number {
	const aRanked = a.rank !== null;
	const bRanked = b.rank !== null;
	if (aRanked && bRanked && a.rank !== b.rank)
		return (a.rank as number) < (b.rank as number) ? -1 : 1;
	if (aRanked !== bRanked) return aRanked ? -1 : 1;
	if (a.issuer_id === b.issuer_id) return 0;
	return a.issuer_id < b.issuer_id ? -1 : 1;
}

/** `getLatest` may return synchronously (the fixture repository, tests only) or
 * asynchronously (`MartStrategyRunRepository`, the production default) — `report()`
 * always `await`s, which is a no-op on an already-resolved value. */
export interface StrategyRunRepositoryLike {
	getLatest(
		strategyId: string,
		context: AccessContext,
	):
		| StrategyRunReport
		| StrategyRunUnavailable
		| Promise<StrategyRunReport | StrategyRunUnavailable>;
}

/**
 * Reads dashboard views from the materialized strategy run. Every read takes a
 * server-derived `AccessContext`; it is never accepted from client input. The context is
 * reserved for a future authorization decision (mirrors the provisional repositories).
 */
export class StrategyRunReadAdapter {
	private readonly repository: StrategyRunRepositoryLike;
	// A single loader call (e.g. loadOverview) reads the same context through more than one
	// public method here (overview() + latestCutoff(), or latestCutoff() + ranking()) on one
	// adapter instance. With a real mart read behind report(), that used to mean a redundant
	// Postgres round trip per request (harmless against the old in-memory fixture read, real
	// cost against Postgres — Copilot review on #438). Keyed by contextId, not unconditional,
	// since nothing stops a caller from reusing one instance across two different contexts.
	private readonly reportCache = new Map<string, Promise<StrategyRunReport>>();
	private readonly runWithClient: <T>(
		fn: (client: MartClientLike) => Promise<T>,
	) => Promise<T>;

	/** `runWithClient` is an injection point for tests only (a fake client, no connection).
	 * Production callers omit it and get the `mart_readonly` session from `withMartReadonly`. */
	constructor(
		repository?: StrategyRunRepositoryLike,
		runWithClient: <T>(
			fn: (client: MartClientLike) => Promise<T>,
		) => Promise<T> = withMartReadonly,
	) {
		this.repository = repository ?? new MartStrategyRunRepository();
		this.runWithClient = runWithClient;
	}

	/** `provenance` is optional because `FixtureStrategyRunRepository` legitimately
	 * has none — a checked-in preview has no mart row to join. An absent map
	 * renders every vintage as "unknown", which is the honest answer. */
	private report(
		context: AccessContext,
	): Promise<
		StrategyRunReport & { provenance?: ReadonlyMap<string, DecisionProvenance> }
	> {
		const cached = this.reportCache.get(context.contextId);
		if (cached !== undefined) return cached;
		const promise = (async () => {
			const result = await this.repository.getLatest(
				DASHBOARD_STRATEGY_ID,
				context,
			);
			if (!("decisions" in result))
				throw new MartReadUnavailable(result.reason);
			return result;
		})();
		this.reportCache.set(context.contextId, promise);
		return promise;
	}

	/** The governed run identity the dashboard renders (#370 appended AC 3):
	 * comparable 1:1 with the MCP strategy_run tool's head run. Fixture-backed
	 * reports carry no run id and honestly render null. */
	async runIdentity(context: AccessContext): Promise<RunIdentity> {
		const report = (await this.report(context)) as StrategyRunReport & {
			strategy_run_id?: string;
			executed_at?: string;
		};
		return {
			strategyRunId: report.strategy_run_id ?? null,
			executedAt: report.executed_at ?? null,
			source: report.source,
			corpusPrefix: report.corpus_sha256.slice(0, 12),
		};
	}

	async latestCutoff(context: AccessContext): Promise<string | null> {
		const cutoffs = (await this.report(context)).decisions.map(
			(decision) => decision.cutoff_at,
		);
		if (cutoffs.length === 0) return null;
		return cutoffs.slice().sort().reverse()[0];
	}

	/** Counts each output table's newest run. One session; one count statement per table. */
	private async readModuleOutputs(): Promise<
		Map<MartOutputTable, ModuleOutputCount>
	> {
		const tables = MODULE_CATALOG.flatMap((entry) =>
			entry.outputTable === null ? [] : [entry.outputTable],
		);
		return this.runWithClient(async (client) => {
			const counts = new Map<MartOutputTable, ModuleOutputCount>();
			for (const table of tables) {
				counts.set(table, await readOutputCount(client, table));
			}
			return counts;
		});
	}

	async overview(context: AccessContext): Promise<ModuleOverviewRow[]> {
		const decisions = (await this.report(context)).decisions;
		const outputs = await this.readModuleOutputs();
		return MODULE_CATALOG.map((entry) => {
			const source = moduleSourceTable(entry);
			if (entry.outputTable !== null) {
				const output = outputs.get(entry.outputTable);
				if (output === undefined) {
					throw new MartReadUnavailable(`no count read for ${entry.outputTable}`);
				}
				return {
					module: entry.module,
					name: entry.name,
					note: entry.note,
					gate: entry.gate,
					availability: output.availableRows > 0 ? "available" : "unavailable",
					coverage: null,
					source,
					output,
				};
			}
			const withValue =
				entry.field === null
					? 0
					: decisions.filter(
							(decision) =>
								decision[entry.field as keyof StrategyRunDecision] !== null,
						).length;
			return {
				module: entry.module,
				name: entry.name,
				note: entry.note,
				gate: entry.gate,
				availability: withValue > 0 ? "available" : "unavailable",
				coverage:
					entry.field === null ? null : { withValue, total: decisions.length },
				source,
				output: null,
			};
		});
	}

	private toRankingRow(
		decision: StrategyRunDecision,
		source: string,
		corpusSha256: string,
		inputs: DecisionProvenance | undefined,
	): RankingRow {
		const status = decisionAvailability(decision);
		return {
			rank: decision.rank,
			issuerId: decision.issuer_id,
			cutoffAt: decision.cutoff_at,
			outcome: decision.outcome,
			tier: decision.tier,
			currentPriceToSales: decision.current_price_to_sales,
			targetPriceToSales: decision.target_price_to_sales,
			currentPriceToGrossProfit: decision.current_price_to_gross_profit ?? null,
			targetPriceToGrossProfit: decision.target_price_to_gross_profit ?? null,
			valuationBasis: valuationBasisOf(decision),
			currentMultiple: currentMultipleOf(decision),
			targetMultiple: targetMultipleOf(decision),
			valuationGap: decision.valuation_gap,
			targetWeight: decision.target_weight,
			peg: decision.peg ?? null,
			pegRank: decision.peg_rank ?? null,
			pegReasonCodes:
				inputs?.peg_reason_codes ??
				(decision as { peg_reason_codes?: readonly string[] })
					.peg_reason_codes ??
				[],
			confidence: decision.confidence,
			availability: status,
			traceId: traceId(
				source,
				decision.issuer_id,
				decision.cutoff_at,
				corpusSha256,
			),
			provenance: provenanceOf(inputs),
		};
	}

	private toComparisonRow(
		decision: StrategyRunDecision,
		source: string,
		corpusSha256: string,
		inputs: DecisionProvenance | undefined,
	): ComparisonRow {
		// The row's own availability mirrors the decision (low_confidence/excluded must stay
		// visible even when this one field is null) — matching toRankingRow. valueAvailability
		// is for a single value's own null-driven downgrade, not the whole row (Copilot review
		// on #387, same class of bug fixed in research_report_fixture.py's #383).
		const status = decisionAvailability(decision);
		return {
			issuerId: decision.issuer_id,
			cutoffAt: decision.cutoff_at,
			capitalAdjustedLaborEfficiency:
				decision.capital_adjusted_labor_efficiency,
			laborEfficiencyMetric: decision.labor_efficiency_metric ?? null,
			currentPriceToSales: decision.current_price_to_sales,
			targetPriceToSales: decision.target_price_to_sales,
			currentPriceToGrossProfit: decision.current_price_to_gross_profit ?? null,
			targetPriceToGrossProfit: decision.target_price_to_gross_profit ?? null,
			valuationBasis: valuationBasisOf(decision),
			currentMultiple: currentMultipleOf(decision),
			targetMultiple: targetMultipleOf(decision),
			tier: decision.tier,
			valuationGap: decision.valuation_gap,
			peg: decision.peg ?? null,
			pegRank: decision.peg_rank ?? null,
			pegReasonCodes:
				inputs?.peg_reason_codes ??
				(decision as { peg_reason_codes?: readonly string[] })
					.peg_reason_codes ??
				[],
			confidence: decision.confidence,
			availability: status,
			traceId: traceId(
				source,
				decision.issuer_id,
				decision.cutoff_at,
				corpusSha256,
			),
			provenance: provenanceOf(inputs),
		};
	}

	async ranking(
		context: AccessContext,
		cutoffAt?: string,
	): Promise<RankingRow[]> {
		const report = await this.report(context);
		const cutoff = cutoffAt ?? (await this.latestCutoff(context));
		if (cutoff === null) return [];
		const decisions = report.decisions.filter(
			(decision) => decision.cutoff_at === cutoff,
		);
		const provenance =
			report.provenance ?? new Map<string, DecisionProvenance>();
		const ordered = decisions.slice().sort(rankingOrder);
		return ordered.map((decision) =>
			this.toRankingRow(
				decision,
				report.source,
				report.corpus_sha256,
				provenance.get(decision.issuer_id),
			),
		);
	}

	async comparison(
		context: AccessContext,
		cutoffAt?: string,
	): Promise<ComparisonRow[]> {
		const report = await this.report(context);
		const cutoff = cutoffAt ?? (await this.latestCutoff(context));
		if (cutoff === null) return [];
		const decisions = report.decisions.filter(
			(decision) => decision.cutoff_at === cutoff,
		);
		const provenance =
			report.provenance ?? new Map<string, DecisionProvenance>();
		const ordered = decisions
			.slice()
			.sort((a, b) =>
				a.issuer_id < b.issuer_id ? -1 : a.issuer_id > b.issuer_id ? 1 : 0,
			);
		return ordered.map((decision) =>
			this.toComparisonRow(
				decision,
				report.source,
				report.corpus_sha256,
				provenance.get(decision.issuer_id),
			),
		);
	}

	/**
	 * Resolves a route issuer coordinate (UUID or legacy_id) to the coordinate carried by
	 * the strategy run's decisions via `mart.entity_identity`. Returns `issuerId` without
	 * querying Postgres when `decisions` already contains `issuerId` or is empty.
	 */
	private async resolveDecisionIssuerId(
		issuerId: string,
		decisions: readonly StrategyRunDecision[],
	): Promise<string> {
		if (
			decisions.length === 0 ||
			decisions.some((decision) => decision.issuer_id === issuerId)
		) {
			return issuerId;
		}
		try {
			const result = await this.runWithClient((client) =>
				client.query(
					`select entity_id::text as entity_id, legacy_id
					 from mart.entity_identity
					 where entity_id::text = $1 or legacy_id = $1`,
					[issuerId],
				),
			);
			const candidates = new Set<string>();
			for (const row of result.rows) {
				if (typeof row.entity_id === "string" && row.entity_id.length > 0) {
					candidates.add(row.entity_id);
				}
				if (typeof row.legacy_id === "string" && row.legacy_id.length > 0) {
					candidates.add(row.legacy_id);
				}
			}
			const matched = decisions.find((decision) =>
				candidates.has(decision.issuer_id),
			);
			return matched ? matched.issuer_id : issuerId;
		} catch {
			return issuerId;
		}
	}

	async entityDetail(
		context: AccessContext,
		issuerId: string,
	): Promise<EntityDetail | null> {
		const report = await this.report(context);
		const resolvedIssuerId = await this.resolveDecisionIssuerId(
			issuerId,
			report.decisions,
		);
		const provenance =
			report.provenance ?? new Map<string, DecisionProvenance>();
		const rows = report.decisions
			.filter((decision) => decision.issuer_id === resolvedIssuerId)
			.slice()
			.sort((a, b) =>
				a.cutoff_at < b.cutoff_at ? -1 : a.cutoff_at > b.cutoff_at ? 1 : 0,
			)
			.map((decision) =>
				this.toComparisonRow(
					decision,
					report.source,
					report.corpus_sha256,
					provenance.get(decision.issuer_id),
				),
			);
		if (rows.length === 0) return null;
		return { issuerId: resolvedIssuerId, rows };
	}

	async traceView(
		context: AccessContext,
		issuerId: string,
		cutoffAt: string,
	): Promise<TraceView | null> {
		const report = await this.report(context);
		const resolvedIssuerId = await this.resolveDecisionIssuerId(
			issuerId,
			report.decisions,
		);
		const decision = report.decisions.find(
			(d) => d.issuer_id === resolvedIssuerId && d.cutoff_at === cutoffAt,
		);
		if (decision === undefined) return null;
		const lineage = await this.readTraceLineage(
			(report as { strategy_run_id?: string }).strategy_run_id,
			decision,
		);
		return {
			traceId: traceId(
				report.source,
				resolvedIssuerId,
				cutoffAt,
				report.corpus_sha256,
			),
			strategyId: report.strategy_id,
			source: report.source,
			issuerId: resolvedIssuerId,
			cutoffAt,
			corpusSha256: report.corpus_sha256,
			links: [
				{
					kind: "materialized_output",
					label: "Strategy decision",
					reference: `${report.strategy_id}:${resolvedIssuerId}:${cutoffAt}`,
				},
				{
					kind: "snapshot",
					label: "Run corpus (snapshot)",
					reference: report.corpus_sha256,
				},
				{ kind: "raw", label: "Immutable raw bytes", reference: null },
			],
			listings: traceInputsFromLineage(lineage?.lineage ?? [], lineage?.listingId ?? null),
			lineageResultId: lineage?.resultId ?? null,
			governed: report.governed ?? null,
		};
	}

	/** The lineage row of the decision's own capture run (#1241). A report with no run id (the
	 * checked-in fixture, tests only) and a decision with no core result both read nothing. */
	private async readTraceLineage(
		strategyRunId: string | undefined,
		decision: StrategyRunDecision,
	): Promise<{ resultId: string; listingId: string | null; lineage: unknown } | null> {
		if (strategyRunId === undefined) return null;
		const result = await this.runWithClient((client) =>
			client.query(TRACE_LINEAGE_SQL, [strategyRunId, decision.issuer_id, decision.cutoff_at]),
		);
		const row = result.rows[0];
		if (row === undefined) return null;
		if (typeof row.result_id !== "string") {
			throw new MartReadUnavailable("lineage row has no result_id");
		}
		return {
			resultId: row.result_id,
			listingId: lineageText(row.listing_id, "result listing_id"),
			lineage: row.lineage,
		};
	}

	private bidirectionalIssuerPredicate(column: string): string {
		return `(${column} = $1 or ${column} in (select entity_id::text from mart.entity_identity where legacy_id = $1) or ${column} in (select legacy_id from mart.entity_identity where entity_id::text = $1 and legacy_id is not null))`;
	}

	async entityThemePurity(
		_context: AccessContext,
		issuerId: string,
	): Promise<
		Array<{
			themeId: string;
			theme: string;
			themeShare: string | null;
			inThemeRevenue: string | null;
			consolidatedRevenue: string | null;
			segments: number | null;
			confidence: string | null;
			availabilityStatus: string;
			unvisitedReason: string | null;
		}>
	> {
		try {
			return await this.runWithClient(async (client) => {
				const result = await client.query(
					`select theme_id,
					        theme,
					        theme_share,
					        in_theme_revenue,
					        consolidated_revenue,
					        segments,
					        confidence,
					        availability_status,
					        unvisited_reason
					 from (
					     select distinct on (p.theme_id)
					            p.theme_id,
					            p.theme,
					            p.theme_share::text as theme_share,
					            p.theme_share as raw_share,
					            p.in_theme_revenue::text as in_theme_revenue,
					            p.consolidated_revenue::text as consolidated_revenue,
					            case when p.partition_id is null then null else p.segments end as segments,
					            case when p.partition_id is null then null else p.confidence::text end as confidence,
					            coalesce(p.availability_status, 'unavailable') as availability_status,
					            case when p.partition_id is null
					                 then coalesce(p.reason_codes[1], 'unvisited') end as unvisited_reason
					     from mart.issuer_theme_purity p
					     where ${this.bidirectionalIssuerPredicate("p.issuer_id")}
					     -- A row with a segment partition comes first. A newer fill row (an issuer
					     -- without a partition, #1117) must not hide an older real row.
					     order by p.theme_id, (p.partition_id is null), p.cutoff desc, p.created_at desc
					 ) latest_per_theme
					 order by raw_share desc nulls last, theme asc`,
					[issuerId],
				);
				return result.rows.map((row) => ({
					themeId: String(row.theme_id ?? ""),
					theme: String(row.theme ?? ""),
					themeShare:
						row.theme_share !== null && row.theme_share !== undefined
							? String(row.theme_share)
							: null,
					inThemeRevenue:
						row.in_theme_revenue !== null && row.in_theme_revenue !== undefined
							? String(row.in_theme_revenue)
							: null,
					consolidatedRevenue:
						row.consolidated_revenue !== null &&
						row.consolidated_revenue !== undefined
							? String(row.consolidated_revenue)
							: null,
					segments: typeof row.segments === "number" ? row.segments : null,
					confidence:
						row.confidence !== null && row.confidence !== undefined
							? String(row.confidence)
							: null,
					availabilityStatus: String(row.availability_status ?? "unavailable"),
					unvisitedReason:
						typeof row.unvisited_reason === "string" ? row.unvisited_reason : null,
				}));
			});
		} catch (error) {
			console.error("entityThemePurity error:", error);
			return [];
		}
	}

	async entityGppeDetail(
		_context: AccessContext,
		issuerId: string,
	): Promise<{
		gppe: string | null;
		operatingBranch: string | null;
		capitalAdjustedGrossProfit: string | null;
		availabilityStatus: string;
		reasonCodes: string[];
	} | null> {
		try {
			return await this.runWithClient(async (client) => {
				const result = await client.query(
					`select gppe::text as gppe,
					        operating_branch,
					        capital_adjusted_gross_profit::text as capital_adjusted_gross_profit,
					        coalesce(availability_status, availability, 'unavailable') as availability_status,
					        reason_codes
					 from mart.topt_gppe_results
					 where ${this.bidirectionalIssuerPredicate("issuer_id")}
					 order by cutoff desc, created_at desc
					 limit 1`,
					[issuerId],
				);
				if (result.rows.length === 0) return null;
				const row = result.rows[0];
				return {
					gppe:
						row.gppe !== null && row.gppe !== undefined
							? String(row.gppe)
							: null,
					operatingBranch:
						row.operating_branch !== null && row.operating_branch !== undefined
							? String(row.operating_branch)
							: null,
					capitalAdjustedGrossProfit:
						row.capital_adjusted_gross_profit !== null &&
						row.capital_adjusted_gross_profit !== undefined
							? String(row.capital_adjusted_gross_profit)
							: null,
					availabilityStatus: String(row.availability_status ?? "unavailable"),
					reasonCodes: Array.isArray(row.reason_codes)
						? row.reason_codes.map(String)
						: [],
				};
			});
		} catch (error) {
			console.error("entityGppeDetail error:", error);
			return null;
		}
	}

	async entityAnalystRatings(
		_context: AccessContext,
		issuerId: string,
	): Promise<AnalystRatingsDetail | null> {
		try {
			return await this.runWithClient(async (client) => {
				const result = await client.query(
					`select r.run_id,
					        to_char(r.cutoff at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') as cutoff,
					        r.consensus_rating::text as consensus_rating,
					        coalesce(
					            to_jsonb(r) ->> 'target_price',
					            (select substring(rc from '^target_price:([0-9.]+)$')
					             from unnest(r.reason_codes) as rc
					             where rc like 'target_price:%'
					             limit 1)
					        ) as target_price,
					        r.analysts_count,
					        r.buy_count,
					        r.hold_count,
					        r.sell_count,
					        r.confidence::text as confidence,
					        r.reason_codes,
					        r.extractor,
					        coalesce(r.availability_status, 'unavailable') as availability_status,
					        coalesce(r.source_evidence_status, 'degraded') as source_evidence_status,
					        coalesce(r.factor_validation_status, 'not_evaluated') as factor_validation_status
					 from mart.issuer_analyst_ratings r
					 where ${this.bidirectionalIssuerPredicate("r.issuer_id")}
					 order by (r.availability_status = 'available') desc, r.cutoff desc, r.created_at desc
					 limit 1`,
					[issuerId],
				);
				if (result.rows.length === 0) return null;
				const row = result.rows[0];
				const reasonCodes = Array.isArray(row.reason_codes)
					? row.reason_codes.map(String)
					: [];
				const targetFromReason = reasonCodes
					.find((code) => code.startsWith("target_price:"))
					?.slice("target_price:".length);
				const rawTarget =
					row.target_price !== null && row.target_price !== undefined
						? String(row.target_price)
						: targetFromReason ?? null;
				return {
					runId: String(row.run_id ?? ""),
					cutoff: String(row.cutoff ?? ""),
					consensusRating:
						row.consensus_rating !== null && row.consensus_rating !== undefined
							? String(row.consensus_rating)
							: null,
					targetPrice: rawTarget,
					analystsCount:
						typeof row.analysts_count === "number" ? row.analysts_count : 0,
					buyCount: typeof row.buy_count === "number" ? row.buy_count : 0,
					holdCount: typeof row.hold_count === "number" ? row.hold_count : 0,
					sellCount: typeof row.sell_count === "number" ? row.sell_count : 0,
					confidence:
						row.confidence !== null && row.confidence !== undefined
							? String(row.confidence)
							: null,
					reasonCodes,
					extractor: String(row.extractor ?? "origin:moomoo:v1"),
					availabilityStatus: String(row.availability_status ?? "unavailable"),
					sourceEvidenceStatus: String(
						row.source_evidence_status ?? "degraded",
					),
					factorValidationStatus: String(
						row.factor_validation_status ?? "not_evaluated",
					),
				};
			});
		} catch (error) {
			console.error("entityAnalystRatings error:", error);
			return null;
		}
	}

	async entitySupplyChainExposure(
		_context: AccessContext,
		issuerId: string,
	): Promise<SupplyChainExposureDetail | null> {
		try {
			return await this.runWithClient(async (client) => {
				const result = await client.query(
					`select s.run_id,
					        to_char(s.cutoff at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') as cutoff,
					        s.exposure_score::text as exposure_score,
					        s.direct_partners,
					        s.suppliers_count,
					        s.customers_count,
					        s.max_partner_share::text as max_partner_share,
					        s.confidence::text as confidence,
					        s.reason_codes,
					        s.extractor,
					        coalesce(s.availability_status, 'unavailable') as availability_status,
					        coalesce(s.source_evidence_status, 'degraded') as source_evidence_status,
					        coalesce(s.factor_validation_status, 'not_evaluated') as factor_validation_status
					 from mart.issuer_supply_chain_exposure s
					 where ${this.bidirectionalIssuerPredicate("s.issuer_id")}
					 order by (s.availability_status = 'available') desc, s.cutoff desc, s.created_at desc
					 limit 1`,
					[issuerId],
				);
				if (result.rows.length === 0) return null;
				const row = result.rows[0];
				return {
					runId: String(row.run_id ?? ""),
					cutoff: String(row.cutoff ?? ""),
					exposureScore:
						row.exposure_score !== null && row.exposure_score !== undefined
							? String(row.exposure_score)
							: null,
					directPartners:
						typeof row.direct_partners === "number" ? row.direct_partners : 0,
					suppliersCount:
						typeof row.suppliers_count === "number" ? row.suppliers_count : 0,
					customersCount:
						typeof row.customers_count === "number" ? row.customers_count : 0,
					maxPartnerShare:
						row.max_partner_share !== null &&
						row.max_partner_share !== undefined
							? String(row.max_partner_share)
							: null,
					confidence:
						row.confidence !== null && row.confidence !== undefined
							? String(row.confidence)
							: null,
					reasonCodes: Array.isArray(row.reason_codes)
						? row.reason_codes.map(String)
						: [],
					extractor: String(row.extractor ?? "graph:kg-edges:v1"),
					availabilityStatus: String(row.availability_status ?? "unavailable"),
					sourceEvidenceStatus: String(
						row.source_evidence_status ?? "degraded",
					),
					factorValidationStatus: String(
						row.factor_validation_status ?? "not_evaluated",
					),
				};
			});
		} catch (error) {
			console.error("entitySupplyChainExposure error:", error);
			return null;
		}
	}
}
