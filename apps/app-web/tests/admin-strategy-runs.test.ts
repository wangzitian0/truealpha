/**
 * #349/#371: /admin/strategy-runs server loader — denial, ready, and error
 * outcomes. #371 adds the role check: a verified session that is not
 * `principal_kind='administrator'` must be denied exactly like no session
 * at all — administrator routing is driven by principal_kind, never by
 * merely being logged in (#368 acceptance criterion).
 *
 * Run standalone (`bun run tests/admin-strategy-runs.test.ts`), not through Next.js.
 */

import {
  loadStrategyRunPage,
  type StrategyRunPrincipal,
  type StrategyRunReadRepositoryLike,
} from "../src/server/admin-strategy-runs";
import {
  FixtureStrategyRunRepository,
  type AccessContext,
  type StrategyRunReport,
  type StrategyRunUnavailable,
} from "../src/contracts/strategyRun";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

// The shipped default is now the async mart read (#362); these outcome tests pin
// the fixture oracle explicitly through an async adapter over the sync fixture,
// so they stay hermetic (no live Postgres) while still asserting the 10 golden
// decisions. The mart adapter itself is proven against a real DB in
// tests/mart-strategy-run-repository.test.ts.
const fixtureRepository: StrategyRunReadRepositoryLike = {
  getLatest: async (strategyId, context) => new FixtureStrategyRunRepository().getLatest(strategyId, context),
};

const TEST_CONTEXT: AccessContext = {
  contextId: "ctx:test",
  principalId: "principal:test-admin",
  tenantId: "tenant:truealpha",
  sessionId: "session:test",
  authenticationMethod: "password",
  issuedAt: "2026-01-01T00:00:00Z",
  expiresAt: "2026-01-01T01:00:00Z",
};

const ADMIN_PRINCIPAL: StrategyRunPrincipal = { context: TEST_CONTEXT, principalKind: "administrator" };
const MEMBER_PRINCIPAL: StrategyRunPrincipal = { context: TEST_CONTEXT, principalKind: "member" };

// --- denied: no session at all, and the repository must never be called ---
{
  let repositoryCalled = false;
  const outcome = await loadStrategyRunPage(null, "large_model_value_v0", {
    getLatest: (_strategyId, _context) => {
      repositoryCalled = true;
      throw new Error("must not be reached when denied");
    },
  });
  assert(outcome.kind === "denied", `expected denied, got ${outcome.kind}`);
  assert(!repositoryCalled, "repository must not be called before authorization");
}

// --- denied: a verified session that is NOT an administrator, repository never called ---
{
  let repositoryCalled = false;
  const outcome = await loadStrategyRunPage(MEMBER_PRINCIPAL, "large_model_value_v0", {
    getLatest: (_strategyId, _context) => {
      repositoryCalled = true;
      throw new Error("must not be reached when a member is denied admin access");
    },
  });
  assert(outcome.kind === "denied", `expected a non-administrator to be denied, got ${outcome.kind}`);
  assert(!repositoryCalled, "repository must not be called for a non-administrator");
}

// --- ready: administrator principal present, repository returns a report ---
{
  const outcome = await loadStrategyRunPage(ADMIN_PRINCIPAL, "large_model_value_v0", fixtureRepository);
  assert(outcome.kind === "ready", `expected ready, got ${outcome.kind}`);
  const report: StrategyRunReport = outcome.report;
  assert(report.decisions.length === 10, `expected 10 decisions, got ${report.decisions.length}`);
  const selected = report.decisions.find((d) => d.issuer_id === "issuer:adm" && d.cutoff_at.startsWith("2026-03"));
  assert(selected !== undefined, "expected issuer:adm at the March cutoff");
  assert(selected.outcome === "selected", "expected selected outcome");
}

// --- unavailable: administrator present, unknown strategy_id ---
{
  const outcome = await loadStrategyRunPage(ADMIN_PRINCIPAL, "does_not_exist", fixtureRepository);
  assert(outcome.kind === "unavailable", `expected unavailable, got ${outcome.kind}`);
  const detail: StrategyRunUnavailable = outcome.detail;
  assert(detail.reason === "unknown_strategy_id", `unexpected reason ${detail.reason}`);
}

// --- error: repository throws, loader must not propagate the exception ---
{
  const outcome = await loadStrategyRunPage(ADMIN_PRINCIPAL, "large_model_value_v0", {
    getLatest: (_strategyId, _context) => {
      throw new Error("simulated fixture read failure");
    },
  });
  assert(outcome.kind === "error", `expected error, got ${outcome.kind}`);
  assert(outcome.message.includes("simulated fixture read failure"), `unexpected message: ${outcome.message}`);
}

console.log("#349/#371 admin-strategy-runs loader outcomes passed");
