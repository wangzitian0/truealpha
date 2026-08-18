#!/usr/bin/env python3
"""Bare-integer gate for SQL strings on the datahub path (truealpha#629).

The fifth universe-size literal (#621) lived INSIDE a SQL string —
``values (%s, ..., 20, 21, 84, %s)`` — where model-layer tests are structurally
blind: the pydantic snapshot was correct and the INSERT lied. This gate scans
every string constant under ``apps/data-engine/src/data_engine/datahub`` that
contains an INSERT, and fails on bare integer literals inside its VALUES
clause. Values must arrive as bound parameters derived from the data they
describe, never as constants smuggled into the statement text.

Scope is deliberately narrow (INSERT ... VALUES only): SELECT/WHERE constants
(``limit 1``, ordinal ``split_part(x, ':', 2)``) are query shape, not smuggled
data. Legitimate exceptions go in ``tools/sql_literal_allowlist.json`` as
``"<path>:<lineno>"`` entries with a reason.

Run: python3 tools/sql_literal_gate.py [--root <repo-root>]
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

DATAHUB = Path("apps/data-engine/src/data_engine/datahub")
ALLOWLIST = Path("tools/sql_literal_allowlist.json")

_INSERT = re.compile(r"\binsert\s+into\b", re.IGNORECASE)
# The VALUES tuple(s): everything between `values` and the clause that follows
# (`on conflict`, `returning`) or the end of the statement.
_VALUES_CLAUSE = re.compile(
    r"\bvalues\b(?P<body>.*?)(?:\bon\s+conflict\b|\breturning\b|;|$)",
    re.IGNORECASE | re.DOTALL,
)
# The optional leading `-` keeps the reported literal identical to the SQL text
# (review: `-1` must not surface as `1` in CI output and allowlist reasoning).
_BARE_INT = re.compile(r"(?<![\w%(:.])-?\d+(?![\w.])")


def _bare_ints_in_values(sql: str) -> list[str]:
    found: list[str] = []
    for clause in _VALUES_CLAUSE.finditer(sql):
        body = clause.group("body")
        # Strip quoted SQL string literals and psycopg placeholders before scanning:
        # '...' contents are data-typed by the author on purpose; %s / %(name)s are
        # exactly what this gate exists to demand. The quote pattern consumes
        # doubled '' escapes so `'O''Reilly'` strips as one literal (review).
        body = re.sub(r"'(?:[^']|'')*'", "''", body)
        body = re.sub(r"%\(\w+\)s|%s", "", body)
        found.extend(_BARE_INT.findall(body))
    return found


def scan(root: Path) -> list[tuple[str, int, list[str]]]:
    violations: list[tuple[str, int, list[str]]] = []
    for path in sorted((root / DATAHUB).rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            if not _INSERT.search(node.value):
                continue
            bare = _bare_ints_in_values(node.value)
            if bare:
                violations.append((str(path.relative_to(root)), node.lineno, bare))
    return violations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()

    allowlist: dict[str, str] = {}
    allowlist_path = args.root / ALLOWLIST
    if allowlist_path.exists():
        allowlist = json.loads(allowlist_path.read_text())

    violations = scan(args.root)
    # Staleness judges the allowlist against the RAW scan, not the unallowlisted
    # remainder — an entry that is still suppressing a live violation is doing its
    # job, not rotting (review).
    active_keys = {f"{rel}:{lineno}" for rel, lineno, _ in violations}
    failures = []
    for rel, lineno, bare in violations:
        key = f"{rel}:{lineno}"
        if key in allowlist:
            continue
        failures.append(f"{key}: bare integer(s) {', '.join(bare)} inside an INSERT VALUES clause")

    for line in failures:
        print(f"::error::{line} — bind it as a parameter derived from the data (#621/#629)")
    if failures:
        return 1
    for key in allowlist:
        if key not in active_keys:
            # Informational only: an allowlist entry whose violation no longer
            # exists should be pruned, but must not fail the build on its own.
            print(f"note: allowlist entry {key} no longer matches a violation; prune it")
    print("sql-literal-gate: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
