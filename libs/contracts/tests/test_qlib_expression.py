import copy
import hashlib
import importlib.util
import json
from decimal import Decimal, localcontext
from pathlib import Path

import pytest
from pydantic import ValidationError
from truealpha_contracts.qlib_expression import (
    MAX_QLIB_AST_DEPTH,
    MAX_QLIB_AST_NODES,
    MAX_QLIB_NUMERIC_CHARACTERS,
    CompiledQlibExpression,
    QlibExpressionExecutionBinding,
    QlibExpressionExecutionEvidence,
    QlibFactorExpressionDefinition,
    QlibFeatureBinding,
    QlibNumericNode,
    QlibOperatorDefinition,
    QlibOperatorRegistry,
    canonical_qlib_numeric,
)

CORPUS_PATH = Path(__file__).with_name("fixtures") / "qlib_expression.v1.json"
CORPUS_SHA256 = "dc1076517908739577bee2b1782e75c106200b0f8eb3594826135788bdebdace"


def _corpus() -> dict[str, object]:
    raw = CORPUS_PATH.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == CORPUS_SHA256
    return json.loads(raw)


def _registry_payload() -> dict[str, object]:
    return copy.deepcopy(_corpus()["operator_registry"])


def _registry() -> QlibOperatorRegistry:
    return QlibOperatorRegistry.model_validate(_registry_payload())


def _case(case_id: str) -> dict[str, object]:
    expressions = _corpus()["expressions"]
    assert isinstance(expressions, list)
    return next(copy.deepcopy(case) for case in expressions if case["case_id"] == case_id)


def _definition_payload(case_id: str) -> dict[str, object]:
    corpus = _corpus()
    case = _case(case_id)
    expected = case["expected"]
    assert isinstance(expected, dict)
    required_ids = set(expected["required_feature_binding_ids"])
    bindings = [binding for binding in corpus["feature_bindings"] if binding["feature_binding_id"] in required_ids]
    definition = case["definition"]
    assert isinstance(definition, dict)
    return {
        **definition,
        "operator_registry_id": _registry().operator_registry_id,
        "feature_bindings": bindings,
        "maximum_lookback_sessions": expected["maximum_lookback_sessions"],
    }


def _definition(case_id: str = "dimensionless-linear-blend") -> QlibFactorExpressionDefinition:
    return QlibFactorExpressionDefinition.model_validate(_definition_payload(case_id))


def test_frozen_corpus_builds_canonical_qlib_independent_contracts() -> None:
    assert importlib.util.find_spec("qlib") is None
    registry = _registry()
    assert tuple(operator.operator_id for operator in registry.operators) == tuple(
        sorted(operator.operator_id for operator in registry.operators)
    )
    assert registry.operator_registry_id == f"qlib-operator-registry:{registry.content_sha256}"

    for case_id in (
        "dimensionless-linear-blend",
        "one-session-price-change",
        "three-session-quality-mean",
    ):
        definition = _definition(case_id)
        assert definition.expression_id == f"qlib-factor-expression:{definition.content_sha256}"
        assert definition.null_policy == "operator_declared"
        assert tuple(binding.feature_binding_id for binding in definition.feature_bindings) == tuple(
            sorted(binding.feature_binding_id for binding in definition.feature_bindings)
        )


def test_numeric_rendering_ignores_decimal_context_and_binary_float() -> None:
    with localcontext() as context:
        context.prec = 2
        context.rounding = "ROUND_DOWN"
        first = QlibNumericNode(value=Decimal("123.45000"))
    with localcontext() as context:
        context.prec = 50
        context.rounding = "ROUND_UP"
        second = QlibNumericNode(value=Decimal("123.45000"))

    assert first == second
    assert canonical_qlib_numeric(first.value) == "123.45"
    assert canonical_qlib_numeric(Decimal("-0.000")) == "0"
    with pytest.raises(ValidationError, match="numeric_must_use_decimal_text_or_integer"):
        QlibNumericNode(value=0.5)
    for non_finite in ("NaN", "Infinity", "-Infinity"):
        with pytest.raises(ValidationError, match="non_finite_numeric"):
            QlibNumericNode(value=non_finite)
    with pytest.raises(ValidationError, match="numeric_literal_too_long"):
        QlibNumericNode(value="1e128")
    assert len(canonical_qlib_numeric(Decimal("9" * MAX_QLIB_NUMERIC_CHARACTERS))) == MAX_QLIB_NUMERIC_CHARACTERS


