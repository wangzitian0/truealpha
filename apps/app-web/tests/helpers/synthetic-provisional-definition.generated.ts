/**
 * GENERATED FILE. DO NOT EDIT. TEST HELPER: the App never imports this file.
 *
 * Source: truealpha_runtime.testing.synthetic_provisional_strategy_definition() (#1235).
 * The definition passed the full model validation, which requires provisional_semantics to
 * equal tier_valuation.provisional_thresholds. No packaged definition is provisional, so
 * the web tests inject this one to keep the provisional marker armed.
 * Regenerate: uv run python libs/contracts/conformance/export_strategy_definitions.py --write
 * Guard: libs/contracts/tests/test_strategy_definition_registry.py fails when this file differs.
 */

import type { RegisteredStrategyDefinition } from "../../src/contracts/strategyDefinitions.generated";

export const SYNTHETIC_PROVISIONAL_DEFINITION: RegisteredStrategyDefinition = { version: "0.0.1-synthetic-provisional", content_sha256: "d5256dc95d4a2e6cfd48ca889bf0e5b6645874d262d1c29d83a30d2994073693", provisional_semantics: true };
