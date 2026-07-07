from datetime import UTC, datetime

# Importing the stub modules registers them.
import factors.base.gross_profit_per_employee  # noqa: F401
import factors.base.peg  # noqa: F401
import factors.composite.three_tier_valuation  # noqa: F401
import pytest
from factors import FACTOR_REGISTRY, Fact, FactorResult
from pydantic import ValidationError


def test_stub_factors_are_registered():
    assert FACTOR_REGISTRY["peg"].kind == "base"
    assert FACTOR_REGISTRY["gross_profit_per_employee"].kind == "base"
    assert FACTOR_REGISTRY["three_tier_valuation"].kind == "composite"


def test_confidence_is_mandatory_and_bounded():
    with pytest.raises(ValidationError):
        Fact(entity_id="e1", metric="revenue", value=1.0, confidence=1.5, as_of=datetime.now(UTC))
    with pytest.raises(ValidationError):
        FactorResult(factor="peg", entity_id="e1", value=1.0, confidence=-0.1, as_of=datetime.now(UTC))


def test_data_availability_defaults_to_unverified():
    r = FactorResult(factor="peg", entity_id="e1", value=None, confidence=0.5, as_of=datetime.now(UTC))
    assert r.data_availability == "unverified"
