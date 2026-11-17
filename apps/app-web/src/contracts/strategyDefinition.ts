import { type RegisteredStrategyDefinition, STRATEGY_DEFINITION_REGISTRY } from "./strategyDefinitions.generated";

export const DEFINITION_REVIEW_STATES = ["reviewed", "provisional", "unknown"] as const;
export type DefinitionReviewState = (typeof DEFINITION_REVIEW_STATES)[number];

export interface StrategyRunDefinition {
  version: string;
  content_sha256: string;
  review_state: DefinitionReviewState;
}

const SHA256_PATTERN = /^[0-9a-f]{64}$/;

export function definitionReviewState(
  version: string,
  contentSha256: string,
  registry: readonly RegisteredStrategyDefinition[] = STRATEGY_DEFINITION_REGISTRY,
): DefinitionReviewState {
  const entry = registry.find((candidate) => candidate.content_sha256 === contentSha256 && candidate.version === version);
  if (entry === undefined) return "unknown";
  return entry.provisional_semantics ? "provisional" : "reviewed";
}

export function strategyRunDefinitionOf(
  version: unknown,
  contentSha256: unknown,
  registry: readonly RegisteredStrategyDefinition[] = STRATEGY_DEFINITION_REGISTRY,
): StrategyRunDefinition | null {
  if (typeof version !== "string" || typeof contentSha256 !== "string" || !SHA256_PATTERN.test(contentSha256)) {
    return null;
  }
  return { version, content_sha256: contentSha256, review_state: definitionReviewState(version, contentSha256, registry) };
}

export function asStrategyRunDefinition(value: unknown): StrategyRunDefinition | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  const object = value as Record<string, unknown>;
  if (Object.keys(object).sort().join(",") !== "content_sha256,review_state,version") return null;
  const definition = strategyRunDefinitionOf(object.version, object.content_sha256);
  if (definition === null || definition.review_state !== object.review_state) return null;
  return definition;
}
