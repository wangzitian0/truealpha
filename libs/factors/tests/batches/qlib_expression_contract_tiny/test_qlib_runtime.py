import copy
import hashlib
import json
import math
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest
from factors.batches.qlib_expression_contract_tiny import (
    FROZEN_CORPUS_SHA256,
    compile_and_parse_qlib_expression,
)
from truealpha_contracts.qlib_expression import QlibCallNode, QlibFactorExpressionDefinition, QlibOperatorRegistry

pytest.importorskip("qlib")

from qlib.data.cache import H  # noqa: E402
from qlib.data.ops import Feature  # noqa: E402

CORPUS_PATH = Path(__file__).parents[4] / "contracts/tests/fixtures/qlib_expression.v1.json"


def _corpus() -> dict[str, object]:
    raw = CORPUS_PATH.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == FROZEN_CORPUS_SHA256
    return json.loads(raw)


def _registry() -> QlibOperatorRegistry:
    return QlibOperatorRegistry.model_validate(_corpus()["operator_registry"])


def _case(case_id: str) -> dict[str, object]:
    expressions = _corpus()["expressions"]
    assert isinstance(expressions, list)
    return next(copy.deepcopy(case) for case in expressions if case["case_id"] == case_id)


def _definition(case_id: str) -> QlibFactorExpressionDefinition:
    corpus = _corpus()
    case = _case(case_id)
    expected = case["expected"]
    assert isinstance(expected, dict)
    required = set(expected["required_feature_binding_ids"])
    definition = case["definition"]
    assert isinstance(definition, dict)
    return QlibFactorExpressionDefinition.model_validate(
        {
            **definition,
            "operator_registry_id": _registry().operator_registry_id,
            "feature_bindings": [
                binding for binding in corpus["feature_bindings"] if binding["feature_binding_id"] in required
            ],
            "maximum_lookback_sessions": expected["maximum_lookback_sessions"],
        }
    )


def _float(value: object) -> float:
    return float("nan") if value is None else float(str(value))


def _independent_evaluate(node: object, *, instrument: str) -> list[float]:
    assert isinstance(node, dict)
    matrix = _corpus()["dimensionless_matrix"]
    values = matrix["instruments"][instrument]
    size = len(matrix["sessions"])
    if node["kind"] == "feature":
        field_by_id = {
            binding["feature_binding_id"]: binding["qlib_field_name"] for binding in _corpus()["feature_bindings"]
        }
        return [_float(value) for value in values[field_by_id[node["feature_binding_id"]]]]
    if node["kind"] == "numeric":
        return [float(node["value"])] * size

    assert node["kind"] == "call"
    arguments = [_independent_evaluate(argument, instrument=instrument) for argument in node["arguments"]]
    operator = node["operator_id"]
    if operator.endswith(".ref.v1"):
        window = int(arguments[1][0])
        return ([float("nan")] * window + arguments[0])[:size]
    if operator.endswith(".mean.v1"):
        window = int(arguments[1][0])
        result: list[float] = []
        for index in range(size):
            observations = [
                value for value in arguments[0][max(0, index - window + 1) : index + 1] if not math.isnan(value)
            ]
            result.append(sum(observations) / len(observations) if observations else float("nan"))
        return result

    result = []
    for left, right in zip(arguments[0], arguments[1], strict=True):
        if math.isnan(left) or math.isnan(right):
            result.append(float("nan"))
        elif operator.endswith(".add.v1"):
            result.append(left + right)
        elif operator.endswith(".sub.v1"):
            result.append(left - right)
        elif operator.endswith(".mul.v1"):
            result.append(left * right)
        elif operator.endswith(".div.v1"):
            result.append(left / right)
        else:  # pragma: no cover - the frozen corpus contains only approved operators
            raise AssertionError(operator)
    return result


def _assert_values(actual: list[float], expected: list[object]) -> None:
    for left, right in zip(actual, expected, strict=True):
        if right is None:
            assert math.isnan(left)
        else:
            assert math.isclose(left, float(str(right)), rel_tol=1e-12, abs_tol=1e-12)


def _load_feature(self: Feature, instrument: str, start_index: int, end_index: int, freq: str) -> pd.Series:
    del freq
    matrix = _corpus()["dimensionless_matrix"]
    values = matrix["instruments"][instrument][self._name]
    return pd.Series([_float(value) for value in values], dtype="float64").iloc[start_index : end_index + 1]


@pytest.mark.parametrize(
    "case_id",
    [
        "dimensionless-linear-blend",
        "one-session-price-change",
        "three-session-quality-mean",
    ],
)
def test_independent_evaluator_matches_pinned_qllib(case_id: str) -> None:
    corpus = _corpus()
    case = _case(case_id)
    definition = _definition(case_id)
    with patch.object(Feature, "_load_internal", _load_feature):
        compiled, expression = compile_and_parse_qlib_expression(definition, _registry())
        assert compiled.qlib_field == case["expected"]["compiled_qlib_field"]
        for instrument in corpus["dimensionless_matrix"]["instruments"]:
            expected = corpus["dimensionless_matrix"]["expected_outputs"][case_id][instrument]
            independent = _independent_evaluate(case["definition"]["root"], instrument=instrument)
            H["f"].clear()
            qlib_values = expression.load(
                instrument,
                0,
                len(corpus["dimensionless_matrix"]["sessions"]) - 1,
                "day",
            ).tolist()
            _assert_values(independent, expected)
            _assert_values(qlib_values, expected)
            for oracle_value, qlib_value in zip(independent, qlib_values, strict=True):
                if math.isnan(oracle_value):
                    assert math.isnan(qlib_value)
                else:
                    assert math.isclose(oracle_value, qlib_value, rel_tol=1e-12, abs_tol=1e-12)


def test_qllib_parser_receives_only_compiler_output() -> None:
    from qlib.data.data import LocalExpressionProvider

    definition = _definition("dimensionless-linear-blend")
    original = LocalExpressionProvider.get_expression_instance
    received: list[str] = []

    def capture(self: LocalExpressionProvider, field: str) -> object:
        received.append(field)
        return original(self, field)

    with patch.object(LocalExpressionProvider, "get_expression_instance", capture):
        compiled, _expression = compile_and_parse_qlib_expression(definition, _registry())
    assert received == [compiled.qlib_field]
    assert received == ["Add($quality,Mul(0.5,$growth))"]
    assert not isinstance(definition.root, str)
    assert isinstance(definition.root, QlibCallNode)
