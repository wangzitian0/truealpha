"""#1235: one band set. The tier bands exist only in the packaged definition files.

Two engines used to tier the same issuers on one tick with different bands: the strategy
read its definition file, and the TOPT core engine carried its own literals in
``factors.production_topt.core``. Both now read ``load_live_strategy_definition()``. This
guard fails when a band value is written anywhere else in a source tree.

Scope: every tree that can hold executable code or schema.

* Python: ``apps/*/src`` and ``libs/*/src`` (``.py`` and ``.json``).
* TypeScript: ``apps/*/src`` (``.ts`` and ``.tsx``).
* SQL: the migrations from this PR's migration on. Older migrations are history.

The two allowed files are the live definition and the 0.1.0 history. Test files are out of
scope on purpose: the independent golden oracle (``libs/contracts/tests/test_strategy_v1.py``)
must carry its own copy of the owner-confirmed values, or it could not catch a wrong value
in the definition file.

Two detectors, because each misses what the other sees:

* Shape: a keyword argument, dict key, object key or column named like a band bound
  (``labor_efficiency_lower_bound``, ``gppe_upper``, ``targetPgpLower``, ...) that holds a
  literal number.
* Value: a literal equal to a distinctive band value. In ``libs/factors/src`` and
  ``libs/contracts/src`` that is every labor-efficiency cut of both definitions and every
  non-integer target of the live definition. In the other trees it leaves out the cuts that
  are a power of ten (1,000,000 and 100,000) and the 0.1.0 cuts: measured on 2026-10-13,
  ``1e6`` and ``1_000_000`` format money in the App, and ``Decimal("300000")`` bounds a
  canary GPPE oracle in the data engine. Neither is a tier band. The shape detector and the
  other values (3,000,000, 7.50, 11.43, 14.29, 26.67) still catch a copied band set there.
"""

from __future__ import annotations

import ast
import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from truealpha_contracts.strategy import (
    LIVE_STRATEGY_DEFINITION_RESOURCE,
    load_live_strategy_definition,
)

_REPO = Path(__file__).resolve().parents[3]
_STRICT_ROOTS = (_REPO / "libs" / "factors" / "src", _REPO / "libs" / "contracts" / "src")
_PYTHON_ROOTS = tuple(sorted({*_REPO.glob("apps/*/src"), *_REPO.glob("libs/*/src")}))
_TYPESCRIPT_ROOTS = tuple(sorted(_REPO.glob("apps/*/src")))
_MIGRATIONS = _REPO / "db" / "migrations"
#: This PR's migration. Every migration from it on is in scope; the older ones are history.
_FIRST_SCANNED_MIGRATION = "20261013T0201_factors_price_to_gross_profit.sql"
_DATA = _REPO / "libs" / "contracts" / "src" / "truealpha_contracts" / "data"
_ALLOWED = frozenset({_DATA / LIVE_STRATEGY_DEFINITION_RESOURCE})
_BAND_FIELD = re.compile(
    r"^(?:labor_efficiency|gppe)_(?:lower|upper)(?:_bound)?$|^target_[a-z]+_(?:lower|upper|midpoint)(?:_bound)?$"
)
#: The same names in text (TypeScript, SQL), snake_case or camelCase, followed by `:`, `=` or
#: `=>` and a literal number, quoted or not.
_TEXT_BAND_FIELD = re.compile(
    r"(?i)\b((?:labor_?efficiency|gppe)_?(?:lower|upper)(?:_?bound)?|target_?[a-z]+?_?(?:lower|upper|midpoint)(?:_?bound)?)"
    r"\b\s*(?::|=>?)\s*[\"']?(-?\d[\d_]*(?:\.\d+)?(?:e\d+)?)"
)
_TEXT_NUMBER = re.compile(r"(?<![\w.$#-])(\d[\d_]*(?:\.\d+)?(?:[eE]\d+)?)(?![\w.])")


def _labor_cuts(definition: Any) -> set[Decimal]:
    return {
        bound
        for band in definition.tier_valuation.bands
        for bound in (band.labor_efficiency_lower_bound, band.labor_efficiency_upper_bound)
        if bound is not None
    }


def _live_fractional_targets() -> set[Decimal]:
    return {
        bound
        for band in load_live_strategy_definition().tier_valuation.bands
        for bound in (band.target_lower_bound, band.target_upper_bound)
        if bound != bound.to_integral()
    }


def _strict_band_values() -> frozenset[Decimal]:
    live = load_live_strategy_definition()
    return frozenset(_labor_cuts(live) | _live_fractional_targets())


def _wide_band_values() -> frozenset[Decimal]:
    def power_of_ten(value: Decimal) -> bool:
        return value > 0 and value.normalize().as_tuple().digits == (1,)

    live_cuts = {cut for cut in _labor_cuts(load_live_strategy_definition()) if not power_of_ten(cut)}
    return frozenset(live_cuts | _live_fractional_targets())


def _parse(text: str) -> Decimal | None:
    try:
        return Decimal(text.replace("_", ""))
    except InvalidOperation:
        return None


