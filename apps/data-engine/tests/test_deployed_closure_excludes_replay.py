"""The deployed composition root does not import a replay harness (#795).

`data_engine/datahub/__init__.py` re-exported from `tiny_replay`, `medium_replay` and
`hardening_replay` — 1,615 lines. Importing any submodule of the package executes that
`__init__`, so every production tick imported all three and called none of them. The one
real dependency was `frozen_topt_list_version`, thirty lines that validate the hand-curated
TOPT corpus and mint its list version; it lived in `medium_replay` only because that is
where it was first needed, and it now lives in `production_topt.universe_corpus`.

The reachability ratchet cannot catch this: those modules WERE imported, so it correctly
called them reachable. It is honest about imports and silent about use, and a package
`__init__` is exactly where that gap gets manufactured. Hence a separate check.

Static, stdlib-only, over the real tree — no import of the scanned package, so the check
cannot be satisfied by an import side effect it caused itself.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
DEPLOYED_ROOT = "data_engine.dagster_defs"
REPLAY_MODULES = (
    "data_engine.datahub.tiny_replay",
    "data_engine.datahub.medium_replay",
    "data_engine.datahub.hardening_replay",
)


def _modules() -> dict[str, Path]:
    modules: dict[str, Path] = {}
    for path in SRC.rglob("*.py"):
        parts = list(path.relative_to(SRC).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        modules[".".join(parts)] = path
    return modules


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("data_engine"):
            found.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("data_engine"):
                    found.add(alias.name)
    return found


def _closure(root: str, modules: dict[str, Path]) -> set[str]:
    """Every module the root reaches, following package `__init__` execution.

    Importing `a.b.c` executes `a/__init__` and `a/b/__init__`, whose own imports run too —
    which is the entire mechanism this test exists to police, so the walk must model it.
    """

    def with_ancestors(name: str) -> list[str]:
        parts = name.split(".")
        return [".".join(parts[: index + 1]) for index in range(len(parts))]

    seen: set[str] = set()
    stack = [candidate for candidate in with_ancestors(root) if candidate in modules]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        for edge in _imports(modules[current]):
            for candidate in with_ancestors(edge):
                if candidate in modules and candidate not in seen:
                    stack.append(candidate)
    return seen


@pytest.fixture(scope="module")
def modules() -> dict[str, Path]:
    return _modules()


def test_the_replay_modules_still_exist(modules) -> None:
    """The point is that the tick does not IMPORT them, not that they were deleted — that
    is a separate decision (#795 step 3). Without this, the check below would pass for the
    wrong reason the day someone removes them."""
    for name in REPLAY_MODULES:
        assert name in modules, f"{name} is gone; this test's premise needs revisiting"


def test_the_deployed_root_imports_no_replay_harness(modules) -> None:
    reached = _closure(DEPLOYED_ROOT, modules)
    offenders = sorted(name for name in REPLAY_MODULES if name in reached)
    assert not offenders, (
        f"the deployed composition root reaches {offenders}. A replay harness is not part of "
        "a production tick; import the module by name from whatever genuinely replays, and "
        "keep it out of data_engine/datahub/__init__.py (#795)."
    )


def test_the_corpus_function_moved_rather_than_being_copied(modules) -> None:
    """`frozen_topt_list_version` must have ONE definition. Copying it to break the import
    would leave two frozen identities for the same 21 listings, drifting silently until a
    list_version_id assertion caught it in production."""
    definitions = [
        name
        for name, path in modules.items()
        if any(
            isinstance(node, ast.FunctionDef) and node.name == "frozen_topt_list_version"
            for node in ast.walk(ast.parse(path.read_text()))
        )
    ]
    assert definitions == ["data_engine.datahub.production_topt.universe_corpus"], (
        f"expected exactly one definition in universe_corpus, found {definitions}"
    )
