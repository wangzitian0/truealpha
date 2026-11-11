"""Deterministic duration-weighted test sharding for parallel CI lanes (#673)."""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple

PATTERNS = ("test_*.py", "*_test.py")


def collect(root: Path | Sequence[Path]) -> list[Path]:
    roots = [root] if isinstance(root, Path) else list(root)
    return sorted({p for r in roots for pattern in PATTERNS for p in r.rglob(pattern)})


def test_functions(path: Path) -> int:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return 1
    count = sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name.startswith("Test")
        for child in ast.iter_child_nodes(node)
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) and child.name.startswith("test")
    )
    count += sum(
        1
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith("test")
    )
    return count


def load_weights() -> dict[str, float]:
    path = Path(__file__).resolve().parents[4] / "tools" / "pytest_shard_weights.json"
    if not path.exists():
        return {}
    measured = json.loads(path.read_text(encoding="utf-8")).get("seconds_by_file", {})
    if not isinstance(measured, dict):
        raise ValueError(f"{path} has no seconds_by_file mapping")
    return {str(key): float(value) for key, value in measured.items()}


def weight_of(path: Path, weights: dict[str, float], repo_root: Path, rate: float) -> float:
    key = str(path.relative_to(repo_root)) if path.is_absolute() else str(path)
    if key in weights:
        return weights[key]
    return float(test_functions(path)) * rate


WEIGHT_COVERAGE_FLOOR = 0.5


class WeightCoverage(NamedTuple):
    label: str
    known: int
    total: int

    @property
    def share(self) -> float:
        return self.known / self.total if self.total else 0.0

    def line(self) -> str:
        return (
            f"- {self.label}: {self.share:.1%} of test files have a harvested weight "
            f"({self.known}/{self.total}); floor {WEIGHT_COVERAGE_FLOOR:.0%}"
        )


def weight_coverage(label: str, files: Sequence[Path], weights: dict[str, float], repo_root: Path) -> WeightCoverage:
    known = sum(1 for path in files if str(path.relative_to(repo_root)) in weights)
    return WeightCoverage(label=label, known=known, total=len(files))


def require_coverage_floor(coverages: Sequence[WeightCoverage]) -> None:
    broken = [coverage for coverage in coverages if coverage.share < WEIGHT_COVERAGE_FLOOR]
    if broken:
        details = "; ".join(f"{c.label} {c.share:.1%} ({c.known}/{c.total})" for c in broken)
        raise ValueError(
            f"the harvest covers less than {WEIGHT_COVERAGE_FLOOR:.0%} of the test files: {details}. "
            f"Run tools/harvest_shard_weights.py <run-id> with a green run."
        )


def write_step_summary(coverages: Sequence[WeightCoverage], summary_path: str | None) -> None:
    if not summary_path:
        return
    with Path(summary_path).open("a", encoding="utf-8") as handle:
        handle.write("### Shard-weight coverage\n\n")
        handle.write("\n".join(coverage.line() for coverage in coverages) + "\n")


def shard(files: Sequence[Path], index: int, total: int) -> list[Path]:
    if total < 1:
        raise ValueError(f"total shards must be >= 1, got {total}")
    if not 0 <= index < total:
        raise ValueError(f"shard index {index} is out of range for {total} shards")
    repo_root = Path(__file__).resolve().parents[4]
    weights = load_weights()
    measured_tests = sum(test_functions(repo_root / key) for key in weights if (repo_root / key).exists())
    rate = (sum(weights.values()) / measured_tests) if measured_tests else 1.0
    weight = {path: weight_of(path, weights, repo_root, rate) for path in files}

    bins: list[list[Path]] = [[] for _ in range(total)]
    load = [0.0] * total
    for path in sorted(files, key=lambda p: (-weight[p], str(p))):
        lightest = load.index(min(load))
        bins[lightest].append(path)
        load[lightest] += weight[path]
    return sorted(bins[index])


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("roots", nargs="+", type=Path)
    parser.add_argument("--shard", type=int, required=True)
    parser.add_argument("--of", type=int, required=True, dest="total")
    arguments = parser.parse_args(list(sys.argv[1:] if argv is None else argv))

    for root in arguments.roots:
        if not root.is_dir():
            print(f"pytest_shard: {root} is not a directory", file=sys.stderr)
            return 2
    files = collect(arguments.roots)
    roots_str = ", ".join(str(r) for r in arguments.roots)
    if not files:
        print(f"pytest_shard: {roots_str} contains no test files", file=sys.stderr)
        return 2
    try:
        selected = shard(files, arguments.shard, arguments.total)
    except ValueError as error:
        print(f"pytest_shard: {error}", file=sys.stderr)
        return 2
    if not selected:
        print(
            f"pytest_shard: shard {arguments.shard} of {arguments.total} is empty "
            f"({len(files)} files under {roots_str}) — use fewer shards",
            file=sys.stderr,
        )
        return 2
    print("\n".join(str(path) for path in selected))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
