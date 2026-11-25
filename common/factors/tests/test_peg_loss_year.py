"""Tests for PEG loss_year_in_growth_window vs insufficient_earnings_history (#1851)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from factors.base.peg import peg
from factors.types import Fact, GrowthConvention
from truealpha_contracts.fiscal_period import encode_annual
from truealpha_contracts.metrics import METRICS
from truealpha_contracts.reasons import ReasonClass, registration

pytestmark = pytest.mark.contract

AS_OF = datetime(2026, 4, 1, tzinfo=UTC)
ENTITY = "issuer:test"


def _fact(metric: str, value: str, *, fiscal_period: str | None = None) -> Fact:
    return Fact(
        entity_id=ENTITY,
        metric=metric,
        value=Decimal(value),
        unit_family=METRICS[metric].unit_family,
        confidence=Decimal("0.9"),
        as_of=AS_OF,
        fiscal_period=fiscal_period,
    )


def _base_facts(net_incomes: list[tuple[int, str]]) -> list[Fact]:
    return [
        _fact("price", "100"),
        _fact("shares_outstanding", "1000000"),
        *[_fact("net_income", val, fiscal_period=encode_annual(date(year, 12, 31))) for year, val in net_incomes],
    ]


def test_peg_reports_loss_year_in_growth_window_instead_of_insufficient_history() -> None:
    facts = _base_facts([(2022, "100"), (2023, "-50"), (2024, "120"), (2025, "150")])
    res = peg(
        facts,
        entity_id=ENTITY,
        growth_convention=GrowthConvention.HISTORICAL_CAGR,
        as_of=AS_OF,
        cagr_years=3,
    )
    assert res.value is None
    assert "loss_year_in_growth_window" in res.flags
    assert "insufficient_earnings_history" not in res.flags
    entry = registration("loss_year_in_growth_window")
    assert entry is not None and entry.reason_class is ReasonClass.WORLD_ABSENCE


def test_peg_reports_insufficient_earnings_history_when_window_is_incomplete() -> None:
    facts = _base_facts([(2024, "100"), (2025, "120")])
    res = peg(
        facts,
        entity_id=ENTITY,
        growth_convention=GrowthConvention.HISTORICAL_CAGR,
        as_of=AS_OF,
        cagr_years=3,
    )
    assert res.value is None
    assert "insufficient_earnings_history" in res.flags
    assert "loss_year_in_growth_window" not in res.flags


def test_peg_computes_positive_value_when_all_window_years_are_positive() -> None:
    facts = _base_facts([(2022, "100"), (2023, "110"), (2024, "125"), (2025, "150")])
    res = peg(
        facts,
        entity_id=ENTITY,
        growth_convention=GrowthConvention.HISTORICAL_CAGR,
        as_of=AS_OF,
        cagr_years=3,
    )
    assert res.value is not None and res.value > 0
