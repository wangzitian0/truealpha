"""`frozen_topt_list_version` has exactly one definition (#795, #1061).

The function validates the hand-curated TOPT corpus and mints its list version. Two copies
would mint two frozen identities for the same 21 listings. The copies would drift until a
`list_version_id` assertion failed in production.

This file once also guarded the deployed composition root against the replay harnesses
(`tiny_replay`, `medium_replay`, `hardening_replay`). #1061 deleted those modules. Their
guard could no longer fail, so it was deleted with them.

Static and stdlib-only, over the real tree. The check imports no scanned module.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"


def _modules() -> dict[str, Path]:
    modules: dict[str, Path] = {}
    for path in SRC.rglob("*.py"):
        parts = list(path.relative_to(SRC).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        modules[".".join(parts)] = path
    return modules


def test_the_corpus_function_has_one_definition() -> None:
    definitions = [
        name
        for name, path in _modules().items()
        if any(
            isinstance(node, ast.FunctionDef) and node.name == "frozen_topt_list_version"
            for node in ast.walk(ast.parse(path.read_text()))
        )
    ]
    assert definitions == ["data_engine.datahub.production_topt.universe_corpus"], (
        f"expected exactly one definition in universe_corpus, found {definitions}"
    )
