"""Unit tests for supply_chain_extraction module (#772)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from data_engine.datahub.standards.supply_chain_extraction import (
    extract_supply_chain_relationships,
    materialize_supply_chain_exposure,
)
from factors.base.supply_chain_exposure import (
    SupplyChainPartner,
    supply_chain_exposure,
)


class _MockCursor:
    def __init__(self):
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        return self


class _MockConnection:
    def __init__(self):
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        return _MockCursor()


def test_materialize_supply_chain_exposure_executes_insert() -> None:
    conn = _MockConnection()
    count = materialize_supply_chain_exposure(
        conn,
        run_id="run:sc1",
        exposure_data=[
            {
                "issuer_id": "issuer:aapl",
                "exposure_score": Decimal("0.85"),
                "direct_partners": 12,
                "availability_status": "available",
                "reason_codes": [],
            },
            {
                "issuer_id": "issuer:tiny",
                "exposure_score": None,
                "direct_partners": 0,
                "availability_status": "unavailable",
                "reason_codes": ["no_disclosed_suppliers"],
            },
        ],
    )
    assert count == 2
    assert len(conn.executed) == 2
    sql1, params1 = conn.executed[0]
    assert "insert into mart.issuer_supply_chain_exposure" in sql1
    assert len(params1) == 14
    assert params1[0] == "run:sc1"
    assert params1[1] == "issuer:aapl"
    assert params1[2] is not None  # cutoff
    assert params1[3] == Decimal("0.85")
    assert params1[4] == 12
    assert params1[11] == "available"
    assert params1[12] == "verified"
    assert params1[13] == "accepted"

    sql2, params2 = conn.executed[1]
    assert len(params2) == 14
    assert params2[0] == "run:sc1"
    assert params2[1] == "issuer:tiny"
    assert params2[3] is None
    assert params2[4] == 0
    assert params2[11] == "unavailable"
    assert params2[12] == "degraded"
    assert params2[13] == "not_evaluated"
    assert params2[9] == ["no_disclosed_suppliers"]


def test_materialize_supply_chain_exposure_from_factor_record() -> None:
    conn = _MockConnection()
    now = datetime(2026, 9, 25, tzinfo=UTC)
    partners = [
        SupplyChainPartner("p:tsmc", "TSMC", "supplier", revenue_share=Decimal("0.5"), confidence=Decimal("0.9")),
    ]
    rec = supply_chain_exposure(partners, entity_id="issuer:nvda", as_of=now)
    count = materialize_supply_chain_exposure(conn, run_id="run:sc2", cutoff=now, exposure_data=[rec])
    assert count == 1
    assert len(conn.executed) == 1
    _, params = conn.executed[0]
    assert len(params) == 14
    assert params[0] == "run:sc2"
    assert params[1] == "issuer:nvda"
    assert params[3] == Decimal("0.25")  # 0.5^2
    assert params[4] == 1
    assert params[11] == "available"


def test_extract_supply_chain_relationships_empty_text() -> None:
    edges = extract_supply_chain_relationships(
        "",
        issuer_id="issuer:test",
        filing_date=date(2026, 1, 1),
        accession="0001-00-00",
    )
    assert edges == ()


def test_extract_supply_chain_relationships_parses_sentences() -> None:
    text = (
        "Item 1. Business\n"
        "We rely on a single supplier for our key semiconductor chips.\n"
        "Our largest customer accounts for 15% of net revenues.\n"
        "General operational descriptions and regulatory disclosures follow."
    )
    edges = extract_supply_chain_relationships(
        text,
        issuer_id="issuer:test",
        filing_date=date(2026, 1, 1),
        accession="0001-00-00",
    )
    assert len(edges) == 2
    assert edges[0].relation_type == "supplier"
    assert "single supplier" in edges[0].evidence_sentence
    assert edges[1].relation_type == "customer"
    assert "largest customer" in edges[1].evidence_sentence


def test_supplies_to_is_classified_as_customer_not_supplier() -> None:
    """Regression: 'supplies to' means the issuer supplies TO a customer.

    The partner in that sentence is a *customer*, not a supplier.
    Before the fix, 'supplies' matched first and set rel='supplier', flipping
    the edge direction for this common wording.
    """
    text = (
        "Item 1. Business\n"
        "The company supplies to Apple Inc. as its primary distribution channel.\n"
        "We also purchase raw materials from a key supplier in Taiwan.\n"
    )
    edges = extract_supply_chain_relationships(
        text,
        issuer_id="issuer:test",
        filing_date=date(2026, 1, 1),
        accession="0001-00-00",
    )
    assert len(edges) == 2
    # "supplies to Apple" → Apple is a customer
    assert edges[0].relation_type == "customer", (
        f"Expected 'customer' for 'supplies to' sentence, got {edges[0].relation_type!r}"
    )
    # "key supplier in Taiwan" → Taiwan partner is a supplier
    assert edges[1].relation_type == "supplier", (
        f"Expected 'supplier' for 'key supplier' sentence, got {edges[1].relation_type!r}"
    )


def test_purchases_from_is_classified_as_supplier_not_customer() -> None:
    """Regression: 'purchases from' means the issuer buys from a supplier.

    The partner in that sentence is a *supplier*, not a customer.
    Before the fix, 'purchases from' was in is_customer_context, which would
    misclassify "We purchase components from Acme" when no supplier/vendor
    keyword was also present.
    """
    text = (
        "Item 1. Business\n"
        "The Company purchases from Acme Corp substantially all of its semiconductor needs.\n"
        "Our largest customer is a major US retailer accounting for 20% of revenue.\n"
    )
    edges = extract_supply_chain_relationships(
        text,
        issuer_id="issuer:test",
        filing_date=date(2026, 1, 1),
        accession="0001-00-00",
    )
    assert len(edges) == 2
    # "purchases from Acme" → Acme is a supplier
    assert edges[0].relation_type == "supplier", (
        f"Expected 'supplier' for 'purchases from' sentence, got {edges[0].relation_type!r}"
    )
    # "largest customer" → partner is a customer
    assert edges[1].relation_type == "customer", (
        f"Expected 'customer' for 'largest customer' sentence, got {edges[1].relation_type!r}"
    )
