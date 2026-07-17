"""Generic runner for typed Qlib factor expressions (init.md Section 7 modules 1-6).

Generalizes the compile-and-execute pattern proven by S8 (#189, the typed AST
and builtin operator registry) and S9 (#203, the first end-to-end factor
smoke): any `QlibFactorExpressionDefinition` composed from the builtin
`Add/Div/Mean/Mul/Ref/Sub` operators can be executed against an in-memory,
named-field feature panel through this module, instead of every factor
hand-rolling its own Qlib `FeatureProvider` adapter. Future base factors
(module 1-6) that are expressible as elementwise or windowed arithmetic over
PIT fields reuse this unchanged; only branching/bucketing logic (e.g. module 7
tier valuation) stays native Decimal Python in `factors.composite`.

This module never imports `qlib` at module scope — the dependency is isolated
in the workspace-excluded `libs/factors/qlib-runtime` project (S8 boundary,
init.md Section 7) and is only pulled in lazily inside function bodies so
importing `factors.qlib_engine` never requires the qlib runtime to be
installed.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import date
from typing import TYPE_CHECKING, Any, Protocol, cast

from truealpha_contracts.qlib_expression import (
    CompiledQlibExpression,
    QlibArgumentKind,
    QlibBuiltinSymbol,
    QlibExpressionExecutionBinding,
    QlibExpressionExecutionEvidence,
    QlibFactorExpressionDefinition,
    QlibLookbackRule,
    QlibNullBehavior,
    QlibOperatorDefinition,
    QlibOperatorRegistry,
)

from factors.batches.qlib_expression_contract_tiny.compiler import (
    bind_qlib_expression_execution,
    compile_and_parse_qlib_expression,
)

if TYPE_CHECKING:
    import pandas as pd

_QLIB_PROVIDER_LOCK = threading.Lock()


class _QlibExpression(Protocol):
    def load(self, instrument: str, start_index: int, end_index: int, freq: str) -> Any: ...

#: The one approved builtin operator registry. Any factor expression built
#: from these six operators (Add/Div/Mean/Mul/Ref/Sub) reuses this constant
#: rather than minting a new registry per factor.
BUILTIN_OPERATOR_REGISTRY = QlibOperatorRegistry(
    registry_version="1.0.0",
    operators=(
        QlibOperatorDefinition(
            operator_id="truealpha.qlib.add.v1",
            qlib_symbol=QlibBuiltinSymbol.ADD,
            argument_kinds=(QlibArgumentKind.EXPRESSION, QlibArgumentKind.EXPRESSION),
            null_behavior=QlibNullBehavior.PANDAS_IEEE,
            lookback_rule=QlibLookbackRule.MAXIMUM_CHILD,
            semantic_spec="truealpha.qlib.add.v1|Add|expression,expression|pandas_ieee|lookback=max(children)",
        ),
        QlibOperatorDefinition(
            operator_id="truealpha.qlib.div.v1",
            qlib_symbol=QlibBuiltinSymbol.DIV,
            argument_kinds=(QlibArgumentKind.EXPRESSION, QlibArgumentKind.EXPRESSION),
            null_behavior=QlibNullBehavior.PANDAS_IEEE,
            lookback_rule=QlibLookbackRule.MAXIMUM_CHILD,
            semantic_spec="truealpha.qlib.div.v1|Div|expression,expression|pandas_ieee|lookback=max(children)",
        ),
        QlibOperatorDefinition(
            operator_id="truealpha.qlib.mean.v1",
            qlib_symbol=QlibBuiltinSymbol.MEAN,
            argument_kinds=(QlibArgumentKind.EXPRESSION, QlibArgumentKind.POSITIVE_INTEGER),
            null_behavior=QlibNullBehavior.SKIP_NULL_MIN_PERIODS_ONE,
            lookback_rule=QlibLookbackRule.CHILD_PLUS_WINDOW_MINUS_ONE,
            semantic_spec=(
                "truealpha.qlib.mean.v1|Mean|expression,positive_integer|skip_null_min_periods_one"
                "|lookback=child+n-1"
            ),
        ),
        QlibOperatorDefinition(
            operator_id="truealpha.qlib.mul.v1",
            qlib_symbol=QlibBuiltinSymbol.MUL,
            argument_kinds=(QlibArgumentKind.EXPRESSION, QlibArgumentKind.EXPRESSION),
            null_behavior=QlibNullBehavior.PANDAS_IEEE,
            lookback_rule=QlibLookbackRule.MAXIMUM_CHILD,
            semantic_spec="truealpha.qlib.mul.v1|Mul|expression,expression|pandas_ieee|lookback=max(children)",
        ),
        QlibOperatorDefinition(
            operator_id="truealpha.qlib.ref.v1",
            qlib_symbol=QlibBuiltinSymbol.REF,
            argument_kinds=(QlibArgumentKind.EXPRESSION, QlibArgumentKind.POSITIVE_INTEGER),
            null_behavior=QlibNullBehavior.SHIFT_PRESERVE_NULL,
            lookback_rule=QlibLookbackRule.CHILD_PLUS_POSITIVE_INTEGER,
            semantic_spec="truealpha.qlib.ref.v1|Ref|expression,positive_integer|shift_preserve_null|lookback=child+n",
        ),
        QlibOperatorDefinition(
            operator_id="truealpha.qlib.sub.v1",
            qlib_symbol=QlibBuiltinSymbol.SUB,
            argument_kinds=(QlibArgumentKind.EXPRESSION, QlibArgumentKind.EXPRESSION),
            null_behavior=QlibNullBehavior.PANDAS_IEEE,
            lookback_rule=QlibLookbackRule.MAXIMUM_CHILD,
            semantic_spec="truealpha.qlib.sub.v1|Sub|expression,expression|pandas_ieee|lookback=max(children)",
        ),
    ),
)


@contextmanager
def _feature_provider(panel: Mapping[str, Mapping[str, tuple[float, ...]]]) -> Iterator[None]:
    """Register a temporary Qlib FeatureProvider serving only the given panel.

    `panel` maps qlib field name -> instrument -> session-aligned float tuple.
    Generalizes S9's `_truealpha_feature_provider`, which served exactly one
    hardcoded field (`$pe`) for one hardcoded instrument set.
    """

    import pandas as pd
    from qlib.data.cache import H  # type: ignore[import-not-found]
    from qlib.data.data import FeatureD, FeatureProvider  # type: ignore[import-not-found]

    class _PanelFeatureProvider(FeatureProvider):
        def feature(
            self,
            instrument: str,
            field: str,
            start_time: int,
            end_time: int,
            freq: str,
        ) -> pd.Series:
            field_name = field.removeprefix("$")
            if freq != "day" or field_name not in panel or instrument not in panel[field_name]:
                raise ValueError("qlib_provider_request_outside_projected_panel")
            values = panel[field_name][instrument]
            return pd.Series(values, dtype="float64").iloc[start_time : end_time + 1]

    with _QLIB_PROVIDER_LOCK:
        original = FeatureD._provider
        H["f"].clear()
        FeatureD.register(_PanelFeatureProvider())
        try:
            yield
        finally:
            H["f"].clear()
            FeatureD.register(original)


def evaluate_expression(
    definition: QlibFactorExpressionDefinition,
    registry: QlibOperatorRegistry,
    *,
    panel: Mapping[str, Mapping[str, tuple[float, ...]]],
    instruments: Sequence[str],
    sessions: Sequence[date],
    execution_binding: QlibExpressionExecutionBinding,
) -> tuple[CompiledQlibExpression, dict[tuple[str, date], float], QlibExpressionExecutionEvidence]:
    """Compile a typed expression and execute it over an in-memory feature panel.

    Requires the pinned Qlib runtime (`libs/factors/qlib-runtime`) to be
    installed; callers that only need the Decimal-native computation should
    not call this. Returns the compiled expression, per-(instrument, session)
    float64 outputs, and execution evidence binding the compile output to the
    pinned runtime — the same evidence shape S9 produced for one factor.
    """

    compiled, expression = compile_and_parse_qlib_expression(definition, registry)
    execution_evidence = bind_qlib_expression_execution(compiled, execution_binding)
    runtime_expression = cast(_QlibExpression, expression)
    outputs: dict[tuple[str, date], float] = {}
    with _feature_provider(panel):
        from qlib.data.cache import H  # type: ignore[import-not-found]

        for instrument in instruments:
            H["f"].clear()
            values = runtime_expression.load(instrument, 0, len(sessions) - 1, "day").tolist()
            if len(values) != len(sessions):
                raise ValueError("qlib_output_denominator_mismatch")
            outputs.update(zip(((instrument, session) for session in sessions), values, strict=True))
    return compiled, outputs, execution_evidence
