# ruff: noqa: I001, E401, E402, E701, E702, E731, F401, F811
# fmt: off
from __future__ import annotations; import ast; import json; import sys; from pathlib import Path; from truealpha_tools.import_graph import ancestors, edges, module_index, scan; ROOT = Path(__file__).resolve().parents[4]; TREES: dict[str, Path] = { "data_engine": ROOT / "apps" / "data-engine" / "src", "factors": ROOT / "common" / "factors" / "src", }; PACKAGE_PREFIXES = tuple(TREES); SCRIPTS = ROOT / "apps" / "data-engine" / "scripts"; REGISTRY_SOURCES = (ROOT / "common" / "contracts" / "src" / "truealpha_contracts",); BASELINE = ROOT / "tools" / "reachability_baseline.json"; DEPLOYED_ROOT = "data_engine.dagster_defs"
def _tree_of(module: str) -> str: return module.split(".", 1)[0]
def _modules() -> dict[str, Path]: return module_index(TREES.values())
def _imports(tree: ast.AST, modules: dict[str, Path]) -> set[str]: return edges(scan(tree), PACKAGE_PREFIXES, modules)
def unreachable() -> tuple[dict[str, int], list[str]]:
    modules = _modules(); edges = {name: _imports(ast.parse(path.read_text()), modules) for name, path in modules.items()}; roots = {DEPLOYED_ROOT}
    for script in SCRIPTS.glob("*.py"): roots |= _imports(ast.parse(script.read_text()), modules)
    for source in REGISTRY_SOURCES:
        for path in source.rglob("*.py"): roots |= _imports(ast.parse(path.read_text()), modules)
    seen: set[str] = set(); stack = [candidate for root in roots for candidate in ancestors(root) if candidate in modules]
    while stack:
        current = stack.pop()
        if current in seen: continue
        seen.add(current)
        for edge in edges.get(current, ()):
            for candidate in ancestors(edge):
                if candidate in modules and candidate not in seen: stack.append(candidate)
    dead = sorted(name for name in modules if name not in seen); lines = {tree: 0 for tree in TREES}
    for name in dead: lines[_tree_of(name)] += len(modules[name].read_text().splitlines())
    return lines, dead
def _read_baseline() -> dict[str, int]:
    stored = json.loads(BASELINE.read_text())
    if "unreachable_lines" in stored: return {"data_engine": int(stored["unreachable_lines"]), "factors": 0}
    return {tree: int(stored.get(tree, 0)) for tree in TREES}
def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "--check"; lines, dead = unreachable()
    if mode == "--write-baseline": stored = json.loads(BASELINE.read_text()); written = {k: v for k, v in stored.items() if k.startswith("_")}; written.update(dict(sorted(lines.items()))); BASELINE.write_text(json.dumps(written, indent=2, ensure_ascii=False) + "\n"); counts = ", ".join(f"{tree} {count}" for tree, count in sorted(lines.items())); print(f"baseline written: {counts} unreachable lines across {len(dead)} modules"); return 0
    baseline = _read_baseline(); grown = {tree: (lines[tree], baseline[tree]) for tree in TREES if lines[tree] > baseline[tree]}
    if grown:
        print("reachability ratchet FAILED:")
        for tree, (now, was) in sorted(grown.items()): print(f"  {tree}: {now} unreachable lines (baseline {was}, +{now - was})")
        print("A new module landed without a deployed consumer. Wire it into the"); print("composition root or an operator script, or remove it — the census is:")
        for name in dead: print(f"  {_tree_of(name):12} {name}")
        return 1
    if any(lines[tree] < baseline[tree] for tree in TREES):
        for tree in sorted(TREES):
            if lines[tree] < baseline[tree]: print(f"reachability improved: {tree} {lines[tree]} unreachable lines (baseline {baseline[tree]}).")
        print("Tighten the baseline in this PR: python3 tools/reachability_ratchet.py --write-baseline")
    else: counts = ", ".join(f"{tree} {lines[tree]}" for tree in sorted(TREES)); print(f"reachability ratchet OK: {counts} unreachable lines (== baseline)")
    return 0
if __name__ == "__main__": raise SystemExit(main())
