"""Deterministic test-tree sharding for parallel CI lanes — A4 B1 (#673).

`apps/data-engine/tests` was a single 168 s pytest step and the pole of every
PR's CI wall (measured on run 33361977933: 207 s job, 168 s of it pytest, the
next-slowest job 163 s). The tree has no `conftest.py` and defines every
fixture inside the file that uses it, so it splits without fixture coupling.

What a split does NOT tolerate is silently dropping files. A shard scheme that
stops covering a renamed directory reports success over nothing — the
green-while-empty shape (#527), and the most expensive kind of green there is.
So the partition lives here, in one place, and its covering property is
asserted by `libs/runtime/tests/test_pytest_shard.py` rather than trusted:
every collectable file lands in exactly one shard, for any shard count.

Assignment is by index over the sorted file list, so a new test file joins a
shard automatically and a new subdirectory needs no workflow edit — an
explicit per-directory list is exactly the kind that rots (#472).

Usage:
    python tools/pytest_shard.py apps/data-engine/tests --shard 0 --of 3
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections.abc import Sequence
from pathlib import Path

# pytest's default `python_files`. Both patterns are collected so that a future
# `foo_test.py` is sharded rather than silently skipped; pyproject does not
# override `python_files`, and if it ever does, the standing test comparing
# this collection against files containing test functions fails first.
PATTERNS = ("test_*.py", "*_test.py")


def collect(root: Path) -> list[Path]:
    """Every file under ``root`` pytest would collect, sorted for determinism."""
    found: set[Path] = set()
    for pattern in PATTERNS:
        found.update(root.rglob(pattern))
    return sorted(found)


def test_functions(path: Path) -> int:
    """How many test functions a file defines, parsed rather than grepped.

    A substring count of ``def test_`` misses what pytest collects (methods on
    ``Test*`` classes) and hits what it does not (the string inside a docstring
    or an f-string) — review on #700. Parametrised cases still count as one,
    which is the known imprecision of this weight; it is a balancing hint, not
    a schedule.

    A file that does not parse weighs 1 and is still assigned: pytest reports
    the syntax error far better than this tool could, and refusing to shard
    would turn one broken file into a CI job that runs nothing.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return 1
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and not node.name.startswith("Test"):
            continue
        for child in ast.iter_child_nodes(node) if isinstance(node, ast.ClassDef) else []:
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) and child.name.startswith("test"):
                count += 1
    count += sum(
        1
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith("test")
    )
    return count


def shard(files: Sequence[Path], index: int, total: int) -> list[Path]:
    """The files belonging to shard ``index`` (0-based) of ``total``.

    Greedy bin-packing weighted by test count, not round-robin by position.
    Measured on run 33365311320, where round-robin gave three equal FILE counts
    and wildly unequal work: 24/24/24 files carrying 127/177/226 tests, which
    took 57 s / 107 s / 137 s. The wall is the slowest lane, so a balanced
    split is the entire point of splitting. Packing by test count predicts
    177/177/176 — within 1%.

    Deterministic: files are placed heaviest-first with ties broken by path,
    into the least-loaded bin with ties broken by index. The same tree always
    produces the same assignment, so a lane's content does not churn between
    runs of the same commit.

    Raises rather than returning an empty list on a bad index: a silently empty
    shard makes pytest exit 5 ("no tests collected") in a job whose name says it
    ran the suite.
    """
    if total < 1:
        raise ValueError(f"total shards must be >= 1, got {total}")
    if not 0 <= index < total:
        raise ValueError(f"shard index {index} is out of range for {total} shards")

    bins: list[list[Path]] = [[] for _ in range(total)]
    load = [0] * total
    for path in sorted(files, key=lambda p: (-test_functions(p), str(p))):
        lightest = load.index(min(load))
        bins[lightest].append(path)
        load[lightest] += test_functions(path)
    return sorted(bins[index])


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="test tree to shard")
    parser.add_argument("--shard", type=int, required=True, help="0-based shard index")
    parser.add_argument("--of", type=int, required=True, dest="total", help="total shard count")
    arguments = parser.parse_args(list(sys.argv[1:] if argv is None else argv))

    if not arguments.root.is_dir():
        print(f"pytest_shard: {arguments.root} is not a directory", file=sys.stderr)
        return 2
    files = collect(arguments.root)
    if not files:
        print(f"pytest_shard: {arguments.root} contains no test files", file=sys.stderr)
        return 2
    try:
        selected = shard(files, arguments.shard, arguments.total)
    except ValueError as error:
        print(f"pytest_shard: {error}", file=sys.stderr)
        return 2
    if not selected:
        # More shards than files: the caller asked for a lane with nothing in
        # it, which would run pytest over an empty argument list — i.e. the
        # whole suite, or nothing, depending on the invocation.
        print(
            f"pytest_shard: shard {arguments.shard} of {arguments.total} is empty "
            f"({len(files)} files under {arguments.root}) — use fewer shards",
            file=sys.stderr,
        )
        return 2
    print("\n".join(str(path) for path in selected))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
