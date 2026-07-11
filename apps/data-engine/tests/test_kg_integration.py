"""Integration tests against a real Postgres (make runtime-up / make db-migrate,
or the CI service container). Skip cleanly when none is reachable. Every test
runs inside one never-committed transaction with uuid-suffixed ids, so a dev
database with real universe data is never polluted or depended on.

transaction_time is always passed explicitly (the runtime contract dropped the
column defaults), so tests pin distinct instants instead of racing now()."""

import uuid
from datetime import UTC, datetime

import pytest
from data_engine.config import settings
from data_engine.sources import moomoo_ledger as ledger
from factors.shared import entity_resolution as er

psycopg = pytest.importorskip("psycopg")

T1 = datetime(2026, 7, 1, tzinfo=UTC)
T2 = datetime(2026, 7, 2, tzinfo=UTC)
T3 = datetime(2026, 7, 3, tzinfo=UTC)
NOW = datetime.now(UTC)
REF = "raw.fetches:0"  # raw_ref is lineage text, not a foreign key


@pytest.fixture
def conn():
    try:
        c = psycopg.connect(settings.database_url, connect_timeout=3)
    except psycopg.OperationalError:
        pytest.skip("no reachable Postgres (make runtime-up && make db-migrate)")
    yield c
    c.rollback()
    c.close()


def _ident(conn, entity_id, value, *, transaction_time=T1, confidence=0.98, id_type="moomoo", source="openfigi"):
    return er.assert_identifier(
        conn,
        entity_id=entity_id,
        source=source,
        identifier_type=id_type,
        identifier_value=value,
        confidence=confidence,
        transaction_time=transaction_time,
        valid_from=transaction_time.date().isoformat(),
        raw_ref=REF,
    )


def test_identifier_roundtrip_idempotency_and_lookup(conn):
    uid = uuid.uuid4().hex[:10]
    company = f"company:test:{uid}"
    code = f"US.T{uid}"
    er.ensure_entity(conn, company, "company", f"Test Co {uid}")
    er.ensure_entity(conn, company, "company", "different name")  # first writer wins, no error

    assert _ident(conn, company, code, transaction_time=T1)
    # identical assertion later -> already current, skipped
    assert not _ident(conn, company, code, transaction_time=T2)
    # a changed assertion (new confidence) -> new vintage
    assert _ident(conn, company, code, transaction_time=T2, confidence=0.9)

    assert er.resolve(conn, "moomoo", code, as_of=NOW) == company
    assert er.resolve(conn, "moomoo", code, as_of=datetime(2026, 6, 1, tzinfo=UTC)) is None  # before T1
    assert er.resolve(conn, "moomoo", "US.DOES_NOT_EXIST", as_of=NOW) is None
    assert er.crosswalk(conn, company, as_of=NOW) == {"moomoo": [code]}
    assert (code, company) in er.identifiers(conn, "moomoo", as_of=NOW)


def test_identifier_correction_can_be_corrected_back(conn):
    """Identifier rows are pointer-like: after X->A is superseded by X->B,
    re-asserting X->A must append a vintage that wins in resolve() — deduping
    against all history would make the supersession permanent."""
    uid = uuid.uuid4().hex[:10]
    a, b = f"company:test:{uid}a", f"company:test:{uid}b"
    isin = f"XX{uid}"
    er.ensure_entity(conn, a, "company", "A")
    er.ensure_entity(conn, b, "company", "B")

    assert _ident(conn, a, isin, id_type="isin", transaction_time=T1)
    assert _ident(conn, b, isin, id_type="isin", transaction_time=T2)  # bad upstream data
    assert er.resolve(conn, "isin", isin, as_of=NOW) == b
    assert _ident(conn, a, isin, id_type="isin", transaction_time=T3)  # corrected back — must insert
    assert er.resolve(conn, "isin", isin, as_of=NOW) == a
    # and re-asserting what is now current is a no-op
    assert not _ident(conn, a, isin, id_type="isin", transaction_time=T3)


