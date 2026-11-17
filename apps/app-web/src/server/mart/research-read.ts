/**
 * TypeScript mart read adapter for the research dashboard (#370).
 * Selects, sorts, and projects materialized outputs with zero cross-factor arithmetic.
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
import { nullableString, numberOrZero, recordOf, stringArray } from "@/client/coerce";
import { servedRunPredicate, withMartReadonly } from "./db";
export { servedRunPredicate } from "./db";
import { type DecisionProvenance, MartStrategyRunRepository } from "./strategy-run-repository";
import type { MartClientLike } from "./topt-gppe-repository";

export type Availability = "available" | "unavailable" | "stale" | "excluded" | "low_confidence" | "not_applicable" | "error";

export const DASHBOARD_STRATEGY_ID = "large_model_value_v0";
export const SERVED_HEAD_SQL = "select run_id, freshness, availability from mart.served_head";
export const NOT_RECORDED = "not recorded";

export interface RunIdentity {
	strategyRunId: string | null;
	executedAt: string | null;
	source: string;
	corpusPrefix: string;
}

type MartOutputTable =
	| "mart.issuer_supply_chain_exposure"
	| "mart.issuer_analyst_ratings"
	| "mart.fund_virtual_company"
	| "mart.issuer_theme_purity";

export interface ModuleOutputCount {
	table: MartOutputTable;
	runId: string | null;
	rows: number;
	availableRows: number;
	latestCutoff: string | null;
}

export interface ModuleOverviewRow {
	module: number;
	name: string;
	note: string;
	gate: string;
	availability: Availability;
	coverage: { withValue: number; total: number } | null;
	source: string;
	output: ModuleOutputCount | null;
}

export const GPPE_FACTOR_VARIANT_KEYS = [
	"gppe_uniform_charge_v0",
	"gppe_banking_tce_v1",
	"gppe_operating_financial_v1",
] as const;
export type GppeFactorVariantKey = (typeof GPPE_FACTOR_VARIANT_KEYS)[number];

export const RANKING_PANEL_INPUT_KEYS = [
	"operating_gross_profit",
	"capital_adjusted_gross_profit",
	"tangible_common_equity",
	"total_gross_return",
	"employees_total",
] as const;
export type RankingPanelInputKey = (typeof RANKING_PANEL_INPUT_KEYS)[number];

export const WIDE_PANEL_INPUT_COLUMNS = [
	{ key: "gross_profit", label: "Gross profit", monetary: true },
	{ key: "pre_provision_profit", label: "Pre-provision profit", monetary: true },
	{ key: "revenue_total", label: "Revenue", monetary: true },
	{ key: "total_assets", label: "Total assets", monetary: true },
	{ key: "stockholders_equity", label: "Stockholders equity", monetary: true },
	{ key: "tangible_common_equity", label: "Tangible common equity", monetary: true },
	{ key: "employees_total", label: "Employees", monetary: false },
	{ key: "last_close", label: "Market close", monetary: true },
	{ key: "shares_outstanding", label: "Shares outstanding", monetary: false },
] as const;

export interface WidePanelCell {
	nodeKey: string;
	period: string | null;
	value: string | null;
	confidence: string | null;
	availability: Availability;
}

const ENTITY_LEGACY_LATERAL = (col: string) =>
	`left join lateral (select ei.legacy_id from mart.entity_identity ei where ei.entity_id::text = ${col} order by ei.legacy_id limit 1) ei on true`;

export const WIDE_PANEL_NODE_VALUES_SQL = `select coalesce(ei.legacy_id, v.issuer_id) as issuer_id, v.node_key, v.period,
v.value::text as value, v.confidence::text as confidence, coalesce(v.availability_status, 'unavailable') as availability_status
from mart.metric_node_values v ${ENTITY_LEGACY_LATERAL("v.issuer_id")}
where ${servedRunPredicate("v.run_id")} and v.node_key = any($1::text[])
order by coalesce(ei.legacy_id, v.issuer_id), v.node_key, v.period desc`;

export interface Provenance {
	operatingPeriodEnd: string | null;
	revenuePeriodEnd: string | null;
	sharesPeriodEnd: string | null;
	universeId: string | null;
	universeVersion: string | null;
	universeSha256: string | null;
	gppeDefinitionSha256: string | null;
	tierDefinitionSha256: string | null;
}

export interface ComparisonRow {
	issuerId: string;
	cutoffAt: string;
	capitalAdjustedLaborEfficiency: string | null;
	laborEfficiencyMetric?: string | null;
	currentPriceToSales: string | null;
	targetPriceToSales: string | null;
	currentPriceToGrossProfit: string | null;
	targetPriceToGrossProfit: string | null;
	valuationBasis: ValuationBasis | null;
	currentMultiple: string | null;
	targetMultiple: string | null;
	tier: string | null;
	valuationGap: string | null;
	peg: string | null;
	pegRank: number | null;
	pegReasonCodes?: readonly string[];
	confidence: string | null;
	availability: Availability;
	traceId: string;
	provenance: Provenance;
}

export interface RankingRow extends ComparisonRow {
	rank: number | null;
	outcome: StrategyRunOutcome;
	targetWeight: string | null;
	gppeVariants?: Readonly<Record<GppeFactorVariantKey, WidePanelCell | null>>;
	panelInputs?: Readonly<Record<RankingPanelInputKey, WidePanelCell | null>>;
}

interface EntityThemePurityRow {
	themeId: string;
	theme: string;
	themeShare: string | null;
	inThemeRevenue: string | null;
	consolidatedRevenue: string | null;
	segments: number | null;
	confidence?: string | null;
	availabilityStatus: Availability;
	unvisitedReason?: string | null;
}

interface EntityGppeDetail {
	gppe: string | null;
	operatingBranch: string | null;
	capitalAdjustedGrossProfit: string | null;
	availabilityStatus: Availability;
	reasonCodes: string[];
}

interface ModuleDetailMeta {
	runId: string;
	cutoff: string;
	confidence: string | null;
	reasonCodes: string[];
	extractor: string;
	availabilityStatus: Availability;
	sourceEvidenceStatus: string;
	factorValidationStatus: string;
}

export interface AnalystRatingsDetail extends ModuleDetailMeta {
	consensusRating: string | null;
	targetPrice: string | null;
	analystsCount: number;
	buyCount: number;
	holdCount: number;
	sellCount: number;
}

export interface SupplyChainExposureDetail extends ModuleDetailMeta {
	exposureScore: string | null;
	directPartners: number;
	suppliersCount: number;
	customersCount: number;
	maxPartnerShare: string | null;
}

export interface EntityDetail {
	issuerId: string;
	rows: readonly ComparisonRow[];
	themes?: EntityThemePurityRow[];
	gppeDetail?: EntityGppeDetail | null;
	analystRatings?: AnalystRatingsDetail | null;
	supplyChainExposure?: SupplyChainExposureDetail | null;
}

export const TRACE_INPUT_SPECS = [
	{ key: "gross_profit", label: "Gross profit", semanticType: "financial-fact", monetary: true },
	{ key: "total_assets", label: "Total assets", semanticType: "financial-fact", monetary: true },
	{ key: "headcount", label: "Headcount", semanticType: "financial-fact", monetary: false },
	{ key: "shares_outstanding", label: "Shares outstanding", semanticType: "financial-fact", monetary: false },
	{ key: "revenue", label: "Revenue", semanticType: "financial-fact", monetary: true },
	{ key: "close", label: "Price (close)", semanticType: "market-price", monetary: true },
] as const;

export type KnowableAtBasis = "filed" | "input" | "observation";
type SourceKind = "source vintage" | "raw pointer" | "evidence ref";

export interface TraceInput {
	key: string;
	label: string;
	value: string | null;
	currency: string | null;
	monetary: boolean;
	period: string | null;
	knowableAt: string | null;
	knowableAtBasis: KnowableAtBasis | null;
	accession: string | null;
	source: string | null;
	sourceKind: SourceKind | null;
}

export interface TraceListing {
	listingId: string | null;
	isResultListing: boolean;
	inputs: readonly TraceInput[];
}

export interface TraceView {
	traceId: string;
	strategyId: string;
	source: string;
	issuerId: string;
	cutoffAt: string;
	corpusSha256: string;
	links: readonly { kind: string; label: string; reference: string | null }[];
	listings: readonly TraceListing[];
	lineageResultId: string | null;
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

interface ModuleCatalogEntry {
	module: number;
	name: string;
	note: string;
	gate: string;
	field: keyof StrategyRunDecision | null;
	outputTable: MartOutputTable | null;
}

export const MODULE_CATALOG: readonly ModuleCatalogEntry[] = [
	{ module: 1, name: "PEG", note: "recency-weighted historical growth", gate: "Gate 2", field: "peg", outputTable: null },
	{ module: 2, name: "Labor efficiency", note: "capital-adjusted labor efficiency, named per issuer class (#1176)", gate: "Gate 1", field: "capital_adjusted_labor_efficiency", outputTable: null },
	{ module: 3, name: "Supply-chain graph", note: "confidence-gated scenario exposure", gate: "Gate 2", field: null, outputTable: "mart.issuer_supply_chain_exposure" },
	{ module: 4, name: "Analyst backtesting", note: "PIT event eligibility and outcomes", gate: "Gate 2", field: null, outputTable: "mart.issuer_analyst_ratings" },
	{ module: 5, name: "ETF virtual company", note: "delayed N-PORT holdings", gate: "Gate 2", field: null, outputTable: "mart.fund_virtual_company" },
	{ module: 6, name: "Pure-blood screening", note: "traceable segment classification", gate: "Gate 2", field: null, outputTable: "mart.issuer_theme_purity" },
	{ module: 7, name: "Three-tier valuation", note: "materialized composite factor", gate: "Gate 1", field: "tier", outputTable: null },
];

export const moduleSourceTable = (entry: ModuleCatalogEntry): string => entry.outputTable ?? "mart.strategy_decisions";

const SERVED_RUN_TABLES: ReadonlySet<MartOutputTable> = new Set([
	"mart.issuer_supply_chain_exposure",
	"mart.issuer_analyst_ratings",
]);

export function moduleOutputCountSql(table: MartOutputTable): string {
	const served = SERVED_RUN_TABLES.has(table) ? `where ${servedRunPredicate("run_id")}` : "";
	return `with newest as (select run_id from ${table} ${served} group by run_id order by max(cutoff) desc limit 1)
select (select run_id from newest) as run_id, count(*)::int as row_count,
count(*) filter (where availability_status = 'available')::int as available_count,
to_char(max(cutoff) at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') as latest_cutoff
from ${table} where run_id = (select run_id from newest)`;
}

function countOf(value: unknown, column: string): number {
	if (typeof value !== "number" || !Number.isInteger(value)) throw new MartReadUnavailable(`${column} is not an integer count`);
	return value;
}

export function decisionAvailability(decision: StrategyRunDecision): Availability {
	if (decision.availability_status === "not_applicable") return "not_applicable";
	return decision.outcome === "excluded" ? (decision.exclusion_reason === "below_confidence_floor" ? "low_confidence" : "excluded") : "available";
}

export const TRACE_LINEAGE_SQL = `select m.result_id, m.listing_id, m.lineage
from mart.strategy_decisions d join mart.strategy_run_capture scope on scope.strategy_run_id = d.strategy_run_id
join mart.topt_core_meta_info m on m.run_id = scope.capture_run_id and m.issuer_id = d.issuer_id and m.cutoff = d.cutoff_at
${ENTITY_LEGACY_LATERAL("d.issuer_id")}
where d.strategy_run_id = $1 and (d.issuer_id = $2 or coalesce(ei.legacy_id, d.issuer_id) = $2) and d.cutoff_at = $3::timestamptz
order by m.result_id limit 1`;

function lineageText(value: unknown, field: string): string | null {
	if (value === null || value === undefined) return null;
	if (typeof value === "string") return value.length > 0 ? value : null;
	throw new MartReadUnavailable(`lineage ${field} is not text`);
}

const cmpText = (a: string, b: string): number => (a < b ? -1 : a > b ? 1 : 0);

function traceInputOf(spec: (typeof TRACE_INPUT_SPECS)[number], item: Record<string, unknown> | undefined): TraceInput {
	const inputs = recordOf(item?.inputs);
	const filing = recordOf(recordOf(item?.vintage)?.[spec.key]);
	const txt = (obj: Record<string, unknown> | null, key: string, label = `${spec.key} ${key}`) => lineageText(obj?.[key], label);
	const observationAt = txt(item ?? null, "knowable_at", "knowable_at");
	const filed = txt(filing, "filed");
	const isHeadcount = spec.key === "headcount";
	const inputAt = isHeadcount ? txt(filing, "knowable_at", "headcount knowable_at") : null;
	const [knowableAt, knowableAtBasis]: [string | null, KnowableAtBasis | null] =
		filed !== null ? [filed, "filed"] : inputAt !== null ? [inputAt, "input"] : observationAt !== null ? [observationAt, "observation"] : [null, null];
	const hcRaw = isHeadcount ? txt(filing, "raw", "headcount raw") : null;
	const hcRef = isHeadcount ? txt(filing, "evidence_ref", "headcount evidence_ref") : null;
	const [source, sourceKind]: [string | null, SourceKind | null] = !isHeadcount
		? [txt(item ?? null, "source_vintage_id", "source_vintage_id"), "source vintage"]
		: hcRaw !== null ? [hcRaw, "raw pointer"] : hcRef !== null ? [hcRef, "evidence ref"] : [null, null];
	return {
		key: spec.key,
		label: spec.label,
		value: txt(inputs, spec.key, `${spec.key} value`),
		currency: spec.monetary ? txt(inputs, "currency", "currency") : null,
		monetary: spec.monetary,
		period: txt(filing, "period_end"),
		knowableAt,
		knowableAtBasis,
		accession: txt(filing, "accession"),
		source,
		sourceKind: source === null ? null : sourceKind,
	};
}

export function traceInputsFromLineage(lineage: unknown, resultListingId: string | null = null): TraceListing[] {
	if (!Array.isArray(lineage)) throw new MartReadUnavailable("lineage is not an array");
	const items = lineage.map((item) => {
		const record = recordOf(item);
		if (record === null) throw new MartReadUnavailable("a lineage item is not an object");
		return record;
	});
	const listingOf = (item: Record<string, unknown>): string => {
		const listingId = lineageText(recordOf(item.inputs)?.listing_id, "inputs listing_id");
		if (listingId === null) throw new MartReadUnavailable("a lineage item names no listing");
		return listingId;
	};
	const carriers = items.filter((item) => item.semantic_type === "financial-fact" || item.semantic_type === "market-price");
	const listingIds = [...new Set(carriers.map(listingOf))].sort(cmpText);
	if (listingIds.length === 0) {
		return [{ listingId: resultListingId, isResultListing: resultListingId !== null, inputs: TRACE_INPUT_SPECS.map((s) => traceInputOf(s, undefined)) }];
	}
	const ordered = [...listingIds.filter((id) => id === resultListingId), ...listingIds.filter((id) => id !== resultListingId)];
	return ordered.map((listingId) => ({
		listingId,
		isResultListing: listingId === resultListingId,
		inputs: TRACE_INPUT_SPECS.map((spec) =>
			traceInputOf(spec, carriers.find((item) => item.semantic_type === spec.semanticType && listingOf(item) === listingId)),
		),
	}));
}

const traceId = (source: string, issuerId: string, cutoffAt: string, corpusSha256: string): string =>
	`${source}:${corpusSha256.slice(0, 12)}:${issuerId}:${cutoffAt}`;

const pickPanelCells = <K extends string>(issuerPanel: Readonly<Record<string, WidePanelCell>> | undefined, keys: readonly K[]): Record<K, WidePanelCell | null> =>
	Object.fromEntries(keys.map((k) => [k, issuerPanel?.[k] ?? null])) as Record<K, WidePanelCell | null>;

function rankingOrder(a: StrategyRunDecision, b: StrategyRunDecision): number {
	const aRanked = a.rank !== null;
	if (aRanked !== (b.rank !== null)) return aRanked ? -1 : 1;
	if (aRanked && a.rank !== b.rank) return (a.rank as number) < (b.rank as number) ? -1 : 1;
	return cmpText(a.issuer_id, b.issuer_id);
}

export interface StrategyRunRepositoryLike {
	getLatest(
		strategyId: string,
		context: AccessContext,
	): StrategyRunReport | StrategyRunUnavailable | Promise<StrategyRunReport | StrategyRunUnavailable>;
}

type ReportWithProvenance = StrategyRunReport & { provenance?: ReadonlyMap<string, DecisionProvenance> };

export class StrategyRunReadAdapter {
	private readonly repository: StrategyRunRepositoryLike;
	private readonly reportCache = new Map<string, Promise<ReportWithProvenance>>();
	private readonly runWithClient: <T>(fn: (client: MartClientLike) => Promise<T>) => Promise<T>;

	constructor(
		repository?: StrategyRunRepositoryLike,
		runWithClient: <T>(fn: (client: MartClientLike) => Promise<T>) => Promise<T> = withMartReadonly,
	) {
		this.repository = repository ?? new MartStrategyRunRepository();
		this.runWithClient = runWithClient;
	}

	private report(context: AccessContext): Promise<ReportWithProvenance> {
		const cached = this.reportCache.get(context.contextId);
		if (cached !== undefined) return cached;
		const promise = (async () => {
			const result = await this.repository.getLatest(DASHBOARD_STRATEGY_ID, context);
			if (!("decisions" in result)) throw new MartReadUnavailable(result.reason);
			return result;
		})();
		this.reportCache.set(context.contextId, promise);
		return promise;
	}

	async runIdentity(context: AccessContext): Promise<RunIdentity> {
		const report = (await this.report(context)) as ReportWithProvenance & { strategy_run_id?: string; executed_at?: string };
		return { strategyRunId: report.strategy_run_id ?? null, executedAt: report.executed_at ?? null, source: report.source, corpusPrefix: report.corpus_sha256.slice(0, 12) };
	}

	async latestCutoff(context: AccessContext): Promise<string | null> {
		const cutoffs = (await this.report(context)).decisions.map((d) => d.cutoff_at);
		return cutoffs.length === 0 ? null : cutoffs.slice().sort().reverse()[0];
	}

	async overview(context: AccessContext): Promise<ModuleOverviewRow[]> {
		const decisions = (await this.report(context)).decisions;
		const tables = MODULE_CATALOG.flatMap((entry) => (entry.outputTable === null ? [] : [entry.outputTable]));
		const outputs = await this.runWithClient(async (client) => {
			const counts = new Map<MartOutputTable, ModuleOutputCount>();
			for (const table of tables) {
				const row = (await client.query(moduleOutputCountSql(table))).rows[0];
				if (row === undefined) throw new MartReadUnavailable(`${table} count returned no row`);
				counts.set(table, {
					table, runId: typeof row.run_id === "string" ? row.run_id : null,
					rows: countOf(row.row_count, `${table}.row_count`), availableRows: countOf(row.available_count, `${table}.available_count`),
					latestCutoff: typeof row.latest_cutoff === "string" ? row.latest_cutoff : null,
				});
			}
			return counts;
		});
		return MODULE_CATALOG.map((entry) => {
			const base = { module: entry.module, name: entry.name, note: entry.note, gate: entry.gate, source: moduleSourceTable(entry) };
			if (entry.outputTable !== null) {
				const output = outputs.get(entry.outputTable);
				if (output === undefined) throw new MartReadUnavailable(`no count read for ${entry.outputTable}`);
				return { ...base, availability: output.availableRows > 0 ? "available" : "unavailable", coverage: null, output };
			}
			const withValue = entry.field === null ? 0 : decisions.filter((d) => d[entry.field as keyof StrategyRunDecision] !== null).length;
			return { ...base, availability: withValue > 0 ? "available" : "unavailable", coverage: entry.field === null ? null : { withValue, total: decisions.length }, output: null };
		});
	}

	private toComparisonRow(decision: StrategyRunDecision, report: ReportWithProvenance): ComparisonRow {
		const inputs = report.provenance?.get(decision.issuer_id);
		return {
			issuerId: decision.issuer_id, cutoffAt: decision.cutoff_at,
			capitalAdjustedLaborEfficiency: decision.capital_adjusted_labor_efficiency, laborEfficiencyMetric: decision.labor_efficiency_metric ?? null,
			currentPriceToSales: decision.current_price_to_sales, targetPriceToSales: decision.target_price_to_sales,
			currentPriceToGrossProfit: decision.current_price_to_gross_profit ?? null, targetPriceToGrossProfit: decision.target_price_to_gross_profit ?? null,
			valuationBasis: valuationBasisOf(decision), currentMultiple: currentMultipleOf(decision), targetMultiple: targetMultipleOf(decision),
			tier: decision.tier, valuationGap: decision.valuation_gap, peg: decision.peg ?? null, pegRank: decision.peg_rank ?? null,
			pegReasonCodes: inputs?.peg_reason_codes ?? (decision as { peg_reason_codes?: readonly string[] }).peg_reason_codes ?? [],
			confidence: decision.confidence, availability: decisionAvailability(decision),
			traceId: traceId(report.source, decision.issuer_id, decision.cutoff_at, report.corpus_sha256),
			provenance: {
				operatingPeriodEnd: inputs?.operating_period_end ?? null, revenuePeriodEnd: inputs?.revenue_period_end ?? null,
				sharesPeriodEnd: inputs?.shares_period_end ?? null, universeId: inputs?.universe_id ?? null,
				universeVersion: inputs?.universe_version ?? null, universeSha256: inputs?.universe_sha256 ?? null,
				gppeDefinitionSha256: inputs?.gppe_definition_sha256 ?? null, tierDefinitionSha256: inputs?.tier_definition_sha256 ?? null,
			},
		};
	}

	private toRankingRow(decision: StrategyRunDecision, report: ReportWithProvenance, issuerPanel?: Readonly<Record<string, WidePanelCell>>): RankingRow {
		const base = this.toComparisonRow(decision, report);
		const activeMetric = decision.labor_efficiency_metric ?? "gppe_uniform_charge_v0";
		const gppeVariants = pickPanelCells(issuerPanel, GPPE_FACTOR_VARIANT_KEYS);
		if ((GPPE_FACTOR_VARIANT_KEYS as readonly string[]).includes(activeMetric) && gppeVariants[activeMetric as GppeFactorVariantKey] === null && decision.capital_adjusted_labor_efficiency !== null) {
			gppeVariants[activeMetric as GppeFactorVariantKey] = {
				nodeKey: activeMetric, period: base.provenance.operatingPeriodEnd,
				value: decision.capital_adjusted_labor_efficiency, confidence: decision.confidence, availability: base.availability,
			};
		}
		return { ...base, rank: decision.rank, outcome: decision.outcome, targetWeight: decision.target_weight, gppeVariants, panelInputs: pickPanelCells(issuerPanel, RANKING_PANEL_INPUT_KEYS) };
	}

	private async cutoffDecisions(context: AccessContext, cutoffAt?: string) {
		const report = await this.report(context);
		const cutoff = cutoffAt ?? (await this.latestCutoff(context));
		return cutoff === null ? null : { report, decisions: report.decisions.filter((d) => d.cutoff_at === cutoff) };
	}

	async ranking(context: AccessContext, cutoffAt?: string): Promise<RankingRow[]> {
		const scoped = await this.cutoffDecisions(context, cutoffAt);
		if (!scoped) return [];
		const widePanel = scoped.report.source === "mart" ? await this.widePanelByIssuer([...GPPE_FACTOR_VARIANT_KEYS, ...RANKING_PANEL_INPUT_KEYS]) : new Map<string, Record<string, WidePanelCell>>();
		return scoped.decisions.slice().sort(rankingOrder).map((d) => this.toRankingRow(d, scoped.report, widePanel.get(d.issuer_id)));
	}

	async widePanelByIssuer(nodeKeys: readonly string[] = WIDE_PANEL_INPUT_COLUMNS.map((col) => col.key)): Promise<Map<string, Record<string, WidePanelCell>>> {
		const byIssuer = new Map<string, Record<string, WidePanelCell>>();
		try {
			await this.runWithClient(async (client) => {
				for (const row of (await client.query(WIDE_PANEL_NODE_VALUES_SQL, [nodeKeys])).rows) {
					const issuerId = nullableString(row.issuer_id), nodeKey = nullableString(row.node_key);
					if (issuerId === null || nodeKey === null) continue;
					let record = byIssuer.get(issuerId);
					if (record === undefined) byIssuer.set(issuerId, (record = {}));
					if (record[nodeKey] !== undefined) continue;
					record[nodeKey] = {
						nodeKey, period: nullableString(row.period), value: nullableString(row.value),
						confidence: nullableString(row.confidence), availability: toAvailabilityStatus(row.availability_status),
					};
				}
			});
		} catch {
			return byIssuer;
		}
		return byIssuer;
	}

	async comparison(context: AccessContext, cutoffAt?: string): Promise<ComparisonRow[]> {
		const scoped = await this.cutoffDecisions(context, cutoffAt);
		return scoped ? scoped.decisions.slice().sort((a, b) => cmpText(a.issuer_id, b.issuer_id)).map((d) => this.toComparisonRow(d, scoped.report)) : [];
	}

	private async resolveDecisionIssuerId(issuerId: string, decisions: readonly StrategyRunDecision[]): Promise<string> {
		if (decisions.length === 0 || decisions.some((d) => d.issuer_id === issuerId)) return issuerId;
		try {
			const rows = (await this.runWithClient((client) => client.query(`select entity_id::text as entity_id, legacy_id from mart.entity_identity ei cross join (select btrim($1::text) as q) inp where ${this.identityMatchSql()}`, [issuerId]))).rows;
			const candidates = new Set<string>();
			for (const row of rows) {
				for (const val of [row.entity_id, row.legacy_id]) {
					if (typeof val === "string" && val.length > 0) candidates.add(val);
				}
			}
			return decisions.find((d) => candidates.has(d.issuer_id))?.issuer_id ?? issuerId;
		} catch {
			return issuerId;
		}
	}

	async entityDetail(context: AccessContext, issuerId: string): Promise<EntityDetail | null> {
		const report = await this.report(context);
		const resolvedIssuerId = await this.resolveDecisionIssuerId(issuerId, report.decisions);
		const rows = report.decisions.filter((d) => d.issuer_id === resolvedIssuerId).slice().sort((a, b) => cmpText(a.cutoff_at, b.cutoff_at)).map((d) => this.toComparisonRow(d, report));
		return rows.length === 0 ? null : { issuerId: resolvedIssuerId, rows };
	}

	async traceView(context: AccessContext, issuerId: string, cutoffAt: string): Promise<TraceView | null> {
		const report = await this.report(context);
		const resolvedIssuerId = await this.resolveDecisionIssuerId(issuerId, report.decisions);
		const decision = report.decisions.find((d) => d.issuer_id === resolvedIssuerId && d.cutoff_at === cutoffAt);
		if (decision === undefined) return null;
		const strategyRunId = (report as { strategy_run_id?: string }).strategy_run_id;
		let lineage: { resultId: string; listingId: string | null; lineage: unknown } | null = null;
		if (strategyRunId !== undefined) {
			const row = (await this.runWithClient((c) => c.query(TRACE_LINEAGE_SQL, [strategyRunId, decision.issuer_id, decision.cutoff_at]))).rows[0];
			if (row !== undefined) {
				if (typeof row.result_id !== "string") throw new MartReadUnavailable("lineage row has no result_id");
				lineage = { resultId: row.result_id, listingId: lineageText(row.listing_id, "result listing_id"), lineage: row.lineage };
			}
		}
		return {
			traceId: traceId(report.source, resolvedIssuerId, cutoffAt, report.corpus_sha256),
			strategyId: report.strategy_id, source: report.source, issuerId: resolvedIssuerId, cutoffAt, corpusSha256: report.corpus_sha256,
			links: [
				{ kind: "materialized_output", label: "Strategy decision", reference: `${report.strategy_id}:${resolvedIssuerId}:${cutoffAt}` },
				{ kind: "snapshot", label: "Run corpus (snapshot)", reference: report.corpus_sha256 },
				{ kind: "raw", label: "Immutable raw bytes", reference: null },
			],
			listings: traceInputsFromLineage(lineage?.lineage ?? [], lineage?.listingId ?? null),
			lineageResultId: lineage?.resultId ?? null, governed: report.governed ?? null,
		};
	}

	private identityMatchSql(): string {
		return `(ei.entity_id::text = lower(inp.q) or ei.legacy_id = inp.q or upper(ei.legacy_id) = upper(inp.q) or upper(ei.current_ticker) = upper(inp.q) or (inp.q ~* '^listing:[0-9a-z]{4}:[0-9a-z.\-]+$' and upper(ei.current_ticker) = upper(split_part(inp.q, ':', 3))) or (regexp_replace(inp.q, '^(:?issuer:cik:|company:cik:|cik:)', '', 'i') ~ '^[0-9]{1,10}$' and ei.cik = lpad(regexp_replace(inp.q, '^(:?issuer:cik:|company:cik:|cik:)', '', 'i'), 10, '0')) or upper(ei.lei) = upper(regexp_replace(inp.q, '^(:?issuer:lei:|company:lei:|lei:)', '', 'i')))`;
	}

	private bidirectionalIssuerPredicate(column: string): string {
		const match = this.identityMatchSql();
		return `(${column} = $1 or ${column} = any (array(select id_val from (select ei.entity_id::text as entity_id, ei.legacy_id from mart.entity_identity ei cross join (select btrim($1::text) as q) inp where ${match} offset 0) matched cross join lateral (values (matched.entity_id), (matched.legacy_id)) as v(id_val) where id_val is not null)))`;
	}

	private async safeQuery<T>(label: string, fallback: T, fn: (client: MartClientLike) => Promise<T>): Promise<T> {
		try {
			return await this.runWithClient(fn);
		} catch (error) {
			console.error(`${label} error:`, error);
			if ((error as { code?: unknown })?.code === "57014") {
				throw error;
			}
			return fallback;
		}
	}

	private async safeQueryFirst(label: string, sql: string, params: readonly unknown[]): Promise<Record<string, unknown> | null> {
		return this.safeQuery(label, null, async (client) => (await client.query(sql, params)).rows[0] ?? null);
	}

	async entityThemePurity(_context: AccessContext, issuerId: string): Promise<EntityThemePurityRow[]> {
		const predicate = this.bidirectionalIssuerPredicate("p.issuer_id");
		return this.safeQuery("entityThemePurity", [], async (client) =>
			(
				await client.query(
					`select theme_id, theme, theme_share, in_theme_revenue, consolidated_revenue, segments, confidence, availability_status, unvisited_reason
from (select distinct on (p.theme_id) p.theme_id, p.theme, p.theme_share::text as theme_share, p.theme_share as raw_share,
p.in_theme_revenue::text as in_theme_revenue, p.consolidated_revenue::text as consolidated_revenue,
case when p.partition_id is null then null else p.segments end as segments,
case when p.partition_id is null then null else p.confidence::text end as confidence,
coalesce(p.availability_status, 'unavailable') as availability_status,
case when p.partition_id is null then coalesce(p.reason_codes[1], 'unvisited') end as unvisited_reason
from mart.issuer_theme_purity p where ${predicate} order by p.theme_id, (p.partition_id is null), p.cutoff desc, p.created_at desc) latest_per_theme
order by raw_share desc nulls last, theme asc`,
					[issuerId],
				)
			).rows.map((row) => ({
				themeId: String(row.theme_id ?? ""), theme: String(row.theme ?? ""),
				themeShare: nullableString(row.theme_share), inThemeRevenue: nullableString(row.in_theme_revenue),
				consolidatedRevenue: nullableString(row.consolidated_revenue), segments: typeof row.segments === "number" ? row.segments : null,
				confidence: nullableString(row.confidence), availabilityStatus: toAvailabilityStatus(row.availability_status),
				unvisitedReason: typeof row.unvisited_reason === "string" ? row.unvisited_reason : null,
			})),
		);
	}

	async entityGppeDetail(_context: AccessContext, issuerId: string): Promise<EntityGppeDetail | null> {
		const predicate = this.bidirectionalIssuerPredicate("issuer_id");
		const row = await this.safeQueryFirst(
			"entityGppeDetail",
			`select gppe::text as gppe, operating_branch, capital_adjusted_gross_profit::text as capital_adjusted_gross_profit,
coalesce(availability_status, availability, 'unavailable') as availability_status, reason_codes
from mart.topt_gppe_results where ${predicate} order by cutoff desc, created_at desc limit 1`,
			[issuerId],
		);
		if (!row) return null;
		return {
			gppe: nullableString(row.gppe), operatingBranch: nullableString(row.operating_branch),
			capitalAdjustedGrossProfit: nullableString(row.capital_adjusted_gross_profit),
			availabilityStatus: toAvailabilityStatus(row.availability_status), reasonCodes: stringArray(row.reason_codes),
		};
	}

	private async queryIssuerServedMeta(
		label: string,
		table: string,
		alias: string,
		predicate: string,
		extraColumns: string,
		defaultExtractor: string,
		issuerId: string,
	): Promise<{ row: Record<string, unknown>; meta: ModuleDetailMeta } | null> {
		const cols = `${alias}.run_id, to_char(${alias}.cutoff at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') as cutoff, ${extraColumns}, ${alias}.confidence::text as confidence, ${alias}.reason_codes, ${alias}.extractor, coalesce(${alias}.availability_status, 'unavailable') as availability_status, coalesce(${alias}.source_evidence_status, 'degraded') as source_evidence_status, coalesce(${alias}.factor_validation_status, 'not_evaluated') as factor_validation_status`;
		const row = await this.safeQueryFirst(
			label,
			`select ${cols} from ${table} ${alias} where ${predicate} and ${servedRunPredicate(`${alias}.run_id`)} order by (${alias}.availability_status = 'available') desc, ${alias}.cutoff desc, ${alias}.created_at desc limit 1`,
			[issuerId],
		);
		return row ? { row, meta: moduleDetailMetadata(row, defaultExtractor) } : null;
	}

	async entityAnalystRatings(_context: AccessContext, issuerId: string): Promise<AnalystRatingsDetail | null> {
		const found = await this.queryIssuerServedMeta(
			"entityAnalystRatings", "mart.issuer_analyst_ratings", "r",
			this.bidirectionalIssuerPredicate("r.issuer_id"),
			`r.consensus_rating::text as consensus_rating, coalesce(to_jsonb(r) ->> 'target_price', (select substring(rc from '^target_price:([0-9.]+)$') from unnest(r.reason_codes) as rc where rc like 'target_price:%' limit 1)) as target_price, r.analysts_count, r.buy_count, r.hold_count, r.sell_count`,
			"origin:moomoo:v1",
			issuerId,
		);
		if (!found) return null;
		const { row, meta } = found;
		const targetFromReason = meta.reasonCodes.find((c) => c.startsWith("target_price:"))?.slice("target_price:".length);
		return {
			...meta, consensusRating: nullableString(row.consensus_rating),
			targetPrice: nullableString(row.target_price) ?? targetFromReason ?? null,
			analystsCount: numberOrZero(row.analysts_count), buyCount: numberOrZero(row.buy_count),
			holdCount: numberOrZero(row.hold_count), sellCount: numberOrZero(row.sell_count),
		};
	}

	async entitySupplyChainExposure(_context: AccessContext, issuerId: string): Promise<SupplyChainExposureDetail | null> {
		const found = await this.queryIssuerServedMeta(
			"entitySupplyChainExposure", "mart.issuer_supply_chain_exposure", "s",
			this.bidirectionalIssuerPredicate("s.issuer_id"),
			"s.exposure_score::text as exposure_score, s.direct_partners, s.suppliers_count, s.customers_count, s.max_partner_share::text as max_partner_share",
			"graph:kg-edges:v1",
			issuerId,
		);
		if (!found) return null;
		const { row, meta } = found;
		return {
			...meta, exposureScore: nullableString(row.exposure_score),
			directPartners: numberOrZero(row.direct_partners), suppliersCount: numberOrZero(row.suppliers_count),
			customersCount: numberOrZero(row.customers_count), maxPartnerShare: nullableString(row.max_partner_share),
		};
	}
}

function moduleDetailMetadata(row: Record<string, unknown>, defaultExtractor: string): ModuleDetailMeta {
	return {
		runId: String(row.run_id ?? ""), cutoff: String(row.cutoff ?? ""), confidence: nullableString(row.confidence),
		reasonCodes: stringArray(row.reason_codes), extractor: String(row.extractor ?? defaultExtractor),
		availabilityStatus: toAvailabilityStatus(row.availability_status),
		sourceEvidenceStatus: String(row.source_evidence_status ?? "degraded"),
		factorValidationStatus: String(row.factor_validation_status ?? "not_evaluated"),
	};
}

const AVAILABILITY_VALUES: ReadonlySet<Availability> = new Set<Availability>([
	"available", "unavailable", "stale", "excluded", "low_confidence", "not_applicable", "error",
]);

export function toAvailabilityStatus(value: unknown): Availability {
	return typeof value === "string" && AVAILABILITY_VALUES.has(value as Availability) ? (value as Availability) : "unavailable";
}
