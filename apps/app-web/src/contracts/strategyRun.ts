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

const SHA256_PATTERN = /^[0-9a-f]{64}$/;
const CUTOFF_PATTERN = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/;
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

export const VALUATION_TIERS = ["traditional", "tech", "large_model_native"] as const;
export type ValuationTier = (typeof VALUATION_TIERS)[number];

export interface StrategyRunDecision {
  issuer_id: string;
  cutoff_at: string;
  outcome: StrategyRunOutcome;
  eligible: boolean;
  tier: ValuationTier | null;
  capital_adjusted_labor_efficiency: string | null;
  current_price_to_sales: string | null;
  target_price_to_sales: string | null;
  valuation_gap: string | null;
  confidence: string | null;
  exclusion_reason: string | null;
  rank: number | null;
  target_weight: string | null;
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
    | "schema_mismatch";
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

function assertExactKeys(value: Record<string, unknown>, expected: readonly string[], path: string): void {
  const expectedSet = new Set(expected);
  const unknown = Object.keys(value).filter((key) => !expectedSet.has(key));
  const missing = expected.filter((key) => !(key in value));
  if (unknown.length > 0) fail(path, `unknown fields: ${unknown.sort().join(", ")}`);
  if (missing.length > 0) fail(path, `missing fields: ${missing.sort().join(", ")}`);
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
  if (left.integerPart !== right.integerPart) return sign * (left.integerPart < right.integerPart ? -1 : 1);
  const width = Math.max(left.fractionalPart.length, right.fractionalPart.length);
  const leftFraction = left.fractionalPart.padEnd(width, "0");
  const rightFraction = right.fractionalPart.padEnd(width, "0");
  if (leftFraction === rightFraction) return 0;
  return sign * (leftFraction < rightFraction ? -1 : 1);
}

function asDecimalString(value: unknown, path: string, bounds?: readonly [number, number]): string | null {
  if (value === null) return null;
  if (typeof value !== "string" || !DECIMAL_PATTERN.test(value)) {
    fail(path, "expected a decimal string");
  }
  if (bounds) {
    const [min, max] = bounds;
    if (compareDecimalStrings(value, String(min)) < 0 || compareDecimalStrings(value, String(max)) > 0) {
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
      "valuation_gap",
      "confidence",
      "exclusion_reason",
      "rank",
      "target_weight",
    ],
    path,
  );

  const issuerId = object.issuer_id;
  if (typeof issuerId !== "string" || issuerId.length === 0) fail(`${path}.issuer_id`, "expected a non-empty string");

  const cutoffAt = object.cutoff_at;
  if (typeof cutoffAt !== "string" || !CUTOFF_PATTERN.test(cutoffAt) || Number.isNaN(Date.parse(cutoffAt))) {
    fail(`${path}.cutoff_at`, "expected an aware ISO date-time");
  }

  const outcome = object.outcome;
  if (typeof outcome !== "string" || !(STRATEGY_RUN_OUTCOMES as readonly string[]).includes(outcome)) {
    fail(`${path}.outcome`, "unknown outcome");
  }

  if (typeof object.eligible !== "boolean") fail(`${path}.eligible`, "expected a boolean");

  const tier = object.tier;
  if (tier !== null && !(VALUATION_TIERS as readonly string[]).includes(tier as string)) {
    fail(`${path}.tier`, "unknown tier");
  }

  if (object.exclusion_reason !== null && typeof object.exclusion_reason !== "string") {
    fail(`${path}.exclusion_reason`, "expected a string or null");
  }

  const rank = object.rank;
  if (rank !== null && (typeof rank !== "number" || !Number.isInteger(rank) || rank < 1)) {
    fail(`${path}.rank`, "expected a positive integer or null");
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
    current_price_to_sales: asDecimalString(object.current_price_to_sales, `${path}.current_price_to_sales`),
    target_price_to_sales: asDecimalString(object.target_price_to_sales, `${path}.target_price_to_sales`),
    valuation_gap: asDecimalString(object.valuation_gap, `${path}.valuation_gap`),
    confidence: asDecimalString(object.confidence, `${path}.confidence`, [0, 1]),
    exclusion_reason: object.exclusion_reason as string | null,
    rank: rank as number | null,
    target_weight: asDecimalString(object.target_weight, `${path}.target_weight`, [0, 1]),
  };
}

/** Parses and strictly validates a `StrategyRunReport`, rejecting unknown fields. */
export function parseStrategyRunReport(value: unknown): StrategyRunReport {
  const object = asObject(value, "$");
  assertExactKeys(object, ["strategy_id", "source", "corpus_sha256", "decisions", "golden_mismatches"], "$");

  if (object.strategy_id !== "large_model_value_v0") fail("$.strategy_id", "unknown strategy_id");
  if (object.source !== "strategy_smoke_fixture") fail("$.source", "unknown source");

  const corpusSha256 = object.corpus_sha256;
  if (typeof corpusSha256 !== "string" || !SHA256_PATTERN.test(corpusSha256)) {
    fail("$.corpus_sha256", "expected a sha256 hex digest");
  }

  if (!Array.isArray(object.decisions)) fail("$.decisions", "expected an array");
  const decisions = object.decisions.map((decision, index) => parseDecision(decision, `$.decisions[${index}]`));

  if (!Array.isArray(object.golden_mismatches) || object.golden_mismatches.some((item) => typeof item !== "string")) {
    fail("$.golden_mismatches", "expected a string array");
  }

  return {
    strategy_id: "large_model_value_v0",
    source: "strategy_smoke_fixture",
    corpus_sha256: corpusSha256,
    decisions,
    golden_mismatches: object.golden_mismatches as string[],
  };
}

// Resolved from process.cwd(), not import.meta.url: Next.js's webpack RSC
// bundling substitutes a URL implementation that node:fs's readFileSync does
// not recognize as `instanceof URL` (verified — both a raw URL and
// fileURLToPath(url) throw ERR_INVALID_ARG_TYPE at request time, though both
// work fine under a bare `bun run` or `tsc`). Every invocation of this
// package (bun test, `next dev`/`build`/`start`) runs with cwd == apps/app-web
// (see the Makefile), so this stays stable across both.
const FIXTURE_PATH = join(
  process.cwd(),
  "..",
  "..",
  "libs/contracts/src/truealpha_contracts/data/strategy_run_preview.v1.json",
);

/**
 * Loads the one checked-in `large_model_value_v0` preview fixture — server-side only.
 * Mirrors `truealpha_contracts.strategy_run_fixture.FixtureStrategyRunRepository`.
 */
export class FixtureStrategyRunRepository {
  /** `context` is reserved for a future authorization decision; unused today. */
  getLatest(strategyId: string, _context: AccessContext): StrategyRunReport | StrategyRunUnavailable {
    let raw: string;
    try {
      raw = readFileSync(FIXTURE_PATH, "utf8");
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") {
        return { strategy_id: strategyId, reason: "fixture_missing" };
      }
      throw error;
    }

    let report: StrategyRunReport;
    try {
      report = parseStrategyRunReport(JSON.parse(raw));
    } catch (error) {
      if (error instanceof StrategyRunContractError || error instanceof SyntaxError) {
        return { strategy_id: strategyId, reason: "fixture_hash_mismatch" };
      }
      throw error;
    }

    if (strategyId !== report.strategy_id) {
      return { strategy_id: strategyId, reason: "unknown_strategy_id" };
    }
    return report;
  }
}
