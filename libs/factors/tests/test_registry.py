from datetime import UTC, datetime
from decimal import Decimal

# Importing the stub modules registers them.
import factors.base.gross_profit_per_employee  # noqa: F401
import factors.base.peg  # noqa: F401
import factors.base.registered_semantic_probe  # noqa: F401
import factors.composite.registered_composite_probe  # noqa: F401
import factors.composite.three_tier_valuation  # noqa: F401
import pytest
from factors import FACTOR_REGISTRY, Fact, FactorResult
from pydantic import ValidationError


def test_stub_factors_are_registered():
    assert FACTOR_REGISTRY["peg"].kind == "base"
    assert FACTOR_REGISTRY["gross_profit_per_employee"].kind == "base"
    assert FACTOR_REGISTRY["three_tier_valuation"].kind == "composite"
    assert FACTOR_REGISTRY["registered_semantic_probe"].kind == "base"
    assert FACTOR_REGISTRY["registered_composite_probe"].kind == "composite"


def test_confidence_is_mandatory_and_bounded():
    with pytest.raises(ValidationError):
        Fact(entity_id="e1", metric="revenue", value=1.0, confidence=1.5, as_of=datetime.now(UTC))
    with pytest.raises(ValidationError):
        FactorResult(factor="peg", entity_id="e1", value=1.0, confidence=-0.1, as_of=datetime.now(UTC))


def test_data_availability_defaults_to_unverified():
    r = FactorResult(factor="peg", entity_id="e1", value=None, confidence=0.5, as_of=datetime.now(UTC))
    assert r.data_availability == "unverified"


def test_factor_wire_values_use_decimal_not_float():
    fact = Fact(entity_id="e1", metric="revenue", value="0.1", confidence="0.9", as_of=datetime.now(UTC))
    assert fact.value == Decimal("0.1")
    assert fact.confidence == Decimal("0.9")
