import inspect
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from factors.composite.three_tier_valuation import three_tier_valuation
from factors.types import FactorResult, UnitFamily
from truealpha_contracts.strategy import (
    LargeModelValueV0Definition,
    load_live_strategy_definition,
)

_AS_OF = datetime(2026, 7, 1, tzinfo=UTC)
_UNIT_FAMILY = {
    "gross_profit_per_employee": UnitFamily.PER_EMPLOYEE,
    "price_to_sales": UnitFamily.RATIO,
    "price_to_gross_profit": UnitFamily.RATIO,
}


def _definition() -> LargeModelValueV0Definition:
    return load_live_strategy_definition()


def _factor_result(name: str, value: str, confidence: str, data_availability: str = "verified") -> FactorResult:
    return FactorResult(
        factor=name,
        entity_id="e1",
        value=Decimal(value),
        unit_family=_UNIT_FAMILY[name],
        confidence=Decimal(confidence),
        as_of=_AS_OF,
        data_availability=data_availability,
    )


def test_traditional_tier_valuation_gap() -> None:
    definition = _definition()
    inputs = [
        _factor_result("gross_profit_per_employee", "500000", "0.9"),
        _factor_result("price_to_gross_profit", "5.0", "0.8"),
    ]

    result = three_tier_valuation(inputs, entity_id="e1", as_of=_AS_OF, definition=definition)

    # traditional band: target P/GP 7.50-10.00, midpoint 8.75; gap = 8.75/5.0 - 1 = 0.75
    assert result.value == Decimal("0.75")
    assert result.unit_family == UnitFamily.RATIO
    assert result.confidence == Decimal("0.8")
    assert result.data_availability == "verified"


def test_tech_and_large_model_native_tiers_select_different_bands() -> None:
    definition = _definition()

    tech = three_tier_valuation(
        [
            _factor_result("gross_profit_per_employee", "1500000", "0.9"),
            _factor_result("price_to_gross_profit", "10.0", "0.9"),
        ],
        entity_id="e1",
        as_of=_AS_OF,
        definition=definition,
    )
    large_model_native = three_tier_valuation(
        [
            _factor_result("gross_profit_per_employee", "5000000", "0.9"),
            _factor_result("price_to_gross_profit", "25.0", "0.9"),
        ],
        entity_id="e1",
        as_of=_AS_OF,
        definition=definition,
    )

    # tech band midpoint = (11.43+14.29)/2 = 12.86; gap = 12.86/10.0 - 1 = 0.286
    assert tech.value == Decimal("12.86") / Decimal("10.0") - Decimal("1")
    # large-model-native midpoint = (26.67+40.00)/2 = 33.335; gap = 33.335/25.0 - 1
    assert large_model_native.value == Decimal("33.335") / Decimal("25.0") - Decimal("1")
    assert tech.value != large_model_native.value


def test_missing_gppe_never_silently_drops() -> None:
    definition = _definition()
    inputs = [_factor_result("price_to_gross_profit", "5.0", "0.9")]

    result = three_tier_valuation(inputs, entity_id="e1", as_of=_AS_OF, definition=definition)

    assert result.value is None
    assert result.flags == ["missing_gross_profit_per_employee"]


def test_missing_price_to_gross_profit_never_silently_drops() -> None:
    definition = _definition()
    inputs = [_factor_result("gross_profit_per_employee", "500000", "0.9")]

    result = three_tier_valuation(inputs, entity_id="e1", as_of=_AS_OF, definition=definition)

    assert result.value is None
    assert result.flags == ["missing_price_to_gross_profit"]


def test_nonpositive_price_to_gross_profit_is_excluded_not_divided() -> None:
    definition = _definition()
    inputs = [
        _factor_result("gross_profit_per_employee", "500000", "0.9"),
        _factor_result("price_to_gross_profit", "0", "0.9"),
    ]

    result = three_tier_valuation(inputs, entity_id="e1", as_of=_AS_OF, definition=definition)

    assert result.value is None
    assert result.flags == ["nonpositive_price_to_gross_profit"]


