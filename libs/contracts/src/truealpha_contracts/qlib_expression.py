"""Qlib-independent contracts for safe, versioned factor expressions."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from enum import StrEnum
from hashlib import sha256
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from truealpha_contracts.common import canonical_sha256

MAX_QLIB_AST_DEPTH = 8
MAX_QLIB_AST_NODES = 64
MAX_QLIB_FEATURE_BINDINGS = 32
MAX_QLIB_LOOKBACK_SESSIONS = 252
MAX_QLIB_NUMERIC_CHARACTERS = 128

_CONTRACT_ID_PATTERN = r"^[a-z][a-z0-9._-]{0,127}$"
_QLIB_FIELD_PATTERN = r"^[a-z][a-z0-9_]{0,62}$"
_SEMVER_PATTERN = r"^[0-9]+\.[0-9]+\.[0-9]+$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class QlibArgumentKind(StrEnum):
    EXPRESSION = "expression"
    POSITIVE_INTEGER = "positive_integer"


class QlibBuiltinSymbol(StrEnum):
    ADD = "Add"
    DIV = "Div"
    MEAN = "Mean"
    MUL = "Mul"
    REF = "Ref"
    SUB = "Sub"


class QlibNullBehavior(StrEnum):
    PANDAS_IEEE = "pandas_ieee"
    SHIFT_PRESERVE_NULL = "shift_preserve_null"
    SKIP_NULL_MIN_PERIODS_ONE = "skip_null_min_periods_one"


class QlibLookbackRule(StrEnum):
    MAXIMUM_CHILD = "maximum_child"
    CHILD_PLUS_POSITIVE_INTEGER = "child_plus_positive_integer"
    CHILD_PLUS_WINDOW_MINUS_ONE = "child_plus_window_minus_one"


class QlibFeatureBinding(_StrictFrozenModel):
    feature_binding_id: str = Field(pattern=_CONTRACT_ID_PATTERN)
    qlib_field_name: str = Field(pattern=_QLIB_FIELD_PATTERN)


class QlibFeatureNode(_StrictFrozenModel):
    kind: Literal["feature"] = "feature"
    feature_binding_id: str = Field(pattern=_CONTRACT_ID_PATTERN)


def canonical_qlib_numeric(value: Decimal) -> str:
    """Render a finite Decimal without consulting the ambient Decimal context."""

    if not value.is_finite():
        raise ValueError("non_finite_numeric")
    if value.is_zero():
        return "0"
    sign, digits_tuple, exponent = value.as_tuple()
    if not isinstance(exponent, int):  # guarded by is_finite; retained for static exhaustiveness
        raise ValueError("non_finite_numeric")
    digits = "".join(str(digit) for digit in digits_tuple)
    split = len(digits) + exponent
    unsigned_length = (
        len(digits) + exponent if exponent >= 0 else len(digits) + 1 if split > 0 else 2 + (-split) + len(digits)
    )
    if unsigned_length + sign > MAX_QLIB_NUMERIC_CHARACTERS:
        raise ValueError("numeric_literal_too_long")
    if exponent >= 0:
        rendered = digits + ("0" * exponent)
    else:
        rendered = f"{digits[:split]}.{digits[split:]}" if split > 0 else f"0.{('0' * -split)}{digits}"
        rendered = rendered.rstrip("0").rstrip(".")
    return f"-{rendered}" if sign else rendered


class QlibNumericNode(_StrictFrozenModel):
    kind: Literal["numeric"] = "numeric"
    value: Decimal

    @field_validator("value", mode="before")
    @classmethod
    def reject_binary_float(cls, value: object) -> object:
        if isinstance(value, bool) or isinstance(value, float):
            raise ValueError("numeric_must_use_decimal_text_or_integer")
        if isinstance(value, (Decimal, int, str)):
            try:
                parsed = Decimal(value)
            except (InvalidOperation, ValueError):
                return value
            if not parsed.is_finite():
                raise ValueError("non_finite_numeric")
            return parsed
        return value

    @field_validator("value")
    @classmethod
    def canonicalize_value(cls, value: Decimal) -> Decimal:
        return Decimal(canonical_qlib_numeric(value))


class QlibCallNode(_StrictFrozenModel):
    kind: Literal["call"] = "call"
    operator_id: str = Field(pattern=_CONTRACT_ID_PATTERN)
    arguments: tuple[Annotated[QlibFeatureNode | QlibNumericNode | QlibCallNode, Field(discriminator="kind")], ...]


QlibExpressionNode = Annotated[
    QlibFeatureNode | QlibNumericNode | QlibCallNode,
    Field(discriminator="kind"),
]


_BUILTIN_SEMANTICS: dict[
    QlibBuiltinSymbol,
    tuple[
        str,
        tuple[QlibArgumentKind, ...],
        QlibNullBehavior,
        QlibLookbackRule,
        str,
    ],
] = {
    QlibBuiltinSymbol.ADD: (
        "truealpha.qlib.add.v1",
        (QlibArgumentKind.EXPRESSION, QlibArgumentKind.EXPRESSION),
        QlibNullBehavior.PANDAS_IEEE,
        QlibLookbackRule.MAXIMUM_CHILD,
        "truealpha.qlib.add.v1|Add|expression,expression|pandas_ieee|lookback=max(children)",
    ),
    QlibBuiltinSymbol.DIV: (
        "truealpha.qlib.div.v1",
        (QlibArgumentKind.EXPRESSION, QlibArgumentKind.EXPRESSION),
        QlibNullBehavior.PANDAS_IEEE,
        QlibLookbackRule.MAXIMUM_CHILD,
        "truealpha.qlib.div.v1|Div|expression,expression|pandas_ieee|lookback=max(children)",
    ),
    QlibBuiltinSymbol.MEAN: (
        "truealpha.qlib.mean.v1",
        (QlibArgumentKind.EXPRESSION, QlibArgumentKind.POSITIVE_INTEGER),
        QlibNullBehavior.SKIP_NULL_MIN_PERIODS_ONE,
        QlibLookbackRule.CHILD_PLUS_WINDOW_MINUS_ONE,
        "truealpha.qlib.mean.v1|Mean|expression,positive_integer|skip_null_min_periods_one|lookback=child+n-1",
    ),
    QlibBuiltinSymbol.MUL: (
        "truealpha.qlib.mul.v1",
        (QlibArgumentKind.EXPRESSION, QlibArgumentKind.EXPRESSION),
        QlibNullBehavior.PANDAS_IEEE,
        QlibLookbackRule.MAXIMUM_CHILD,
        "truealpha.qlib.mul.v1|Mul|expression,expression|pandas_ieee|lookback=max(children)",
    ),
    QlibBuiltinSymbol.REF: (
        "truealpha.qlib.ref.v1",
        (QlibArgumentKind.EXPRESSION, QlibArgumentKind.POSITIVE_INTEGER),
        QlibNullBehavior.SHIFT_PRESERVE_NULL,
        QlibLookbackRule.CHILD_PLUS_POSITIVE_INTEGER,
        "truealpha.qlib.ref.v1|Ref|expression,positive_integer|shift_preserve_null|lookback=child+n",
    ),
    QlibBuiltinSymbol.SUB: (
        "truealpha.qlib.sub.v1",
        (QlibArgumentKind.EXPRESSION, QlibArgumentKind.EXPRESSION),
        QlibNullBehavior.PANDAS_IEEE,
        QlibLookbackRule.MAXIMUM_CHILD,
        "truealpha.qlib.sub.v1|Sub|expression,expression|pandas_ieee|lookback=max(children)",
    ),
}


class QlibOperatorDefinition(_StrictFrozenModel):
    operator_id: str = Field(pattern=_CONTRACT_ID_PATTERN)
    qlib_symbol: QlibBuiltinSymbol
    argument_kinds: tuple[QlibArgumentKind, ...]
    null_behavior: QlibNullBehavior
    lookback_rule: QlibLookbackRule
    semantic_spec: str = Field(min_length=1, max_length=512)
    semantic_spec_sha256: str = Field(default="", pattern=rf"^(?:|{_SHA256_PATTERN[1:-1]})$")
    builtin_only: Literal[True] = True

    @model_validator(mode="after")
    def bind_approved_builtin_semantics(self) -> Self:
        expected_id, expected_args, expected_null, expected_lookback, expected_spec = _BUILTIN_SEMANTICS[
            self.qlib_symbol
        ]
        actual = (
            self.operator_id,
            self.argument_kinds,
            self.null_behavior,
            self.lookback_rule,
            self.semantic_spec,
        )
        expected = (expected_id, expected_args, expected_null, expected_lookback, expected_spec)
        if actual != expected:
            raise ValueError("unapproved_builtin_semantics")
        digest = sha256(self.semantic_spec.encode()).hexdigest()
        if self.semantic_spec_sha256 and self.semantic_spec_sha256 != digest:
            raise ValueError("semantic_spec_hash_mismatch")
        object.__setattr__(self, "semantic_spec_sha256", digest)
        return self


class QlibOperatorRegistry(_StrictFrozenModel):
    operator_registry_id: str = Field(default="", pattern=r"^(?:|qlib-operator-registry:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=rf"^(?:|{_SHA256_PATTERN[1:-1]})$")
    registry_version: str = Field(pattern=_SEMVER_PATTERN)
    builtin_only: Literal[True] = True
    operators: tuple[QlibOperatorDefinition, ...] = Field(
        min_length=len(QlibBuiltinSymbol), max_length=len(QlibBuiltinSymbol)
    )

    @model_validator(mode="after")
    def canonicalize_and_identify(self) -> Self:
        operator_ids = [operator.operator_id for operator in self.operators]
        symbols = [operator.qlib_symbol for operator in self.operators]
        if len(operator_ids) != len(set(operator_ids)):
            raise ValueError("duplicate_operator_id")
        if len(symbols) != len(set(symbols)):
            raise ValueError("duplicate_qlib_symbol")
        if set(symbols) != set(QlibBuiltinSymbol):
            raise ValueError("incomplete_builtin_operator_registry")
        ordered = tuple(sorted(self.operators, key=lambda operator: operator.operator_id))
        object.__setattr__(self, "operators", ordered)
        payload = self.model_dump(mode="json", exclude={"operator_registry_id", "content_sha256"})
        digest = canonical_sha256(payload)
        expected_id = f"qlib-operator-registry:{digest}"
        if self.content_sha256 and self.content_sha256 != digest:
            raise ValueError("registry_content_hash_mismatch")
        if self.operator_registry_id and self.operator_registry_id != expected_id:
            raise ValueError("operator_registry_id_mismatch")
        object.__setattr__(self, "content_sha256", digest)
        object.__setattr__(self, "operator_registry_id", expected_id)
        return self


def _expression_shape(node: QlibExpressionNode) -> tuple[int, int]:
    if not isinstance(node, QlibCallNode):
        return 1, 1
    children = tuple(_expression_shape(argument) for argument in node.arguments)
    depth = 1 + max((child[0] for child in children), default=0)
    nodes = 1 + sum(child[1] for child in children)
    return depth, nodes


def _referenced_feature_ids(node: QlibExpressionNode) -> set[str]:
    if isinstance(node, QlibFeatureNode):
        return {node.feature_binding_id}
    if isinstance(node, QlibNumericNode):
        return set()
    result: set[str] = set()
    for argument in node.arguments:
        result.update(_referenced_feature_ids(argument))
    return result


class QlibFactorExpressionDefinition(_StrictFrozenModel):
    expression_id: str = Field(default="", pattern=r"^(?:|qlib-factor-expression:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=rf"^(?:|{_SHA256_PATTERN[1:-1]})$")
    factor_id: str = Field(pattern=_CONTRACT_ID_PATTERN)
    factor_version: str = Field(pattern=_SEMVER_PATTERN)
    operator_registry_id: str = Field(pattern=r"^qlib-operator-registry:[0-9a-f]{64}$")
    feature_bindings: tuple[QlibFeatureBinding, ...] = Field(min_length=1, max_length=MAX_QLIB_FEATURE_BINDINGS)
    root: QlibExpressionNode
    output_kind: Literal["dimensionless_float64"] = "dimensionless_float64"
    null_policy: Literal["operator_declared"] = "operator_declared"
    maximum_lookback_sessions: int = Field(ge=0, le=MAX_QLIB_LOOKBACK_SESSIONS)

    @model_validator(mode="after")
    def validate_and_identify(self) -> Self:
        binding_ids = [binding.feature_binding_id for binding in self.feature_bindings]
        field_names = [binding.qlib_field_name for binding in self.feature_bindings]
        if len(binding_ids) != len(set(binding_ids)):
            raise ValueError("duplicate_feature_binding_id")
        if len(field_names) != len(set(field_names)):
            raise ValueError("duplicate_qlib_field_name")
        referenced = _referenced_feature_ids(self.root)
        unknown = referenced - set(binding_ids)
        if unknown:
            raise ValueError(f"unknown_feature_binding:{sorted(unknown)!r}")
        unused = set(binding_ids) - referenced
        if unused:
            raise ValueError(f"unused_feature_binding:{sorted(unused)!r}")
        depth, nodes = _expression_shape(self.root)
        if depth > MAX_QLIB_AST_DEPTH:
            raise ValueError("maximum_ast_depth_exceeded")
        if nodes > MAX_QLIB_AST_NODES:
            raise ValueError("maximum_ast_nodes_exceeded")
        ordered = tuple(sorted(self.feature_bindings, key=lambda binding: binding.feature_binding_id))
        object.__setattr__(self, "feature_bindings", ordered)
        payload = self.model_dump(mode="json", exclude={"expression_id", "content_sha256"})
        digest = canonical_sha256(payload)
        expected_id = f"qlib-factor-expression:{digest}"
        if self.content_sha256 and self.content_sha256 != digest:
            raise ValueError("expression_content_hash_mismatch")
        if self.expression_id and self.expression_id != expected_id:
            raise ValueError("expression_id_mismatch")
        object.__setattr__(self, "content_sha256", digest)
        object.__setattr__(self, "expression_id", expected_id)
        return self


class CompiledQlibExpression(_StrictFrozenModel):
    compiled_expression_id: str = Field(default="", pattern=r"^(?:|compiled-qlib-expression:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=rf"^(?:|{_SHA256_PATTERN[1:-1]})$")
    expression_id: str = Field(pattern=r"^qlib-factor-expression:[0-9a-f]{64}$")
    expression_semantic_sha256: str = Field(pattern=_SHA256_PATTERN)
    operator_registry_id: str = Field(pattern=r"^qlib-operator-registry:[0-9a-f]{64}$")
    operator_registry_sha256: str = Field(pattern=_SHA256_PATTERN)
    qlib_field: str = Field(min_length=1, max_length=4096)
    required_feature_binding_ids: tuple[str, ...] = Field(min_length=1, max_length=MAX_QLIB_FEATURE_BINDINGS)
    maximum_lookback_sessions: int = Field(ge=0, le=MAX_QLIB_LOOKBACK_SESSIONS)

    @model_validator(mode="after")
    def identify(self) -> Self:
        if tuple(sorted(set(self.required_feature_binding_ids))) != self.required_feature_binding_ids:
            raise ValueError("required_feature_binding_ids_not_canonical")
        if self.expression_id != f"qlib-factor-expression:{self.expression_semantic_sha256}":
            raise ValueError("compiled_expression_semantic_reference_mismatch")
        if self.operator_registry_id != f"qlib-operator-registry:{self.operator_registry_sha256}":
            raise ValueError("compiled_expression_registry_reference_mismatch")
        payload = self.model_dump(mode="json", exclude={"compiled_expression_id", "content_sha256"})
        digest = canonical_sha256(payload)
        expected_id = f"compiled-qlib-expression:{digest}"
        if self.content_sha256 and self.content_sha256 != digest:
            raise ValueError("compiled_expression_content_hash_mismatch")
        if self.compiled_expression_id and self.compiled_expression_id != expected_id:
            raise ValueError("compiled_expression_id_mismatch")
        object.__setattr__(self, "content_sha256", digest)
        object.__setattr__(self, "compiled_expression_id", expected_id)
        return self


class QlibExpressionExecutionBinding(_StrictFrozenModel):
    execution_binding_id: str = Field(default="", pattern=r"^(?:|qlib-expression-execution-binding:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=rf"^(?:|{_SHA256_PATTERN[1:-1]})$")
    distribution: Literal["pyqlib"] = "pyqlib"
    version: str = Field(pattern=_SEMVER_PATTERN)
    release_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    runtime_artifact_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_lock_sha256: str = Field(pattern=_SHA256_PATTERN)
    adapter_id: str = Field(pattern=_CONTRACT_ID_PATTERN)
    adapter_implementation_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def identify(self) -> Self:
        payload = self.model_dump(mode="json", exclude={"execution_binding_id", "content_sha256"})
        digest = canonical_sha256(payload)
        expected_id = f"qlib-expression-execution-binding:{digest}"
        if self.content_sha256 and self.content_sha256 != digest:
            raise ValueError("execution_binding_content_hash_mismatch")
        if self.execution_binding_id and self.execution_binding_id != expected_id:
            raise ValueError("execution_binding_id_mismatch")
        object.__setattr__(self, "content_sha256", digest)
        object.__setattr__(self, "execution_binding_id", expected_id)
        return self


class QlibExpressionExecutionEvidence(_StrictFrozenModel):
    execution_evidence_id: str = Field(default="", pattern=r"^(?:|qlib-expression-execution:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=rf"^(?:|{_SHA256_PATTERN[1:-1]})$")
    compiled_expression_id: str = Field(pattern=r"^compiled-qlib-expression:[0-9a-f]{64}$")
    compiled_expression_sha256: str = Field(pattern=_SHA256_PATTERN)
    execution_binding_id: str = Field(pattern=r"^qlib-expression-execution-binding:[0-9a-f]{64}$")
    execution_binding_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def identify(self) -> Self:
        if self.compiled_expression_id != f"compiled-qlib-expression:{self.compiled_expression_sha256}":
            raise ValueError("execution_compiled_reference_mismatch")
        if self.execution_binding_id != f"qlib-expression-execution-binding:{self.execution_binding_sha256}":
            raise ValueError("execution_binding_reference_mismatch")
        payload = self.model_dump(mode="json", exclude={"execution_evidence_id", "content_sha256"})
        digest = canonical_sha256(payload)
        expected_id = f"qlib-expression-execution:{digest}"
        if self.content_sha256 and self.content_sha256 != digest:
            raise ValueError("execution_evidence_content_hash_mismatch")
        if self.execution_evidence_id and self.execution_evidence_id != expected_id:
            raise ValueError("execution_evidence_id_mismatch")
        object.__setattr__(self, "content_sha256", digest)
        object.__setattr__(self, "execution_evidence_id", expected_id)
        return self
