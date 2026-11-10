# ruff: noqa: I001, E401, E402, E701, E702, E731, F401, F811
# fmt: off
from __future__ import annotations; import ast; import re; import sys; from pathlib import Path; REPO = Path(__file__).resolve().parents[4]; FACTORS = REPO / "common" / "factors" / "src" / "factors"; MIGRATIONS = REPO / "db" / "migrations"; QUESTIONS = REPO / "common" / "contracts" / "src" / "truealpha_contracts" / "question_requirements.py"
def observed_factors() -> dict[str, dict[str, object]]:
    found: dict[str, dict[str, object]] = {}
    for path in sorted(FACTORS.rglob("*.py")):
        if "batches" in path.parts: continue
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.FunctionDef): continue
            for decorator in node.decorator_list:
                if not (isinstance(decorator, ast.Call) and getattr(decorator.func, "id", None) == "factor"): continue
                name = ast.literal_eval(decorator.args[0]); spec: dict[str, object] = { "function": node.name, "module_path": str(path.relative_to(REPO)), }
                for kw in decorator.keywords:
                    if kw.arg in {"kind", "module"}: spec[kw.arg] = ast.literal_eval(kw.value)
                    elif kw.arg == "inputs": spec["inputs"] = list(ast.literal_eval(kw.value))
                found[name] = spec
    return found
def observed_question_columns() -> list[dict[str, object]]:
    fields = ("table", "column", "factor", "module"); found: list[dict[str, object]] = []
    for node in ast.walk(ast.parse(QUESTIONS.read_text())):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "FactorColumn"): continue
        spec: dict[str, object] = dict(zip(fields, (ast.literal_eval(arg) for arg in node.args[: len(fields)])))
        for kw in node.keywords:
            if kw.arg in fields: spec[kw.arg] = ast.literal_eval(kw.value)
        found.append(spec)
    return found
def observed_columns(table: str) -> list[str]:
    schema, _, bare = table.partition("."); columns: list[str] = []; create = re.compile(rf"create table (?:if not exists )?{re.escape(table)}\s*\((.*?)\n\);", re.S | re.I); add = re.compile(rf"alter table {re.escape(table)}\s+add column (?:if not exists )?(\w+)", re.I); drop = re.compile(rf"alter table {re.escape(table)}\s+drop column (?:if exists )?(\w+)", re.I)
    for path in sorted(MIGRATIONS.glob("*.sql")):
        sql = path.read_text(); body = create.search(sql)
        if body:
            for line in body.group(1).splitlines():
                stripped = line.strip()
                if ( not stripped or stripped.startswith("--") or stripped.startswith(("constraint", "unique", "primary", "check", "foreign")) ): continue
                word = stripped.split()[0]
                if word.isidentifier() and word not in columns: columns.append(word)
        for name in add.findall(sql):
            if name not in columns: columns.append(name)
        for name in drop.findall(sql):
            if name in columns: columns.remove(name)
    if not columns: print(f"  no DDL found for {schema}.{bare}")
    return columns
def _check_module_and_question_invariants( actual_factors: dict[str, dict[str, object]], failures: list[str], ) -> None:
    for name, spec in sorted(actual_factors.items()):
        module, kind = spec.get("module"), spec.get("kind")
        if not isinstance(module, int) or not 1 <= module <= 7: failures.append(f"factor {name!r}: module {module!r} is outside init.md Section 7's 1-7")
        elif module == 7 and kind not in ("composite", "feeder"): failures.append( f"factor {name!r}: module 7 is the composite (three-tier valuation); a base factor cannot claim it" )
        elif kind == "feeder" and module != 7: failures.append( f"factor {name!r}: a feeder registers the module it feeds, and only module 7 has a composite" )
    question_columns = observed_question_columns(); named = {str(column["factor"]) for column in question_columns}; non_question_kinds = { name for name, spec in actual_factors.items() if spec.get("kind") in ("feeder", "composite") } | {"registered_semantic_probe"}
    for name in sorted(set(actual_factors) - named - non_question_kinds): failures.append( f"factor {name!r} lands nowhere the coverage report counts: add a FactorColumn to QUESTION_REQUIREMENTS" )
    for column in question_columns:
        table, col, factor, module = (column.get(field) for field in ("table", "column", "factor", "module")); where = f"{table}.{col}"
        if factor not in actual_factors: failures.append(f"QUESTION_REQUIREMENTS names factor {factor!r} for {where}, which is not registered")
        elif actual_factors[factor].get("module") != module: failures.append( f"QUESTION_REQUIREMENTS files {where} under module {module!r}; factor {factor!r} registers module {actual_factors[factor].get('module')!r}" )
        if col not in observed_columns(str(table)): failures.append(f"QUESTION_REQUIREMENTS names {where} for {factor!r}, and no migration creates that column")
