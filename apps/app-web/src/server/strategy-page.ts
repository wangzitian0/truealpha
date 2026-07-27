/**
 * Server-only route loader for /research/strategy — see #349/#371/#362/#493.
 *
 * Re-authorizes on every call (never trusts a cached decision), then reads
 * through the real `mart` schema (`MartStrategyRunRepository`, via the
 * `mart_readonly` role) — #362 retired the checked-in fixture as the shipped
 * consumer path. No App code here queries raw/staging or calls FastAPI for
 * data display.
 *
 * #371: takes an already-resolved `StrategyRunPrincipal | null` (this
 * module never derives identity itself — same pattern as `dashboard.ts`).
 * #493 moved this page from /admin/strategy-runs into the research world:
 * strategy decisions are research output, readable by every verified
 * principal — the same mart rows /research/rankings already serves them.
 * Anonymous (null principal) stays denied; the administrator-only gate is
 * gone by design, not by accident.
 */

import type { AccessContext, StrategyRunReport, StrategyRunUnavailable } from "@/contracts/strategyRun";
import { MartStrategyRunRepository } from "@/server/mart/strategy-run-repository";

export interface StrategyRunReadRepositoryLike {
  getLatest(strategyId: string, context: AccessContext): Promise<StrategyRunReport | StrategyRunUnavailable>;
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
export async function loadStrategyRunPage(
  principal: StrategyRunPrincipal | null,
  strategyId: string,
  repository: StrategyRunReadRepositoryLike = new MartStrategyRunRepository(),
): Promise<StrategyRunPageOutcome> {
  if (principal === null) {
    return { kind: "denied" };
  }

  try {
    const result = await repository.getLatest(strategyId, principal.context);
    if ("decisions" in result) return { kind: "ready", report: result };
    return { kind: "unavailable", detail: result };
  } catch (error) {
    return { kind: "error", message: error instanceof Error ? error.message : String(error) };
  }
}
