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

import type { AccessContext, StrategyRunDecision, StrategyRunOutcome, StrategyRunReport, StrategyRunUnavailable } from "@/contracts/strategyRun";
import { MartStrategyRunRepository } from "./strategy-run-repository";

export type Availability = "available" | "unavailable" | "stale" | "excluded" | "low_confidence" | "error";

/** The strategy run whose materialized decisions back the current dashboard surfaces. */
export const DASHBOARD_STRATEGY_ID = "large_model_value_v0";

export interface ModuleOverviewRow {
  module: number;
  name: string;
  note: string;
  gate: string;
  availability: Availability;
}

export interface RankingRow {
  rank: number | null;
  issuerId: string;
  cutoffAt: string;
  outcome: StrategyRunOutcome;
  tier: string | null;
  currentPriceToSales: string | null;
  targetPriceToSales: string | null;
  valuationGap: string | null;
  targetWeight: string | null;
  confidence: string | null;
  availability: Availability;
  traceId: string;
}

export interface ComparisonRow {
  issuerId: string;
  cutoffAt: string;
  capitalAdjustedLaborEfficiency: string | null;
  currentPriceToSales: string | null;
  tier: string | null;
  valuationGap: string | null;
  confidence: string | null;
  availability: Availability;
  traceId: string;
}

export interface EntityDetail {
  issuerId: string;
  rows: readonly ComparisonRow[];
}

export interface TraceLink {
  kind: string;
  label: string;
  reference: string | null;
}

export interface TraceView {
  traceId: string;
  strategyId: string;
  source: string;
  issuerId: string;
  cutoffAt: string;
  corpusSha256: string;
  links: readonly TraceLink[];
}

export class MartReadUnavailable extends Error {
  readonly reason: string;
  constructor(reason: string) {
    super(`mart read unavailable: ${reason}`);
    this.name = "MartReadUnavailable";
    this.reason = reason;
  }
}

// The seven modules and the strategy composite. `field` names the decision field whose
// presence proves the module is materialized in the current mart (via the strategy fixture);
// `null` means no mart output exists for it yet (Gate 2 modules).
const MODULE_CATALOG: readonly {
  module: number;
  name: string;
  note: string;
  gate: string;
  field: keyof StrategyRunDecision | null;
}[] = [
  { module: 1, name: "PEG", note: "three versioned growth conventions", gate: "Gate 2", field: null },
  {
    module: 2,
    name: "Gross profit / employee",
    note: "capital-adjusted labor efficiency",
    gate: "Gate 1",
    field: "capital_adjusted_labor_efficiency",
  },
  { module: 3, name: "Supply-chain graph", note: "confidence-gated scenario exposure", gate: "Gate 2", field: null },
  { module: 4, name: "Analyst backtesting", note: "PIT event eligibility and outcomes", gate: "Gate 2", field: null },
  { module: 5, name: "ETF virtual company", note: "delayed N-PORT holdings", gate: "Gate 2", field: null },
  { module: 6, name: "Pure-blood screening", note: "traceable segment classification", gate: "Gate 2", field: null },
  { module: 7, name: "Three-tier valuation", note: "materialized composite factor", gate: "Gate 1", field: "tier" },
];

/** Exported for `tests/dashboard-read.test.ts`'s fixture-independent hard-excluded case —
 * see #370's rebase note: the shared fixture no longer contains a naturally-occurring
 * non-confidence-floor exclusion, so that branch is tested with a synthetic decision. */
export function decisionAvailability(decision: StrategyRunDecision): Availability {
  if (decision.outcome === "excluded") {
    if (decision.exclusion_reason === "below_confidence_floor") return "low_confidence";
    return "excluded";
  }
  return "available";
}

