/**
 * TypeScript mart read adapter for the research dashboard — see #370.
 *
 * Two adapters share the same pure projection functions over the provisional read models
 * the App and MCP already share (`#347`'s `strategyRun.ts`):
 *
 * - `MartResearchReadAdapter` (the production default, #370's appended acceptance /
 *   #429 P3): reads the real `mart.strategy_runs`/`strategy_decisions` head run through
 *   `MartStrategyRunRepository` under the `mart_readonly` role — the same repository
 *   `/admin/strategy-runs` ships with. Async, because a real Postgres read is.
 * - `FixtureMartReadAdapter` (TESTS ONLY): projects the checked-in strategy-run fixture
 *   bytes so tests assert exact known values without a database. Never wire it into a
 *   route loader default; `tests/dashboard-boundary.test.ts` statically enforces this.
 *
 * Boundary: this module performs NO cross-factor computation. It selects, labels, sorts,
 * and copies already-materialized values through byte-exact. It never joins two factors or
 * two time points into a new metric (init.md Section 1, rule 2). A boundary scan in
 * `tests/dashboard-boundary.test.ts` statically forbids metric arithmetic here.
 *
 * Both repositories do I/O (filesystem or Postgres) — server-only. Never import this from
 * a client component.
 */

import {
  FixtureStrategyRunRepository,
  type AccessContext,
  type StrategyRunDecision,
  type StrategyRunOutcome,
  type StrategyRunReport,
  type StrategyRunUnavailable,
} from "@/contracts/strategyRun";
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

/** The identity of the governed run a page rendered, so its output can be compared
 * one-to-one with the MCP `strategy_run` tool (same head run, same corpus). The mart
 * repository carries `strategy_run_id`/`executed_at`; the fixture has neither (null). */
export interface RunIdentity {
  strategyRunId: string | null;
  executedAt: string | null;
  corpusSha256: string;
  source: StrategyRunReport["source"];
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
// presence proves the module is materialized in the current mart run; `null` means no
// mart output exists for it yet (Gate 2 modules).
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

/** A report optionally enriched with the mart run row's identity columns.
 * `MartStrategyRunRepository` provides them; the fixture repository does not. */
type SourcedStrategyRunReport = StrategyRunReport & {
  strategy_run_id?: string | null;
  executed_at?: string | null;
};

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
  // multiple same-day cutoffs (Copilot review on #387). Mirrors research_report_fixture.py's
  // _trace() (#369/#383) so both sides stay byte-identical. The prefix is the report's own
  // source — never a hardcoded literal, which would label real mart rows as fixture-backed.
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

// --- Shared pure projections (report -> view models). Both adapters call exactly these,
// so the fixture-backed tests prove the same code path production runs on the mart. ---

function projectLatestCutoff(report: StrategyRunReport): string | null {
  const cutoffs = report.decisions.map((decision) => decision.cutoff_at);
  if (cutoffs.length === 0) return null;
  return cutoffs.slice().sort().reverse()[0];
}

function projectOverview(report: StrategyRunReport): ModuleOverviewRow[] {
  const decisions = report.decisions;
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

function projectRunIdentity(report: SourcedStrategyRunReport): RunIdentity {
  return {
    strategyRunId: report.strategy_run_id ?? null,
    executedAt: report.executed_at ?? null,
    corpusSha256: report.corpus_sha256,
    source: report.source,
  };
}

function toRankingRow(report: StrategyRunReport, decision: StrategyRunDecision): RankingRow {
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
    traceId: traceId(report.source, decision.issuer_id, decision.cutoff_at, report.corpus_sha256),
  };
}

function toComparisonRow(report: StrategyRunReport, decision: StrategyRunDecision): ComparisonRow {
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
    traceId: traceId(report.source, decision.issuer_id, decision.cutoff_at, report.corpus_sha256),
  };
}

function projectRanking(report: StrategyRunReport, cutoffAt?: string): RankingRow[] {
  const cutoff = cutoffAt ?? projectLatestCutoff(report);
  if (cutoff === null) return [];
  const decisions = report.decisions.filter((decision) => decision.cutoff_at === cutoff);
  const ordered = decisions.slice().sort(rankingOrder);
  return ordered.map((decision) => toRankingRow(report, decision));
}

function projectComparison(report: StrategyRunReport, cutoffAt?: string): ComparisonRow[] {
  const cutoff = cutoffAt ?? projectLatestCutoff(report);
  if (cutoff === null) return [];
  const decisions = report.decisions.filter((decision) => decision.cutoff_at === cutoff);
  const ordered = decisions.slice().sort((a, b) => (a.issuer_id < b.issuer_id ? -1 : a.issuer_id > b.issuer_id ? 1 : 0));
  return ordered.map((decision) => toComparisonRow(report, decision));
}

