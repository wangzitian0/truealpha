#!/usr/bin/env python3
"""#284: freeze the factor surface — public signatures and the datahub schema they read.

Two contracts drift silently and both were caught doing it this week:

  1. **Factor signatures.** Every base/composite factor is called by name from the
     evaluator, from Dagster, and from the tests that stand in for both. A parameter
     added, renamed, or made optional changes what a published number means, and nothing
     failed when module 1 grew a second entry point that production never called.
  2. **The datahub columns factors read.** `staging.strategy_backtest_inputs` gained a
     period axis in 0043 and lost a `CHECK`-enumerated key in the same change. A column
     removed or a constraint loosened upstream reaches the factor layer as a wrong number,
     not as an error.

This is a FREEZE, not a design: it asserts the surface matches what is checked in, so a
change to either must be a deliberate edit to `tools/factor_contract.json` that shows up
in review as its own diff. It is not a judgement about whether the surface is good — the
manifest records today's shape, including the parts #284's review calls wrong.

Stdlib only: runs as `python3 tools/check_factor_contract.py` in ci-python without any
dependency setup, and reads the SQL text rather than a live database so it needs none.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MANIFEST = REPO / "tools" / "factor_contract.json"
FACTORS = REPO / "libs" / "factors" / "src" / "factors"
MIGRATIONS = REPO / "db" / "migrations"


def _signature(fn: ast.FunctionDef) -> str:
    """The signature INCLUDING defaults.

    Defaults are the load-bearing half. #284's defect was a parameter whose default meant
    "skip the computation" — `earnings_cagr_years: int | None = None` — which the only
    deployed caller never overrode, so two parser vintages published nothing while every
    gate stayed green. A freeze that ignored defaults would have watched that land.
    """
    args = fn.args

    def render(arg: ast.arg, default: ast.expr | None) -> str:
        annotation = ast.unparse(arg.annotation) if arg.annotation else "?"
        return f"{arg.arg}: {annotation}" + (f" = {ast.unparse(default)}" if default is not None else "")

    pos_defaults: list[ast.expr | None] = [None] * (len(args.args) - len(args.defaults)) + list(args.defaults)
    positional = [render(a, d) for a, d in zip(args.args, pos_defaults, strict=True)]
    keyword = [render(a, d) for a, d in zip(args.kwonlyargs, args.kw_defaults, strict=True)]
    returns = ast.unparse(fn.returns) if fn.returns else "?"
    inner = ", ".join(positional)
    if keyword:
        inner = f"{inner}, *, {', '.join(keyword)}" if inner else f"*, {', '.join(keyword)}"
    return f"({inner}) -> {returns}"


def observed_factors() -> dict[str, dict[str, object]]:
    """Every `@factor`-decorated function, by its REGISTERED name.

    Keyed by the registered name rather than the Python name because the registered name
    is what the rest of the system resolves — a rename of one without the other is exactly
    the drift this freezes.
    """
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
                spec: dict[str, object] = {
                    "function": node.name,
                    "module_path": str(path.relative_to(REPO)),
                    "signature": _signature(node),
                }
                for kw in decorator.keywords:
                    if kw.arg in {"kind", "module"}:
                        spec[kw.arg] = ast.literal_eval(kw.value)
                found[name] = spec
    return found


def observed_columns(table: str) -> list[str]:
    """Columns a table has after every migration that touches it, in declaration order.

    Reads the migration text rather than a database so the check runs anywhere and
    describes what a fresh environment would get, which is the thing that must not drift.
    """
    schema, _, bare = table.partition(".")
    columns: list[str] = []
    create = re.compile(rf"create table (?:if not exists )?{re.escape(table)}\s*\((.*?)\n\);", re.S | re.I)
    add = re.compile(rf"alter table {re.escape(table)}\s+add column (?:if not exists )?(\w+)", re.I)
    drop = re.compile(rf"alter table {re.escape(table)}\s+drop column (?:if exists )?(\w+)", re.I)
    for path in sorted(MIGRATIONS.glob("*.sql")):
        sql = path.read_text()
        body = create.search(sql)
        if body:
            for line in body.group(1).splitlines():
                stripped = line.strip()
                if (
                    not stripped
                    or stripped.startswith("--")
                    or stripped.startswith(("constraint", "unique", "primary", "check", "foreign"))
                ):
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
        print(f"  no DDL found for {schema}.{bare} — the manifest names a table no migration creates")
    return columns


def main() -> int:
    manifest = json.loads(MANIFEST.read_text())
    failures: list[str] = []

    expected_factors = manifest["factors"]
    actual_factors = observed_factors()
    for name in sorted(set(expected_factors) | set(actual_factors)):
        if name not in actual_factors:
            failures.append(f"factor {name!r} is frozen in the manifest but no longer registered")
            continue
        if name not in expected_factors:
            failures.append(
                f"factor {name!r} is registered but not frozen. Add it to tools/factor_contract.json: "
                f"{json.dumps(actual_factors[name], sort_keys=True)}"
            )
            continue
        for field in ("function", "module_path", "signature", "kind", "module"):
            want, got = expected_factors[name].get(field), actual_factors[name].get(field)
            if want != got:
                failures.append(f"factor {name!r} {field}: frozen {want!r}, found {got!r}")

    for table, expected_columns in manifest["datahub_tables"].items():
        actual_columns = observed_columns(table)
        missing = [c for c in expected_columns if c not in actual_columns]
        added = [c for c in actual_columns if c not in expected_columns]
        if missing:
            failures.append(f"{table}: frozen column(s) {missing} no longer exist — factors read these")
        if added:
            failures.append(f"{table}: new column(s) {added} are not in the freeze; add them deliberately")

    if failures:
        print("factor contract FAILED — the frozen surface moved:\n")
        for failure in failures:
            print(f"  {failure}")
        print("\nThis check exists so a signature or a column changes in review rather than in production.")
        print("If the change is intended, edit tools/factor_contract.json in the same PR.")
        return 1
    print(
        f"factor contract OK: {len(actual_factors)} factor signatures and "
        f"{len(manifest['datahub_tables'])} datahub tables match the freeze"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
