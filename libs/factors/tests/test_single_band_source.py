"""#1235: one band set. The tier bands exist only in the packaged definition files.

Two engines used to tier the same issuers on one tick with different bands: the strategy
read its definition file, and the TOPT core engine carried its own literals in
``factors.production_topt.core``. Both now read ``load_live_strategy_definition()``. This
guard fails when a band value is written anywhere else in the source of ``libs/factors``
or ``libs/contracts``.

Scope: the ``src`` trees. The two allowed files are the live definition and the 0.1.0
history. Test files are out of scope on purpose: the independent golden oracle
(``libs/contracts/tests/test_strategy_v1.py``) must carry its own copy of the
owner-confirmed values, or it could not catch a wrong value in the definition file.

Two detectors, because each misses what the other sees:

* Shape: a keyword argument, dict key or JSON key named like a band bound
  (``labor_efficiency_lower_bound``, ``gppe_upper``, ``target_ps_lower``, ...) that holds a
  literal number.
* Value: a literal equal to a labor-efficiency cut of either definition, or to a
  non-integer target bound of the live definition. Integer targets (10, 40) are left to the
  shape detector, because those integers are common for unrelated reasons.
"""

from __future__ import annotations

import ast
import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from truealpha_contracts.strategy import (
    HISTORICAL_V0_DEFINITION_RESOURCE,
    LIVE_STRATEGY_DEFINITION_RESOURCE,
    load_live_strategy_definition,
    load_strategy_definition,
)

_REPO = Path(__file__).resolve().parents[3]
_ROOTS = (_REPO / "libs" / "factors" / "src", _REPO / "libs" / "contracts" / "src")
_DATA = _REPO / "libs" / "contracts" / "src" / "truealpha_contracts" / "data"
_ALLOWED = frozenset({_DATA / LIVE_STRATEGY_DEFINITION_RESOURCE, _DATA / HISTORICAL_V0_DEFINITION_RESOURCE})
_BAND_FIELD = re.compile(
    r"^(?:labor_efficiency|gppe)_(?:lower|upper)(?:_bound)?$|^target_[a-z]+_(?:lower|upper|midpoint)(?:_bound)?$"
)


def _distinctive_band_values() -> frozenset[Decimal]:
    live = load_live_strategy_definition()
    history = load_strategy_definition(HISTORICAL_V0_DEFINITION_RESOURCE)
    values: set[Decimal] = set()
    for definition in (live, history):
        for band in definition.tier_valuation.bands:
            values.update(
                bound
                for bound in (band.labor_efficiency_lower_bound, band.labor_efficiency_upper_bound)
                if bound is not None
            )
    for band in live.tier_valuation.bands:
        values.update(
            bound for bound in (band.target_lower_bound, band.target_upper_bound) if bound != bound.to_integral()
        )
    return frozenset(values)


def _number(node: ast.expr) -> Decimal | None:
    """The literal number a node spells: a constant, or ``Decimal(<constant>)``."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Decimal" and node.args:
        node = node.args[0]
    if (
        isinstance(node, ast.Constant)
        and isinstance(node.value, int | float | str)
        and not isinstance(node.value, bool)
    ):
        try:
            return Decimal(str(node.value).replace("_", ""))
        except InvalidOperation:
            return None
    return None


def python_violations(source: str, label: str, values: frozenset[Decimal]) -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        pairs: list[tuple[str, ast.expr]] = []
        if isinstance(node, ast.Call):
            pairs.extend((keyword.arg, keyword.value) for keyword in node.keywords if keyword.arg)
        elif isinstance(node, ast.Dict):
            pairs.extend(
                (key.value, value)
                for key, value in zip(node.keys, node.values, strict=True)
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            )
        for name, value in pairs:
            if _BAND_FIELD.match(name) and _number(value) is not None:
                found.append(f"{label}:{value.lineno}: band field {name} = {ast.unparse(value)}")
        if isinstance(node, ast.Constant | ast.Call):
            number = _number(node)
            if number is not None and number in values:
                found.append(f"{label}:{node.lineno}: band value {ast.unparse(node)}")
    return sorted(set(found))


def json_violations(payload: Any, label: str) -> list[str]:
    found: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            if _BAND_FIELD.match(str(key)) and value is not None and not isinstance(value, dict | list):
                found.append(f"{label}: band field {key} = {value!r}")
            found.extend(json_violations(value, label))
    elif isinstance(payload, list):
        for item in payload:
            found.extend(json_violations(item, label))
    return found


def test_band_values_live_only_in_the_definition_files() -> None:
    values = _distinctive_band_values()
    violations: list[str] = []
    scanned = 0
    for root in _ROOTS:
        for path in sorted(root.rglob("*")):
            if path in _ALLOWED or not path.is_file():
                continue
            label = str(path.relative_to(_REPO))
            if path.suffix == ".py":
                violations.extend(python_violations(path.read_text(), label, values))
                scanned += 1
            elif path.suffix == ".json":
                violations.extend(json_violations(json.loads(path.read_text()), label))
                scanned += 1
    assert scanned > 50, f"the guard scanned only {scanned} files; its roots moved"
    assert not violations, "a second band set exists outside the definition files:\n" + "\n".join(violations)


def test_the_guard_sees_a_planted_band_set() -> None:
    """A guard never seen failing is no evidence: plant both shapes the TOPT core used to hold."""
    values = _distinctive_band_values()
    planted = (
        "TierBandDefinition(tier=TECH, gppe_lower=Decimal('1000000'), target_ps_lower=Decimal('8'))\n"
        "BANDS = {'target_pgp_upper_bound': '14.29'}\n"
        "CUT = Decimal('3000000')\n"
    )
    found = python_violations(planted, "planted.py", values)
    assert any("gppe_lower" in item for item in found)
    assert any("target_ps_lower" in item for item in found)
    assert any("target_pgp_upper_bound" in item for item in found)
    assert any("3000000" in item for item in found)
    assert json_violations({"bands": [{"labor_efficiency_upper_bound": "1000000"}]}, "planted.json")
    # A computed bound is not a literal: reading the definition is the allowed shape.
    assert not python_violations("Result(target_ps_lower=band.target_lower_bound * margin)\n", "ok.py", values)
