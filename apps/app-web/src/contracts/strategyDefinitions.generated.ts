/** GENERATED FILE. DO NOT EDIT. Regenerate: uv run python common/contracts/conformance/export_strategy_definitions.py --write */

export interface RegisteredStrategyDefinition {
  readonly version: string;
  readonly content_sha256: string;
  readonly provisional_semantics: boolean;
}

export const STRATEGY_DEFINITION_REGISTRY: readonly RegisteredStrategyDefinition[] = [
  { version: "1.0.0", content_sha256: "a0c6f1f003373b5eff85872fd493de91e2fd6d9fa3d15e78e278f96b88bd8803", provisional_semantics: false },
  { version: "1.1.0", content_sha256: "b8768334a00f88f9f34244a758182454ef220343e161f6c5008dde5c9d4611e3", provisional_semantics: false },
];
