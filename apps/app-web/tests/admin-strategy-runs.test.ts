/**
 * #349: /admin/strategy-runs server loader — denial, ready, and error outcomes.
 *
 * Run standalone (`bun run tests/admin-strategy-runs.test.ts`), not through
 * Next.js, so it controls process.env directly without a live server.
 */

import { loadStrategyRunPage } from "../src/server/admin-strategy-runs";
import type { StrategyRunReport, StrategyRunUnavailable } from "../src/contracts/strategyRun";

const ADMIN_ENV_VAR = "TRUEALPHA_LOCAL_ADMIN_PRINCIPAL_ID";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

// --- denied: absent principal, and the repository must never be called ---
{
  delete process.env[ADMIN_ENV_VAR];
  let repositoryCalled = false;
  const outcome = loadStrategyRunPage("large_model_value_v0", {
    getLatest: () => {
      repositoryCalled = true;
      throw new Error("must not be reached when denied");
    },
  });
  assert(outcome.kind === "denied", `expected denied, got ${outcome.kind}`);
  assert(!repositoryCalled, "repository must not be called before authorization");
}

// --- ready: principal present, repository returns a report ---
{
  process.env[ADMIN_ENV_VAR] = "principal:test-admin";
  const outcome = loadStrategyRunPage("large_model_value_v0");
  assert(outcome.kind === "ready", `expected ready, got ${outcome.kind}`);
  const report: StrategyRunReport = outcome.report;
  assert(report.decisions.length === 10, `expected 10 decisions, got ${report.decisions.length}`);
  const selected = report.decisions.find((d) => d.issuer_id === "issuer:adm" && d.cutoff_at.startsWith("2026-03"));
  assert(selected !== undefined, "expected issuer:adm at the March cutoff");
  assert(selected.outcome === "selected", "expected selected outcome");
}

// --- unavailable: principal present, unknown strategy_id ---
{
  process.env[ADMIN_ENV_VAR] = "principal:test-admin";
  const outcome = loadStrategyRunPage("does_not_exist");
  assert(outcome.kind === "unavailable", `expected unavailable, got ${outcome.kind}`);
  const detail: StrategyRunUnavailable = outcome.detail;
  assert(detail.reason === "unknown_strategy_id", `unexpected reason ${detail.reason}`);
}

// --- error: repository throws, loader must not propagate the exception ---
{
  process.env[ADMIN_ENV_VAR] = "principal:test-admin";
  const outcome = loadStrategyRunPage("large_model_value_v0", {
    getLatest: () => {
      throw new Error("simulated fixture read failure");
    },
  });
  assert(outcome.kind === "error", `expected error, got ${outcome.kind}`);
  assert(outcome.message.includes("simulated fixture read failure"), `unexpected message: ${outcome.message}`);
}

delete process.env[ADMIN_ENV_VAR];
console.log("#349 admin-strategy-runs loader outcomes passed");
