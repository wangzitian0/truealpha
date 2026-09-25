"""Unit tests for Module 3 factor: supply_chain_exposure."""

from datetime import UTC, datetime
from decimal import Decimal

from factors.base.supply_chain_exposure import (
    SupplyChainExposure,
    SupplyChainPartner,
    supply_chain_exposure,
)


def test_supply_chain_exposure_refuses_when_no_partners() -> None:
    now = datetime(2026, 9, 25, tzinfo=UTC)
    res = supply_chain_exposure([], entity_id="issuer:test:1", as_of=now)
    assert isinstance(res, SupplyChainExposure)
    assert res.exposure_score is None
    assert res.result.data_availability == "unverified"
    assert "no_disclosed_suppliers" in res.result.flags
    assert res.direct_partners == 0


def test_supply_chain_exposure_computes_with_shares() -> None:
    now = datetime(2026, 9, 25, tzinfo=UTC)
    partners = [
        SupplyChainPartner("partner:tsmc", "TSMC", "supplier", revenue_share=Decimal("0.40"), confidence=Decimal("0.9")),
        SupplyChainPartner("partner:foxconn", "Foxconn", "supplier", revenue_share=Decimal("0.30"), confidence=Decimal("0.85")),
    ]
    res = supply_chain_exposure(partners, entity_id="issuer:aapl", as_of=now)
    assert isinstance(res, SupplyChainExposure)
    assert res.exposure_score is not None
    # 0.4^2 + 0.3^2 = 0.16 + 0.09 = 0.25
    assert res.exposure_score == Decimal("0.25")
    assert res.direct_partners == 2
    assert res.suppliers_count == 2
    assert res.result.data_availability == "verified"
    assert res.result.value == Decimal("0.25")
