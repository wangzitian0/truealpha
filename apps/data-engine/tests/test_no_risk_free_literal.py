"""#1255: a risk-free rate is a measurement. No strategy code holds a number for it.

The labor-efficiency factor subtracts `total_assets * risk_free_rate`. The rate comes from the stored
Treasury series (`datahub.risk_free`). A literal, a default value or a table in code would stand in for
that measurement and pass every test built on its own value. This test scans the code that runs the
strategy and the data files of the contracts. It fails on a numeric risk-free literal.

What counts as a risk-free binding: a name that contains `risk_free`, `riskfree`, `tbill`, `t_bill`,
`treasury_rate`, `treasury_bill_rate` or `rf_rate`. The name can be an assignment target (also a dict item
`d["risk_free_rate"] = ...`, an augmented assignment or a walrus), a parameter, a keyword argument, a dict
key or the name of a function whose return value is a literal. Five shapes are violations:

1. A nonzero numeric literal anywhere on the value side: `Decimal("0.05")`, `0.05`, `Decimal(5) / 100`,
   `float("0.05")`, or a number in a string such as `os.getenv("X", "0.05")`.
2. A numeric literal of any value, zero too, as the whole value of a default, an assignment or a
   `Field(default=...)`. A zero default is the likeliest silent fallback.
3. A name that the same module binds to a numeric literal, used as the value: `rate = Decimal("0.05")`
   and then `risk_free_rate=rate`.
4. A numeric literal, zero too, as a positional or keyword argument of a call to a consumer of the
   rate (`GppeV0Definition`, `evaluate_cutoff`, `run_strategy_replay_for_cutoff`), or a name that the
   module binds to a literal, passed as such an argument.
5. In a data file: a risk-free key with a number below it, outside the golden decision set.

The one allowed zero is nested: `NamedRateParameter(..., value=Decimal(0))` is the declared Sharpe
parameter (`backtest.SHARPE_RISK_FREE`). A rate of zero there is "no adjustment", and the parameter
names its series and its basis.

The second rule: the table of explicit rates in the golden decision set is fixture data. Only the
golden replay may read it. The live tick, the core engine and the backtest may not.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
#: The code that runs the strategy: the data engine, the factors and the contracts. Tests are not scanned.
SCANNED_ROOTS = (
    REPO / "apps" / "data-engine" / "src",
    REPO / "common" / "factors" / "src",
    REPO / "common" / "contracts" / "src",
)
#: The data files of the contracts. The golden decision set holds explicit rates for the known-answer replay.
DATA_ROOT = REPO / "common" / "contracts" / "src" / "truealpha_contracts" / "data"
ALLOWED_DATA_PATHS = {("large_model_value_v0_definition_1_0_0.json", ("golden_decision_set", "risk_free_rates"))}
_NAME = re.compile(r"risk_?free|t_?bill|treasury_(bill_)?rate|(^|_)rf_?rate", re.IGNORECASE)
#: Calls that take the rate. A literal argument, zero too, would hand them a number the code made up.
CONSUMERS = {"GppeV0Definition", "evaluate_cutoff", "run_strategy_replay_for_cutoff"}

#: The only code that may read `golden_decision_set.risk_free_rates`: the model of the golden set and the two
#: replays that run the golden corpus. All three are fixture paths. None runs in the live tick.
GOLDEN_RATE_READERS = {
    "apps/data-engine/src/data_engine/core_strategy_replay.py",
    "apps/data-engine/src/data_engine/strategy_backtest_gateway.py",
    "common/contracts/src/truealpha_contracts/strategy.py",
}


def _is_number_text(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None


def _literal_value(node: ast.AST) -> float | None:
    """The number if `node` is a numeric literal, a numeric string, or a conversion of one; else None."""
    if isinstance(node, ast.Constant) and not isinstance(node.value, bool):
        if isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node.value, str):
            return _is_number_text(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        inner = _literal_value(node.operand)
        return None if inner is None else (-inner if isinstance(node.op, ast.USub) else inner)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"Decimal", "float", "int"}
        and len(node.args) == 1
        and not node.keywords
    ):
        return _literal_value(node.args[0])
    if isinstance(node, ast.BinOp):
        left, right = _literal_value(node.left), _literal_value(node.right)
        return None if left is None or right is None else 1.0
    if isinstance(node, ast.Lambda):
        return _literal_value(node.body)
    return None


def _whole_value_is_literal(node: ast.AST) -> bool:
    """Rule 2: the value IS a numeric literal (zero too). Also the default of a `Field`."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Field":
        values = [*node.args[:1], *(k.value for k in node.keywords if k.arg in {"default", "default_factory"})]
        return any(_whole_value_is_literal(value) for value in values)
    return _literal_value(node) is not None