def test_registry_reordering_is_stable_and_semantic_drift_changes_identity() -> None:
    payload = _registry_payload()
    reversed_payload = copy.deepcopy(payload)
    reversed_payload["operators"] = list(reversed(reversed_payload["operators"]))
    assert QlibOperatorRegistry.model_validate(payload) == QlibOperatorRegistry.model_validate(reversed_payload)

    changed = copy.deepcopy(payload)
    changed["operators"][0]["semantic_spec"] += "-changed"
    changed["operators"][0]["semantic_spec_sha256"] = hashlib.sha256(
        changed["operators"][0]["semantic_spec"].encode()
    ).hexdigest()
    with pytest.raises(ValidationError, match="unapproved_builtin_semantics"):
        QlibOperatorRegistry.model_validate(changed)


def test_expression_reordering_is_stable_and_stale_identity_fails() -> None:
    payload = _definition_payload("dimensionless-linear-blend")
    reversed_payload = copy.deepcopy(payload)
    reversed_payload["feature_bindings"] = list(reversed(reversed_payload["feature_bindings"]))
    assert QlibFactorExpressionDefinition.model_validate(payload) == QlibFactorExpressionDefinition.model_validate(
        reversed_payload
    )

    definition = QlibFactorExpressionDefinition.model_validate(payload)
    stale = copy.deepcopy(payload)
    stale["content_sha256"] = definition.content_sha256
    stale["root"]["arguments"][1]["arguments"][0]["value"] = "0.25"
    with pytest.raises(ValidationError, match="expression_content_hash_mismatch"):
        QlibFactorExpressionDefinition.model_validate(stale)


@pytest.mark.parametrize(
    ("field_name", "value", "error"),
    [
        ("qlib_field_name", "quality);__import__('os')", "string_pattern_mismatch"),
        ("qlib_field_name", "quality.real", "string_pattern_mismatch"),
        ("qlib_field_name", "quality[0]", "string_pattern_mismatch"),
    ],
)
def test_feature_binding_rejects_qlib_syntax_injection(field_name: str, value: str, error: str) -> None:
    with pytest.raises(ValidationError, match=error):
        QlibFeatureBinding.model_validate({"feature_binding_id": "feature.quality.v1", field_name: value})


def test_expression_rejects_raw_string_extra_inputs_and_unresolved_features() -> None:
    payload = _definition_payload("dimensionless-linear-blend")
    raw = copy.deepcopy(payload)
    raw["root"] = "Add($quality,$growth)"
    with pytest.raises(ValidationError):
        QlibFactorExpressionDefinition.model_validate(raw)

    extra = copy.deepcopy(payload)
    extra["root"]["module_path"] = "os.system"
    with pytest.raises(ValidationError, match="extra_forbidden"):
        QlibFactorExpressionDefinition.model_validate(extra)

    unknown = copy.deepcopy(payload)
    unknown["root"]["arguments"][0]["feature_binding_id"] = "feature.unknown.v1"
    with pytest.raises(ValidationError, match="unknown_feature_binding"):
        QlibFactorExpressionDefinition.model_validate(unknown)


def test_expression_rejects_duplicate_and_unused_feature_bindings() -> None:
    payload = _definition_payload("dimensionless-linear-blend")
    duplicate = copy.deepcopy(payload)
    duplicate["feature_bindings"].append(copy.deepcopy(duplicate["feature_bindings"][0]))
    with pytest.raises(ValidationError, match="duplicate_feature_binding_id"):
        QlibFactorExpressionDefinition.model_validate(duplicate)

    unused = copy.deepcopy(payload)
    unused["feature_bindings"].append({"feature_binding_id": "feature.price.v1", "qlib_field_name": "price"})
    with pytest.raises(ValidationError, match="unused_feature_binding"):
        QlibFactorExpressionDefinition.model_validate(unused)


def _deep_node(depth: int) -> dict[str, object]:
    node: dict[str, object] = {"kind": "feature", "feature_binding_id": "feature.quality.v1"}
    for _ in range(depth - 1):
        node = {
            "kind": "call",
            "operator_id": "truealpha.qlib.add.v1",
            "arguments": [node, {"kind": "numeric", "value": "1"}],
        }
    return node


