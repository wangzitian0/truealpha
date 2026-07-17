import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from factors.composite.three_tier_valuation import three_tier_valuation
from factors.types import FactorResult, UnitFamily
from truealpha_contracts.strategy import ThreeTierValuationDefinition

_AS_OF = datetime(2026, 7, 1, tzinfo=UTC)
_CORPUS_PATH = Path(__file__).parents[2] / "contracts" / "tests" / "fixtures" / "large_model_value_v0_strategy.v1.json"
_CORPUS_SHA256 = "0d110a3adc94500cba2bc35d5cd33a788a18bc76ef66895c5625489be6ea50e6"
_UNIT_FAMILY = {
    "gross_profit_per_employee": UnitFamily.PER_EMPLOYEE,
    "price_to_sales": UnitFamily.RATIO,
}


def _v0_definition() -> ThreeTierValuationDefinition:
    """The locked v0 tier bands from #21/#335's golden fixture, not a local copy."""

    raw = _CORPUS_PATH.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == _CORPUS_SHA256
    corpus = json.loads(raw)
    return ThreeTierValuationDefinition.model_validate_json(json.dumps(corpus["strategy_definition"]["tier_valuation"]))


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
    definition = _v0_definition()
    inputs = [
        _factor_result("gross_profit_per_employee", "50000", "0.9"),
        _factor_result("price_to_sales", "1.0", "0.8"),
    ]

    result = three_tier_valuation(inputs, entity_id="e1", as_of=_AS_OF, definition=definition)

    # traditional band: target P/S 0.30-2.00, midpoint 1.15; gap = 1.15/1.0 - 1 = 0.15
    assert result.value == Decimal("0.15")
    assert result.unit_family == UnitFamily.RATIO
    assert result.confidence == Decimal("0.8")
    assert result.data_availability == "verified"


def test_tech_and_large_model_native_tiers_select_different_bands() -> None:
    definition = _v0_definition()

    tech = three_tier_valuation(
        [
            _factor_result("gross_profit_per_employee", "150000", "0.9"),
            _factor_result("price_to_sales", "3.0", "0.9"),
        ],
        entity_id="e1",
        as_of=_AS_OF,
        definition=definition,
    )
    large_model_native = three_tier_valuation(
        [
            _factor_result("gross_profit_per_employee", "500000", "0.9"),
            _factor_result("price_to_sales", "5.0", "0.9"),
        ],
        entity_id="e1",
        as_of=_AS_OF,
        definition=definition,
    )

    # tech band midpoint = (2.50+6.00)/2 = 4.25; gap = 4.25/3.0 - 1 = 0.4166...
    assert tech.value == Decimal("4.25") / Decimal("3.0") - Decimal("1")
    # large-model-native midpoint = (6.00+12.00)/2 = 9.00; gap = 9.00/5.0 - 1 = 0.8
    assert large_model_native.value == Decimal("0.8")
    assert tech.value != large_model_native.value


def test_missing_gppe_never_silently_drops() -> None:
    definition = _v0_definition()
    inputs = [_factor_result("price_to_sales", "1.0", "0.9")]

    result = three_tier_valuation(inputs, entity_id="e1", as_of=_AS_OF, definition=definition)

    assert result.value is None
    assert result.flags == ["missing_gross_profit_per_employee"]


def test_missing_price_to_sales_never_silently_drops() -> None:
    definition = _v0_definition()
    inputs = [_factor_result("gross_profit_per_employee", "50000", "0.9")]

    result = three_tier_valuation(inputs, entity_id="e1", as_of=_AS_OF, definition=definition)

    assert result.value is None
    assert result.flags == ["missing_price_to_sales"]


def test_nonpositive_price_to_sales_is_excluded_not_divided() -> None:
    definition = _v0_definition()
    inputs = [
        _factor_result("gross_profit_per_employee", "50000", "0.9"),
        _factor_result("price_to_sales", "0", "0.9"),
    ]

    result = three_tier_valuation(inputs, entity_id="e1", as_of=_AS_OF, definition=definition)

    assert result.value is None
    assert result.flags == ["nonpositive_price_to_sales"]


def test_confidence_is_the_minimum_of_consumed_inputs() -> None:
    definition = _v0_definition()
    inputs = [
        _factor_result("gross_profit_per_employee", "50000", "0.95"),
        _factor_result("price_to_sales", "1.0", "0.4"),
    ]

    result = three_tier_valuation(inputs, entity_id="e1", as_of=_AS_OF, definition=definition)

    assert result.confidence == Decimal("0.4")


def test_data_availability_is_verified_only_when_every_consumed_input_is() -> None:
    """A composite cannot claim stronger verification than what it was built
    from -- mirrors gross_profit_per_employee's own real-world "unverified"
    default for this preview round."""

    definition = _v0_definition()
    inputs = [
        _factor_result("gross_profit_per_employee", "50000", "0.9", data_availability="unverified"),
        _factor_result("price_to_sales", "1.0", "0.8", data_availability="verified"),
    ]

    result = three_tier_valuation(inputs, entity_id="e1", as_of=_AS_OF, definition=definition)

    assert result.data_availability == "unverified"
