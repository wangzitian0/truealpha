"""Generate the TypeScript twin of the strategy definition registry (#1235).

The web image builds from apps/app-web alone, so the App cannot read the packaged
definition files at run time. This script writes
``truealpha_contracts.strategy.strategy_definition_registry()`` as a TypeScript module.
``libs/contracts/tests/test_strategy_definition_registry.py`` renders the text again and
fails when the checked-in file differs.

Run from the repository root:

    uv run python libs/contracts/conformance/export_strategy_definitions.py --write
    uv run python libs/contracts/conformance/export_strategy_definitions.py --check
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from truealpha_contracts.strategy import strategy_definition_registry

REPO_ROOT = Path(__file__).resolve().parents[3]
TARGET = REPO_ROOT / "apps" / "app-web" / "src" / "contracts" / "strategyDefinitions.generated.ts"

_HEADER = """\
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
"""


def render() -> str:
    """The TypeScript text of the registry, one entry per line."""
    entries = "".join(
        f"  {{ version: {json.dumps(entry.version)}, content_sha256: {json.dumps(entry.content_sha256)}, "
        f"provisional_semantics: {json.dumps(entry.provisional_semantics)} }},\n"
        for entry in strategy_definition_registry()
    )
    return f"{_HEADER}{entries}];\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help=f"write {TARGET.relative_to(REPO_ROOT)}")
    mode.add_argument("--check", action="store_true", help="exit 1 when the checked-in file differs")
    args = parser.parse_args()
    text = render()
    if args.write:
        TARGET.write_text(text, encoding="utf-8")
        return 0
    if TARGET.read_text(encoding="utf-8") != text:
        print(f"{TARGET.relative_to(REPO_ROOT)} differs from the Python registry; run with --write", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
