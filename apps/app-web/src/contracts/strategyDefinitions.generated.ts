/**
 * GENERATED FILE. DO NOT EDIT.
 *
 * Source: truealpha_contracts.strategy.strategy_definition_registry() (#1235). It loads
 * every packaged definition file in common/contracts/src/truealpha_contracts/data/.
 * Regenerate: uv run python common/contracts/conformance/export_strategy_definitions.py --write
 * Guard: common/contracts/tests/test_strategy_definition_registry.py fails when this file differs.
 */

/** One packaged strategy definition, by the identity that a strategy run records. */
export interface RegisteredStrategyDefinition {
  readonly version: string;
  readonly content_sha256: string;
  readonly provisional_semantics: boolean;
}

export const STRATEGY_DEFINITION_REGISTRY: readonly RegisteredStrategyDefinition[] = [
  { version: "1.0.0", content_sha256: "a0c6f1f003373b5eff85872fd493de91e2fd6d9fa3d15e78e278f96b88bd8803", provisional_semantics: false },
  { version: "1.1.0", content_sha256: "ef03b123b84c16ade2a0a81898da2b74bcc56dc7c19f82c561a26a7a8379b181", provisional_semantics: false },
];
