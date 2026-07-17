/**
 * Python/TypeScript parity for #347's provisional StrategyRunReadRepository.
 *
 * Both `truealpha_contracts.strategy_run_fixture.FixtureStrategyRunRepository`
 * (see libs/contracts/tests/test_strategy_run.py) and
 * `FixtureStrategyRunRepository` here read the identical checked-in fixture
 * bytes at `libs/contracts/src/truealpha_contracts/data/strategy_run_preview.v1.json`.
 * This test asserts the same field values the Python test asserts, proving
 * both adapters agree rather than merely both parsing without error.
 */

import { readFileSync } from "node:fs";

import {
  FixtureStrategyRunRepository,
  parseStrategyRunReport,
  StrategyRunContractError,
  type StrategyRunReport,
} from "../src/contracts/strategyRun";

const fixtureUrl = new URL(
  "../../../libs/contracts/src/truealpha_contracts/data/strategy_run_preview.v1.json",
  import.meta.url,
);

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

async function expectRejected(label: string, fn: () => unknown, matcher: RegExp): Promise<void> {
  try {
    fn();
  } catch (error) {
    if (error instanceof StrategyRunContractError && matcher.test(error.message)) return;
    throw new Error(`${label}: rejected for the wrong reason: ${String(error)}`);
  }
  throw new Error(`${label}: expected rejection but none occurred`);
}

const raw = JSON.parse(readFileSync(fixtureUrl, "utf8")) as Record<string, unknown>;

// The committed fixture is deterministic: no wall-clock field, exactly 10 decisions.
assert(!raw.generated_at, "committed fixture must not carry generated_at");
assert(Array.isArray(raw.decisions) && raw.decisions.length === 10, "expected 10 committed decisions");

const report: StrategyRunReport = parseStrategyRunReport(raw);
assert(report.strategy_id === "large_model_value_v0", "strategy_id mismatch");
assert(report.golden_mismatches.length === 0, "committed fixture must have zero golden mismatches");

const repository = new FixtureStrategyRunRepository();
const fromRepository = repository.getLatest("large_model_value_v0");
assert("decisions" in fromRepository, "expected a StrategyRunReport from the repository");
assert(fromRepository.decisions.length === report.decisions.length, "repository/parse decision count mismatch");

const selected = report.decisions.find((d) => d.issuer_id === "issuer:adm" && d.cutoff_at.startsWith("2026-03"));
assert(selected !== undefined, "expected issuer:adm at the March cutoff");
assert(selected.outcome === "selected", "expected issuer:adm to be selected");
assert(selected.tier === "traditional", "expected traditional tier");
assert(selected.valuation_gap === "1.6388", `unexpected valuation_gap ${selected.valuation_gap}`);
assert(selected.confidence === "0.90", `unexpected confidence ${selected.confidence}`);
assert(selected.rank === 1, "expected rank 1");
assert(selected.target_weight === "0.500000", `unexpected target_weight ${selected.target_weight}`);

const excluded = report.decisions.find((d) => d.exclusion_reason === "below_confidence_floor");
assert(excluded !== undefined, "expected at least one below_confidence_floor exclusion");
assert(excluded.eligible === false, "excluded decision must be ineligible");
assert(excluded.confidence !== null, "excluded decision must still surface its confidence");

const unavailable = repository.getLatest("does_not_exist");
assert(!("decisions" in unavailable), "expected a StrategyRunUnavailable");
assert(unavailable.reason === "unknown_strategy_id", "expected unknown_strategy_id reason");

await expectRejected(
  "unknown field",
  () => parseStrategyRunReport({ ...raw, extra_field: "nope" }),
  /unknown fields/,
);

await expectRejected(
  "bad cutoff",
  () =>
    parseStrategyRunReport({
      ...raw,
      decisions: [{ ...(raw.decisions as Record<string, unknown>[])[0], cutoff_at: "not-a-date" }],
    }),
  /expected an aware ISO date-time/,
);

console.log("#347 Python/TypeScript strategy-run fixture parity passed");
