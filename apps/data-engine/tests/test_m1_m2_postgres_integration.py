"""Physical PostgreSQL Integration Test for M1 & M2 Materializers (#771, #772).

Tests physical database table constraints, DDL conformance, and cell extraction
against a real PostgreSQL database (localhost:5432).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from data_engine.datahub import question_coverage
from data_engine.datahub.analyst_ratings import (
    AnalystRatingItem,
    analyst_track_record,
    materialize_analyst_ratings,
)
from data_engine.datahub.question_coverage import (
    Cell,
    GovernedHead,
    analyst_rating_cells,
    compile_report,
    supply_chain_cells,
)
from data_engine.datahub.standards.supply_chain_extraction import (
    SupplyChainPartner,
    materialize_supply_chain_exposure,
    materialize_universe_supply_chain_exposure,
    supply_chain_exposure,
)
from truealpha_contracts.question_requirements import QUESTION_REQUIREMENTS_SHA256


@pytest.fixture
def connection():
    url = (
        os.environ.get("TRUEALPHA_TEST_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or "postgresql://postgres@localhost:5432/truealpha_m1_m2"
    )
    try:
        conn = psycopg.connect(url, connect_timeout=3, autocommit=False)
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs the required integration coverage")

    try:
        with conn.cursor() as cur:
            cur.execute(
                "select to_regclass('mart.issuer_analyst_ratings'), to_regclass('mart.issuer_supply_chain_exposure')"
            )
            res = cur.fetchone()
            if not (res and res[0] and res[1]):
                if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
                    pytest.fail(
                        "Connected to Postgres but M1/M2 mart tables are absent — run migrations before this shard.",
                        pytrace=False,
                    )
                pytest.skip("PostgreSQL mart tables not available")
        yield conn
    finally:
        conn.rollback()
        conn.close()


def test_physical_postgres_analyst_ratings_and_supply_chain_insertion(connection: psycopg.Connection[Any]) -> None:
    now = datetime.now(tz=UTC)
    run_id = f"test_run_{int(now.timestamp())}"
    conn = connection
    # 1. Evaluate factor and materialize analyst ratings
    ratings = [
        AnalystRatingItem("analyst:test:1", 5, confidence=Decimal("0.9")),
        AnalystRatingItem("analyst:test:2", 4, confidence=Decimal("0.8")),
    ]
    rec_analyst = analyst_track_record(ratings, entity_id="issuer:test:pg_aapl", as_of=now)
    count_a = materialize_analyst_ratings(conn, run_id=run_id, cutoff=now, ratings_data=[rec_analyst])
    assert count_a == 1

    # 2. Evaluate factor and materialize supply chain exposure
    partners = [
        SupplyChainPartner("p:tsmc", "TSMC", "supplier", revenue_share=Decimal("0.4"), confidence=Decimal("0.9")),
        SupplyChainPartner(
            "p:foxconn", "Foxconn", "supplier", revenue_share=Decimal("0.3"), confidence=Decimal("0.85")
        ),
    ]
    rec_sc = supply_chain_exposure(partners, entity_id="issuer:test:pg_aapl", as_of=now, supplies_to_edges_exist=True)
    count_sc = materialize_supply_chain_exposure(conn, run_id=run_id, cutoff=now, exposure_data=[rec_sc])
    assert count_sc == 1

    conn.commit()

    # 3. Read back cells and verify Touch Reality invariant
    cells_a = analyst_rating_cells(conn, run_id)
    assert len(cells_a) == 1
    assert cells_a[0].subject_id == "issuer:test:pg_aapl"
    assert cells_a[0].answered is True
    assert cells_a[0].reason is None

    cells_sc = supply_chain_cells(conn, run_id)
    assert len(cells_sc) == 1
    assert cells_sc[0].subject_id == "issuer:test:pg_aapl"
    assert cells_sc[0].answered is True
    assert cells_sc[0].reason is None

    # Clean up test rows
    with conn.cursor() as cur:
        cur.execute("delete from mart.issuer_analyst_ratings where run_id = %s", (run_id,))
        cur.execute("delete from mart.issuer_supply_chain_exposure where run_id = %s", (run_id,))
    conn.commit()


# #772: the reason an unavailable supply-chain row carries must match what the graph knows.
# The cutoff lies before every row any other test commits. The graph read is point in time,
# so edges knowable after the cutoff stay invisible and these tests see only their own seed.
_T772_CUTOFF = datetime(1990, 6, 30, tzinfo=UTC)
_T772_KNOWABLE = datetime(1990, 1, 1, tzinfo=UTC)
_T772_ISSUERS = {"issuer:t772:a": "AAA", "issuer:t772:b": "BBB"}


def _seed_edge(
    conn: psycopg.Connection[Any],
    *,
    from_id: str,
    to_id: str,
    relation_type: str,
    knowable_at: datetime = _T772_KNOWABLE,
    valid: str = "[1989-01-01,)",
) -> None:
    for entity_id in (from_id, to_id):
        conn.execute(
            "insert into staging.kg_entities (id, entity_type, display_name) values (%s, 'company', %s) "
            "on conflict (id) do nothing",
            (entity_id, entity_id),
        )
    conn.execute(
        "insert into staging.kg_edges "
        "(from_id, to_id, relation_type, valid_time, transaction_time, confidence, source, raw_ref) "
        "values (%s, %s, %s, %s::daterange, %s, 0.9, 'test:t772', 'raw:t772')",
        (from_id, to_id, relation_type, valid, knowable_at),
    )


def _materialize_t772(conn: psycopg.Connection[Any], run_id: str) -> dict[str, tuple[str, list[str], int]]:
    written = materialize_universe_supply_chain_exposure(
        conn, run_id=run_id, cutoff=_T772_CUTOFF, tickers=_T772_ISSUERS
    )
    assert written == len(_T772_ISSUERS)
    rows = conn.execute(
        "select issuer_id, availability_status, reason_codes, direct_partners "
        "from mart.issuer_supply_chain_exposure where run_id = %s",
        (run_id,),
    ).fetchall()
    return {str(row[0]): (str(row[1]), list(row[2]), int(row[3])) for row in rows}


def test_a_graph_with_no_supplies_to_edge_says_no_extraction_ran(connection: psycopg.Connection[Any]) -> None:
    # `holds` and `same_as` edges exist, as on Staging. Neither is a supplier edge, so neither
    # may count as an extraction and neither may turn an issuer into a supply-chain partner.
    _seed_edge(connection, from_id="fund:t772", to_id="issuer:t772:a", relation_type="holds")
    _seed_edge(connection, from_id="issuer:t772:a", to_id="figi:t772:a", relation_type="same_as")
    rows = _materialize_t772(connection, "run:t772:no-extraction")
    assert rows == {
        "issuer:t772:a": ("unavailable", ["no_supply_chain_extraction"], 0),
        "issuer:t772:b": ("unavailable", ["no_supply_chain_extraction"], 0),
    }


def test_edges_for_one_issuer_leave_the_other_with_no_disclosed_suppliers(
    connection: psycopg.Connection[Any],
) -> None:
    _seed_edge(connection, from_id="issuer:t772:a", to_id="customer:t772", relation_type="supplies_to")
    rows = _materialize_t772(connection, "run:t772:edges-for-a")
    assert rows == {
        "issuer:t772:a": ("available", [], 1),
        "issuer:t772:b": ("unavailable", ["no_disclosed_suppliers"], 0),
    }


def test_an_edge_knowable_after_the_cutoff_is_not_an_extraction(connection: psycopg.Connection[Any]) -> None:
    _seed_edge(
        connection,
        from_id="issuer:t772:a",
        to_id="customer:t772",
        relation_type="supplies_to",
        knowable_at=datetime(1991, 1, 1, tzinfo=UTC),
    )
    rows = _materialize_t772(connection, "run:t772:edge-after-cutoff")
    assert rows == {
        "issuer:t772:a": ("unavailable", ["no_supply_chain_extraction"], 0),
        "issuer:t772:b": ("unavailable", ["no_supply_chain_extraction"], 0),
    }


def test_an_expired_edge_still_proves_an_extraction_ran(connection: psycopg.Connection[Any]) -> None:
    # The relationship ended before the cutoff, so no issuer has a partner at the cutoff.
    # The graph still holds a supplier edge, so the system did extract: not "no extraction".
    _seed_edge(
        connection,
        from_id="issuer:t772:a",
        to_id="customer:t772",
        relation_type="supplies_to",
        valid="[1980-01-01,1985-01-01)",
    )
    rows = _materialize_t772(connection, "run:t772:expired-edge")
    assert rows == {
        "issuer:t772:a": ("unavailable", ["no_disclosed_suppliers"], 0),
        "issuer:t772:b": ("unavailable", ["no_disclosed_suppliers"], 0),
    }


_T772_UNIVERSE = "universe:qqq-us-2026-06-30"
_T772_HEAD = "capture-run:t772-head"


def _head_with_issuers(monkeypatch: pytest.MonkeyPatch) -> None:
    """A governed head over the two issuers. Head resolution has its own tests; this seam only
    names the head, so q3 and q4 are read from the real mart tables through `compile_report`."""
    monkeypatch.setattr(
        question_coverage,
        "governed_head",
        lambda _connection, *, universe_prefix: GovernedHead(_T772_UNIVERSE, _T772_HEAD, _T772_CUTOFF),
    )
    monkeypatch.setattr(
        question_coverage,
        "gppe_cells",
        lambda _connection, _run_id: tuple(Cell(issuer_id, True) for issuer_id in _T772_ISSUERS),
    )


def test_the_report_counts_unavailable_q3_and_q4_rows_by_reason_and_names_their_columns(
    connection: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Staging report listed `column: null` and `missing: 20` for q3 and q4. A head whose
    q3 and q4 rows are all unavailable must report the column, `answered 0`, and the reason."""
    _head_with_issuers(monkeypatch)
    # q3 comes from the real writer over a graph with no supplier edge.
    written = materialize_universe_supply_chain_exposure(
        connection, run_id=_T772_HEAD, cutoff=_T772_CUTOFF, tickers=_T772_ISSUERS
    )
    assert written == len(_T772_ISSUERS)
    # q4 rows carry the shape the analyst-ratings writer gives an issuer without coverage.
    for issuer_id in _T772_ISSUERS:
        connection.execute(
            "insert into mart.issuer_analyst_ratings "
            "(run_id, issuer_id, cutoff, reason_codes, availability_status, source_evidence_status, "
            "factor_validation_status) "
            "values (%s, %s, %s, array['no_analyst_coverage'], 'unavailable', 'degraded', 'not_evaluated')",
            (_T772_HEAD, issuer_id, _T772_CUTOFF),
        )

    report = compile_report(connection, universe="universe-list:qqq", executed_at=datetime(2026, 10, 6, tzinfo=UTC))

    assert report is not None
    assert report["requirements_sha256"] == QUESTION_REQUIREMENTS_SHA256
    q3 = report["questions"]["q3"]
    assert q3["column"] == "mart.issuer_supply_chain_exposure.exposure_score"
    assert (q3["denominator"], q3["answered"], q3["missing"]) == (2, 0, 0)
    assert q3["unavailable"] == {"no_supply_chain_extraction": 2}
    q4 = report["questions"]["q4"]
    assert q4["column"] == "mart.issuer_analyst_ratings.consensus_rating"
    assert (q4["denominator"], q4["answered"], q4["missing"]) == (2, 0, 0)
    assert q4["unavailable"] == {"no_analyst_coverage": 2}


