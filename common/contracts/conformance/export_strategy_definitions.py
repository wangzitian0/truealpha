"""Generate TypeScript twins of the strategy definition registry (#1235)."""

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
_REGENERATE = "uv run python common/contracts/conformance/export_strategy_definitions.py --write"

_HEADER = f"""\
/** GENERATED FILE. DO NOT EDIT. Regenerate: {_REGENERATE} */

export interface RegisteredStrategyDefinition {{
  readonly version: string;
  readonly content_sha256: string;
  readonly provisional_semantics: boolean;
}}

export const STRATEGY_DEFINITION_REGISTRY: readonly RegisteredStrategyDefinition[] = [
"""

_SYNTHETIC_HEADER = f"""\
/** GENERATED FILE. DO NOT EDIT. TEST HELPER. Regenerate: {_REGENERATE} */

import type {{ RegisteredStrategyDefinition }} from "../../src/contracts/strategyDefinitions.generated";

"""


def _entry(e: StrategyDefinitionIdentity) -> str:
    return (
        f"{{ version: {json.dumps(e.version)}, content_sha256: {json.dumps(e.content_sha256)}, "
        f"provisional_semantics: {json.dumps(e.provisional_semantics)} }}"
    )


def render() -> str:
    entries = "".join(f"  {_entry(e)},\n" for e in strategy_definition_registry())
    return f"{_HEADER}{entries}];\n"


def render_synthetic() -> str:
    d = synthetic_provisional_strategy_definition()
    identity = StrategyDefinitionIdentity(d.definition_version, d.content_sha256, d.provisional_semantics)
    return f"{_SYNTHETIC_HEADER}export const SYNTHETIC_PROVISIONAL_DEFINITION: RegisteredStrategyDefinition = {_entry(identity)};\n"


def rendered_files() -> dict[Path, str]:
    return {TARGET: render(), SYNTHETIC_TARGET: render_synthetic()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
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