def test_expression_rejects_excessive_depth_and_node_count() -> None:
    payload = _definition_payload("dimensionless-linear-blend")
    too_deep = copy.deepcopy(payload)
    too_deep["root"] = _deep_node(MAX_QLIB_AST_DEPTH + 1)
    too_deep["feature_bindings"] = [
        binding for binding in too_deep["feature_bindings"] if binding["feature_binding_id"] == "feature.quality.v1"
    ]
    with pytest.raises(ValidationError, match="maximum_ast_depth_exceeded"):
        QlibFactorExpressionDefinition.model_validate(too_deep)

    too_many = copy.deepcopy(payload)
    root: dict[str, object] = {"kind": "feature", "feature_binding_id": "feature.quality.v1"}
    while True:
        candidate = {
            "kind": "call",
            "operator_id": "truealpha.qlib.add.v1",
            "arguments": [root, copy.deepcopy(root)],
        }
        root = candidate
        if len(json.dumps(root)) > MAX_QLIB_AST_NODES * 100:
            break
    too_many["root"] = root
    too_many["feature_bindings"] = [
        binding for binding in too_many["feature_bindings"] if binding["feature_binding_id"] == "feature.quality.v1"
    ]
    with pytest.raises(ValidationError, match="maximum_ast_nodes_exceeded"):
        QlibFactorExpressionDefinition.model_validate(too_many)


def test_registry_rejects_stale_hash_duplicate_and_unapproved_operator() -> None:
    payload = _registry_payload()
    stale = copy.deepcopy(payload)
    stale["content_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="registry_content_hash_mismatch"):
        QlibOperatorRegistry.model_validate(stale)

    duplicate = copy.deepcopy(payload)
    duplicate["operators"][-1] = copy.deepcopy(duplicate["operators"][0])
    with pytest.raises(ValidationError, match="duplicate_operator_id"):
        QlibOperatorRegistry.model_validate(duplicate)

    unapproved = copy.deepcopy(payload["operators"][0])
    unapproved["qlib_symbol"] = "__import__"
    with pytest.raises(ValidationError):
        QlibOperatorDefinition.model_validate(unapproved)


def test_execution_binding_identity_is_separate_from_expression_semantics() -> None:
    definition = _definition()
    common = {
        "version": "0.9.7",
        "release_commit": "d" * 40,
        "runtime_lock_sha256": "1" * 64,
        "adapter_id": "truealpha.qlib.expression.adapter.v1",
        "adapter_implementation_sha256": "2" * 64,
    }
    first = QlibExpressionExecutionBinding(runtime_artifact_sha256="3" * 64, **common)
    second = QlibExpressionExecutionBinding(runtime_artifact_sha256="4" * 64, **common)
    assert first.content_sha256 != second.content_sha256
    assert definition.content_sha256 == _definition().content_sha256


def test_compiled_and_execution_references_must_match_their_hashes() -> None:
    registry = _registry()
    definition = _definition()
    with pytest.raises(ValidationError, match="compiled_expression_semantic_reference_mismatch"):
        CompiledQlibExpression(
            expression_id=definition.expression_id,
            expression_semantic_sha256="0" * 64,
            operator_registry_id=registry.operator_registry_id,
            operator_registry_sha256=registry.content_sha256,
            qlib_field="Add($quality,Mul(0.5,$growth))",
            required_feature_binding_ids=("feature.growth.v1", "feature.quality.v1"),
            maximum_lookback_sessions=0,
        )
    binding = QlibExpressionExecutionBinding(
        version="0.9.7",
        release_commit="d" * 40,
        runtime_artifact_sha256="1" * 64,
        runtime_lock_sha256="2" * 64,
        adapter_id="truealpha.qlib.expression.adapter.v1",
        adapter_implementation_sha256="3" * 64,
    )
    with pytest.raises(ValidationError, match="execution_binding_reference_mismatch"):
        QlibExpressionExecutionEvidence(
            compiled_expression_id="compiled-qlib-expression:" + "4" * 64,
            compiled_expression_sha256="4" * 64,
            execution_binding_id=binding.execution_binding_id,
            execution_binding_sha256="5" * 64,
        )


def test_contract_models_are_frozen() -> None:
    definition = _definition()
    with pytest.raises(ValidationError, match="frozen_instance"):
        definition.factor_version = "2.0.0"
