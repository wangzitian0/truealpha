/**
 * Server-only route loader for /admin/strategy-runs — see #349/#371.
 *
 * Re-authorizes on every call (never trusts a cached decision), then reads
 * exclusively through #347's `FixtureStrategyRunRepository`. No App code
 * here queries Postgres directly or calls FastAPI for data display.
 *
 * #371: takes an already-resolved `StrategyRunPrincipal | null` (this
 * module never derives identity itself — same pattern as `dashboard.ts`).
 * A verified session that is not `principal_kind='administrator'` is denied
 * exactly like no session at all: administrator routing is driven by
 * `app.principals.principal_kind`, never by merely being logged in.
 */

import type { AccessContext, StrategyRunReport, StrategyRunUnavailable } from "@/contracts/strategyRun";
import { FixtureStrategyRunRepository } from "@/contracts/strategyRun";

export interface StrategyRunReadRepositoryLike {
  getLatest(strategyId: string, context: AccessContext): StrategyRunReport | StrategyRunUnavailable;
}

export interface StrategyRunPrincipal {
  context: AccessContext;
  principalKind: "member" | "administrator" | "service";
}

export type StrategyRunPageOutcome =
  | { kind: "ready"; report: StrategyRunReport }
  | { kind: "unavailable"; detail: StrategyRunUnavailable }
  | { kind: "denied" }
  | { kind: "error"; message: string };

/** `repository` is an injection point for tests only; production code omits it. */
export function loadStrategyRunPage(
  principal: StrategyRunPrincipal | null,
  strategyId: string,
  repository: StrategyRunReadRepositoryLike = new FixtureStrategyRunRepository(),
): StrategyRunPageOutcome {
  if (principal === null || principal.principalKind !== "administrator") {
    return { kind: "denied" };
  }

  try {
    const result = repository.getLatest(strategyId, principal.context);
    if ("decisions" in result) return { kind: "ready", report: result };
    return { kind: "unavailable", detail: result };
  } catch (error) {
    return { kind: "error", message: error instanceof Error ? error.message : String(error) };
  }
}
