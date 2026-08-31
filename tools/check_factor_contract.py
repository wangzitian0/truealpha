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

    # --- I4: the module number is an identity; init.md Section 7 is the authority ---
    for name, spec in sorted(actual_factors.items()):
        module, kind = spec.get("module"), spec.get("kind")
        if not isinstance(module, int) or not 1 <= module <= 7:
            failures.append(f"factor {name!r}: module {module!r} is outside init.md Section 7's 1-7")
        elif module == 7 and kind != "composite":
            failures.append(
                f"factor {name!r}: module 7 is the composite (three-tier valuation); a base factor cannot claim it"
            )

    # --- I3: adding a metric is a registry edit, never a migration and never a branch ---
    inputs_ddl = "\n".join(
        path.read_text() for path in sorted(MIGRATIONS.glob("*.sql")) if "strategy_backtest_inputs" in path.read_text()
    )
    for match in re.finditer(r"input_key[^;]*?check\s*\(", inputs_ddl, re.I | re.S):
        del match
        failures.append(
            "staging.strategy_backtest_inputs.input_key still carries an enumerated CHECK. "
            "init.md rule 22: adding a metric is a registry edit, not a migration"
        )
        break
    bridge = REPO / "apps" / "data-engine" / "src" / "data_engine" / "datahub" / "strategy_bridge.py"
    # A LITERAL list of metric names is the violation; deriving the same mapping from the
    # registry is the fix, so match the literal rather than the variable's name.
    if bridge.exists() and re.search(r"^_STRATEGY_PERIODIC_KEYS\s*=\s*[({\[]\s*[\"']", bridge.read_text(), re.M):
        failures.append(
            "strategy_bridge._STRATEGY_PERIODIC_KEYS hard-codes which metrics are period-shaped inside "
            "generic transport — init.md rule 22 forbids branching on record type; declare it on the registry"
        )

    # --- I1: fusion is exercised at snapshot freeze, by declared priority ---
    # init.md rule 12: "The winner is chosen by declared rules, NEVER by ingestion
    # recency", and Section 6 orders it source-priority first, restatement recency second.
    # The selection window today orders by `knowable_at desc, observation_id desc` alone --
    # correct WITHIN a source, and silently "whoever published later" the moment a second
    # source registers for the same obligation. Harmless while every metric resolves from
    # SEC alone; a wrong number on the day fusion is first exercised.
    selection = (
        REPO / "apps" / "data-engine" / "src" / "data_engine" / "datahub" / "production_topt" / "materialization.py"
    )
    if selection.exists():
        window = re.search(
            r"row_number\(\) over\s*\(\s*partition by obligation\.obligation_id\s*order by(?P<order>.*?)\)",
            selection.read_text(),
            re.S,
        )
        if window and not re.search(r"priorit|source_rank|array_position", window.group("order"), re.I):
            failures.append(
                "snapshot selection orders by recency alone with no source-priority rank "
                "(materialization.py `row_number() over (partition by obligation_id ...)`). "
                "init.md rule 12 ranks the source FIRST and recency second"
            )

    # --- The governed pointer is keyed by universe; a consumer may not drop the key ---
    # `mart.current_pointer` has always been unique on
    # (environment, universe_id, universe_version, factor_id, sequence), and the head view
    # partitions the same way. Five consumers -- Python, TypeScript and this very tool --
    # nonetheless resolved it with `where environment/factor_id ... order by advanced_at
    # desc limit 1`, which serves whichever PIPELINE advanced last. Once the canary
    # universe began running the real pipeline after each deploy, its 24-cell run displaced
    # the 84-cell core everywhere, and a module card read "available" at 4% coverage.
    for path in (
        list((REPO / "apps").rglob("*.py"))
        + list((REPO / "libs").rglob("*.py"))
        + list((REPO / "apps").rglob("*.ts"))
        + list((REPO / "tools").glob("*.py"))
    ):
        if "test" in path.name or "node_modules" in str(path):
            continue
        text = path.read_text()
        for block in re.finditer(r"from mart\.current_pointer_head(?P<body>.{0,600})", text, re.S):
            body = block.group("body")
            clause = body.split(";")[0]
            if "universe_id" not in clause:
                failures.append(
                    f"{path.relative_to(REPO)} resolves mart.current_pointer_head without a "
                    "universe predicate — the universe is part of the governed key, and dropping "
                    "it serves whichever pipeline advanced last"
                )
                break

    # --- I2: one encoder, one parser. Nobody re-implements the period-tag format ---
    for path in sorted((REPO / "libs").rglob("*.py")) + sorted((REPO / "apps").rglob("*.py")):
        if "test" in path.name or path.name == "fiscal_period.py":
            continue
        text = path.read_text()
        # The four-part tag specifically. `f"FY{fy}"` alone is an older, different
        # single-segment tag on dead modules and is not this format.
        if re.search(r"FY\{[^}]*\}:FY:|:FY:\(\?P?<?\\d\{4\}|:FY:\(\\d\{4\}", text):
            failures.append(
                f"{path.relative_to(REPO)} builds or matches the fiscal-period tag itself. "
                "Use truealpha_contracts.fiscal_period — a format known only to a producer and a "
                "regex fails as an empty series, not an error"
            )

    waived = {w["failure_contains"]: w for w in manifest.get("exemptions", [])}
    today = manifest["exemptions_evaluated_on"]
    deferred: list[str] = []
    for index, failure in enumerate(list(failures)):
        for needle, waiver in waived.items():
            if needle not in failure:
                continue
            if waiver["expires"] <= today:
                # An expired deferral does not just stop deferring — it must SAY it
                # expired, or the failure reads as a new regression and the next person
                # re-litigates a decision that was already made and dated.
                failures[index] = f"{failure}\n      [deferral for {waiver['issue']} EXPIRED {waiver['expires']}]"
            else:
                failures.remove(failure)
                deferred.append(f"{failure}\n      deferred to {waiver['issue']} until {waiver['expires']}")
            break
    for note in deferred:
        print(f"  DEFERRED {note}")

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
