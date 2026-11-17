import type { AccessContext, StrategyRunReport, StrategyRunUnavailable } from "@/contracts/strategyRun";
import { StrategyRunReadAdapter, type WidePanelCell } from "@/server/mart/research-read";
import { MartStrategyRunRepository } from "@/server/mart/strategy-run-repository";

export interface StrategyRunReadRepositoryLike {
  getLatest(strategyId: string, context: AccessContext): Promise<StrategyRunReport | StrategyRunUnavailable>;
}

export interface StrategyRunPrincipal {
  context: AccessContext;
  principalKind: "member" | "administrator" | "service";
}

export type StrategyRunPageOutcome =
  | {
      kind: "ready";
      report: StrategyRunReport;
      widePanel?: Readonly<Record<string, Readonly<Record<string, WidePanelCell>>>>;
    }
  | { kind: "empty" }
  | { kind: "unavailable"; detail: StrategyRunUnavailable }
  | { kind: "denied" }
  | { kind: "error"; message: string };

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
    if ("decisions" in result) {
      if (result.decisions.length === 0) return { kind: "empty" };
      const widePanel: Record<string, Readonly<Record<string, WidePanelCell>>> = {};
      if (repository instanceof MartStrategyRunRepository) {
        const panelMap = await new StrategyRunReadAdapter(repository).widePanelByIssuer();
        for (const [issuerId, cells] of panelMap.entries()) {
          widePanel[issuerId] = cells;
        }
      }
      return { kind: "ready", report: result, widePanel };
    }
    return { kind: "unavailable", detail: result };
  } catch (error) {
    return { kind: "error", message: error instanceof Error ? error.message : String(error) };
  }
}
