"""Falsifiable Wave 4 tests for M2 geometric CAGR, M3 edge threshold + shock, M4 PIT track record, and M5 virtual company."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from factors.base.analyst_track_record import AnalystRatingItem, analyst_track_record
from factors.base.etf_virtual_company import HoldingLine, consolidate_fund
from factors.base.peg import peg
from factors.base.supply_chain_exposure import (
    BELOW_EDGE_CONFIDENCE_THRESHOLD,
    MIN_EDGE_CONFIDENCE,
    SupplyChainPartner,
    supply_chain_exposure,
)
from factors.types import Fact, GrowthConvention
from truealpha_contracts.etf_virtual_company import ETF_CONSOLIDATION_V0
from truealpha_contracts.fiscal_period import encode_annual
from truealpha_contracts.metrics import METRICS

pytestmark = pytest.mark.contract

AS_OF = datetime(2026, 4, 1, tzinfo=UTC)
ENTITY = "issuer:test"


def _fact(metric: str, value: str, *, fiscal_period: str | None = None, conf: str = "0.90") -> Fact:
    return Fact(
        entity_id=ENTITY,
        metric=metric,
        value=Decimal(value),
        unit_family=METRICS[metric].unit_family,
        confidence=Decimal(conf),
        as_of=AS_OF,
        fiscal_period=fiscal_period,
    )


def test_geometric_cagr_handles_intermediate_loss_year_while_historical_refuses() -> None:
    facts = [
        _fact("price", "100"),
        _fact("shares_outstanding", "10"),
        _fact("net_income", "100", fiscal_period=encode_annual(date(2022, 12, 31)), conf="0.80"),
        _fact("net_income", "-50", fiscal_period=encode_annual(date(2023, 12, 31)), conf="0.70"),
        _fact("net_income", "120", fiscal_period=encode_annual(date(2024, 12, 31)), conf="0.85"),
        _fact("net_income", "150", fiscal_period=encode_annual(date(2025, 12, 31)), conf="0.90"),
    ]
    weighted = peg(
        facts, entity_id=ENTITY, growth_convention=GrowthConvention.HISTORICAL_CAGR, as_of=AS_OF, cagr_years=3
    )
    assert weighted.value is None
    assert "loss_year_in_growth_window" in weighted.flags

    geom = peg(facts, entity_id=ENTITY, growth_convention=GrowthConvention.GEOMETRIC_CAGR, as_of=AS_OF, cagr_years=3)
    assert geom.value is not None and geom.value > Decimal("0.4")
    assert "growth_convention:geometric_cagr" in geom.flags
    # Endpoint confidence uses base (0.80) and latest (0.90), ignoring intermediate 2023 (0.70).
    assert geom.confidence == Decimal("0.80")


def test_supply_chain_kills_edges_below_040_and_computes_propagated_shock() -> None:
    low_edges = [SupplyChainPartner("p1", "Small", "supplier", Decimal("0.30"), Decimal("0.39"))]
    refused = supply_chain_exposure(
        low_edges, entity_id=ENTITY, as_of=AS_OF, supplies_to_edges_exist=True, shock_magnitude=Decimal("0.40")
    )
    assert refused.exposure_score is None
    assert BELOW_EDGE_CONFIDENCE_THRESHOLD in refused.result.flags
    assert refused.propagated_shock is None

    mixed = [
        SupplyChainPartner("p1", "Noise", "supplier", Decimal("0.80"), MIN_EDGE_CONFIDENCE - Decimal("0.01")),
        SupplyChainPartner("p2", "TSMC", "supplier", Decimal("0.50"), Decimal("0.80")),
    ]
    res = supply_chain_exposure(
        mixed, entity_id=ENTITY, as_of=AS_OF, supplies_to_edges_exist=True, shock_magnitude=Decimal("0.40")
    )
    assert res.direct_partners == 1
    assert res.exposure_score == Decimal("0.25")
    assert res.propagated_shock == Decimal("0.05")
    assert "shock_model:supply-chain-shock:v1" in res.result.flags


def test_analyst_track_record_enforces_strict_pit_boundaries() -> None:
    future_only = [
        AnalystRatingItem(
            analyst_id="a1",
            rating=5,
            target_price=Decimal("200"),
            confidence=Decimal("0.8"),
            knowable_at=datetime(2026, 4, 2, tzinfo=UTC),
        )
    ]
    refused = analyst_track_record(future_only, entity_id=ENTITY, as_of=AS_OF)
    assert refused.consensus_rating is None
    assert "not_knowable_at_cutoff" in refused.result.flags

    mixed = [
        AnalystRatingItem(
            analyst_id="a1",
            rating=5,
            target_price=Decimal("200"),
            confidence=Decimal("0.85"),
            knowable_at=datetime(2026, 1, 10, tzinfo=UTC),
            outcome_end=datetime(2026, 3, 20, tzinfo=UTC),
            realized_return=Decimal("0.12"),
        ),
        AnalystRatingItem(
            analyst_id="a2",
            rating=1,
            target_price=Decimal("80"),
            confidence=Decimal("0.75"),
            knowable_at=datetime(2026, 2, 1, tzinfo=UTC),
            # Unmatured outcome after as_of must be excluded from track_record_score.
            outcome_end=datetime(2026, 5, 1, tzinfo=UTC),
            realized_return=Decimal("0.50"),
        ),
    ]
    scored = analyst_track_record(mixed, entity_id=ENTITY, as_of=AS_OF)
    assert scored.ratings_count == 2
    assert scored.track_record_score == Decimal("1")


def test_etf_virtual_company_consolidates_virtual_fundamentals() -> None:
    lines = [
        HoldingLine(
            holding_name="NVIDIA",
            weight=Decimal("60"),
            listing_id="listing:XNAS:NVDA",
            valuation_gap=Decimal("0.20"),
            availability="available",
            confidence=Decimal("0.9"),
            availability_status="available",
            revenue=Decimal("1000"),
            gross_profit=Decimal("700"),
            employees=Decimal("100"),
            gppe=Decimal("7"),
        ),
        HoldingLine(
            holding_name="Apple",
            weight=Decimal("40"),
            listing_id="listing:XNAS:AAPL",
            valuation_gap=Decimal("0.10"),
            availability="available",
            confidence=Decimal("0.8"),
            availability_status="available",
            revenue=Decimal("500"),
            gross_profit=Decimal("200"),
            employees=Decimal("50"),
            gppe=Decimal("4"),
        ),
    ]
    res = consolidate_fund(lines, fund_id="etf:ivv", as_of=AS_OF, definition=ETF_CONSOLIDATION_V0)
    assert res.virtual_revenue == Decimal("800")
    assert res.virtual_gross_profit == Decimal("500")
    assert res.virtual_employees == Decimal("80")
    assert res.virtual_gppe == Decimal("5.8")


def test_corporate_actions_accepts_non_split_spinoff_adjustment_for_spgi() -> None:
    from factors.backtest.corporate_actions import SeriesBar, derive_split_events

    bars = [
        SeriesBar(
            date(2026, 6, 30),
            Decimal("406.02"),
            Decimal("406.70"),
            Decimal("385.29473684210524"),
        ),
        SeriesBar(date(2026, 7, 1), Decimal("399.58"), Decimal("414.97"), Decimal("414.97")),
    ]
    assert derive_split_events("SPGI", bars) == ()
