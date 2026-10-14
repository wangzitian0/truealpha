"""Generate the TypeScript twins of the strategy definition registry (#1235).

The web image builds from apps/app-web alone, so the App cannot read the packaged
definition files at run time. This script writes two files:

- ``TARGET``: ``truealpha_contracts.strategy.strategy_definition_registry()``, which the
  App reads to give each served run its review state.
- ``SYNTHETIC_TARGET``: the identity of
  ``truealpha_runtime.testing.synthetic_provisional_strategy_definition()``. No packaged
  definition is provisional, so the web tests inject this one to keep the provisional
  marker armed. It is a test helper and never reaches the App.

``libs/contracts/tests/test_strategy_definition_registry.py`` renders both texts again and
fails when a checked-in file differs.

Run from the repository root:

    uv run python libs/contracts/conformance/export_strategy_definitions.py --write
    uv run python libs/contracts/conformance/export_strategy_definitions.py --check
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from truealpha_contracts.strategy import StrategyDefinitionIdentity, strategy_definition_registry
from truealpha_runtime.testing import synthetic_provisional_strategy_definition

REPO_ROOT = Path(__file__).resolve().parents[3]
TARGET = REPO_ROOT / "apps" / "app-web" / "src" / "contracts" / "strategyDefinitions.generated.ts"
SYNTHETIC_TARGET = (
    REPO_ROOT / "apps" / "app-web" / "tests" / "helpers" / "synthetic-provisional-definition.generated.ts"
)

_REGENERATE = "uv run python libs/contracts/conformance/export_strategy_definitions.py --write"
_GUARD = "libs/contracts/tests/test_strategy_definition_registry.py"

_HEADER = f"""\
/**
 * GENERATED FILE. DO NOT EDIT.
 *
 * Source: truealpha_contracts.strategy.strategy_definition_registry() (#1235). It loads
 * every packaged definition file in libs/contracts/src/truealpha_contracts/data/.
 * Regenerate: {_REGENERATE}
 * Guard: {_GUARD} fails when this file differs.
 */

/** One packaged strategy definition, by the identity that a strategy run records. */
export interface RegisteredStrategyDefinition {{
  readonly version: string;
  readonly content_sha256: string;
  readonly provisional_semantics: boolean;
}}

export const STRATEGY_DEFINITION_REGISTRY: readonly RegisteredStrategyDefinition[] = [
"""

_SYNTHETIC_HEADER = f"""\
/**
 * GENERATED FILE. DO NOT EDIT. TEST HELPER: the App never imports this file.
 *
 * Source: truealpha_runtime.testing.synthetic_provisional_strategy_definition() (#1235).
 * The definition passed the full model validation, which requires provisional_semantics to
 * equal tier_valuation.provisional_thresholds. No packaged definition is provisional, so
 * the web tests inject this one to keep the provisional marker armed.
 * Regenerate: {_REGENERATE}
 * Guard: {_GUARD} fails when this file differs.
 */

import type {{ RegisteredStrategyDefinition }} from "../../src/contracts/strategyDefinitions.generated";

"""


def _entry(entry: StrategyDefinitionIdentity) -> str:
    return (
        f"{{ version: {json.dumps(entry.version)}, content_sha256: {json.dumps(entry.content_sha256)}, "
        f"provisional_semantics: {json.dumps(entry.provisional_semantics)} }}"
    )


def render() -> str:
    """The TypeScript text of the registry, one entry per line."""
    entries = "".join(f"  {_entry(entry)},\n" for entry in strategy_definition_registry())
    return f"{_HEADER}{entries}];\n"


def render_synthetic() -> str:
    """The TypeScript text of the synthetic provisional definition's identity."""
    definition = synthetic_provisional_strategy_definition()
    identity = StrategyDefinitionIdentity(
        definition.definition_version, definition.content_sha256, definition.provisional_semantics
    )
    return (
        f"{_SYNTHETIC_HEADER}export const SYNTHETIC_PROVISIONAL_DEFINITION: RegisteredStrategyDefinition = "
        f"{_entry(identity)};\n"
    )


def rendered_files() -> dict[Path, str]:
    """Each generated file and the text it must hold."""
    return {TARGET: render(), SYNTHETIC_TARGET: render_synthetic()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="write the generated files")
    mode.add_argument("--check", action="store_true", help="exit 1 when a checked-in file differs")
    args = parser.parse_args()
    stale = []
    for path, text in rendered_files().items():
        if args.write:
            path.write_text(text, encoding="utf-8")
        elif not path.exists() or path.read_text(encoding="utf-8") != text:
            stale.append(str(path.relative_to(REPO_ROOT)))
    for name in stale:
        print(f"{name} differs from its Python source; run with --write", file=sys.stderr)
    return 1 if stale else 0


if __name__ == "__main__":
    raise SystemExit(main())
