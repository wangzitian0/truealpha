"""Unit tests for supply_chain_extraction module (#772)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from data_engine.datahub.standards.supply_chain_extraction import (
    extract_supply_chain_relationships,
    materialize_supply_chain_exposure,
    materialize_universe_supply_chain_exposure,
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

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class _MockConnection:
    def __init__(self):
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        return _MockCursor()

    def cursor(self):
        return _MockCursor()


def test_materialize_supply_chain_exposure_handles_dict_and_factor_record() -> None:
    """Consolidated: tests both raw dict rows and SupplyChainExposure factor results."""
    conn = _MockConnection()
    now = datetime(2026, 9, 25, tzinfo=UTC)
    partners = [
        SupplyChainPartner("p:tsmc", "TSMC", "supplier", revenue_share=Decimal("0.5"), confidence=Decimal("0.9")),
    ]
    rec = supply_chain_exposure(partners, entity_id="issuer:nvda", as_of=now)
    count = materialize_supply_chain_exposure(
        conn,
        run_id="run:sc_both",
        cutoff=now,
        exposure_data=[
            {
                "issuer_id": "issuer:aapl",
                "exposure_score": Decimal("0.85"),
                "direct_partners": 12,
                "availability_status": "available",
                "reason_codes": [],
            },
            rec,
        ],
    )
    assert count == 2
    assert len(conn.executed) == 2
    # Row 1: dict
    sql1, params1 = conn.executed[0]
    assert "insert into mart.issuer_supply_chain_exposure" in sql1
    assert params1[1] == "issuer:aapl"
    assert params1[3] == Decimal("0.85")
    assert params1[11] == "available"
    # Row 2: factor record
    _, params2 = conn.executed[1]
    assert params2[1] == "issuer:nvda"
    assert params2[3] == Decimal("0.25")
    assert params2[4] == 1
    assert params2[11] == "available"


def test_extract_supply_chain_relationships_directions_and_entities() -> None:
    """Consolidated: tests sentence parsing, relation direction rules, and entity name extraction."""
    # 1. Empty text check
    assert extract_supply_chain_relationships("", issuer_id="test", filing_date=date(2026, 1, 1), accession="000") == ()

    # 2. Directions and named entity extraction
    text = (
        "Item 1. Business\n"
        "The company supplies to Apple Inc. as its primary distribution partner.\n"
        "We also purchase raw materials from Acme Corp for our semiconductor needs.\n"
        "We rely on a single supplier for our key semiconductor chips.\n"
        "Our largest customer accounts for 15% of net revenues.\n"
    )
    edges = extract_supply_chain_relationships(
        text,
        issuer_id="issuer:test",
        filing_date=date(2026, 1, 1),
        accession="0001-00-00",
    )
    assert len(edges) == 4
    # supplies to Apple Inc. -> customer
    assert edges[0].relation_type == "customer"
    assert edges[0].target_entity_name == "Apple Inc."
    # purchase from Acme Corp -> supplier
    assert edges[1].relation_type == "supplier"
    assert edges[1].target_entity_name == "Acme Corp"
    # single supplier -> supplier
    assert edges[2].relation_type == "supplier"
    assert edges[2].target_entity_name == "Key Supplier"
    # largest customer -> customer
    assert edges[3].relation_type == "customer"
    assert edges[3].target_entity_name == "Major Customer"


def test_extract_supply_chain_adversarial_negative_corpus() -> None:
    """Adversarial Negative Corpus: generic customer/vendor mentions must NOT produce supply chain edges."""
    negative_text = (
        "Item 1. Business\n"
        "We maintain customer service centers across the country to assist users.\n"
        "Customer deposits are insured by the FDIC up to legal limits.\n"
        "We emphasize customer experience and customer satisfaction metrics.\n"
        "Vendor management programs are reviewed on an annual basis.\n"
    )
    edges = extract_supply_chain_relationships(
        negative_text,
        issuer_id="issuer:bank",
        filing_date=date(2026, 1, 1),
        accession="0001-00-00",
    )
    assert len(edges) == 0, f"False positives detected in negative corpus: {edges}"


def test_materialize_universe_supply_chain_exposure_iterates_all_issuers() -> None:
    conn = _MockConnection()
    tickers = {"issuer:1": "NVDA", "issuer:2": "AAPL"}
    total = materialize_universe_supply_chain_exposure(
        conn,
        run_id="run:sc_uni",
        cutoff=datetime.now(tz=UTC),
        tickers=tickers,
    )
    assert total == 2
    assert len(conn.executed) == 2
