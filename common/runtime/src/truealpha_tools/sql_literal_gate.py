# ruff: noqa: I001, E401, E402, E701, E702, E731, F401, F811
# fmt: off
from __future__ import annotations; import argparse; import ast; import json; import re; import sys; from pathlib import Path; DATAHUB = Path("apps/data-engine/src/data_engine/datahub"); ALLOWLIST = Path("tools/sql_literal_allowlist.json"); _INSERT = re.compile(r"\binsert\s+into\b", re.IGNORECASE); _VALUES_CLAUSE = re.compile( r"\bvalues\b(?P<body>.*?)(?:\bon\s+conflict\b|\breturning\b|;|$)", re.IGNORECASE | re.DOTALL, ); _BARE_INT = re.compile(r"(?<![\w%(:.])-?\d+(?![\w.])")
def _bare_ints_in_values(sql: str) -> list[str]:
    found: list[str] = []
    for clause in _VALUES_CLAUSE.finditer(sql): body = clause.group("body"); body = re.sub(r"'(?:[^']|'')*'", "''", body); body = re.sub(r"%\(\w+\)s|%s", "", body); found.extend(_BARE_INT.findall(body))
    return found
def scan(root: Path) -> list[tuple[str, int, list[str]]]:
    violations: list[tuple[str, int, list[str]]] = []
    for path in sorted((root / DATAHUB).rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)): continue
            if not _INSERT.search(node.value): continue
            bare = _bare_ints_in_values(node.value)
            if bare: violations.append((str(path.relative_to(root)), node.lineno, bare))
    return violations
def main() -> int:
    parser = argparse.ArgumentParser(description=""); parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[4]); args = parser.parse_args(); allowlist: dict[str, str] = {}; allowlist_path = args.root / ALLOWLIST
    if allowlist_path.exists(): allowlist = json.loads(allowlist_path.read_text())
    violations = scan(args.root); active_keys = {f"{rel}:{lineno}" for rel, lineno, _ in violations}; failures = []
    for rel, lineno, bare in violations:
        key = f"{rel}:{lineno}"
        if key in allowlist: continue
        failures.append(f"{key}: bare integer(s) {', '.join(bare)} inside an INSERT VALUES clause")
    for line in failures:
        print(f"::error::{line} — bind it as a parameter derived from the data (#621/#629)")
    if failures: return 1
    for key in allowlist:
        if key not in active_keys: print(f"note: allowlist entry {key} no longer matches a violation; prune it")
    print("sql-literal-gate: clean"); return 0
if __name__ == "__main__": sys.exit(main())
