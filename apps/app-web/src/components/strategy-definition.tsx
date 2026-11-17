import type { StrategyRunDefinition } from "@/contracts/strategyDefinition";

function lineText(definition: StrategyRunDefinition | undefined): string {
  if (definition === undefined) return "Definition unknown · not reviewed";
  if (definition.review_state === "reviewed") return `Definition ${definition.version} · reviewed`;
  if (definition.review_state === "provisional") {
    return `Definition ${definition.version} · PROVISIONAL: bands not reviewed`;
  }
  return `Definition ${definition.version} · not reviewed (not in the registry)`;
}

export function StrategyDefinitionLine({ definition }: { definition: StrategyRunDefinition | undefined }) {
  const state = definition?.review_state ?? "unknown";
  return (
    <p
      data-testid="strategy-definition"
      data-review-state={state}
      title={definition ? `Definition content sha256 ${definition.content_sha256}` : undefined}
      className={state === "reviewed" ? "mt-1 text-sm text-gray-400" : "mt-1 text-sm font-medium text-amber-300"}
    >
      {lineText(definition)}
    </p>
  );
}