def _check_pointer_and_period_invariants(failures: list[str]) -> None:
    for path in ( list((REPO / "apps").rglob("*.py")) + list((REPO / "common").rglob("*.py")) + list((REPO / "apps").rglob("*.ts")) + list((REPO / "tools").glob("*.py")) ):
        if "test" in path.name or "node_modules" in str(path): continue
        text = path.read_text()
        for block in re.finditer(r"from mart\.current_pointer_head(?P<body>.{0,600})", text, re.S):
            body = re.sub(r"--[^\n]*", "", block.group("body"))
            if "universe_id" not in body: failures.append( f'{path.relative_to(REPO)} resolves mart.current_pointer_head without a universe predicate — the universe is part of the governed key, and dropping it serves whichever pipeline advanced last' ); break
        for block in re.finditer(r"from mart\.topt_capture_status(?P<body>.{0,600})", text, re.S):
            body = re.sub(r"--[^\n]*", "", block.group("body"))
            if "datahub_quality_report" in body and "universe_id" not in body: failures.append( f'{path.relative_to(REPO)} resolves mart.topt_capture_status without a universe predicate — the universe is part of the governed key, and dropping it serves whichever pipeline advanced last' ); break
    for path in sorted((REPO / "common").rglob("*.py")) + sorted((REPO / "apps").rglob("*.py")):
        if "test" in path.name or path.name == "fiscal_period.py": continue
        text = path.read_text()
        if re.search(r"FY\{[^}]*\}:FY:|:FY:\(\?P?<?\\d\{4\}|:FY:\(\\d\{4\}", text): failures.append( f'{path.relative_to(REPO)} builds or matches the fiscal-period tag itself. Use truealpha_contracts.fiscal_period — a format known only to a producer and a regex fails as an empty series, not an error' )
def main() -> int:
    failures: list[str] = []; actual_factors = observed_factors(); _check_module_and_question_invariants(actual_factors, failures); evaluator = FACTORS / "composite" / "strategy_evaluator.py"
    if evaluator.exists() and re.search(r"^_[A-Z_]*KEYS\s*=\s*\(\s*[\"']", evaluator.read_text(), re.M):
        failures.append( "strategy_evaluator.py hard-codes a factor's input keys in a tuple — declare them on the factor (`@factor(..., inputs=...)`) and let the evaluator derive them (#855 B2)" )
    present = False; add_check_re = re.compile( r"input_key\s+text\s+not\s+null\s*\n?\s*check\s*\(|add constraint strategy_backtest_inputs_input_key_check", re.I, ); drop_check_re = re.compile(r"drop constraint (?:if exists )?strategy_backtest_inputs_input_key_check", re.I)
    for path in sorted(MIGRATIONS.glob("*.sql")):
        sql = path.read_text()
        if "strategy_backtest_inputs" not in sql: continue
        if drop_check_re.search(sql): present = False
        if add_check_re.search(sql): present = True
    if present: failures.append( 'staging.strategy_backtest_inputs.input_key still carries an enumerated CHECK. init.md rule 22: adding a metric is a registry edit, not a migration' )
    bridge = REPO / "apps" / "data-engine" / "src" / "data_engine" / "datahub" / "input_cells.py"; bridge_text = bridge.read_text() if bridge.exists() else ""
    if not bridge_text: failures.append(f"{bridge.relative_to(REPO)} is missing: the guard of the metric-key tuples checks nothing")
    if re.search(r"^_STRATEGY_PERIODIC_KEYS\s*=\s*[({\[]\s*[\"']", bridge_text, re.M): failures.append( 'input_cells._STRATEGY_PERIODIC_KEYS hard-codes which metrics are period-shaped inside generic transport — init.md rule 22 forbids branching on record type; declare it on the registry'       )
    if re.search(r"^_STRATEGY_FINANCIAL_KEYS\s*=\s*\(\s*\n\s*[\"']", bridge_text, re.M): failures.append( 'input_cells._STRATEGY_FINANCIAL_KEYS hard-codes a metric name list inside generic transport — init.md rule 22 forbids it; derive it from truealpha_contracts.metrics.METRICS instead'       )
    _check_pointer_and_period_invariants(failures)
    if failures:
        print("factor contract FAILED:\n")
        for failure in failures: print(f"  {failure}")
        return 1
    print(f"factor contract OK: {len(actual_factors)} registered factors satisfy I2-I6 invariants"); return 0
if __name__ == "__main__": sys.exit(main())
