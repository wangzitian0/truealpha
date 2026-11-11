#!/usr/bin/env python3
"""Code cyclomatic complexity ratchet gate."""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
BASELINE_PATH = ROOT / "tools" / "complexity_baseline.json"
DEFAULT_THRESHOLD = 20
METRICS = ("function_cc", "file_lines")
SCAN_DIRS = (ROOT / "apps", ROOT / "common", ROOT / "tools")


def measure_ast_complexity(fn_node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
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
                ast.MatchValue,
                getattr(ast, "match_case", ()),
            ),
        ):
            complexity += 1
        elif isinstance(current, ast.BoolOp):
            complexity += len(current.values) - 1
        for _, value in ast.iter_fields(current):
            if isinstance(value, list):
                stack.extend(item for item in value if isinstance(item, ast.AST))
            elif isinstance(value, ast.AST):
                stack.append(value)
    return complexity


class ComplexityVisitor(ast.NodeVisitor):
    def __init__(self, rel_path: str) -> None:
        self.rel_path = rel_path
        self.scope: list[str] = []
        self.results: dict[str, int] = {}

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def _visit_fn(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        qualname = ".".join([*self.scope, node.name])
        self.results[f"{self.rel_path}:{qualname}"] = measure_ast_complexity(node)
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_FunctionDef = _visit_fn
    visit_AsyncFunctionDef = _visit_fn


def _iter_source_files(root: Path, scan_dirs: tuple[Path, ...]) -> list[tuple[str, Path]]:
    out: list[tuple[str, Path]] = []
    for d in scan_dirs:
        for py_file in sorted(d.rglob("*.py")):
            rel_path = py_file.relative_to(root).as_posix()
            if not any(skip in rel_path for skip in ("tests/", "samples/", ".venv/")):
                out.append((rel_path, py_file))
    return out


def scan_functions(root: Path = ROOT, scan_dirs: tuple[Path, ...] = SCAN_DIRS) -> dict[str, int]:
    results: dict[str, int] = {}
    for rel_path, py_file in _iter_source_files(root, scan_dirs):
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"), str(py_file))
        except Exception:
            continue
        visitor = ComplexityVisitor(rel_path)
        visitor.visit(tree)
        results.update(visitor.results)
    return results


def scan_file_lines(root: Path = ROOT, scan_dirs: tuple[Path, ...] = SCAN_DIRS) -> dict[str, int]:
    results: dict[str, int] = {}
    for rel_path, py_file in _iter_source_files(root, scan_dirs):
        try:
            results[rel_path] = len(py_file.read_text(encoding="utf-8").splitlines())
        except Exception:
            continue
    return results


def _matches_only(key: str, only_scopes: Sequence[str]) -> bool:
    return any(scope in key for scope in only_scopes)


def _parse_only(only: Sequence[str] | None) -> tuple[set[str], list[str]]:
    if not only:
        return set(METRICS), []
    metric_filters = [item for item in only if item in METRICS]
    only_scopes = [item for item in only if item not in METRICS]
    return (set(metric_filters) if metric_filters else set(METRICS)), only_scopes


def check_complexity(
    *,
    threshold: int = DEFAULT_THRESHOLD,
    baseline_path: Path = BASELINE_PATH,
    write_baseline: bool = False,
    only: Sequence[str] | None = None,
) -> int:
    selected_metrics, only_scopes = _parse_only(only)
    if "file_lines" in selected_metrics and "function_cc" not in selected_metrics:
        files_map = scan_file_lines()
        if only_scopes:
            files_map = {k: v for k, v in files_map.items() if _matches_only(k, only_scopes)}
        print(
            f"Complexity gate OK (file_lines): {len(files_map)} files scanned (max lines: {max(files_map.values(), default=0)})."
        )
        return 0

    current_map = scan_functions()
    if only_scopes and not write_baseline:
        current_map = {k: v for k, v in current_map.items() if _matches_only(k, only_scopes)}

    high_complexity = {k: v for k, v in current_map.items() if v > threshold}
    if write_baseline:
        if only_scopes and baseline_path.exists():
            merged: dict[str, int] = json.loads(baseline_path.read_text(encoding="utf-8"))
            merged = {k: v for k, v in merged.items() if not _matches_only(k, only_scopes)}
            merged.update({k: c for k, c in high_complexity.items() if _matches_only(k, only_scopes)})
            high_complexity = merged
        baseline_path.write_text(json.dumps(high_complexity, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(
            f"Updated {baseline_path.name}: {len(high_complexity)} functions recorded exceeding threshold {threshold}"
        )
        return 0

    if not baseline_path.exists():
        print(f"::error::Complexity baseline file {baseline_path} missing. Run with --write-baseline.", file=sys.stderr)
        return 1

    baseline: dict[str, int] = json.loads(baseline_path.read_text(encoding="utf-8"))
    if only_scopes:
        baseline = {k: v for k, v in baseline.items() if _matches_only(k, only_scopes)}

    violations = [
        f"NEW high-complexity function (complexity {c} > {threshold}): {key}"
        if key not in baseline
        else f"Complexity regressed: {key} increased from {baseline[key]} to {c} (threshold: {threshold})"
        for key, c in high_complexity.items()
        if key not in baseline or c > baseline[key]
    ]
    if violations:
        print("::error::Code cyclomatic complexity gate failed:", file=sys.stderr)
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
        return 1

    decreased = [k for k, base_val in baseline.items() if k not in high_complexity or high_complexity[k] < base_val]
    print(
        f"Complexity gate OK: {len(current_map)} functions scanned. {len(high_complexity)} functions in baseline (> {threshold})."
    )
    if decreased:
        print(f"Note: {len(decreased)} functions have decreased complexity! Run with --write-baseline to ratchet down.")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cyclomatic complexity ratchet gate")
    parser.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--write-baseline", action="store_true")
    parser.add_argument("--only", nargs="+", default=None)
    args = parser.parse_args(argv)
    return check_complexity(threshold=args.threshold, write_baseline=args.write_baseline, only=args.only)


if __name__ == "__main__":
    sys.exit(main())
