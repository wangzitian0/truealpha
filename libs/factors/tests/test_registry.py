from datetime import UTC, datetime
from decimal import Decimal

# Importing the factor modules registers them. Both kinds are covered by REAL factors
# now — module 6 was a Gate 0 probe standing in for a factor that did not exist until
# `theme_purity` (#772). The composite probe stays: it is the subject of
# `test_issue58_additive_probe.py`'s conformance contract, which needs a signature that
# does not move.
import factors.base.gross_profit_per_employee  # noqa: F401
import factors.base.peg  # noqa: F401
import factors.base.theme_purity  # noqa: F401
import factors.composite.registered_composite_probe  # noqa: F401
import factors.composite.three_tier_valuation  # noqa: F401
import pytest
from factors import FACTOR_REGISTRY, Fact, FactorResult, UnitFamily
from pydantic import ValidationError


def test_every_registered_factor_is_registered_under_its_declared_kind():
    assert FACTOR_REGISTRY["peg"].kind == "base"
    assert FACTOR_REGISTRY["gross_profit_per_employee"].kind == "base"
    assert FACTOR_REGISTRY["three_tier_valuation"].kind == "composite"
    assert FACTOR_REGISTRY["theme_purity"].kind == "base"
    assert FACTOR_REGISTRY["registered_composite_probe"].kind == "composite"


def test_confidence_is_mandatory_and_bounded():
    with pytest.raises(ValidationError):
        Fact(
            entity_id="e1",
            metric="revenue",
            value=1.0,
            unit_family=UnitFamily.CURRENCY,
            confidence=1.5,
            as_of=datetime.now(UTC),
        )
    with pytest.raises(ValidationError):
        FactorResult(
            factor="peg",
            entity_id="e1",
            value=1.0,
            unit_family=UnitFamily.RATIO,
            confidence=-0.1,
            as_of=datetime.now(UTC),
        )


def test_data_availability_defaults_to_unverified():
    r = FactorResult(
        factor="peg",
        entity_id="e1",
        value=None,
        unit_family=UnitFamily.RATIO,
        confidence=0.5,
        as_of=datetime.now(UTC),
    )
    assert r.data_availability == "unverified"


def test_factor_wire_values_use_decimal_not_float():
    fact = Fact(
        entity_id="e1",
        metric="revenue",
        value="0.1",
        unit_family=UnitFamily.CURRENCY,
        confidence="0.9",
        as_of=datetime.now(UTC),
    )
    assert fact.value == Decimal("0.1")
    assert fact.confidence == Decimal("0.9")


def test_unregistered_metric_is_rejected():
    with pytest.raises(ValidationError):
        Fact(
            entity_id="e1",
            metric="not_a_real_metric",
            value=1.0,
            unit_family=UnitFamily.CURRENCY,
            confidence=0.9,
            as_of=datetime.now(UTC),
        )


def test_mismatched_unit_family_is_rejected():
    with pytest.raises(ValidationError):
        Fact(
            entity_id="e1",
            metric="revenue",
            value=1.0,
            unit_family=UnitFamily.RATIO,
            confidence=0.9,
            as_of=datetime.now(UTC),
        )
