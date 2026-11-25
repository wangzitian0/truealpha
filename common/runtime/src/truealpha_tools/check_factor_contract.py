#!/usr/bin/env python3
"""Check registered factor modules and question requirement columns against migrations (#284, #855, #1610)."""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
FACTORS = REPO / "common" / "factors" / "src" / "factors"
MIGRATIONS = REPO / "db" / "migrations"
REGISTRIES_JSON = REPO / "common" / "contracts" / "src" / "truealpha_contracts" / "data" / "registries.v1.json"


def observed_factors() -> dict[str, dict[str, object]]:
    found: dict[str, dict[str, object]] = {}
    for path in sorted(FACTORS.rglob("*.py")):
        if "batches" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.FunctionDef):
                continue
            for decorator in node.decorator_list:
                if not (isinstance(decorator, ast.Call) and getattr(decorator.func, "id", None) == "factor"):
                    continue
                name = ast.literal_eval(decorator.args[0])
                spec: dict[str, object] = {"function": node.name, "module_path": str(path.relative_to(REPO))}
                for kw in decorator.keywords:
                    if kw.arg in {"kind", "module"}:
                        spec[kw.arg] = ast.literal_eval(kw.value)
                    elif kw.arg == "inputs":
                        spec["inputs"] = list(ast.literal_eval(kw.value))
                found[name] = spec
    return found


def observed_question_columns() -> list[dict[str, object]]:
    reqs = json.loads(REGISTRIES_JSON.read_text())["question_requirements"]
    return [dict(column) for req in reqs for column in req["columns"]]


def observed_columns(table: str) -> list[str]:
    schema, _, bare = table.partition(".")
    columns: list[str] = []
    create = re.compile(rf"create table (?:if not exists )?{re.escape(table)}\s*\((.*?)\n\);", re.S | re.I)
    add = re.compile(rf"alter table {re.escape(table)}\s+add column (?:if not exists )?(\w+)", re.I)
    drop = re.compile(rf"alter table {re.escape(table)}\s+drop column (?:if exists )?(\w+)", re.I)
    for path in sorted(MIGRATIONS.glob("*.sql")):
        sql = path.read_text()
        if body := create.search(sql):
            for line in body.group(1).splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith(("--", "constraint", "unique", "primary", "check", "foreign")):
                    continue
                word = stripped.split()[0]
                if word.isidentifier() and word not in columns:
                    columns.append(word)
        for name in add.findall(sql):
            if name not in columns:
                columns.append(name)
        for name in drop.findall(sql):
            if name in columns:
                columns.remove(name)
    if not columns:
        print(f"  no DDL found for {schema}.{bare}")
    return columns


def _check_module_and_question_invariants(actual_factors: dict[str, dict[str, object]], failures: list[str]) -> None:
    for name, spec in sorted(actual_factors.items()):
        module, kind = spec.get("module"), spec.get("kind")
        if not isinstance(module, int) or not 1 <= module <= 7:
            failures.append(f"factor {name!r}: module {module!r} is outside init.md Section 7's 1-7")
        elif module == 7 and kind not in ("composite", "feeder"):
            failures.append(
                f"factor {name!r}: module 7 is the composite (three-tier valuation); a base factor cannot claim it"
            )
        elif kind == "feeder" and module != 7:
            failures.append(
                f"factor {name!r}: a feeder registers the module it feeds, and only module 7 has a composite"
            )

    question_columns = observed_question_columns()
    named = {str(column["factor"]) for column in question_columns}
    non_question_kinds = {name for name, spec in actual_factors.items() if spec.get("kind") in ("feeder", "composite")}
    for name in sorted(set(actual_factors) - named - non_question_kinds):
        failures.append(
            f"factor {name!r} lands nowhere the coverage report counts: add a FactorColumn to QUESTION_REQUIREMENTS"
        )
    for column in question_columns:
        table, col, factor, module = (column.get(field) for field in ("table", "column", "factor", "module"))
        where = f"{table}.{col}"
        if factor not in actual_factors:
            failures.append(f"QUESTION_REQUIREMENTS names factor {factor!r} for {where}, which is not registered")
        elif actual_factors[factor].get("module") != module:
            failures.append(
                f"QUESTION_REQUIREMENTS files {where} under module {module!r}; "
                f"factor {factor!r} registers module {actual_factors[factor].get('module')!r}"
            )
        if col not in observed_columns(str(table)):
            failures.append(f"QUESTION_REQUIREMENTS names {where} for {factor!r}, and no migration creates that column")


def main() -> int:
    failures: list[str] = []
    actual_factors = observed_factors()
    _check_module_and_question_invariants(actual_factors, failures)
    if failures:
        print("factor contract FAILED:\n")
        for failure in failures:
            print(f"  {failure}")
        return 1
    print(f"factor contract OK: {len(actual_factors)} registered factors satisfy module/column schema")
    return 0


if __name__ == "__main__":
    sys.exit(main())
