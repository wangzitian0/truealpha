/**
 * #1235: the strategy definition that a run evaluated, and the review state of its bands.
 *
 * Mirrors `truealpha_contracts.strategy.definition_review_state` and
 * `truealpha_contracts.strategy_run.StrategyRunDefinition`. The registry is the generated
 * twin of the Python registry: the web image cannot read libs/contracts at run time.
 */

import { type RegisteredStrategyDefinition, STRATEGY_DEFINITION_REGISTRY } from "./strategyDefinitions.generated";

export const DEFINITION_REVIEW_STATES = ["reviewed", "provisional", "unknown"] as const;
export type DefinitionReviewState = (typeof DEFINITION_REVIEW_STATES)[number];

export interface StrategyRunDefinition {
  version: string;
  content_sha256: string;
  review_state: DefinitionReviewState;
}

const SHA256_PATTERN = /^[0-9a-f]{64}$/;

/** The content hash selects the registry entry and the version must agree with it.
 * A pair that the registry does not hold is "unknown", never "reviewed".
 * `registry` is the generated registry; tests pass another one to add a definition. */
export function definitionReviewState(
  version: string,
  contentSha256: string,
  registry: readonly RegisteredStrategyDefinition[] = STRATEGY_DEFINITION_REGISTRY,
): DefinitionReviewState {
  const entry = registry.find((candidate) => candidate.content_sha256 === contentSha256 && candidate.version === version);
  if (entry === undefined) return "unknown";
  return entry.provisional_semantics ? "provisional" : "reviewed";
}

/** The marker of a recorded run, from `mart.strategy_runs.strategy_version` and
 * `definition_content_sha256`. Null when a value does not have the contract shape,
 * which the repositories report as `schema_mismatch`, as the Python twin does. */
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

/** Parses a serialized marker. Null when the shape is wrong, or when the registry gives
 * a different review state: a serialized "reviewed" does not make a definition reviewed. */
export function asStrategyRunDefinition(value: unknown): StrategyRunDefinition | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  const object = value as Record<string, unknown>;
  if (Object.keys(object).sort().join(",") !== "content_sha256,review_state,version") return null;
  const definition = strategyRunDefinitionOf(object.version, object.content_sha256);
  if (definition === null || definition.review_state !== object.review_state) return null;
  return definition;
}