function projectEntityDetail(report: StrategyRunReport, issuerId: string): EntityDetail | null {
  const rows = report.decisions
    .filter((decision) => decision.issuer_id === issuerId)
    .slice()
    .sort((a, b) => (a.cutoff_at < b.cutoff_at ? -1 : a.cutoff_at > b.cutoff_at ? 1 : 0))
    .map((decision) => toComparisonRow(report, decision));
  if (rows.length === 0) return null;
  return { issuerId, rows };
}

function projectTraceView(report: StrategyRunReport, issuerId: string, cutoffAt: string): TraceView | null {
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

function ensureReport(result: SourcedStrategyRunReport | StrategyRunUnavailable): SourcedStrategyRunReport {
  if (!("decisions" in result)) throw new MartReadUnavailable(result.reason);
  return result;
}

/**
 * Production default: reads dashboard views from the governed head run in
 * `mart.strategy_runs`/`strategy_decisions` under `mart_readonly` (#362's repository).
 * Every read takes a server-derived `AccessContext`; it is never accepted from client
 * input. The context is reserved for a future authorization decision.
 */
export class MartResearchReadAdapter {
  private readonly repository: {
    getLatest(
      strategyId: string,
      context: AccessContext,
    ): Promise<SourcedStrategyRunReport | StrategyRunUnavailable> | SourcedStrategyRunReport | StrategyRunUnavailable;
  };

  constructor(repository?: MartResearchReadAdapter["repository"]) {
    this.repository = repository ?? new MartStrategyRunRepository();
  }

  private async report(context: AccessContext): Promise<SourcedStrategyRunReport> {
    return ensureReport(await this.repository.getLatest(DASHBOARD_STRATEGY_ID, context));
  }

  async latestCutoff(context: AccessContext): Promise<string | null> {
    return projectLatestCutoff(await this.report(context));
  }

  async overview(context: AccessContext): Promise<ModuleOverviewRow[]> {
    return projectOverview(await this.report(context));
  }

  async runIdentity(context: AccessContext): Promise<RunIdentity> {
    return projectRunIdentity(await this.report(context));
  }

  async ranking(context: AccessContext, cutoffAt?: string): Promise<RankingRow[]> {
    return projectRanking(await this.report(context), cutoffAt);
  }

  async comparison(context: AccessContext, cutoffAt?: string): Promise<ComparisonRow[]> {
    return projectComparison(await this.report(context), cutoffAt);
  }

  async entityDetail(context: AccessContext, issuerId: string): Promise<EntityDetail | null> {
    return projectEntityDetail(await this.report(context), issuerId);
  }

  async traceView(context: AccessContext, issuerId: string, cutoffAt: string): Promise<TraceView | null> {
    return projectTraceView(await this.report(context), issuerId, cutoffAt);
  }
}

/**
 * TESTS ONLY: projects the checked-in strategy-run fixture through the exact same
 * projection functions as the mart adapter, so tests assert known byte-exact values
 * without a database. Never a route-loader default (statically enforced in
 * `tests/dashboard-boundary.test.ts`).
 */
export class FixtureMartReadAdapter {
  private readonly repository: { getLatest(strategyId: string, context: AccessContext): StrategyRunReport | StrategyRunUnavailable };

  constructor(repository?: {
    getLatest(strategyId: string, context: AccessContext): StrategyRunReport | StrategyRunUnavailable;
  }) {
    this.repository = repository ?? new FixtureStrategyRunRepository();
  }

  private report(context: AccessContext): SourcedStrategyRunReport {
    return ensureReport(this.repository.getLatest(DASHBOARD_STRATEGY_ID, context));
  }

  latestCutoff(context: AccessContext): string | null {
    return projectLatestCutoff(this.report(context));
  }

  overview(context: AccessContext): ModuleOverviewRow[] {
    return projectOverview(this.report(context));
  }

  runIdentity(context: AccessContext): RunIdentity {
    return projectRunIdentity(this.report(context));
  }

  ranking(context: AccessContext, cutoffAt?: string): RankingRow[] {
    return projectRanking(this.report(context), cutoffAt);
  }

  comparison(context: AccessContext, cutoffAt?: string): ComparisonRow[] {
    return projectComparison(this.report(context), cutoffAt);
  }

  entityDetail(context: AccessContext, issuerId: string): EntityDetail | null {
    return projectEntityDetail(this.report(context), issuerId);
  }

  traceView(context: AccessContext, issuerId: string, cutoffAt: string): TraceView | null {
    return projectTraceView(this.report(context), issuerId, cutoffAt);
  }
}
