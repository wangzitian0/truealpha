#!/usr/bin/env python3
"""Unreachable-code ratchet for the data engine and the factor library
(#429 / truealpha#539 P6; extended to ``common/factors`` by #791).

Computes the import closure from the DEPLOYED roots — the Dagster composition
root plus every module an operator script imports — and compares the unreachable
line count against the committed baseline. The count may only go DOWN: 21k+ lines
of parallel implementations accreted precisely because nothing objected when a new
one landed (three headcount implementations, a four-way governed-read copy, a
batches graveyard).

Both source trees are walked, because the closure crosses the package boundary:
``data_engine`` imports ``factors``, and a factor is reachable exactly when the
deployed composition can get to it. Guarding only the data engine left the half
that holds the factors unmeasured, and answering "can production reach this
factor?" by hand takes eight steps — grep the importers, tell a comment
reference from an import, follow the batch chain, then check two workflows.

The baseline is recorded PER TREE. A single total lets one tree's deletion pay
for another tree's accretion, which is the ratchet failing silently at the one
thing it exists to catch.

Note the roots include operator scripts, so an unreferenced script keeps whatever
it imports on the reachable side. That is deliberate (a script IS a deployment
path) and it is also how one unused file anchored 2,415 lines of the retired
batch machine — the census below names modules, so the anchor is visible.

Fails when a count grows. When one shrinks, prints the new numbers so the
baseline can be tightened in the same PR (`--write-baseline`).

Run: python3 tools/reachability_ratchet.py [--check|--write-baseline]
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

from truealpha_tools.import_graph import ancestors, edges, module_index, scan

ROOT = Path(__file__).resolve().parents[4]
#: tree name -> source root. The tree name is also the top-level package it holds,
#: which is what lets a module be attributed back to its tree for the per-tree count.
TREES: dict[str, Path] = {
    "data_engine": ROOT / "apps" / "data-engine" / "src",
    "factors": ROOT / "common" / "factors" / "src",
}
PACKAGE_PREFIXES = tuple(TREES)
SCRIPTS = ROOT / "apps" / "data-engine" / "scripts"
#: Scanned for EDGES but never counted in the census: a lower layer whose registries name
#: modules in the trees above. `truealpha_contracts.standards.STANDARDS` declares each
#: standard's adapter as "module:function" and `backfill._adapter` resolves it at run time
#: (#800), so that string is a wiring exactly like an operator script's import — and a
#: registry the deployed loop reads is a deployment path whatever layer it lives in.
#: Contracts' own modules are not censused here; this file guards the trees that consume it.
REGISTRY_SOURCES = (ROOT / "common" / "contracts" / "src" / "truealpha_contracts",)
BASELINE = ROOT / "tools" / "reachability_baseline.json"
DEPLOYED_ROOT = "data_engine.dagster_defs"


def _tree_of(module: str) -> str:
    return module.split(".", 1)[0]


def _modules() -> dict[str, Path]:
    return module_index(TREES.values())


def _imports(tree: ast.AST, modules: dict[str, Path]) -> set[str]:
    """The edge rule is shared with the diff scope (`import_graph.py`, #1412): one definition
    of "module A depends on module B". It covers `from package import submodule` (#429's
    census under-reported without it), registry strings "data_engine.<module>:<function>"
    (#72) and bare lane names in `data_engine.lanes.LANE_MODULES` (#731)."""
    return edges(scan(tree), PACKAGE_PREFIXES, modules)


def unreachable() -> tuple[dict[str, int], list[str]]:
    modules = _modules()
    edges = {name: _imports(ast.parse(path.read_text()), modules) for name, path in modules.items()}

    roots = {DEPLOYED_ROOT}
    for script in SCRIPTS.glob("*.py"):
        # Operator scripts are deployment-adjacent: what they import is alive.
        roots |= _imports(ast.parse(script.read_text()), modules)
    for source in REGISTRY_SOURCES:
        for path in source.rglob("*.py"):
            roots |= _imports(ast.parse(path.read_text()), modules)

    # Importing a.b.c executes a/__init__ and a.b/__init__, whose own imports are alive —
    # a walker that skips ancestors counts modules reachable only through a package
    # __init__ as dead, and this tool guides deletions (Copilot Medium on #601).
    seen: set[str] = set()
    stack = [candidate for root in roots for candidate in ancestors(root) if candidate in modules]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        for edge in edges.get(current, ()):
            for candidate in ancestors(edge):
                if candidate in modules and candidate not in seen:
                    stack.append(candidate)

    dead = sorted(name for name in modules if name not in seen)
    lines = {tree: 0 for tree in TREES}
    for name in dead:
        lines[_tree_of(name)] += len(modules[name].read_text().splitlines())
    return lines, dead


def _read_baseline() -> dict[str, int]:
    """Per-tree baseline. The pre-#791 file held a single `unreachable_lines` total for
    the data engine alone; it is read as that tree's number so the ratchet keeps working
    across the change instead of failing on a key it has not seen."""
    stored = json.loads(BASELINE.read_text())
    if "unreachable_lines" in stored:
        return {"data_engine": int(stored["unreachable_lines"]), "factors": 0}
    return {tree: int(stored.get(tree, 0)) for tree in TREES}


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "--check"
    lines, dead = unreachable()
    if mode == "--write-baseline":
        # `_comment` carries WHY a tree's number is not zero. Rewriting the file from the
        # counts alone would delete that explanation on the next tightening, and the number
        # would go back to being a mystery someone re-derives by deleting a file to see what
        # breaks.
        stored = json.loads(BASELINE.read_text())
        written = {k: v for k, v in stored.items() if k.startswith("_")}
        written.update(dict(sorted(lines.items())))
        BASELINE.write_text(json.dumps(written, indent=2, ensure_ascii=False) + "\n")
        counts = ", ".join(f"{tree} {count}" for tree, count in sorted(lines.items()))
        print(f"baseline written: {counts} unreachable lines across {len(dead)} modules")
        return 0
    baseline = _read_baseline()
    # Judged per tree on purpose: a single total lets a deletion in one tree pay for an
    # accretion in the other, which is the ratchet silently failing at its one job.
    grown = {tree: (lines[tree], baseline[tree]) for tree in TREES if lines[tree] > baseline[tree]}
    if grown:
        print("reachability ratchet FAILED:")
        for tree, (now, was) in sorted(grown.items()):
            print(f"  {tree}: {now} unreachable lines (baseline {was}, +{now - was})")
        print("A new module landed without a deployed consumer. Wire it into the")
        print("composition root or an operator script, or remove it — the census is:")
        for name in dead:
            print(f"  {_tree_of(name):12} {name}")
        return 1
    if any(lines[tree] < baseline[tree] for tree in TREES):
        for tree in sorted(TREES):
            if lines[tree] < baseline[tree]:
                print(f"reachability improved: {tree} {lines[tree]} unreachable lines (baseline {baseline[tree]}).")
        print("Tighten the baseline in this PR: python3 tools/reachability_ratchet.py --write-baseline")
    else:
        counts = ", ".join(f"{tree} {lines[tree]}" for tree in sorted(TREES))
        print(f"reachability ratchet OK: {counts} unreachable lines (== baseline)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