def test_the_report_answers_q3_and_q4_only_for_a_row_that_carries_the_value(
    connection: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _head_with_issuers(monkeypatch)
    # q3: issuer a has a supplier edge, issuer b has none while the graph holds edges.
    _seed_edge(connection, from_id="issuer:t772:a", to_id="customer:t772", relation_type="supplies_to")
    materialize_universe_supply_chain_exposure(
        connection, run_id=_T772_HEAD, cutoff=_T772_CUTOFF, tickers=_T772_ISSUERS
    )
    # q4: issuer a has a rating, issuer b is `available` with a null rating.
    for issuer_id, rating in (("issuer:t772:a", Decimal("4.2")), ("issuer:t772:b", None)):
        connection.execute(
            "insert into mart.issuer_analyst_ratings "
            "(run_id, issuer_id, cutoff, consensus_rating, availability_status, source_evidence_status, "
            "factor_validation_status) values (%s, %s, %s, %s, 'available', 'verified', 'accepted')",
            (_T772_HEAD, issuer_id, _T772_CUTOFF, rating),
        )

    report = compile_report(connection, universe="universe-list:qqq", executed_at=datetime(2026, 10, 6, tzinfo=UTC))

    assert report is not None
    q3 = report["questions"]["q3"]
    assert (q3["answered"], q3["missing"], q3["unavailable"]) == (1, 0, {"no_disclosed_suppliers": 1})
    q4 = report["questions"]["q4"]
    assert (q4["answered"], q4["missing"], q4["unavailable"]) == (1, 0, {"null_metric_value": 1})


def test_a_head_with_no_q3_or_q4_row_reports_no_row_and_no_missing(
    connection: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`missing` means no column is bound. An issuer with a bound column and no row is
    `unavailable:no_row`, so the M1 acceptance (zero missing) never hides a gap behind a count."""
    _head_with_issuers(monkeypatch)

    report = compile_report(connection, universe="universe-list:qqq", executed_at=datetime(2026, 10, 6, tzinfo=UTC))

    assert report is not None
    for question in ("q3", "q4"):
        entry = report["questions"][question]
        assert entry["column"] is not None
        assert (entry["answered"], entry["missing"]) == (0, 0)
        assert entry["unavailable"] == {"no_row": 2}
