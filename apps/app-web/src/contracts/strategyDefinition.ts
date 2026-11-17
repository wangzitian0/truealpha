export const DEFINITION_REVIEW_STATES = ["reviewed", "provisional", "unknown"] as const;
export type DefinitionReviewState = (typeof DEFINITION_REVIEW_STATES)[number];

export interface StrategyRunDefinition {
  version: string;
  content_sha256: string;
  review_state: DefinitionReviewState;
}
