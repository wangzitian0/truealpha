/**
 * Server-only route loader for /admin/strategy-runs — see #349.
 *
 * Re-authorizes on every call (never trusts a cached decision), then reads
 * exclusively through #347's `FixtureStrategyRunRepository`. No App code
 * here queries Postgres directly or calls FastAPI for data display.
 */

import {
  FixtureStrategyRunRepository,
  type StrategyRunReport,
  type StrategyRunUnavailable,
} from "@/contracts/strategyRun";
import { getLocalAdminAccessContext } from "@/server/auth-context";

export interface StrategyRunReadRepositoryLike {
  getLatest(strategyId: string): StrategyRunReport | StrategyRunUnavailable;
}

export type StrategyRunPageOutcome =
  | { kind: "ready"; report: StrategyRunReport }
  | { kind: "unavailable"; detail: StrategyRunUnavailable }
  | { kind: "denied" }
  | { kind: "error"; message: string };

/** `repository` is an injection point for tests only; production code omits it. */
export function loadStrategyRunPage(
  strategyId: string,
  repository: StrategyRunReadRepositoryLike = new FixtureStrategyRunRepository(),
): StrategyRunPageOutcome {
  const context = getLocalAdminAccessContext();
  if (context === null) return { kind: "denied" };

  try {
    const result = repository.getLatest(strategyId);
    if ("decisions" in result) return { kind: "ready", report: result };
    return { kind: "unavailable", detail: result };
  } catch (error) {
    return { kind: "error", message: error instanceof Error ? error.message : String(error) };
  }
}
