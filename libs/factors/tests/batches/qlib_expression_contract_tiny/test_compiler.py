import copy
import hashlib
import json
from pathlib import Path

import pytest
from factors.batches.qlib_expression_contract_tiny import (
    FROZEN_CORPUS_SHA256,
    QLIB_LOCK_SHA256,
    S8_PREPARED_MANIFEST_SHA256,
    QlibFactorExpressionTinyActivation,
    QlibFactorExpressionTinyEvidence,
    bind_qlib_expression_execution,
    compile_and_parse_qlib_expression,
    compile_qlib_expression,
)
from pydantic import ValidationError
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.qlib_expression import (
    QlibExpressionExecutionBinding,
    QlibFactorExpressionDefinition,
    QlibOperatorRegistry,
)

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


def _definition_payload(case_id: str) -> dict[str, object]:
    corpus = _corpus()
    case = _case(case_id)
    expected = case["expected"]
    assert isinstance(expected, dict)
    required_ids = set(expected["required_feature_binding_ids"])
    definition = case["definition"]
    assert isinstance(definition, dict)
    return {
        **definition,
        "operator_registry_id": _registry().operator_registry_id,
        "feature_bindings": [
            binding for binding in corpus["feature_bindings"] if binding["feature_binding_id"] in required_ids
        ],
        "maximum_lookback_sessions": expected["maximum_lookback_sessions"],
    }


def _definition(case_id: str) -> QlibFactorExpressionDefinition:
    return QlibFactorExpressionDefinition.model_validate(_definition_payload(case_id))


def _binding(runtime_artifact_sha256: str = "3" * 64) -> QlibExpressionExecutionBinding:
    return QlibExpressionExecutionBinding(
        version="0.9.7",
        release_commit="d" * 40,
        runtime_artifact_sha256=runtime_artifact_sha256,
        runtime_lock_sha256=QLIB_LOCK_SHA256,
        adapter_id="truealpha.qlib.expression.adapter.v1",
        adapter_implementation_sha256="4" * 64,
    )


@pytest.mark.parametrize(
    "case_id",
    [
        "dimensionless-linear-blend",
        "one-session-price-change",
        "three-session-quality-mean",
    ],
)
def test_compile_frozen_expression(case_id: str) -> None:
    case = _case(case_id)
    expected = case["expected"]
    assert isinstance(expected, dict)
    compiled = compile_qlib_expression(_definition(case_id), _registry())
    assert compiled.qlib_field == expected["compiled_qlib_field"]
    assert list(compiled.required_feature_binding_ids) == expected["required_feature_binding_ids"]
    assert compiled.maximum_lookback_sessions == expected["maximum_lookback_sessions"]


def test_compile_rejects_raw_strings_before_qllib_import() -> None:
    with pytest.raises(TypeError, match="definition_must_be_typed_expression"):
        compile_and_parse_qlib_expression("Add($quality,$growth)", _registry())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="registry_must_be_typed_operator_registry"):
        compile_qlib_expression(_definition("dimensionless-linear-blend"), {})  # type: ignore[arg-type]


def test_compile_rejects_unknown_operator_and_invalid_arity() -> None:
    payload = _definition_payload("dimensionless-linear-blend")
    unknown = copy.deepcopy(payload)
    unknown["root"]["operator_id"] = "truealpha.qlib.unknown.v1"
    with pytest.raises(ValueError, match="unknown_operator"):
        compile_qlib_expression(QlibFactorExpressionDefinition.model_validate(unknown), _registry())

    arity = copy.deepcopy(payload)
    arity["root"]["arguments"] = arity["root"]["arguments"][:1]
    arity["feature_bindings"] = [
        binding for binding in arity["feature_bindings"] if binding["feature_binding_id"] == "feature.quality.v1"
    ]
    with pytest.raises(ValueError, match="invalid_operator_arity"):
        compile_qlib_expression(QlibFactorExpressionDefinition.model_validate(arity), _registry())


@pytest.mark.parametrize("window", ["0", "1.5"])
def test_compile_rejects_non_positive_integer_window(window: str) -> None:
    payload = _definition_payload("three-session-quality-mean")
    payload["root"]["arguments"][1]["value"] = window
    with pytest.raises(ValueError, match="invalid_argument_kind"):
        compile_qlib_expression(QlibFactorExpressionDefinition.model_validate(payload), _registry())


def test_compile_rejects_excessive_and_misdeclared_lookback() -> None:
    excessive = _definition_payload("three-session-quality-mean")
    excessive["root"]["arguments"][1]["value"] = "253"
    excessive["maximum_lookback_sessions"] = 252
    with pytest.raises(ValueError, match="maximum_lookback_exceeded"):
        compile_qlib_expression(QlibFactorExpressionDefinition.model_validate(excessive), _registry())

    mismatch = _definition_payload("three-session-quality-mean")
    mismatch["maximum_lookback_sessions"] = 3
    with pytest.raises(ValueError, match="declared_lookback_mismatch"):
        compile_qlib_expression(QlibFactorExpressionDefinition.model_validate(mismatch), _registry())


def test_execution_identity_changes_without_changing_expression_semantics() -> None:
    definition = _definition("dimensionless-linear-blend")
    compiled = compile_qlib_expression(definition, _registry())
    first = bind_qlib_expression_execution(compiled, _binding("3" * 64))
    second = bind_qlib_expression_execution(compiled, _binding("5" * 64))
    assert first.content_sha256 != second.content_sha256
    assert first.compiled_expression_sha256 == second.compiled_expression_sha256 == compiled.content_sha256
    assert definition.content_sha256 == _definition("dimensionless-linear-blend").content_sha256


def test_tiny_evidence_binds_equal_oracle_and_qllib_outputs() -> None:
    registry = _registry()
    compiled = tuple(
        compile_qlib_expression(_definition(case_id), registry)
        for case_id in (
            "dimensionless-linear-blend",
            "one-session-price-change",
            "three-session-quality-mean",
        )
    )
    execution = tuple(bind_qlib_expression_execution(item, _binding()) for item in compiled)
    output_sha256 = canonical_sha256(_corpus()["dimensionless_matrix"])
    evidence = QlibFactorExpressionTinyEvidence(
        activation=QlibFactorExpressionTinyActivation(environment="ci"),
        operator_registry_id=registry.operator_registry_id,
        operator_registry_sha256=registry.content_sha256,
        compiled_expression_ids=tuple(sorted(item.compiled_expression_id for item in compiled)),
        execution_evidence_ids=tuple(sorted(item.execution_evidence_id for item in execution)),
        independent_oracle_sha256=output_sha256,
        qlib_output_sha256=output_sha256,
    )
    assert evidence.evidence_id == f"qlib-factor-expression-tiny-evidence:{evidence.content_sha256}"
    assert evidence.activation.s8_prepared_manifest_sha256 == S8_PREPARED_MANIFEST_SHA256

    with pytest.raises(ValidationError, match="independent_oracle_qlib_output_mismatch"):
        QlibFactorExpressionTinyEvidence.model_validate(
            {**evidence.model_dump(mode="json"), "qlib_output_sha256": "0" * 64}
        )


def test_activation_rejects_frozen_artifact_drift() -> None:
    with pytest.raises(ValidationError, match="S8 activation artifact identity drifted"):
        QlibFactorExpressionTinyActivation(environment="local", frozen_corpus_sha256="0" * 64)
