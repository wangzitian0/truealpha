"""Module 2: gross profit per employee (capital-adjusted, v0).

v0 formula locked by the 2026-07-17 owner decision on #59/#21/#24:

    capital_adjusted_gppe = (gross_profit - total_assets * risk_free_rate) / headcount

applied uniformly to financial and non-financial issuers — the capital charge
(``total_assets * risk_free_rate``) absorbs balance-sheet-heavy issuers like
banks, so v0 needs no sector branch. See
``truealpha_contracts.strategy.CapitalAdjustedLaborEfficiencyDefinition`` for
the versioned parameter contract this mirrors.

The same formula is also expressed as a matrix-compatible Qlib expression
(`GPPE_EXPRESSION_DEFINITION`) built only from the approved
Add/Div/Mean/Mul/Ref/Sub operators, so a pinned-Qlib execution through
`factors.qlib_engine` reproduces this function's Decimal output — proven by
the cross-check test, not invoked on every call (the Decimal path below is
the fast, dependency-light source of truth; Qlib execution is the
reproducibility proof).
"""

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from truealpha_contracts.qlib_expression import (
    QlibCallNode,
    QlibFactorExpressionDefinition,
    QlibFeatureBinding,
    QlibFeatureNode,
)

from factors.qlib_engine import BUILTIN_OPERATOR_REGISTRY
from factors.registry import factor
from factors.types import Fact, FactorResult

_REQUIRED_METRICS = ("gross_profit", "total_assets", "risk_free_rate", "headcount")

GPPE_EXPRESSION_DEFINITION = QlibFactorExpressionDefinition(
    factor_id="factor.gross_profit_per_employee.capital_adjusted",
    factor_version="0.1.0",
    operator_registry_id=BUILTIN_OPERATOR_REGISTRY.operator_registry_id,
    feature_bindings=(
        QlibFeatureBinding(feature_binding_id="feature.gross_profit", qlib_field_name="gross_profit"),
        QlibFeatureBinding(feature_binding_id="feature.total_assets", qlib_field_name="total_assets"),
        QlibFeatureBinding(feature_binding_id="feature.risk_free_rate", qlib_field_name="risk_free_rate"),
        QlibFeatureBinding(feature_binding_id="feature.headcount", qlib_field_name="headcount"),
    ),
    root=QlibCallNode(
        operator_id="truealpha.qlib.div.v1",
        arguments=(
            QlibCallNode(
                operator_id="truealpha.qlib.sub.v1",
                arguments=(
                    QlibFeatureNode(feature_binding_id="feature.gross_profit"),
                    QlibCallNode(
                        operator_id="truealpha.qlib.mul.v1",
                        arguments=(
                            QlibFeatureNode(feature_binding_id="feature.total_assets"),
                            QlibFeatureNode(feature_binding_id="feature.risk_free_rate"),
                        ),
                    ),
                ),
            ),
            QlibFeatureNode(feature_binding_id="feature.headcount"),
        ),
    ),
    maximum_lookback_sessions=0,
)


def _missing_reason(metric: str) -> str:
    return {
        "gross_profit": "missing_gross_profit_fact",
        "total_assets": "missing_total_assets_fact",
        "risk_free_rate": "missing_risk_free_rate_parameter",
        "headcount": "missing_headcount_disclosure",
    }[metric]


@factor("gross_profit_per_employee", kind="base", module=2)
def gross_profit_per_employee(
    facts: Sequence[Fact],
    *,
    entity_id: str,
    as_of: datetime,
) -> FactorResult:
    by_metric = {fact.metric: fact for fact in facts if fact.entity_id == entity_id}
    missing = [
        metric for metric in _REQUIRED_METRICS if by_metric.get(metric) is None or by_metric[metric].value is None
    ]
    if missing:
        return FactorResult(
            factor="gross_profit_per_employee",
            entity_id=entity_id,
            value=None,
            confidence=Decimal(0),
            as_of=as_of,
            data_availability="unverified",
            flags=[_missing_reason(metric) for metric in missing],
        )

    headcount = by_metric["headcount"].value
    assert headcount is not None
    if headcount <= 0:
        return FactorResult(
            factor="gross_profit_per_employee",
            entity_id=entity_id,
            value=None,
            confidence=Decimal(0),
            as_of=as_of,
            data_availability="unverified",
            flags=["nonpositive_headcount"],
        )

    gross_profit = by_metric["gross_profit"].value
    total_assets = by_metric["total_assets"].value
    risk_free_rate = by_metric["risk_free_rate"].value
    assert gross_profit is not None
    assert total_assets is not None
    assert risk_free_rate is not None

    value = (gross_profit - total_assets * risk_free_rate) / headcount
    confidence = min(by_metric[metric].confidence for metric in _REQUIRED_METRICS)
    return FactorResult(
        factor="gross_profit_per_employee",
        entity_id=entity_id,
        value=value,
        confidence=confidence,
        as_of=as_of,
        data_availability="verified",
    )