def test_resolve_follows_one_merge_hop(conn):
    uid = uuid.uuid4().hex[:10]
    a, b = f"company:test:{uid}a", f"company:test:{uid}b"
    er.ensure_entity(conn, a, "company", "A")
    er.ensure_entity(conn, b, "company", "B")
    _ident(conn, a, f"XX{uid}", id_type="isin")
    er.add_edge(
        conn,
        from_id=a,
        to_id=b,
        relation_type="same_as",
        confidence=0.9,
        source="test",
        transaction_time=T2,
        valid_from="2026-07-02",
        raw_ref=REF,
    )
    assert er.resolve(conn, "isin", f"XX{uid}", as_of=NOW) == b


def test_holds_edge_rerun_skips_and_new_filing_appends(conn):
    uid = uuid.uuid4().hex[:10]
    etf, company = f"etf:test:{uid}", f"company:test:{uid}"
    er.ensure_entity(conn, etf, "etf", "Test ETF")
    er.ensure_entity(conn, company, "company", "Held Co")

    def hold(tx, valid):
        return er.add_edge(
            conn,
            from_id=etf,
            to_id=company,
            relation_type="holds",
            confidence=1.0,
            source="nport",
            transaction_time=tx,
            valid_from=valid,
            raw_ref=REF,
        )

    assert hold(T1, "2026-02-28")
    assert not hold(T1, "2026-02-28")  # re-run of the same filing
    assert not hold(T2, "2026-02-28")  # same assertion at a later time is still current
    # a genuinely different assertion (nothing changed here except... nothing) stays skipped;
    # a new filing writes through the changed-confidence path in real life. Simulate a
    # revision: same pair, revised confidence -> new vintage.
    assert er.add_edge(
        conn,
        from_id=etf,
        to_id=company,
        relation_type="holds",
        confidence=0.9,
        source="nport",
        transaction_time=T2,
        valid_from="2026-05-31",
        raw_ref=REF,
    )


def test_fund_holding_dual_lines_coexist_and_rerun_conflicts_away(conn):
    """MCHI really holds one issuer via the A-share AND H-share line in one
    period with an identical name — 0005's unique key keeps both, and a re-run
    of the same filing collapses onto the existing rows (on conflict)."""
    uid = uuid.uuid4().hex[:10]
    etf, company = f"etf:test:{uid}", f"company:test:{uid}"
    er.ensure_entity(conn, etf, "etf", "Test ETF")
    er.ensure_entity(conn, company, "company", "Ping An Test")

    def line(isin, pct, value):
        return conn.execute(
            """
            insert into staging.fund_holding_facts
                (fund_id, holding_id, holding_name, report_period, transaction_time,
                 isin, value_usd, percent_of_net_assets, confidence, raw_ref)
            values (%s, %s, 'Ping An Test Co', '2026-02-28', %s, %s, %s, %s, 1.0, %s)
            on conflict do nothing
            returning id
            """,
            (etf, company, T1, isin, value, pct, REF),
        ).fetchone()

    assert line("CNE000001R84", 2.08, 1000.0) is not None
    assert line("CNE1000003X6", 1.10, 500.0) is not None  # H-share line, same name
    assert line("CNE000001R84", 2.08, 1000.0) is None  # re-run collapses
    assert line("CNE1000003X6", 1.10, 500.0) is None


def test_ledger_postgres_backend(conn, monkeypatch):
    monkeypatch.setattr(ledger.settings, "moomoo_ledger_backend", "postgres")
    monkeypatch.setattr(ledger, "_pg_conn", conn)  # ride the test transaction, rolled back after

    # Assertions are delta-based: in postgres mode calls_this_month() also sums
    # the local json ledger (probe sessions must stay visible to a sweep's
    # gate), and both stores may carry real prior calls on a dev machine.
    before = ledger.calls_this_month()
    ledger.record("ep", "test", ok=True)
    ledger.record("ep", "test", ok=False)  # failures count too — they may have spent server-side quota
    assert ledger.calls_this_month() == before + 2

    monkeypatch.setattr(ledger.settings, "moomoo_monthly_call_budget", before + 2)
    with pytest.raises(ledger.BudgetExceededError):
        ledger.gate("ep", "test")
