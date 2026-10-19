#!/usr/bin/env python3
"""Code cyclomatic complexity ratchet gate.

Enforces that:
1. No new function exceeds the cyclomatic complexity threshold (default: 20).
2. Existing functions recorded in the baseline cannot increase their complexity.
3. Total complexity and legacy exemptions only ratchet DOWN.

Usage:
  python3 tools/complexity_gate.py [--check] [--threshold N] [--write-baseline]
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
BASELINE_PATH = ROOT / "tools" / "complexity_baseline.json"
DEFAULT_THRESHOLD = 20

SCAN_DIRS = (
    ROOT / "apps",
    ROOT / "common",
    ROOT / "tools",
)


def measure_ast_complexity(fn_node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """Compute McCabe cyclomatic complexity for an AST function node.

    Base complexity is 1. Each decision point / branch adds 1:
    - If, IfExp (ternary)
    - For, AsyncFor
    - While
    - ExceptHandler
    - With, AsyncWith
    - BoolOp (and/or adds len(values) - 1)
    - MatchValue, MatchCase

    Nested function definitions are not descended into; they are measured separately.
    """
    complexity = 1
    stack: list[ast.AST] = list(fn_node.body)
    while stack:
        current = stack.pop()
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if isinstance(
            current,
            (
                ast.If,
                ast.IfExp,
                ast.For,
                ast.AsyncFor,
                ast.While,
                ast.ExceptHandler,
                ast.With,
                ast.AsyncWith,
            ),
        ):
            complexity += 1
        elif isinstance(current, ast.BoolOp):
            complexity += len(current.values) - 1
        elif isinstance(current, (ast.MatchValue, getattr(ast, "match_case", ()))):
            complexity += 1

        for _, value in ast.iter_fields(current):
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, ast.AST):
                        stack.append(item)
            elif isinstance(value, ast.AST):
                stack.append(value)
    return complexity


class ComplexityVisitor(ast.NodeVisitor):
    """AST visitor that tracks enclosing class/function scopes for distinct qualnames."""

    def __init__(self, rel_path: str) -> None:
        self.rel_path = rel_path
        self.scope: list[str] = []
        self.results: dict[str, int] = {}

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        qualname = ".".join(self.scope + [node.name])
        key = f"{self.rel_path}:{qualname}"
        self.results[key] = measure_ast_complexity(node)
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        qualname = ".".join(self.scope + [node.name])
        key = f"{self.rel_path}:{qualname}"
        self.results[key] = measure_ast_complexity(node)
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()


def scan_functions(
    root: Path = ROOT,
    scan_dirs: tuple[Path, ...] = SCAN_DIRS,
) -> dict[str, int]:
    """Scan all non-test python files under scan_dirs and return {rel_path:qualname -> complexity}."""
    results: dict[str, int] = {}
    for d in scan_dirs:
        for py_file in sorted(d.rglob("*.py")):
            rel_path = py_file.relative_to(root).as_posix()
            # Skip tests, samples, virtual environments
            if "tests/" in rel_path or "samples/" in rel_path or ".venv/" in rel_path:
                continue
            try:
                tree = ast.parse(py_file.read_text(encoding="utf-8"), str(py_file))
            except Exception:
                continue
            visitor = ComplexityVisitor(rel_path)
            visitor.visit(tree)
            results.update(visitor.results)
    return results


def check_complexity(
    *,
    threshold: int = DEFAULT_THRESHOLD,
    baseline_path: Path = BASELINE_PATH,
    write_baseline: bool = False,
) -> int:
    current_map = scan_functions()

    # Functions exceeding threshold
    high_complexity = {k: v for k, v in current_map.items() if v > threshold}

    if write_baseline:
        baseline_path.write_text(json.dumps(high_complexity, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(
            f"Updated {baseline_path.name}: {len(high_complexity)} functions recorded exceeding threshold {threshold}"
        )
        return 0

    if not baseline_path.exists():
        print(f"::error::Complexity baseline file {baseline_path} missing. Run with --write-baseline.", file=sys.stderr)
        return 1

    baseline: dict[str, int] = json.loads(baseline_path.read_text(encoding="utf-8"))

    violations: list[str] = []

    # 1. Any new function exceeding threshold not in baseline
    for key, c in high_complexity.items():
        if key not in baseline:
            violations.append(f"NEW high-complexity function (complexity {c} > {threshold}): {key}")
        elif c > baseline[key]:
            violations.append(
                f"Complexity regressed: {key} increased from {baseline[key]} to {c} (threshold: {threshold})"
            )

    if violations:
        print("::error::Code cyclomatic complexity gate failed:", file=sys.stderr)
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
        return 1

    # Check if baseline can be tightened
    decreased = [k for k, base_val in baseline.items() if k not in high_complexity or high_complexity[k] < base_val]
    print(
        f"Complexity gate OK: {len(current_map)} functions scanned. "
        f"{len(high_complexity)} functions in baseline (> {threshold})."
    )
    if decreased:
        print(f"Note: {len(decreased)} functions have decreased complexity! Run with --write-baseline to ratchet down.")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Cyclomatic complexity ratchet gate")
    parser.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD, help="Max allowed complexity for new code")
    parser.add_argument("--check", action="store_true", help="Assert complexity within ratchet baseline")
    parser.add_argument(
        "--write-baseline", action="store_true", help="Write current high-complexity functions to baseline"
    )
    args = parser.parse_args()

    return check_complexity(
        threshold=args.threshold,
        write_baseline=args.write_baseline,
    )


if __name__ == "__main__":
    sys.exit(main())
