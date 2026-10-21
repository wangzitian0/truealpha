"""#1255: a risk-free rate is a measurement. No strategy code holds a number for it.

The labor-efficiency factor subtracts `total_assets * risk_free_rate`. The rate comes from the stored
Treasury series (`datahub.risk_free`). A literal, a default value or a table in code would stand in for
that measurement and pass every test built on its own value. This test scans the code that runs the
strategy and fails on a numeric risk-free literal.

What counts as a risk-free binding: a name that contains `risk_free`, `riskfree`, `tbill` or
`treasury_rate`. The name can be an assignment target, a parameter, a keyword argument or a dict key.
A numeric literal anywhere on the value side of such a binding is a violation. `Decimal("0.05")`,
`Decimal(5) / 100` and `0.05` are numeric literals. The exception is the literal zero: a rate of zero
is "no adjustment". It is not a stand-in for a measurement. The Sharpe ratio uses it as a declared,
named parameter (`backtest.SHARPE_RISK_FREE`).

The second rule: the table of explicit rates in the golden decision set is fixture data. Only the
golden replay may read it. The live tick, the core engine and the backtest may not.
"""

from __future__ import annotations

import ast
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
_NAME = re.compile(r"risk_?free|tbill|treasury_rate", re.IGNORECASE)

#: The only code that may read `golden_decision_set.risk_free_rates`: the model of the golden set and the two
#: replays that run the golden corpus. All three are fixture paths. None runs in the live tick.
GOLDEN_RATE_READERS = {
    "apps/data-engine/src/data_engine/core_strategy_replay.py",
    "apps/data-engine/src/data_engine/strategy_backtest_gateway.py",
    "common/contracts/src/truealpha_contracts/strategy.py",
}


def _numeric_nonzero_literals(node: ast.AST) -> list[ast.AST]:
    """Every numeric constant below `node` that is not zero, with the `Decimal("0.05")` string form included.

    The bounds of a pydantic `Field(ge=0, le=1, min_length=2)` are not values, so only the default of a
    `Field` call is read."""
    found: list[ast.AST] = []
    pending = [node]
    while pending:
        child = pending.pop()
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Name) and child.func.id == "Field":
            pending.extend(child.args[:1])
            pending.extend(keyword.value for keyword in child.keywords if keyword.arg == "default")
            continue
        if (
            isinstance(child, ast.Constant)
            and isinstance(child.value, (int, float))
            and not isinstance(child.value, bool)
        ):
            if child.value != 0:
                found.append(child)
        elif (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Name)
            and child.func.id == "Decimal"
            and child.args
            and isinstance(child.args[0], ast.Constant)
            and isinstance(child.args[0].value, str)
        ):
            try:
                if float(child.args[0].value) != 0:
                    found.append(child)
            except ValueError:
                pass
        pending.extend(ast.iter_child_nodes(child))
    return found


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
    if isinstance(target, (ast.Tuple, ast.List)):
        return [name for element in target.elts for name in _target_names(element)]
    return []


def risk_free_literals(source: str) -> list[tuple[int, str]]:
    """(line, name) of every binding of a risk-free name whose value holds a nonzero numeric literal."""
    violations: list[tuple[int, str]] = []
    for name, value, line in _bindings(ast.parse(source)):
        if _NAME.search(name) and _numeric_nonzero_literals(value):
            violations.append((line, name))
    return sorted(set(violations))


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
