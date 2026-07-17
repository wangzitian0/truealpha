from datetime import UTC, datetime
from decimal import Decimal

import pytest
from factors.base.gross_profit_per_employee import (
    GPPE_EXPRESSION_DEFINITION,
    gross_profit_per_employee,
)
from factors.types import Fact

_AS_OF = datetime(2026, 7, 1, tzinfo=UTC)


def _fact(metric: str, value: str, confidence: str = "0.9") -> Fact:
    return Fact(entity_id="e1", metric=metric, value=Decimal(value), confidence=Decimal(confidence), as_of=_AS_OF)


def test_capital_adjusted_formula() -> None:
    facts = [
        _fact("gross_profit", "1000000"),
        _fact("total_assets", "2000000"),
        _fact("risk_free_rate", "0.05", confidence="1"),
        _fact("headcount", "500", confidence="0.8"),
    ]

    result = gross_profit_per_employee(facts, entity_id="e1", as_of=_AS_OF)

    assert result.value == Decimal("1800.00")
    assert result.confidence == Decimal("0.8")
    assert result.data_availability == "verified"
    assert result.flags == []


def test_applies_uniformly_to_financial_issuers() -> None:
    """v0 owner decision: the capital charge absorbs balance-sheet-heavy issuers
    like banks — no sector branch, same formula as any other issuer."""

    facts = [
        _fact("gross_profit", "50000000000"),
        _fact("total_assets", "3800000000000"),
        _fact("risk_free_rate", "0.05", confidence="1"),
        _fact("headcount", "250000", confidence="0.8"),
    ]

    result = gross_profit_per_employee(facts, entity_id="e1", as_of=_AS_OF)

    expected = (Decimal("50000000000") - Decimal("3800000000000") * Decimal("0.05")) / Decimal("250000")
    assert result.value == expected
    assert result.data_availability == "verified"


@pytest.mark.parametrize(
    "missing_metric,expected_flag",
    [
        ("gross_profit", "missing_gross_profit_fact"),
        ("total_assets", "missing_total_assets_fact"),
        ("risk_free_rate", "missing_risk_free_rate_parameter"),
        ("headcount", "missing_headcount_disclosure"),
    ],
)
def test_missing_required_input_never_silently_drops(missing_metric: str, expected_flag: str) -> None:
    all_metrics = {
        "gross_profit": "1000000",
        "total_assets": "2000000",
        "risk_free_rate": "0.05",
        "headcount": "500",
    }
    facts = [_fact(metric, value) for metric, value in all_metrics.items() if metric != missing_metric]

    result = gross_profit_per_employee(facts, entity_id="e1", as_of=_AS_OF)

    assert result.value is None
    assert result.confidence == Decimal(0)
    assert result.data_availability == "unverified"
    assert expected_flag in result.flags


def test_nonpositive_headcount_is_excluded_not_divided() -> None:
    facts = [
        _fact("gross_profit", "1000000"),
        _fact("total_assets", "2000000"),
        _fact("risk_free_rate", "0.05"),
        _fact("headcount", "0"),
    ]

    result = gross_profit_per_employee(facts, entity_id="e1", as_of=_AS_OF)

    assert result.value is None
    assert result.flags == ["nonpositive_headcount"]


def test_qlib_expression_reproduces_the_decimal_result() -> None:
    """Matrix-compatible cross-check: the pinned Qlib runtime must reproduce
    the same value as the native Decimal computation above (#26 acceptance
    style — independent oracle and Qlib adapter agree)."""

    qlib = pytest.importorskip("qlib")
    del qlib
    from datetime import date

    from factors.qlib_engine import BUILTIN_OPERATOR_REGISTRY, evaluate_expression
    from truealpha_contracts.qlib_expression import QlibExpressionExecutionBinding

    session = date(2026, 7, 1)
    panel = {
        "gross_profit": {"e1": (1_000_000.0,)},
        "total_assets": {"e1": (2_000_000.0,)},
        "risk_free_rate": {"e1": (0.05,)},
        "headcount": {"e1": (500.0,)},
    }
    binding = QlibExpressionExecutionBinding(
        version="0.9.7",
        release_commit="a" * 40,
        runtime_artifact_sha256="b" * 64,
        runtime_lock_sha256="c" * 64,
        adapter_id="factors.qlib_engine.test",
        adapter_implementation_sha256="d" * 64,
    )

    _, outputs, _ = evaluate_expression(
        GPPE_EXPRESSION_DEFINITION,
        BUILTIN_OPERATOR_REGISTRY,
        panel=panel,
        instruments=("e1",),
        sessions=(session,),
        execution_binding=binding,
    )

    assert outputs[("e1", session)] == pytest.approx(1800.0)
