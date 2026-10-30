/**
 * GENERATED FILE. DO NOT EDIT. TEST HELPER: the App never imports this file.
 *
 * Source: truealpha_runtime.testing.synthetic_provisional_strategy_definition() (#1235).
 * The definition passed the full model validation, which requires provisional_semantics to
 * equal tier_valuation.provisional_thresholds. No packaged definition is provisional, so
 * the web tests inject this one to keep the provisional marker armed.
 * Regenerate: uv run python common/contracts/conformance/export_strategy_definitions.py --write
 * Guard: common/contracts/tests/test_strategy_definition_registry.py fails when this file differs.
 */

import type { RegisteredStrategyDefinition } from "../../src/contracts/strategyDefinitions.generated";

export const SYNTHETIC_PROVISIONAL_DEFINITION: RegisteredStrategyDefinition = { version: "0.0.1-synthetic-provisional", content_sha256: "8675d6139ccce5556d21cd6b0ecede962deb568cf89de8e2df37c0c3865b05e2", provisional_semantics: true };
