"""Tests for tools/complexity_gate.py (Cyclomatic complexity ratchet gate)."""

import ast
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "tools"))

import complexity_gate  # noqa: E402


def test_measure_ast_complexity_accurately_counts_decision_points() -> None:
    code = """
def sample_function(x, y, z):
    if x > 0:
        for i in range(10):
            while y > 0:
                y -= 1
    elif z and (x or y):
        try:
            return 1 if x else 2
        except Exception:
            return 0
    return -1
"""
    tree = ast.parse(code)
    fn = tree.body[0]
    assert isinstance(fn, ast.FunctionDef)
    # Base: 1
    # if x > 0: +1
    # for: +1
    # while: +1
    # elif: +1
    # BoolOp (and): +1
    # BoolOp (or): +1
    # ExceptHandler: +1
    # IfExp: +1
    # Total = 1 + 8 = 9
    c = complexity_gate.measure_ast_complexity(fn)
    assert c == 9


def test_complexity_gate_passes_current_codebase() -> None:
    """The gate passes cleanly with 0 violations against the committed baseline."""
    code = complexity_gate.check_complexity()
    assert code == 0


def test_complexity_gate_rejects_new_high_complexity_function(tmp_path: Path) -> None:
    baseline_file = tmp_path / "baseline.json"
    baseline_file.write_text("{}", encoding="utf-8")

    # Mock scan_functions to return a function exceeding threshold
    mock_scan = {"apps/test_module.py:complex_fn": 25}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(complexity_gate, "scan_functions", lambda *args, **kwargs: mock_scan)
        code = complexity_gate.check_complexity(
            threshold=20,
            baseline_path=baseline_file,
        )
        assert code == 1


def test_complexity_gate_rejects_complexity_increase(tmp_path: Path) -> None:
    baseline_file = tmp_path / "baseline.json"
    baseline_file.write_text(json.dumps({"apps/test_module.py:legacy_fn": 22}), encoding="utf-8")

    mock_scan = {"apps/test_module.py:legacy_fn": 26}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(complexity_gate, "scan_functions", lambda *args, **kwargs: mock_scan)
        code = complexity_gate.check_complexity(
            threshold=20,
            baseline_path=baseline_file,
        )
        assert code == 1


def test_complexity_gate_allows_functions_within_threshold(tmp_path: Path) -> None:
    baseline_file = tmp_path / "baseline.json"
    baseline_file.write_text("{}", encoding="utf-8")

    mock_scan = {"apps/test_module.py:clean_fn": 12}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(complexity_gate, "scan_functions", lambda *args, **kwargs: mock_scan)
        code = complexity_gate.check_complexity(
            threshold=20,
            baseline_path=baseline_file,
        )
        assert code == 0
