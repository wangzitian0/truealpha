#!/usr/bin/env python3
"""Unreachable-code ratchet for the data engine (#429 / truealpha#539 P6).

Computes the import closure of ``data_engine`` from its DEPLOYED roots — the
Dagster composition root plus every module an operator script imports — and
compares the unreachable line count against the committed baseline. The count
may only go DOWN: 21k+ lines of parallel implementations accreted precisely
because nothing objected when a new one landed (three headcount
implementations, a four-way governed-read copy, a batches graveyard).

Fails when the count grows. When it shrinks, prints the new number so the
baseline can be tightened in the same PR (`--write-baseline`).

Run: python3 tools/reachability_ratchet.py [--check|--write-baseline]
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "apps" / "data-engine" / "src"
SCRIPTS = ROOT / "apps" / "data-engine" / "scripts"
BASELINE = Path(__file__).with_name("reachability_baseline.json")
DEPLOYED_ROOT = "data_engine.dagster_defs"


def _modules() -> dict[str, Path]:
    modules: dict[str, Path] = {}
    for path in SRC.rglob("*.py"):
        parts = list(path.relative_to(SRC).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        modules[".".join(parts)] = path
    return modules


def _imports(tree: ast.AST, modules: dict[str, Path]) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("data_engine"):
            found.add(node.module)
            for alias in node.names:
                # `from package import submodule` — resolving module+name against
                # the module set is what a naive walker misses (#429's census
                # under-reported without it).
                candidate = f"{node.module}.{alias.name}"
                if candidate in modules:
                    found.add(candidate)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("data_engine"):
                    found.add(alias.name)
    return found


def unreachable() -> tuple[int, list[str]]:
    modules = _modules()
    edges = {name: _imports(ast.parse(path.read_text()), modules) for name, path in modules.items()}

    roots = {DEPLOYED_ROOT}
    for script in SCRIPTS.glob("*.py"):
        # Operator scripts are deployment-adjacent: what they import is alive.
        roots |= _imports(ast.parse(script.read_text()), modules)

    def with_ancestors(name: str) -> list[str]:
        # Importing a.b.c executes a/__init__ and a.b/__init__, whose own imports
        # are alive — a walker that skips ancestors counts modules reachable only
        # through a package __init__ as dead, and this tool guides deletions
        # (Copilot Medium on #601).
        parts = name.split(".")
        return [".".join(parts[: i + 1]) for i in range(len(parts))]

    seen: set[str] = set()
    stack = [candidate for root in roots for candidate in with_ancestors(root) if candidate in modules]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        for edge in edges.get(current, ()):
            for candidate in with_ancestors(edge):
                if candidate in modules and candidate not in seen:
                    stack.append(candidate)

    dead = sorted(name for name in modules if name not in seen)
    lines = sum(len(modules[name].read_text().splitlines()) for name in dead)
    return lines, dead


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "--check"
    lines, dead = unreachable()
    if mode == "--write-baseline":
        BASELINE.write_text(json.dumps({"unreachable_lines": lines}, indent=2) + "\n")
        print(f"baseline written: {lines} unreachable lines across {len(dead)} modules")
        return 0
    baseline = json.loads(BASELINE.read_text())["unreachable_lines"]
    if lines > baseline:
        grew = lines - baseline
        print(f"reachability ratchet FAILED: {lines} unreachable lines (baseline {baseline}, +{grew}).")
        print("A new module landed without a deployed consumer. Wire it into the")
        print("composition root or an operator script, or remove it — the census is:")
        for name in dead:
            print(f"  {name}")
        return 1
    if lines < baseline:
        print(f"reachability improved: {lines} unreachable lines (baseline {baseline}).")
        print("Tighten the baseline in this PR: python3 tools/reachability_ratchet.py --write-baseline")
    else:
        print(f"reachability ratchet OK: {lines} unreachable lines (== baseline)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