def test_confidence_is_the_minimum_of_consumed_inputs() -> None:
    definition = _definition()
    inputs = [
        _factor_result("gross_profit_per_employee", "500000", "0.95"),
        _factor_result("price_to_gross_profit", "5.0", "0.4"),
    ]

    result = three_tier_valuation(inputs, entity_id="e1", as_of=_AS_OF, definition=definition)

    assert result.confidence == Decimal("0.4")


def test_data_availability_is_verified_only_when_every_consumed_input_is() -> None:
    definition = _definition()
    inputs = [
        _factor_result("gross_profit_per_employee", "500000", "0.9", data_availability="unverified"),
        _factor_result("price_to_gross_profit", "5.0", "0.8", data_availability="verified"),
    ]

    result = three_tier_valuation(inputs, entity_id="e1", as_of=_AS_OF, definition=definition)

    assert result.data_availability == "unverified"


def test_multiple_entities_in_inputs_does_not_cross_talk() -> None:
    definition = _definition()
    inputs = [
        # Entity 1 has low GPPE (traditional band)
        FactorResult(
            factor="gross_profit_per_employee",
            entity_id="e1",
            value=Decimal("500000"),
            unit_family=_UNIT_FAMILY["gross_profit_per_employee"],
            confidence=Decimal("0.9"),
            as_of=_AS_OF,
            data_availability="verified",
        ),
        # Entity 2 has high GPPE (large-model-native band)
        FactorResult(
            factor="gross_profit_per_employee",
            entity_id="e2",
            value=Decimal("5000000"),
            unit_family=_UNIT_FAMILY["gross_profit_per_employee"],
            confidence=Decimal("0.9"),
            as_of=_AS_OF,
            data_availability="verified",
        ),
        # Entity 2 price_to_gross_profit
        FactorResult(
            factor="price_to_gross_profit",
            entity_id="e2",
            value=Decimal("25.0"),
            unit_family=_UNIT_FAMILY["price_to_gross_profit"],
            confidence=Decimal("0.9"),
            as_of=_AS_OF,
            data_availability="verified",
        ),
    ]

    result = three_tier_valuation(inputs, entity_id="e2", as_of=_AS_OF, definition=definition)

    # Entity 2 uses its own GPPE (5000000 -> large_model_native midpoint 33.335; gap = 33.335/25.0 - 1)
    assert result.value == Decimal("33.335") / Decimal("25.0") - Decimal("1")


def test_duplicate_factor_for_same_entity_fails_closed() -> None:
    definition = _definition()
    inputs = [
        _factor_result("gross_profit_per_employee", "500000", "0.9"),
        _factor_result("gross_profit_per_employee", "600000", "0.9"),
        _factor_result("price_to_gross_profit", "5.0", "0.9"),
    ]

    with pytest.raises(ValueError, match="e1: multiple factor results for factor 'gross_profit_per_employee'"):
        three_tier_valuation(inputs, entity_id="e1", as_of=_AS_OF, definition=definition)


def test_definition_1_0_0_has_no_margin_gate() -> None:
    """P/GP removes the margin effect, so 1.0.0 tiers on labor efficiency only."""
    definition = _definition()
    inputs = [
        _factor_result("gross_profit_per_employee", "1500000", "0.9"),
        _factor_result("price_to_gross_profit", "12", "0.9"),
    ]
    result = three_tier_valuation(inputs, entity_id="e1", as_of=_AS_OF, definition=definition)
    assert result.value == Decimal("12.86") / Decimal("12") - Decimal("1")


def test_gross_margin_parameter_is_deleted() -> None:
    """Topic 1: gross_margin parameter is completely removed from three_tier_valuation."""
    assert "gross_margin" not in inspect.signature(three_tier_valuation).parameters
