from datetime import UTC, date, datetime
from decimal import Decimal

import polars as pl
import pytest
from factors import FACTOR_REGISTRY
from factors.base.price_to_gross_profit import PRICE_TO_GROSS_PROFIT_EXPRESSION_DEFINITION, price_to_gross_profit
from factors.expressions.compiler import compile_expression
from factors.types import Fact, UnitFamily

_AS_OF = datetime(2026, 10, 8, tzinfo=UTC)
_UNIT_FAMILY = {
    "price": UnitFamily.PER_SHARE,
    "shares_outstanding": UnitFamily.COUNT,
    "gross_profit": UnitFamily.CURRENCY,
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


def test_price_to_gross_profit_ratio() -> None:
    facts = [
        _fact("price", "50"),
        _fact("shares_outstanding", "1000000", confidence="0.8"),
        _fact("gross_profit", "4000000", confidence="0.95"),
    ]

    result = price_to_gross_profit(facts, entity_id="e1", as_of=_AS_OF)

    # market value = 50 * 1,000,000 = 50,000,000; P/GP = 50,000,000 / 4,000,000 = 12.5
    assert result.factor == "price_to_gross_profit"
    assert result.value == Decimal("12.5")
    assert result.unit_family == UnitFamily.RATIO
    assert result.confidence == Decimal("0.8")
    assert result.flags == []


def test_it_is_a_module_7_feeder_that_declares_its_metrics() -> None:
    spec = FACTOR_REGISTRY["price_to_gross_profit"]
    assert (spec.kind, spec.module) == ("feeder", 7)
    assert spec.inputs == ("price", "shares_outstanding", "gross_profit")


@pytest.mark.parametrize(
    ("missing_metric", "flag"),
    [
        ("price", "missing_market_value_input"),
        ("shares_outstanding", "missing_market_value_input"),
        ("gross_profit", "missing_gross_profit_fact"),
    ],
)
def test_a_missing_input_is_named_not_dropped(missing_metric: str, flag: str) -> None:
    values = {"price": "50", "shares_outstanding": "1000000", "gross_profit": "4000000"}
    facts = [_fact(metric, value) for metric, value in values.items() if metric != missing_metric]

    result = price_to_gross_profit(facts, entity_id="e1", as_of=_AS_OF)

    assert result.value is None
    assert result.confidence == Decimal(0)
    assert result.flags == [flag]


@pytest.mark.parametrize("gross_profit", ["0", "-1"])
def test_nonpositive_gross_profit_is_refused_not_divided(gross_profit: str) -> None:
    facts = [_fact("price", "50"), _fact("shares_outstanding", "1000000"), _fact("gross_profit", gross_profit)]

    result = price_to_gross_profit(facts, entity_id="e1", as_of=_AS_OF)

    assert result.value is None
    assert result.flags == ["nonpositive_gross_profit"]


def test_nonpositive_market_value_is_refused() -> None:
    facts = [_fact("price", "0"), _fact("shares_outstanding", "1000000"), _fact("gross_profit", "4000000")]

    result = price_to_gross_profit(facts, entity_id="e1", as_of=_AS_OF)

    assert result.value is None
    assert result.flags == ["nonpositive_market_value"]


def test_two_facts_for_one_metric_fail_closed() -> None:
    facts = [
        _fact("price", "50"),
        _fact("shares_outstanding", "1000000"),
        _fact("gross_profit", "4000000"),
        _fact("gross_profit", "4100000"),
    ]
    with pytest.raises(ValueError, match="multiple PIT-resolved facts"):
        price_to_gross_profit(facts, entity_id="e1", as_of=_AS_OF)


def test_polars_expression_reproduces_the_decimal_result() -> None:
    facts = [_fact("price", "50"), _fact("shares_outstanding", "1000000"), _fact("gross_profit", "4000000")]
    native = price_to_gross_profit(facts, entity_id="e1", as_of=_AS_OF)
    assert native.value is not None, native.flags

    panel = pl.DataFrame(
        {
            "symbol": ["e1"],
            "date": [date(2026, 10, 8)],
            "price": [50.0],
            "shares_outstanding": [1_000_000.0],
            "gross_profit": [4_000_000.0],
        }
    )
    compiled = panel.with_columns(factor_value=compile_expression(PRICE_TO_GROSS_PROFIT_EXPRESSION_DEFINITION))
    assert compiled["factor_value"].to_list()[0] == pytest.approx(float(native.value), rel=1e-12)
