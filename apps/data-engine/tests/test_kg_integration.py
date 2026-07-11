"""Integration tests against a real Postgres (make db-up && make db-migrate, or
the CI service container). Skip cleanly when none is reachable. Every test runs
inside one never-committed transaction with uuid-suffixed ids, so a dev database
with real universe data is never polluted or depended on."""

import uuid

import pytest
from data_engine import raw_store
from data_engine.config import settings
from data_engine.sources import moomoo_ledger as ledger
from factors.shared import entity_resolution as er

psycopg = pytest.importorskip("psycopg")


@pytest.fixture
def conn():
    try:
        c = psycopg.connect(settings.database_url, connect_timeout=3)
    except psycopg.OperationalError:
        pytest.skip("no reachable Postgres (make db-up && make db-migrate)")
    yield c
    c.rollback()
    c.close()


def test_same_as_roundtrip_idempotency_and_lookup(conn):
    uid = uuid.uuid4().hex[:10]
    company = f"company:test:{uid}"
    code = f"US.T{uid}"
    er.ensure_entity(conn, company, "company", f"Test Co {uid}")
    er.ensure_entity(conn, company, "company", "different name")  # first writer wins, no error

    assert er.add_same_as(
        conn, namespace="moomoo", value=code, entity_id=company, confidence=0.98, source="test", valid_from="2026-07-11"
    )
    # identical assertion on a later day -> skipped, not a duplicate vintage
    assert not er.add_same_as(
        conn, namespace="moomoo", value=code, entity_id=company, confidence=0.98, source="test", valid_from="2026-07-12"
    )
    # a changed assertion (new confidence) -> new vintage
    assert er.add_same_as(
        conn, namespace="moomoo", value=code, entity_id=company, confidence=0.9, source="test", valid_from="2026-07-12"
    )

    assert er.resolve(conn, "moomoo", code) == company
    assert er.resolve(conn, "moomoo", "US.DOES_NOT_EXIST") is None
    assert er.crosswalk(conn, company) == {"moomoo": [code]}
    assert (code, company) in er.identifiers(conn, "moomoo")


def test_resolve_follows_one_merge_hop(conn):
    uid = uuid.uuid4().hex[:10]
    a, b = f"company:test:{uid}a", f"company:test:{uid}b"
    er.ensure_entity(conn, a, "company", "A")
    er.ensure_entity(conn, b, "company", "B")
    er.add_same_as(
        conn, namespace="isin", value=f"XX{uid}", entity_id=a, confidence=1.0, source="test", valid_from="2026-07-11"
    )
    er.add_edge(
        conn, from_id=a, to_id=b, relation_type="same_as", confidence=0.9, source="test", valid_from="2026-07-11"
    )
    assert er.resolve(conn, "isin", f"XX{uid}") == b


def test_parallel_holds_edges_coexist_and_rerun_skips(conn):
    """One ETF holding the same company via two lines (A-share + H-share) keeps
    two holds edges, discriminated by properties.isin — and re-running writes
    neither again (the ping-pong duplicate failure mode)."""
    uid = uuid.uuid4().hex[:10]
    etf, company = f"etf:test:{uid}", f"company:test:{uid}"
    er.ensure_entity(conn, etf, "etf", "Test ETF")
    er.ensure_entity(conn, company, "company", "Dual-listed Co")

    def hold(isin, pct):
        return er.add_edge(
            conn,
            from_id=etf,
            to_id=company,
            relation_type="holds",
            confidence=1.0,
            source="test",
            valid_from="2026-02-28",
            properties={"pct_val": pct, "isin": isin, "report_period": "2026-02-28"},
        )

    assert hold("CNE000001R84", 2.08)
    assert hold("CNE1000003X6", 1.1)
    assert not hold("CNE000001R84", 2.08)
    assert not hold("CNE1000003X6", 1.1)
    # a new report period is a genuinely new vintage
    assert er.add_edge(
        conn,
        from_id=etf,
        to_id=company,
        relation_type="holds",
        confidence=1.0,
        source="test",
        valid_from="2026-05-31",
        properties={"pct_val": 2.5, "isin": "CNE000001R84", "report_period": "2026-05-31"},
    )


def test_raw_store_roundtrip(conn):
    uid = uuid.uuid4().hex[:10]
    assert not raw_store.already_fetched(conn, source="test", endpoint="ep", entity_key=uid)
    fetch_id = raw_store.insert_fetch(
        conn, source="test", endpoint="ep", entity_key=uid, payload={"a": 1}, params={"p": 2}
    )
    assert raw_store.raw_ref(fetch_id) == f"raw.fetches:{fetch_id}"
    assert raw_store.already_fetched(conn, source="test", endpoint="ep", entity_key=uid)
    # content-only rows (XML/HTML) are the other legal shape
    raw_store.insert_fetch(conn, source="test", endpoint="xml", entity_key=uid, content="<x/>")


def test_ledger_postgres_backend(conn, monkeypatch):
    monkeypatch.setattr(ledger.settings, "moomoo_ledger_backend", "postgres")
    monkeypatch.setattr(ledger, "_pg_conn", conn)  # ride the test transaction, rolled back after

    before = ledger.calls_this_month()
    ledger.record("ep", "test", ok=True)
    ledger.record("ep", "test", ok=False)  # failures count too — they may have spent server-side quota
    assert ledger.calls_this_month() == before + 2

    monkeypatch.setattr(ledger.settings, "moomoo_monthly_call_budget", before + 2)
    with pytest.raises(ledger.BudgetExceededError):
        ledger.gate("ep", "test")
