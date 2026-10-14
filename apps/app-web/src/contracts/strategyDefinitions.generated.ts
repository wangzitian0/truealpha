/**
 * GENERATED FILE. DO NOT EDIT.
 *
 * Source: truealpha_contracts.strategy.strategy_definition_registry() (#1235). It loads
 * every packaged definition file in libs/contracts/src/truealpha_contracts/data/.
 * Regenerate: uv run python libs/contracts/conformance/export_strategy_definitions.py --write
 * Guard: libs/contracts/tests/test_strategy_definition_registry.py fails when this file differs.
 */

/** One packaged strategy definition, by the identity that a strategy run records. */
export interface RegisteredStrategyDefinition {
  readonly version: string;
  readonly content_sha256: string;
  readonly provisional_semantics: boolean;
}

export const STRATEGY_DEFINITION_REGISTRY: readonly RegisteredStrategyDefinition[] = [
  { version: "0.1.0", content_sha256: "e5c56da455a2845d99e67265d96b30e9b479384ef11a8a4d242494b1816fe79d", provisional_semantics: true },
  { version: "1.0.0", content_sha256: "a0c6f1f003373b5eff85872fd493de91e2fd6d9fa3d15e78e278f96b88bd8803", provisional_semantics: false },
];
