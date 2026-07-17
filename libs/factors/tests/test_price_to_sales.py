from datetime import UTC, datetime
from decimal import Decimal

import pytest
from factors.base.price_to_sales import PRICE_TO_SALES_EXPRESSION_DEFINITION, price_to_sales
from factors.types import Fact, UnitFamily

_AS_OF = datetime(2026, 7, 1, tzinfo=UTC)
_UNIT_FAMILY = {
    "price": UnitFamily.PER_SHARE,
    "shares_outstanding": UnitFamily.COUNT,
    "revenue": UnitFamily.CURRENCY,
}


def _fact(metric: str, value: str, confidence: str = "0.9") -> Fact:
    return Fact(
        entity_id="e1",
        metric=metric,
        value=Decimal(value),
        unit_family=_UNIT_FAMILY[metric],
        confidence=Decimal(confidence),
        as_of=_AS_OF,
    )


def test_price_to_sales_ratio() -> None:
    facts = [
        _fact("price", "50"),
        _fact("shares_outstanding", "1000000", confidence="0.8"),
        _fact("revenue", "20000000", confidence="0.95"),
    ]

    result = price_to_sales(facts, entity_id="e1", as_of=_AS_OF)

    # market_cap = 50 * 1,000,000 = 50,000,000; P/S = 50,000,000 / 20,000,000 = 2.5
    assert result.value == Decimal("2.5")
    assert result.unit_family == UnitFamily.RATIO
    assert result.confidence == Decimal("0.8")
    assert result.data_availability == "unverified"
    assert result.flags == []


def test_duplicate_fact_for_the_same_metric_fails_closed() -> None:
    facts = [
        _fact("price", "50"),
        _fact("price", "51"),
        _fact("shares_outstanding", "1000000"),
        _fact("revenue", "20000000"),
    ]

    with pytest.raises(ValueError, match="multiple PIT-resolved facts"):
        price_to_sales(facts, entity_id="e1", as_of=_AS_OF)


@pytest.mark.parametrize(
    "missing_metric",
    ["price", "shares_outstanding", "revenue"],
)
def test_missing_required_input_never_silently_drops(missing_metric: str) -> None:
    all_metrics = {"price": "50", "shares_outstanding": "1000000", "revenue": "20000000"}
    facts = [_fact(metric, value) for metric, value in all_metrics.items() if metric != missing_metric]

    result = price_to_sales(facts, entity_id="e1", as_of=_AS_OF)

    assert result.value is None
    assert result.confidence == Decimal(0)
    assert result.data_availability == "unverified"
    assert result.flags


def test_nonpositive_revenue_is_excluded_not_divided() -> None:
    facts = [
        _fact("price", "50"),
        _fact("shares_outstanding", "1000000"),
        _fact("revenue", "0"),
    ]

    result = price_to_sales(facts, entity_id="e1", as_of=_AS_OF)

    assert result.value is None
    assert result.flags == ["nonpositive_revenue"]


def test_qlib_expression_reproduces_the_decimal_result() -> None:
    """Matrix-compatible cross-check: the pinned Qlib runtime must reproduce
    the same value as the native Decimal computation above (#21 criterion 3
    — independent oracle and Qlib adapter agree)."""

    qlib = pytest.importorskip("qlib")
    del qlib
    from datetime import date

    from factors.qlib_engine import BUILTIN_OPERATOR_REGISTRY, evaluate_expression
    from truealpha_contracts.qlib_expression import QlibExpressionExecutionBinding

    session = date(2026, 6, 30)
    panel = {
        "price": {"e1": (50.0,)},
        "shares_outstanding": {"e1": (1_000_000.0,)},
        "revenue": {"e1": (20_000_000.0,)},
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
        PRICE_TO_SALES_EXPRESSION_DEFINITION,
        BUILTIN_OPERATOR_REGISTRY,
        panel=panel,
        instruments=("e1",),
        sessions=(session,),
        execution_binding=binding,
    )

    # market_cap = 50 * 1,000,000 = 50,000,000; P/S = 50,000,000 / 20,000,000 = 2.5
    assert outputs[("e1", session)] == pytest.approx(2.5)
