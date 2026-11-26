"""Tests for PEG loss_year_in_growth_window vs insufficient_earnings_history (#1851)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

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


def test_evaluate_cutoff_computes_and_ranks_peg_geometric_cagr() -> None:
    from factors.composite.strategy_evaluator import IssuerInput, evaluate_cutoff
    from factors.measure import measure_for_input_key
    from factors.panel import IssuerClass, uniform_tree_pins
    from truealpha_contracts.strategy import load_strategy_definition

    cutoff = datetime(2026, 4, 1, tzinfo=UTC)
    known = datetime(2026, 3, 1, tzinfo=UTC)

    def _rec(key: str, val: str, period: str | None = None) -> tuple[Any, Decimal]:
        kw: dict[str, Any] = {"subject": "issuer:a", "knowable_at": known}
        if period is not None:
            kw["period"] = period
        return measure_for_input_key(key, Decimal(val), **kw), Decimal("0.9")

    issuer = IssuerInput(
        issuer_id="issuer:a",
        records={
            "gross_profit": _rec("gross_profit", "500000000"),
            "total_assets": _rec("total_assets", "1000000000"),
            "headcount": _rec("headcount", "100"),
            "revenue": _rec("revenue", "800000000"),
            "shares_outstanding": _rec("shares_outstanding", "1000000"),
            "last_close": _rec("last_close", "100"),
            "net_income": _rec("net_income", "150000000", encode_annual(date(2025, 12, 31))),
        },
        knowable_at={
            k: known
            for k in (
                "gross_profit",
                "total_assets",
                "headcount",
                "revenue",
                "shares_outstanding",
                "last_close",
                "net_income",
            )
        },
        periodic_records={
            "net_income": {
                encode_annual(date(2022, 12, 31)): _rec("net_income", "100000000", encode_annual(date(2022, 12, 31))),
                encode_annual(date(2023, 12, 31)): _rec("net_income", "-50000000", encode_annual(date(2023, 12, 31))),
                encode_annual(date(2024, 12, 31)): _rec("net_income", "120000000", encode_annual(date(2024, 12, 31))),
                encode_annual(date(2025, 12, 31)): _rec("net_income", "150000000", encode_annual(date(2025, 12, 31))),
            }
        },
        issuer_class=IssuerClass.NON_FINANCIAL,
    )
    decisions = evaluate_cutoff(
        [issuer],
        definition=load_strategy_definition("large_model_value_v0_definition_1_1_0.json"),
        cutoff_at=cutoff,
        risk_free_rate=Decimal("0.04"),
        tree_pins=uniform_tree_pins(),
    )
    assert len(decisions) == 1
    dec = decisions[0]
    assert dec.peg is None and "loss_year_in_growth_window" in dec.peg_reason_codes
    assert dec.peg_geometric_cagr is not None and dec.peg_geometric_cagr > 0
    assert dec.peg_geometric_cagr_rank == 1
    assert dec.peg_geometric_cagr_confidence == Decimal("0.9")
