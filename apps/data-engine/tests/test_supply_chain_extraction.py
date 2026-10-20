"""Unit tests for supply_chain_extraction module (#772)."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from data_engine.datahub.canonical_issuer import CanonicalIssuer
from data_engine.datahub.standards.supply_chain_extraction import (
    extract_supply_chain_relationships,
    materialize_supply_chain_exposure,
    materialize_universe_supply_chain_exposure,
)
from factors.base.supply_chain_exposure import (
    SupplyChainPartner,
    supply_chain_exposure,
)


def _issuer_id(name: str) -> str:
    """The wide row's issuer id for a test name: a lower-case UUID. A writer refuses any other (#1079)."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"https://truealpha.invalid/test/issuer/{name}"))


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
    rec = supply_chain_exposure(partners, entity_id=_issuer_id("nvda"), as_of=now, supplies_to_edges_exist=True)
    count = materialize_supply_chain_exposure(
        conn,
        run_id="run:sc_both",
        cutoff=now,
        exposure_data=[
            {
                "issuer_id": _issuer_id("aapl"),
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
    assert params1[1] == _issuer_id("aapl")
    assert params1[3] == Decimal("0.85")
    assert params1[11] == "available"
    # Row 2: factor record
    _, params2 = conn.executed[1]
    assert params2[1] == _issuer_id("nvda")
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
    assert len(edges) == 2
    # supplies to Apple Inc. -> customer
    assert edges[0].relation_type == "customer"
    assert edges[0].target_entity_name == "Apple Inc."
    # purchase from Acme Corp -> supplier
    assert edges[1].relation_type == "supplier"
    assert edges[1].target_entity_name == "Acme Corp"


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
    issuers = [
        CanonicalIssuer(issuer_id=_issuer_id("1"), legacy_id="issuer:lei:AAAAAAAAAAAAAAAAAA01", ticker="NVDA"),
        CanonicalIssuer(issuer_id=_issuer_id("2"), legacy_id="issuer:lei:BBBBBBBBBBBBBBBBBB02", ticker="AAPL"),
    ]
    total = materialize_universe_supply_chain_exposure(
        conn,
        run_id="run:sc_uni",
        cutoff=datetime.now(tz=UTC),
        issuers=issuers,
    )
    assert total == 2
    assert len(conn.executed) == 2
    # The mock holds no `staging.kg_edges` table, so no extraction exists: both rows say so.
    reasons = [params[9] for _, params in conn.executed]
    assert reasons == [["no_supply_chain_extraction"], ["no_supply_chain_extraction"]]


def test_materialize_dict_with_partners_states_the_graph_measurement() -> None:
    conn = _MockConnection()
    now = datetime(2026, 9, 25, tzinfo=UTC)
    materialize_supply_chain_exposure(
        conn,
        run_id="run:sc_dict",
        cutoff=now,
        exposure_data=[
            {"issuer_id": _issuer_id("a"), "partners": [], "supplies_to_edges_exist": True},
            {"issuer_id": _issuer_id("b"), "partners": [], "supplies_to_edges_exist": False},
        ],
    )
    assert [params[9] for _, params in conn.executed] == [["no_disclosed_suppliers"], ["no_supply_chain_extraction"]]
    with pytest.raises(KeyError, match="supplies_to_edges_exist"):
        materialize_supply_chain_exposure(
            conn, run_id="run:sc_dict", cutoff=now, exposure_data=[{"issuer_id": _issuer_id("c"), "partners": []}]
        )


@pytest.mark.parametrize(
    "legacy_id",
    ["issuer:lei:AAAAAAAAAAAAAAAAAA01", "issuer:cik:0000320193", "", "NOT-A-UUID", str(uuid.uuid4()).upper()],
    ids=["lei", "cik", "empty", "not-a-uuid", "upper-case-uuid"],
)
def test_a_row_is_never_written_under_a_legacy_id(legacy_id: str) -> None:
    """#1079: the legacy id of the corpus is not the wide row's id, so no row may carry it."""
    conn = _MockConnection()
    with pytest.raises(ValueError, match="not the wide row's id"):
        materialize_supply_chain_exposure(
            conn,
            run_id="run:sc_legacy",
            cutoff=datetime(2026, 9, 25, tzinfo=UTC),
            exposure_data=[{"issuer_id": legacy_id, "partners": [], "supplies_to_edges_exist": True}],
        )
    assert conn.executed == [], "nothing reached the table"


def test_persist_extracted_supply_chain_edges_and_continuous_confidence() -> None:
    from data_engine.datahub.standards.supply_chain_extraction import persist_extracted_supply_chain_edges

    text = (
        "Item 1. Business\n"
        "The company supplies to Apple Inc. as its primary distribution partner across North America.\n"
    )
    edges = extract_supply_chain_relationships(
        text,
        issuer_id="company:cik:0001045810",
        filing_date=date(2026, 2, 15),
        accession="0001045810-26-000001",
    )
    assert len(edges) == 1
    assert Decimal("0.55") < edges[0].confidence < Decimal("0.70")
    assert edges[0].confidence != Decimal("0.85")

    class _FilingConn:
        def __init__(self) -> None:
            self.queries: list[tuple[str, object]] = []

        def cursor(self):
            class _RegCur(_MockCursor):
                def __enter__(self):
                    return self

                def __exit__(self, *args: object):
                    pass

                def fetchone(self):
                    return ("raw.fetches",)

            return _RegCur()

        def execute(self, sql: str, params: object = None):
            self.queries.append((sql, params))
            cur = _MockCursor()
            if "from raw.fetches" in sql:
                cur.fetchall = lambda: [  # type: ignore[method-assign]
                    (
                        101,
                        "filing-document:CIK0001045810:0001045810-26-000001",
                        "sec/10k.txt",
                        datetime(2026, 2, 15, tzinfo=UTC),
                        {"plain_text": text, "accession": "0001045810-26-000001"},
                    )
                ]
            elif "insert into staging.kg_edges" in sql:
                cur.fetchone = lambda: (1,)  # type: ignore[method-assign]
            return cur

    conn = _FilingConn()
    issuers = [CanonicalIssuer(issuer_id=_issuer_id("nvda"), legacy_id="company:cik:0001045810", ticker="NVDA")]
    inserted = persist_extracted_supply_chain_edges(
        conn,  # type: ignore[arg-type]
        issuers=issuers,
        cutoff=datetime(2026, 10, 6, tzinfo=UTC),
    )
    assert inserted == 1
    assert any("insert into staging.kg_edges" in q[0] for q in conn.queries)


def test_real_10k_html_extraction_with_lei_issuer_and_supplier_customer_directions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Standing check: `filing_plain_text` collapses whitespace to a single line, and TOPT issuers
    use `issuer:lei:...` resolved to CIK via `staging.entity_aliases`. Both supplier (`to_id`) and
    customer (`from_id`) edges must materialize onto `mart.issuer_supply_chain_exposure`."""
    from data_engine import raw_store
    from data_engine.datahub.standards.filing_extraction import filing_plain_text

    html_10k = (
        b"<html><body><p>Item 1. Business.</p>"
        b"<p>We maintain customer service centers worldwide to assist enterprise users.</p>"
        b"<p>We utilize foundries, such as Taiwan Semiconductor Manufacturing Company Limited, or TSMC, "
        b"and Samsung Electronics Co., Ltd., or Samsung, to produce our semiconductor wafers.</p>"
        b"<p>We purchase memory from SK Hynix Inc., Micron Technology, Inc., and Samsung.</p>"
        b"<p>In 2024, three wholesale distributors (McKesson Corporation, Cardinal Health, Inc. and Cencora, Inc.) "
        b"accounted for substantially all of our product sales in the United States.</p>"
        b"</body></html>"
    )
    plain = filing_plain_text(html_10k)
    assert "\n" not in plain, "filing_plain_text collapses newlines into spaces"

    nvda_uuid = _issuer_id("nvda")
    nvda_legacy = "issuer:lei:549300S4KLFTLO7GSQ80"
    monkeypatch.setattr(raw_store, "get_payload", lambda _conn, _fid, **_kw: html_10k)

    class _StatefulGraphConn:
        def __init__(self) -> None:
            self.edges: list[tuple[str, str, str, float]] = []
            self.entities: dict[str, str] = {}
            self.mart_rows: list[tuple[object, ...]] = []

        def cursor(self):
            class _RegCur(_MockCursor):
                def __enter__(self):
                    return self

                def __exit__(self, *args: object):
                    pass

                def fetchone(self):
                    return ("exists",)

            return _RegCur()

        def execute(self, sql: str, params: object = None):
            cur = _MockCursor()
            p = tuple(params) if isinstance(params, list | tuple) else ()
            if "from staging.entity_aliases" in sql:
                cur.fetchall = lambda: [(nvda_uuid, "0001045810")]  # type: ignore[method-assign]
            elif "from raw.fetches" in sql:
                cur.fetchall = lambda: [  # type: ignore[method-assign]
                    (
                        38677,
                        "filing-document:CIK0001045810:000104581025000023:nvda-20250126.htm",
                        "s3://truealpha-raw/raw/sec/nvda.htm",
                        datetime(2025, 2, 26, tzinfo=UTC),
                        {"cik": 1045810, "form": "10-K", "accession": "000104581025000023"},
                    )
                ]
            elif "insert into staging.kg_entities" in sql:
                self.entities[str(p[0])] = str(p[2])
            elif "select source, confidence from staging.kg_edges" in sql:
                cur.fetchone = lambda: None  # type: ignore[method-assign]
            elif "insert into staging.kg_edges" in sql:
                self.edges.append((str(p[0]), str(p[1]), str(p[2]), float(str(p[5]))))
                cur.fetchone = lambda: (len(self.edges),)  # type: ignore[method-assign]
            elif "select exists (select 1 from staging.kg_edges" in sql:
                cur.fetchone = lambda: (bool(self.edges),)  # type: ignore[method-assign]
            elif "from staging.kg_edges e" in sql and "where e.from_id = %s" in sql:
                target = str(p[0])
                cur.fetchall = lambda: [  # type: ignore[method-assign]
                    (to_id, self.entities.get(to_id, to_id), Decimal(str(conf)))
                    for from_id, to_id, rel, conf in self.edges
                    if from_id == target and rel == "supplies_to"
                ]
            elif "from staging.kg_edges e" in sql and "where e.to_id = %s" in sql:
                target = str(p[0])
                cur.fetchall = lambda: [  # type: ignore[method-assign]
                    (from_id, self.entities.get(from_id, from_id), Decimal(str(conf)))
                    for from_id, to_id, rel, conf in self.edges
                    if to_id == target and rel == "supplies_to"
                ]
            elif "insert into mart.issuer_supply_chain_exposure" in sql:
                self.mart_rows.append(p)
            return cur

    conn = _StatefulGraphConn()
    issuers = [CanonicalIssuer(issuer_id=nvda_uuid, legacy_id=nvda_legacy, ticker="NVDA")]
    written = materialize_universe_supply_chain_exposure(
        conn,  # type: ignore[arg-type]
        run_id="capture-run:test",
        cutoff=datetime(2026, 10, 20, tzinfo=UTC),
        issuers=issuers,
    )
    assert written == 1
    assert len(conn.mart_rows) == 1
    row = conn.mart_rows[0]
    # (run_id, issuer_id, cutoff, exposure_score, direct_partners, suppliers_count, customers_count, max_partner_share, confidence, reason_codes, extractor, avail, ...)
    assert row[1] == nvda_uuid
    assert row[3] is not None and row[3] > Decimal("0")
    assert (
        row[4] == 7
    )  # 4 suppliers (TSMC, Samsung, SK Hynix, Micron) + 3 customers (McKesson, Cardinal Health, Cencora)
    assert row[5] == 4  # suppliers_count
    assert row[6] == 3  # customers_count
    assert row[11] == "available"
