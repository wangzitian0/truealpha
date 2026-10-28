"""Tests for Measure, dimensional units, and times() (#1519 Step 2)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from factors.base.peg import peg
from factors.base.price_to_gross_profit import price_to_gross_profit
from factors.base.price_to_sales import price_to_sales
from factors.composite.strategy_evaluator import IssuerInput, evaluate_cutoff
from factors.forest import IssuerClass
from factors.measure import (
    UNIT_MISMATCH_REASON,
    Measure,
    Money,
    PerUnit,
    UnitMismatchError,
    Units,
    times,
)
from factors.types import Fact, GrowthConvention, UnitFamily
from truealpha_contracts.strategy import ExclusionReason, load_live_strategy_definition

AS_OF = datetime(2026, 6, 30, 16, 0, tzinfo=UTC)


def test_times_multiplies_matching_per_unit_and_units_into_money() -> None:
    price = Measure(
        value=Decimal("125.50"),
        unit=PerUnit(currency="USD", instrument_id="listing:xnas:aapl"),
        knowable_at=AS_OF,
        subject="listing:xnas:aapl",
    )
    shares = Measure(
        value=Decimal("1000"),
        unit=Units(instrument_id="listing:xnas:aapl"),
        period="FY2025",
        knowable_at=AS_OF,
        subject="issuer:aapl",
    )

    product = times(price, shares)
    reverse_product = times(shares, price)

    assert product.value == Decimal("125500.00")
    assert product.unit == Money(currency="USD")
    assert product.period == "FY2025"
    assert product.knowable_at == AS_OF
    assert reverse_product.value == Decimal("125500.00")
    assert reverse_product.unit == Money(currency="USD")


def test_times_rejects_mismatched_instrument_ids_with_unit_mismatch() -> None:
    price = Measure(
        value=Decimal("125.50"),
        unit=PerUnit(currency="USD", instrument_id="listing:xnas:pdd"),
        knowable_at=AS_OF,
        subject="listing:xnas:pdd",
    )
    shares = Measure(
        value=Decimal("5600000000"),
        unit=Units(instrument_id="security:ordinary:pdd"),
        knowable_at=AS_OF,
        subject="issuer:pdd",
    )

    with pytest.raises(UnitMismatchError, match=UNIT_MISMATCH_REASON) as exc_info:
        times(price, shares)
    assert exc_info.value.reason == UNIT_MISMATCH_REASON


def test_times_rejects_incompatible_unit_kinds_with_unit_mismatch() -> None:
    money = Measure(value=Decimal("100"), unit=Money(currency="USD"))
    units = Measure(value=Decimal("10"), unit=Units(instrument_id="inst:1"))

    with pytest.raises(UnitMismatchError, match=UNIT_MISMATCH_REASON):
        times(money, units)


def test_base_factors_refuse_mismatched_instrument_units_with_unit_mismatch() -> None:
    mismatched_price = Fact(
        entity_id="issuer:pdd",
        metric="price",
        value=Decimal("100"),
        unit_family=UnitFamily.PER_SHARE,
        unit=PerUnit(currency="USD", instrument_id="listing:xnas:pdd"),
        confidence=Decimal("0.9"),
        as_of=AS_OF,
    )
    mismatched_shares = Fact(
        entity_id="issuer:pdd",
        metric="shares_outstanding",
        value=Decimal("5000000"),
        unit_family=UnitFamily.COUNT,
        unit=Units(instrument_id="security:ordinary:pdd"),
        confidence=Decimal("0.9"),
        as_of=AS_OF,
    )
    revenue = Fact(
        entity_id="issuer:pdd",
        metric="revenue",
        value=Decimal("100000000"),
        unit_family=UnitFamily.CURRENCY,
        unit=Money(currency="USD"),
        confidence=Decimal("0.9"),
        as_of=AS_OF,
    )
    gross_profit = Fact(
        entity_id="issuer:pdd",
        metric="gross_profit",
        value=Decimal("60000000"),
        unit_family=UnitFamily.CURRENCY,
        unit=Money(currency="USD"),
        confidence=Decimal("0.9"),
        as_of=AS_OF,
    )
    net_income_series = [
        Fact(
            entity_id="issuer:pdd",
            metric="net_income",
            value=Decimal(str(10000000 * (idx + 1))),
            unit_family=UnitFamily.CURRENCY,
            unit=Money(currency="USD"),
            confidence=Decimal("0.9"),
            as_of=AS_OF,
            fiscal_period=f"FY{2022 + idx}:FY:{2022 + idx}-01-01:{2022 + idx}-12-31",
        )
        for idx in range(4)
    ]

    ps = price_to_sales([mismatched_price, mismatched_shares, revenue], entity_id="issuer:pdd", as_of=AS_OF)
    assert ps.value is None
    assert ps.flags == [UNIT_MISMATCH_REASON]

    pgp = price_to_gross_profit(
        [mismatched_price, mismatched_shares, gross_profit], entity_id="issuer:pdd", as_of=AS_OF
    )
    assert pgp.value is None
    assert pgp.flags == [UNIT_MISMATCH_REASON]

    peg_res = peg(
        [mismatched_price, mismatched_shares, *net_income_series],
        entity_id="issuer:pdd",
        growth_convention=GrowthConvention.HISTORICAL_CAGR,
        as_of=AS_OF,
        cagr_years=3,
    )
    assert peg_res.value is None
    assert UNIT_MISMATCH_REASON in peg_res.flags


def test_issuer_input_carries_measures_and_excludes_on_unit_mismatch() -> None:
    definition = load_live_strategy_definition()
    issuer = IssuerInput(
        issuer_id="issuer:pdd",
        records={
            "gross_profit": (Measure(value=Decimal("210000000"), unit=Money(currency="USD")), Decimal("0.9")),
            "total_assets": (Measure(value=Decimal("200000000"), unit=Money(currency="USD")), Decimal("0.9")),
            "headcount": (Measure(value=Decimal("100"), unit=Units(instrument_id="issuer:pdd")), Decimal("0.9")),
            "revenue": (Measure(value=Decimal("100000000"), unit=Money(currency="USD")), Decimal("0.9")),
            "shares_outstanding": (
                Measure(value=Decimal("10000000"), unit=Units(instrument_id="security:ordinary:pdd")),
                Decimal("0.9"),
            ),
            "last_close": (
                Measure(value=Decimal("40"), unit=PerUnit(currency="USD", instrument_id="listing:xnas:pdd")),
                Decimal("0.9"),
            ),
        },
        knowable_at=dict.fromkeys(
            ("gross_profit", "total_assets", "headcount", "revenue", "shares_outstanding", "last_close"),
            AS_OF,
        ),
        issuer_class=IssuerClass.NON_FINANCIAL,
    )

    [decision] = evaluate_cutoff(
        [issuer],
        definition=definition,
        cutoff_at=AS_OF,
        risk_free_rate=Decimal("0.05"),
    )
    assert decision.eligible is False
    assert decision.exclusion_reason is ExclusionReason.MISSING_MARKET_VALUE_INPUT