def _holds_nonzero_literal(node: ast.AST) -> bool:
    """Rule 1: a nonzero numeric literal anywhere below `node`.

    The bounds of a pydantic `Field(ge=0, le=1, min_length=2)` are not values, so only the default of a
    `Field` call is read."""
    pending = [node]
    while pending:
        child = pending.pop()
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Name) and child.func.id == "Field":
            pending.extend(child.args[:1])
            pending.extend(k.value for k in child.keywords if k.arg in {"default", "default_factory"})
            continue
        value = _literal_value(child)
        if value is not None and value != 0:
            return True
        pending.extend(ast.iter_child_nodes(child))
    return False


def _bindings(tree: ast.AST):
    """(name, value node, line) for each place that binds a value to a name."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                for name in _target_names(target):
                    yield name, node.value, node.lineno
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            for name in _target_names(node.target):
                yield name, node.value, node.lineno
        elif isinstance(node, ast.AugAssign):
            for name in _target_names(node.target):
                yield name, node.value, node.lineno
        elif isinstance(node, ast.NamedExpr):
            for name in _target_names(node.target):
                yield name, node.value, node.lineno
        elif isinstance(node, ast.keyword) and node.arg is not None:
            yield node.arg, node.value, node.value.lineno
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            args = node.args
            positional = [*args.posonlyargs, *args.args]
            for argument, default in zip(
                positional[len(positional) - len(args.defaults) :], args.defaults, strict=True
            ):
                yield argument.arg, default, default.lineno
            for argument, default in zip(args.kwonlyargs, args.kw_defaults, strict=True):
                if default is not None:
                    yield argument.arg, default, default.lineno
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    yield key.value, value, value.lineno


def _target_names(target: ast.AST) -> list[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, ast.Attribute):
        return [target.attr]
    if (
        isinstance(target, ast.Subscript)
        and isinstance(target.slice, ast.Constant)
        and isinstance(target.slice.value, str)
    ):
        return [target.slice.value]
    if isinstance(target, (ast.Tuple, ast.List)):
        return [name for element in target.elts for name in _target_names(element)]
    return []


def risk_free_literals(source: str) -> list[tuple[int, str]]:
    """(line, name) of every violation of rules 1 to 4 in `source`."""
    tree = ast.parse(source)
    bindings = list(_bindings(tree))
    literal_names = {name for name, value, _ in bindings if _whole_value_is_literal(value) and _literal_value(value)}
    violations: list[tuple[int, str]] = []
    for name, value, line in bindings:
        if not _NAME.search(name):
            continue
        if (
            _whole_value_is_literal(value)
            or _holds_nonzero_literal(value)
            or (isinstance(value, ast.Name) and value.id in literal_names)
        ):
            violations.append((line, name))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            callee = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
            if callee in CONSUMERS:
                for argument in [*node.args, *(k.value for k in node.keywords)]:
                    if (
                        _whole_value_is_literal(argument)
                        or _holds_nonzero_literal(argument)
                        or (isinstance(argument, ast.Name) and argument.id in literal_names)
                    ):
                        violations.append((argument.lineno, callee))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _NAME.search(node.name):
            for child in ast.walk(node):
                if isinstance(child, ast.Return) and child.value is not None:
                    if _whole_value_is_literal(child.value) or _holds_nonzero_literal(child.value):
                        violations.append((child.lineno, node.name))
    return sorted(set(violations))


def risk_free_data_literals(document: object, path: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    """Rule 5: the paths of a JSON document where a risk-free key holds a number below it."""
    found: list[tuple[str, ...]] = []
    if isinstance(document, dict):
        for key, value in document.items():
            here = (*path, key)
            if _NAME.search(key) and _json_holds_number(value):
                found.append(here)
            else:
                found.extend(risk_free_data_literals(value, here))
    elif isinstance(document, list):
        for index, item in enumerate(document):
            found.extend(risk_free_data_literals(item, (*path, str(index))))
    return found


def _json_holds_number(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        return _is_number_text(value) is not None
    if isinstance(value, dict):
        return any(_json_holds_number(item) for item in value.values())
    if isinstance(value, list):
        return any(_json_holds_number(item) for item in value)
    return False


def _scanned_files() -> list[Path]:
    return sorted(path for root in SCANNED_ROOTS for path in root.rglob("*.py"))


# -- the detector is seen failing on the shapes the repository once had -------------------------------


@pytest.mark.parametrize(
    "source",
    [
        'RISK_FREE_RATE = Decimal("0.05")',
        "RISK_FREE_RATE = 0.05",
        "risk_free_rate: Decimal = Decimal('0.0365')",
        "def f(risk_free_rate=0.05): ...",
        "def f(*, risk_free_rate=Decimal('0.0426')): ...",
        "lambda risk_free_rate=5: risk_free_rate",
        "GppeV0Definition(risk_free_rate=Decimal('0.05'))",
        "evaluate_cutoff(inputs, risk_free_rate=0.0380)",
        "rates = {'risk_free_rate': 0.05}",
        "risk_free_rate: Decimal = Field(default=Decimal('0.05'), ge=0)",
        "TBILL_YIELD = Decimal(4) / 100",
        "self.risk_free = Decimal('3.65') / Decimal('100')",
        "risk_free_by_cutoff = {'2026-03-31': Decimal('0.0380'), '2026-06-30': Decimal('0.0365')}",
        "GppeV0Definition(Decimal('0.05'))",
        "GppeV0Definition(Decimal(0))",
        "evaluate_cutoff(inputs, definition, cutoff, 0.05)",
        "rate = Decimal('0.05')\nGppeV0Definition(risk_free_rate=rate)",
        "risk_free_rate = Decimal(os.getenv('RISK_FREE', '0.05'))",
        "risk_free_rate = '0.05'",
        "risk_free_rate = float('0.05')",
        "risk_free_rate: Decimal = Field(default_factory=lambda: Decimal('0.05'))",
        "settings['risk_free_rate'] = 0.05",
        "def f(*, risk_free_rate=Decimal(0)): ...",
        "def f(risk_free_rate: Decimal = 0): ...",
        "risk_free_rate: Decimal = Decimal('0')",
        "D = Decimal('0.05')\nevaluate_cutoff(inputs, definition, cutoff, D)",
        "def risk_free_rate():\n    return Decimal('0.05')",
        "class A:\n    @property\n    def treasury_bill_rate(self):\n        return 0.05",
        "rf_rate = 0.05",
        "t_bill_rate = Decimal('0.05')",
        "risk_free_rate += 0.01",
        "(risk_free_rate := Decimal('0.05'))",
    ],
    ids=[
        "module-constant",
        "module-float",
        "annotated-default",
        "parameter-default",
        "keyword-only-default",
        "lambda-default",
        "keyword-argument",
        "keyword-float",
        "field-default",
        "dict-key",
        "tbill-name",
        "attribute",
        "table-in-code",
        "positional-consumer-argument",
        "zero-consumer-argument",
        "positional-evaluator-argument",
        "neutral-name-flow",
        "environment-default",
        "string-only",
        "float-conversion",
        "field-default-factory",
        "subscript-assignment",
        "zero-keyword-only-default",
        "zero-parameter-default",
        "zero-assignment",
        "neutral-name-positional",
        "function-return",
        "property-return",
        "rf-rate-name",
        "t-bill-name",
        "augmented-assignment",
        "walrus",
    ],
)
def test_the_scan_finds_a_numeric_risk_free_literal(source: str) -> None:
    assert risk_free_literals(source), source


@pytest.mark.parametrize(
    "source",
    [
        "rate = risk_free_rate_at(connection, cutoff)",
        "GppeV0Definition(risk_free_rate=rate.value)",
        "def f(risk_free_rate: Decimal): ...",
        "SHARPE_RISK_FREE = NamedRateParameter(name='sharpe_risk_free_rate', series_id=None, value=Decimal(0))",
        "risk_free_rate: Decimal = Field(ge=0, le=1)",
        "risk_free_rates: tuple = Field(min_length=2)",
        "risk_free_benchmark: Literal['3m-us-tbill'] = '3m-us-tbill'",
        "rate = Decimal('0.05')",
        "MAX_RATE_LAG_DAYS = 2 * macro_registration_for(INSTRUMENT).max_age_days",
    ],
    ids=[
        "resolved",
        "resolved-keyword",
        "parameter-without-default",
        "declared-zero",
        "field-bounds",
        "field-length",
        "label-text",
        "not-a-risk-free-name",
        "lag-days",
    ],
)
def test_the_scan_leaves_a_resolved_rate_a_declared_zero_and_other_names_alone(source: str) -> None:
    assert risk_free_literals(source) == [], source


def test_the_scan_names_the_line_and_the_binding() -> None:
    assert risk_free_literals("x = 1\n\nRISK_FREE_RATE = Decimal('0.05')\n") == [(3, "RISK_FREE_RATE")]


# -- data files (rule 5) -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "document",
    [
        {"risk_free_rate": 0.05},
        {"strategy": {"risk_free": {"annualized_rate": "0.0365"}}},
        {"tbill": [{"cutoff": "2026-03-31", "rate": 0.038}]},
        {"parameters": {"risk_free_rates": ["0.0380", "0.0365"]}},
    ],
    ids=["number", "nested-string-number", "list-of-objects", "list-of-strings"],
)
def test_the_data_scan_finds_a_number_below_a_risk_free_key(document: object) -> None:
    assert risk_free_data_literals(document)


@pytest.mark.parametrize(
    "document",
    [
        {"risk_free": {"rule_id": "per_cutoff_versioned_parameter", "instrument": "us_treasury_bill_3_month"}},
        {"risk_free_rate": None},
        {"labor_efficiency": {"formula": "(a-b*risk_free_rate)/c"}},
    ],
    ids=["names-only", "null", "formula-text"],
)
def test_the_data_scan_leaves_names_and_formulas_alone(document: object) -> None:
    assert risk_free_data_literals(document) == []


def test_the_data_scan_reads_the_contract_files_and_finds_only_the_golden_table() -> None:
    files = sorted(DATA_ROOT.glob("*.json"))
    assert len(files) >= 4, "the scan reads the data files; a moved directory must not turn it green and empty"
    found = {
        (file.name, hit)
        for file in files
        for hit in risk_free_data_literals(json.loads(file.read_text(encoding="utf-8")))
    }
    assert found == ALLOWED_DATA_PATHS, sorted(found - ALLOWED_DATA_PATHS)


# -- the strategy code of the repository --------------------------------------------------------------


def test_the_scan_reads_the_code_that_runs_the_strategy() -> None:
    """Red when a root moves and the scan reads nothing: a guard that scans no file is green for ever."""
    files = {path.relative_to(REPO).as_posix() for path in _scanned_files()}
    assert len(files) > 100
    for required in (
        "apps/data-engine/src/data_engine/datahub/risk_free.py",
        "apps/data-engine/src/data_engine/datahub/backtest.py",
        "apps/data-engine/src/data_engine/datahub/production_topt/composition.py",
        "apps/data-engine/src/data_engine/lanes/capture.py",
        "common/factors/src/factors/composite/strategy_evaluator.py",
        "common/factors/src/factors/production_topt/core.py",
        "common/contracts/src/truealpha_contracts/strategy.py",
    ):
        assert required in files, required


@pytest.mark.criterion("R2.B6")
def test_no_strategy_code_holds_a_numeric_risk_free_literal() -> None:
    violations = [
        f"{path.relative_to(REPO).as_posix()}:{line} {name}"
        for path in _scanned_files()
        for line, name in risk_free_literals(path.read_text(encoding="utf-8"))
    ]
    assert violations == []


def _reads_the_golden_rate_table(source: str) -> bool:
    """True when the code reads a member or an item named `risk_free_rates`. Writing a dict key does not count."""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and node.attr == "risk_free_rates":
            return True
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.slice, ast.Constant)
            and node.slice.value == "risk_free_rates"
        ):
            return True
    return False


@pytest.mark.parametrize(
    ("source", "reads"),
    [
        ('rates = corpus["golden_decision_set"]["risk_free_rates"]', True),
        ("rates = golden.risk_free_rates", True),
        ('payload = {"risk_free_rates": [1, 2]}', False),
        ("rate = risk_free_rate_at(connection, cutoff)", False),
    ],
)
def test_the_golden_table_read_is_detected(source: str, reads: bool) -> None:
    assert _reads_the_golden_rate_table(source) is reads


def test_only_the_golden_replay_reads_the_explicit_rates_of_the_golden_corpus() -> None:
    readers = {
        path.relative_to(REPO).as_posix()
        for path in _scanned_files()
        if _reads_the_golden_rate_table(path.read_text(encoding="utf-8"))
    }
    assert readers <= GOLDEN_RATE_READERS, sorted(readers - GOLDEN_RATE_READERS)
    assert readers, "the golden replay reads its own rates; a scan that finds none has lost its pattern"


def test_the_provisional_resolver_is_gone() -> None:
    """`live_risk_free_rate` read the two-point table of the definition file for the live path."""
    names = {
        path.relative_to(REPO).as_posix()
        for path in _scanned_files()
        if "live_risk_free_rate" in path.read_text(encoding="utf-8")
    }
    assert names == set()
