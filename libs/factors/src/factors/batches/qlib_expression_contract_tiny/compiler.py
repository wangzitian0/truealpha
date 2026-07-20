"""Safe compiler boundary for the frozen S8 typed-expression corpus."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.qlib_expression import (
    MAX_QLIB_LOOKBACK_SESSIONS,
    CompiledQlibExpression,
    QlibArgumentKind,
    QlibExpressionExecutionBinding,
    QlibExpressionExecutionEvidence,
    QlibExpressionNode,
    QlibFactorExpressionDefinition,
    QlibFeatureNode,
    QlibLookbackRule,
    QlibNumericNode,
    QlibOperatorDefinition,
    QlibOperatorRegistry,
    canonical_qlib_numeric,
)

S8_PREPARED_MANIFEST_SHA256 = "3c3c6a0071f50cdd1dd3f117c882416e2c891560866015394646d6351d46c660"
FROZEN_CORPUS_SHA256 = "db836233a6ce6d71127acbe575fc2aee729dc996c0b6bb13465a8a3604235b8c"
QLIB_LOCK_SHA256 = "bd15aa78073f2dd4565a65e32d2942b2def399928897fc48036db9aefd1ffd9c"
QLIB_DISTRIBUTION = "pyqlib"
QLIB_VERSION = "0.9.7"
QLIB_RELEASE_COMMIT = "da920b7f954f48ab1bb64117c976710de198373e"


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class QlibFactorExpressionTinyActivation(_StrictFrozenModel):
    batch_id: Literal["S8-qlib-factor-expression"] = "S8-qlib-factor-expression"
    environment: Literal["local", "ci"]
    s8_prepared_manifest_sha256: str = Field(default=S8_PREPARED_MANIFEST_SHA256, pattern=r"^[0-9a-f]{64}$")
    frozen_corpus_sha256: str = Field(default=FROZEN_CORPUS_SHA256, pattern=r"^[0-9a-f]{64}$")
    qlib_lock_sha256: str = Field(default=QLIB_LOCK_SHA256, pattern=r"^[0-9a-f]{64}$")
    live_source_allowed: Literal[False] = False
    staging_allowed: Literal[False] = False
    persistence_allowed: Literal[False] = False
    factor_registration_allowed: Literal[False] = False
    replay_allowed: Literal[False] = False
    release_allowed: Literal[False] = False

    @model_validator(mode="after")
    def bind_exact_artifacts(self) -> Self:
        actual = (self.s8_prepared_manifest_sha256, self.frozen_corpus_sha256, self.qlib_lock_sha256)
        expected = (S8_PREPARED_MANIFEST_SHA256, FROZEN_CORPUS_SHA256, QLIB_LOCK_SHA256)
        if actual != expected:
            raise ValueError("S8 activation artifact identity drifted")
        return self


class QlibFactorExpressionTinyEvidence(_StrictFrozenModel):
    evidence_id: str = Field(default="", pattern=r"^(?:|qlib-factor-expression-tiny-evidence:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    activation: QlibFactorExpressionTinyActivation
    operator_registry_id: str = Field(pattern=r"^qlib-operator-registry:[0-9a-f]{64}$")
    operator_registry_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    compiled_expression_ids: tuple[str, ...] = Field(min_length=1)
    execution_evidence_ids: tuple[str, ...] = Field(min_length=1)
    independent_oracle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    qlib_output_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    product_semantics_approved: Literal[False] = False
    real_data_used: Literal[False] = False
    stable_handoff: Literal[False] = False
    release_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_and_identify(self) -> Self:
        if self.independent_oracle_sha256 != self.qlib_output_sha256:
            raise ValueError("independent_oracle_qlib_output_mismatch")
        if tuple(sorted(set(self.compiled_expression_ids))) != self.compiled_expression_ids:
            raise ValueError("compiled_expression_ids_not_canonical")
        if tuple(sorted(set(self.execution_evidence_ids))) != self.execution_evidence_ids:
            raise ValueError("execution_evidence_ids_not_canonical")
        if self.operator_registry_id != f"qlib-operator-registry:{self.operator_registry_sha256}":
            raise ValueError("tiny_evidence_registry_reference_mismatch")
        payload = self.model_dump(mode="json", exclude={"evidence_id", "content_sha256"})
        digest = canonical_sha256(payload)
        expected_id = f"qlib-factor-expression-tiny-evidence:{digest}"
        if self.content_sha256 and self.content_sha256 != digest:
            raise ValueError("tiny_evidence_content_hash_mismatch")
        if self.evidence_id and self.evidence_id != expected_id:
            raise ValueError("tiny_evidence_id_mismatch")
        object.__setattr__(self, "content_sha256", digest)
        object.__setattr__(self, "evidence_id", expected_id)
        return self


def _positive_integer(node: QlibExpressionNode) -> int:
    if not isinstance(node, QlibNumericNode):
        raise ValueError("invalid_argument_kind:positive_integer")
    exponent = node.value.as_tuple().exponent
    if not isinstance(exponent, int) or exponent < 0 or node.value <= 0:
        raise ValueError("invalid_argument_kind:positive_integer")
    value = int(node.value)
    if value > MAX_QLIB_LOOKBACK_SESSIONS:
        raise ValueError("maximum_lookback_exceeded")
    return value


def _compile_node(
    node: QlibExpressionNode,
    *,
    bindings: dict[str, str],
    operators: dict[str, QlibOperatorDefinition],
) -> tuple[str, set[str], int]:
    if isinstance(node, QlibFeatureNode):
        try:
            field_name = bindings[node.feature_binding_id]
        except KeyError as exc:
            raise ValueError(f"unknown_feature_binding:{node.feature_binding_id}") from exc
        return f"${field_name}", {node.feature_binding_id}, 0
    if isinstance(node, QlibNumericNode):
        return canonical_qlib_numeric(node.value), set(), 0

    try:
        operator = operators[node.operator_id]
    except KeyError as exc:
        raise ValueError(f"unknown_operator:{node.operator_id}") from exc
    if len(node.arguments) != len(operator.argument_kinds):
        raise ValueError(f"invalid_operator_arity:{node.operator_id}")

    compiled_arguments: list[str] = []
    required_features: set[str] = set()
    child_lookbacks: list[int] = []
    integer_arguments: dict[int, int] = {}
    for index, (argument, argument_kind) in enumerate(zip(node.arguments, operator.argument_kinds, strict=True)):
        if argument_kind is QlibArgumentKind.POSITIVE_INTEGER:
            integer_arguments[index] = _positive_integer(argument)
        compiled, features, lookback = _compile_node(argument, bindings=bindings, operators=operators)
        compiled_arguments.append(compiled)
        required_features.update(features)
        child_lookbacks.append(lookback)

    if operator.lookback_rule is QlibLookbackRule.MAXIMUM_CHILD:
        lookback = max(child_lookbacks, default=0)
    elif operator.lookback_rule is QlibLookbackRule.CHILD_PLUS_POSITIVE_INTEGER:
        lookback = child_lookbacks[0] + integer_arguments[1]
    elif operator.lookback_rule is QlibLookbackRule.CHILD_PLUS_WINDOW_MINUS_ONE:
        lookback = child_lookbacks[0] + integer_arguments[1] - 1
    else:  # pragma: no cover - the contract enum and registry make this unreachable
        raise ValueError(f"unsupported_lookback_rule:{operator.lookback_rule}")
    if lookback > MAX_QLIB_LOOKBACK_SESSIONS:
        raise ValueError("maximum_lookback_exceeded")
    return f"{operator.qlib_symbol.value}({','.join(compiled_arguments)})", required_features, lookback


def compile_qlib_expression(
    definition: QlibFactorExpressionDefinition,
    registry: QlibOperatorRegistry,
) -> CompiledQlibExpression:
    """Compile an approved typed AST into one canonical Qlib field."""

    if not isinstance(definition, QlibFactorExpressionDefinition):
        raise TypeError("definition_must_be_typed_expression")
    if not isinstance(registry, QlibOperatorRegistry):
        raise TypeError("registry_must_be_typed_operator_registry")
    if definition.operator_registry_id != registry.operator_registry_id:
        raise ValueError("operator_registry_identity_mismatch")
    bindings = {binding.feature_binding_id: binding.qlib_field_name for binding in definition.feature_bindings}
    operators = {operator.operator_id: operator for operator in registry.operators}
    qlib_field, required_features, lookback = _compile_node(
        definition.root,
        bindings=bindings,
        operators=operators,
    )
    if required_features != set(bindings):
        raise ValueError("compiled_feature_binding_partition_mismatch")
    if lookback != definition.maximum_lookback_sessions:
        raise ValueError("declared_lookback_mismatch")
    return CompiledQlibExpression(
        expression_id=definition.expression_id,
        expression_semantic_sha256=definition.content_sha256,
        operator_registry_id=registry.operator_registry_id,
        operator_registry_sha256=registry.content_sha256,
        qlib_field=qlib_field,
        required_feature_binding_ids=tuple(sorted(required_features)),
        maximum_lookback_sessions=lookback,
    )


def compile_and_parse_qlib_expression(
    definition: QlibFactorExpressionDefinition,
    registry: QlibOperatorRegistry,
) -> tuple[CompiledQlibExpression, object]:
    """Compile trusted nodes first, then pass only that output to pinned Qlib."""

    compiled = compile_qlib_expression(definition, registry)
    from qlib.data.data import LocalExpressionProvider  # type: ignore[import-not-found]
    from qlib.data.ops import register_all_ops  # type: ignore[import-not-found]

    isolated_config = SimpleNamespace(custom_ops=None)
    register_all_ops(isolated_config)
    expression = LocalExpressionProvider(time2idx=False).get_expression_instance(compiled.qlib_field)
    return compiled, expression


def bind_qlib_expression_execution(
    compiled: CompiledQlibExpression,
    binding: QlibExpressionExecutionBinding,
) -> QlibExpressionExecutionEvidence:
    """Bind semantic compiler output to a separately versioned runtime."""

    return QlibExpressionExecutionEvidence(
        compiled_expression_id=compiled.compiled_expression_id,
        compiled_expression_sha256=compiled.content_sha256,
        execution_binding_id=binding.execution_binding_id,
        execution_binding_sha256=binding.content_sha256,
    )
