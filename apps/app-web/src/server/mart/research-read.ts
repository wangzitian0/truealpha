/**
 * TypeScript mart read adapter for the research dashboard — see #370.
 *
 * Conforms to the provisional read models the App and MCP already share (`#347`'s
 * `strategyRun.ts`), pending #41's stable seven-module `ResearchReadRepository`. It reads
 * the same checked-in strategy-run fixture bytes the MCP tool reads, so the App reproduces
 * the MCP comparison/ranking values exactly. When #41 lands a mart-backed read role, only
 * this adapter changes — the loaders, states, and pages are untouched.
 *
 * Boundary: this module performs NO cross-factor computation. It selects, labels, sorts,
 * and copies already-materialized values through byte-exact. It never joins two factors or
 * two time points into a new metric (init.md Section 1, rule 2). A boundary scan in
 * `tests/dashboard-boundary.test.ts` statically forbids metric arithmetic here.
 *
 * `FixtureStrategyRunRepository` does filesystem I/O — server-only. Never import this from
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

function valueAvailability(value: string | null, sectionStatus: Availability): Availability {
  return value === null ? "unavailable" : sectionStatus;
}

function traceId(issuerId: string, cutoffAt: string, corpusSha256: string): string {
  const corpusPrefix = corpusSha256.slice(0, 12);
  const date = cutoffAt.slice(0, 10);
  return `strategy_smoke_fixture:${corpusPrefix}:${issuerId}:${date}`;
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

/**
 * Reads dashboard views from the materialized strategy run. Every read takes a
 * server-derived `AccessContext`; it is never accepted from client input. The context is
 * reserved for a future authorization decision (mirrors the provisional repositories).
 */
export class FixtureMartReadAdapter {
  private readonly repository: { getLatest(strategyId: string, context: AccessContext): StrategyRunReport | StrategyRunUnavailable };

  constructor(repository?: {
    getLatest(strategyId: string, context: AccessContext): StrategyRunReport | StrategyRunUnavailable;
  }) {
    this.repository = repository ?? new FixtureStrategyRunRepository();
  }

  private report(context: AccessContext): StrategyRunReport {
    const result = this.repository.getLatest(DASHBOARD_STRATEGY_ID, context);
    if (!("decisions" in result)) throw new MartReadUnavailable(result.reason);
    return result;
  }

  latestCutoff(context: AccessContext): string | null {
    const cutoffs = this.report(context).decisions.map((decision) => decision.cutoff_at);
    if (cutoffs.length === 0) return null;
    return cutoffs.slice().sort().reverse()[0];
  }

  overview(context: AccessContext): ModuleOverviewRow[] {
    const decisions = this.report(context).decisions;
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

  private toRankingRow(decision: StrategyRunDecision, corpusSha256: string): RankingRow {
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
      traceId: traceId(decision.issuer_id, decision.cutoff_at, corpusSha256),
    };
  }

  private toComparisonRow(decision: StrategyRunDecision, corpusSha256: string): ComparisonRow {
    const status = decisionAvailability(decision);
    return {
      issuerId: decision.issuer_id,
      cutoffAt: decision.cutoff_at,
      capitalAdjustedLaborEfficiency: decision.capital_adjusted_labor_efficiency,
      currentPriceToSales: decision.current_price_to_sales,
      tier: decision.tier,
      valuationGap: decision.valuation_gap,
      confidence: decision.confidence,
      availability: valueAvailability(decision.capital_adjusted_labor_efficiency, status),
      traceId: traceId(decision.issuer_id, decision.cutoff_at, corpusSha256),
    };
  }

  ranking(context: AccessContext, cutoffAt?: string): RankingRow[] {
    const report = this.report(context);
    const cutoff = cutoffAt ?? this.latestCutoff(context);
    if (cutoff === null) return [];
    const decisions = report.decisions.filter((decision) => decision.cutoff_at === cutoff);
    const ordered = decisions.slice().sort(rankingOrder);
    return ordered.map((decision) => this.toRankingRow(decision, report.corpus_sha256));
  }

  comparison(context: AccessContext, cutoffAt?: string): ComparisonRow[] {
    const report = this.report(context);
    const cutoff = cutoffAt ?? this.latestCutoff(context);
    if (cutoff === null) return [];
    const decisions = report.decisions.filter((decision) => decision.cutoff_at === cutoff);
    const ordered = decisions.slice().sort((a, b) => (a.issuer_id < b.issuer_id ? -1 : a.issuer_id > b.issuer_id ? 1 : 0));
    return ordered.map((decision) => this.toComparisonRow(decision, report.corpus_sha256));
  }

  entityDetail(context: AccessContext, issuerId: string): EntityDetail | null {
    const report = this.report(context);
    const rows = report.decisions
      .filter((decision) => decision.issuer_id === issuerId)
      .slice()
      .sort((a, b) => (a.cutoff_at < b.cutoff_at ? -1 : a.cutoff_at > b.cutoff_at ? 1 : 0))
      .map((decision) => this.toComparisonRow(decision, report.corpus_sha256));
    if (rows.length === 0) return null;
    return { issuerId, rows };
  }

  traceView(context: AccessContext, issuerId: string, cutoffAt: string): TraceView | null {
    const report = this.report(context);
    const decision = report.decisions.find((d) => d.issuer_id === issuerId && d.cutoff_at === cutoffAt);
    if (decision === undefined) return null;
    return {
      traceId: traceId(issuerId, cutoffAt, report.corpus_sha256),
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