function traceId(source: string, issuerId: string, cutoffAt: string, corpusSha256: string): string {
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
  if (aRanked && bRanked && a.rank !== b.rank) return (a.rank as number) < (b.rank as number) ? -1 : 1;
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
  ): StrategyRunReport | StrategyRunUnavailable | Promise<StrategyRunReport | StrategyRunUnavailable>;
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

  constructor(repository?: StrategyRunRepositoryLike) {
    this.repository = repository ?? new MartStrategyRunRepository();
  }

  private report(context: AccessContext): Promise<StrategyRunReport> {
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

  async latestCutoff(context: AccessContext): Promise<string | null> {
    const cutoffs = (await this.report(context)).decisions.map((decision) => decision.cutoff_at);
    if (cutoffs.length === 0) return null;
    return cutoffs.slice().sort().reverse()[0];
  }

  async overview(context: AccessContext): Promise<ModuleOverviewRow[]> {
    const decisions = (await this.report(context)).decisions;
    return MODULE_CATALOG.map((entry) => {
      const materialized =
        entry.field !== null && decisions.some((decision) => decision[entry.field as keyof StrategyRunDecision] !== null);
      return {
        module: entry.module,
        name: entry.name,
        note: entry.note,
        gate: entry.gate,
        availability: materialized ? "available" : "unavailable",
      };
    });
  }

  private toRankingRow(decision: StrategyRunDecision, source: string, corpusSha256: string): RankingRow {
    const status = decisionAvailability(decision);
    return {
      rank: decision.rank,
      issuerId: decision.issuer_id,
      cutoffAt: decision.cutoff_at,
      outcome: decision.outcome,
      tier: decision.tier,
      currentPriceToSales: decision.current_price_to_sales,
      targetPriceToSales: decision.target_price_to_sales,
      valuationGap: decision.valuation_gap,
      targetWeight: decision.target_weight,
      confidence: decision.confidence,
      availability: status,
      traceId: traceId(source, decision.issuer_id, decision.cutoff_at, corpusSha256),
    };
  }

  private toComparisonRow(decision: StrategyRunDecision, source: string, corpusSha256: string): ComparisonRow {
    // The row's own availability mirrors the decision (low_confidence/excluded must stay
    // visible even when this one field is null) — matching toRankingRow. valueAvailability
    // is for a single value's own null-driven downgrade, not the whole row (Copilot review
    // on #387, same class of bug fixed in research_report_fixture.py's #383).
    const status = decisionAvailability(decision);
    return {
      issuerId: decision.issuer_id,
      cutoffAt: decision.cutoff_at,
      capitalAdjustedLaborEfficiency: decision.capital_adjusted_labor_efficiency,
      currentPriceToSales: decision.current_price_to_sales,
      tier: decision.tier,
      valuationGap: decision.valuation_gap,
      confidence: decision.confidence,
      availability: status,
      traceId: traceId(source, decision.issuer_id, decision.cutoff_at, corpusSha256),
    };
  }

  async ranking(context: AccessContext, cutoffAt?: string): Promise<RankingRow[]> {
    const report = await this.report(context);
    const cutoff = cutoffAt ?? (await this.latestCutoff(context));
    if (cutoff === null) return [];
    const decisions = report.decisions.filter((decision) => decision.cutoff_at === cutoff);
    const ordered = decisions.slice().sort(rankingOrder);
    return ordered.map((decision) => this.toRankingRow(decision, report.source, report.corpus_sha256));
  }

  async comparison(context: AccessContext, cutoffAt?: string): Promise<ComparisonRow[]> {
    const report = await this.report(context);
    const cutoff = cutoffAt ?? (await this.latestCutoff(context));
    if (cutoff === null) return [];
    const decisions = report.decisions.filter((decision) => decision.cutoff_at === cutoff);
    const ordered = decisions.slice().sort((a, b) => (a.issuer_id < b.issuer_id ? -1 : a.issuer_id > b.issuer_id ? 1 : 0));
    return ordered.map((decision) => this.toComparisonRow(decision, report.source, report.corpus_sha256));
  }

  async entityDetail(context: AccessContext, issuerId: string): Promise<EntityDetail | null> {
    const report = await this.report(context);
    const rows = report.decisions
      .filter((decision) => decision.issuer_id === issuerId)
      .slice()
      .sort((a, b) => (a.cutoff_at < b.cutoff_at ? -1 : a.cutoff_at > b.cutoff_at ? 1 : 0))
      .map((decision) => this.toComparisonRow(decision, report.source, report.corpus_sha256));
    if (rows.length === 0) return null;
    return { issuerId, rows };
  }

  async traceView(context: AccessContext, issuerId: string, cutoffAt: string): Promise<TraceView | null> {
    const report = await this.report(context);
    const decision = report.decisions.find((d) => d.issuer_id === issuerId && d.cutoff_at === cutoffAt);
    if (decision === undefined) return null;
    return {
      traceId: traceId(report.source, issuerId, cutoffAt, report.corpus_sha256),
      strategyId: report.strategy_id,
      source: report.source,
      issuerId,
      cutoffAt,
      corpusSha256: report.corpus_sha256,
      links: [
        { kind: "materialized_output", label: "Strategy decision", reference: `${report.strategy_id}:${issuerId}:${cutoffAt}` },
        { kind: "snapshot", label: "Run corpus (snapshot)", reference: report.corpus_sha256 },
        { kind: "raw", label: "Immutable raw bytes", reference: null },
      ],
    };
  }
}
