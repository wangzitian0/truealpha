"""Unit tests for Module 3 factor: supply_chain_exposure."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from factors.base.supply_chain_exposure import (
    SupplyChainExposure,
    SupplyChainPartner,
    supply_chain_exposure,
)


def test_supply_chain_exposure_says_no_extraction_ran_when_the_graph_has_no_supplier_edge() -> None:
    now = datetime(2026, 9, 25, tzinfo=UTC)
    res = supply_chain_exposure([], entity_id="issuer:test:1", as_of=now, supplies_to_edges_exist=False)
    assert isinstance(res, SupplyChainExposure)
    assert res.exposure_score is None
    assert res.result.data_availability == "unverified"
    assert res.result.flags == ["no_supply_chain_extraction"]
    assert res.direct_partners == 0


def test_supply_chain_exposure_says_no_disclosed_suppliers_when_other_issuers_have_edges() -> None:
    now = datetime(2026, 9, 25, tzinfo=UTC)
    res = supply_chain_exposure([], entity_id="issuer:test:1", as_of=now, supplies_to_edges_exist=True)
    assert res.exposure_score is None
    assert res.result.data_availability == "unverified"
    assert res.result.flags == ["no_disclosed_suppliers"]
    assert res.direct_partners == 0


def test_supply_chain_exposure_rejects_partners_that_contradict_an_empty_graph() -> None:
    now = datetime(2026, 9, 25, tzinfo=UTC)
    partners = [SupplyChainPartner("partner:tsmc", "TSMC", "supplier", confidence=Decimal("0.9"))]
    with pytest.raises(ValueError, match="supplies_to_edges_exist"):
        supply_chain_exposure(partners, entity_id="issuer:test:1", as_of=now, supplies_to_edges_exist=False)


def test_supply_chain_exposure_computes_with_shares() -> None:
    now = datetime(2026, 9, 25, tzinfo=UTC)
    partners = [
        SupplyChainPartner(
            "partner:tsmc", "TSMC", "supplier", revenue_share=Decimal("0.40"), confidence=Decimal("0.9")
        ),
        SupplyChainPartner(
            "partner:foxconn", "Foxconn", "supplier", revenue_share=Decimal("0.30"), confidence=Decimal("0.85")
        ),
    ]
    res = supply_chain_exposure(partners, entity_id="issuer:aapl", as_of=now, supplies_to_edges_exist=True)
    assert isinstance(res, SupplyChainExposure)
    assert res.exposure_score is not None
    # 0.4^2 + 0.3^2 = 0.16 + 0.09 = 0.25
    assert res.exposure_score == Decimal("0.25")
    assert res.direct_partners == 2
    assert res.suppliers_count == 2
    assert res.result.data_availability == "unverified"
    assert res.result.value == Decimal("0.25")
    assert res.result.flags == []


def test_supply_chain_exposure_emits_unquantified_and_customer_only_flags() -> None:
    now = datetime(2026, 9, 25, tzinfo=UTC)
    customer_only_unquantified = [
        SupplyChainPartner("partner:mck", "McKesson", "customer", revenue_share=None, confidence=Decimal("0.8")),
        SupplyChainPartner("partner:cah", "Cardinal Health", "customer", revenue_share=None, confidence=Decimal("0.7")),
    ]
    res = supply_chain_exposure(
        customer_only_unquantified, entity_id="issuer:nvda", as_of=now, supplies_to_edges_exist=True
    )
    assert res.exposure_score == Decimal("0.5")
    assert res.max_partner_share == Decimal("0")
    assert res.suppliers_count == 0
    assert res.customers_count == 2
    assert res.result.flags == ["unquantified_partner_shares", "customer_only_graph"]

    quantified_customer_only = [
        SupplyChainPartner(
            "partner:mck", "McKesson", "customer", revenue_share=Decimal("0.20"), confidence=Decimal("0.8")
        ),
    ]
    res_q = supply_chain_exposure(
        quantified_customer_only, entity_id="issuer:nvda", as_of=now, supplies_to_edges_exist=True
    )
    assert res_q.exposure_score == Decimal("0.04")
    assert res_q.max_partner_share == Decimal("0.20")
    assert res_q.result.flags == ["customer_only_graph"]