def _number(node: ast.expr) -> Decimal | None:
    """The literal number a node spells: a constant, or ``Decimal(<constant>)``."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Decimal" and node.args:
        node = node.args[0]
    if (
        isinstance(node, ast.Constant)
        and isinstance(node.value, int | float | str)
        and not isinstance(node.value, bool)
    ):
        return _parse(str(node.value))
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


def text_violations(source: str, label: str, values: frozenset[Decimal]) -> list[str]:
    """TypeScript and SQL: the same two detectors, on text."""
    found: list[str] = []
    for line_number, line in enumerate(source.splitlines(), start=1):
        for match in _TEXT_BAND_FIELD.finditer(line):
            found.append(f"{label}:{line_number}: band field {match.group(1)} = {match.group(2)}")
        for match in _TEXT_NUMBER.finditer(line):
            number = _parse(match.group(1))
            if number is not None and number in values:
                found.append(f"{label}:{line_number}: band value {match.group(1)}")
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


def _source_files(roots: tuple[Path, ...], suffixes: tuple[str, ...]) -> list[Path]:
    return sorted(
        path
        for root in roots
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix in suffixes
        and path not in _ALLOWED
        and "node_modules" not in path.parts
        and "__pycache__" not in path.parts
    )


def test_band_values_live_only_in_the_definition_files() -> None:
    strict, wide = _strict_band_values(), _wide_band_values()
    violations: list[str] = []
    scanned: dict[str, int] = {"py": 0, "json": 0, "ts": 0, "sql": 0}
    for path in _source_files(_PYTHON_ROOTS, (".py", ".json")):
        label = str(path.relative_to(_REPO))
        if path.suffix == ".py":
            values = strict if any(root in path.parents for root in _STRICT_ROOTS) else wide
            violations.extend(python_violations(path.read_text(), label, values))
            scanned["py"] += 1
        else:
            violations.extend(json_violations(json.loads(path.read_text()), label))
            scanned["json"] += 1
    for path in _source_files(_TYPESCRIPT_ROOTS, (".ts", ".tsx")):
        violations.extend(text_violations(path.read_text(), str(path.relative_to(_REPO)), wide))
        scanned["ts"] += 1
    for path in sorted(_MIGRATIONS.glob("*.sql")):
        if path.name >= _FIRST_SCANNED_MIGRATION:
            violations.extend(text_violations(path.read_text(), str(path.relative_to(_REPO)), wide))
            scanned["sql"] += 1
    assert scanned["py"] > 150 and scanned["ts"] > 60 and scanned["sql"] >= 1, f"the guard roots moved: {scanned}"
    assert not violations, "a second band set exists outside the definition files:\n" + "\n".join(violations)


def test_the_guard_sees_a_planted_band_set() -> None:
    """A guard never seen failing is no evidence: plant the shapes a copied band set takes."""
    strict, wide = _strict_band_values(), _wide_band_values()
    planted_python = (
        "TierBandDefinition(tier=TECH, gppe_lower=Decimal('1000000'), target_ps_lower=Decimal('8'))\n"
        "BANDS = {'target_pgp_upper_bound': '14.29'}\n"
        "CUT = Decimal('3000000')\n"
    )
    found = python_violations(planted_python, "planted.py", strict)
    assert any("gppe_lower" in item for item in found)
    assert any("target_ps_lower" in item for item in found)
    assert any("target_pgp_upper_bound" in item for item in found)
    assert any("3000000" in item for item in found)
    assert json_violations({"bands": [{"labor_efficiency_upper_bound": "1000000"}]}, "planted.json")
    # A computed bound is not a literal: reading the definition is the allowed shape.
    assert not python_violations("Result(target_ps_lower=band.target_lower_bound * margin)\n", "ok.py", strict)

    planted_typescript = (
        'const BANDS = [{ tier: "tech", gppeLower: 1_000_000, targetPgpUpper: 14.29 }];\n'
        "const LARGE_MODEL_CUT = 3e6;\n"
        'const LOWER = { target_ps_lower: "3" };\n'
    )
    found = text_violations(planted_typescript, "planted.ts", wide)
    assert any("gppeLower" in item for item in found)
    assert any("targetPgpUpper" in item for item in found)
    assert any("target_ps_lower" in item for item in found)
    assert any("band value 14.29" in item for item in found)
    assert any("band value 3e6" in item for item in found)
    planted_sql = "insert into mart.bands (tier, target_pgp_lower) values ('tech', 11.43);\n"
    assert any("11.43" in item for item in text_violations(planted_sql, "planted.sql", wide))
    # The App's money formatting is not a band (measured): it must stay clean.
    assert not text_violations("if (abs >= 1e6) return `$${(abs / 1e6).toFixed(2)}M`;\n", "format.ts", wide)
    assert not text_violations(
        "row.target_ps_midpoint === null ? null : String(row.target_ps_midpoint)\n", "ok.ts", wide
    )


def test_no_hardcoded_risk_free_rate_or_margin_gate_in_runtime_code() -> None:
    """Topic 1: eliminates scattered 0.35 margin gates and 0.05 risk-free rate constants.

    The live strategy definition (large_model_value_v0_definition_1_0_0.json) is the sole
    source of parameters for valuation bands, midpoint, and risk-free rates. Runtime code
    in apps/ and libs/ must not declare hardcoded literals for these values.
    """
    risk_free_pattern = re.compile(r"\bRISK_FREE_RATE\s*=\s*(?:Decimal\([\"']0\.05[\"']\)|0\.05)")
    margin_gate_pattern = re.compile(
        r"\b(?:LEGACY_V0_MARGIN_GATE|MARGIN_GATE)\b|gross_margin\s*[<>=!]+\s*(?:0\.35|Decimal\([\"']0\.35[\"']\))"
    )

    violations: list[str] = []
    for root in _PYTHON_ROOTS:
        for py_file in root.rglob("*.py"):
            text = py_file.read_text(encoding="utf-8")
            rel = py_file.relative_to(_REPO)
            if risk_free_pattern.search(text):
                violations.append(f"{rel}: hardcoded 0.05 RISK_FREE_RATE found")
            if margin_gate_pattern.search(text):
                violations.append(f"{rel}: hardcoded 0.35 margin gate found")

    assert not violations, "hardcoded risk-free rate or margin gate found in runtime code:\n" + "\n".join(violations)
