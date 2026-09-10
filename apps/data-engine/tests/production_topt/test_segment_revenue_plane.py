"""The segment-revenue fact plane's contract (#772, q6).

A headcount is one number per issuer; segment revenue is many rows that are only meaningful
as a SET. A share computed over a set that missed a segment silently raises every remaining
segment's share and inverts the ranking q6 exists to produce — so the plane carries the set's
own accounting identity (`partition_id`, `partition_total`, `partition_residual`) rather than
leaving a consumer to trust rows it did not accept.

These assert the properties a reader depends on: the rows are append-only, a partition cannot
state the same segment twice, and the identity columns are there to be re-checked.
"""

from __future__ import annotations

import os
from decimal import Decimal

import psycopg
import pytest
from data_engine.config import settings

TABLE = "staging.issuer_segment_revenue_facts"
CIK = 999_000_001  # outside the real corpus; these rows are this test's own
PARTITION = "segment-partition:" + "b" * 64


@pytest.fixture
def connection():
    try:
        active = psycopg.connect(settings.database_url, connect_timeout=3, autocommit=False)
    except psycopg.OperationalError as error:
        # Fail where a database is configured; skip only on a bare developer machine
        # (the convention every production_topt test follows, and the one I had to be
        # told about on #798).
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs the required integration coverage")
    try:
        yield active
    finally:
        # Append-only by trigger, so cleanup by DELETE is impossible by design — the
        # transaction is rolled back instead.
        active.rollback()
        active.close()


def _insert(connection, *, segment: str, value: str, partition: str = PARTITION, residual: str = "0"):
    return connection.execute(
        f"""
        insert into {TABLE}
            (cik, segment_name, segment_revenue, partition_id, partition_total,
             partition_residual, knowable_at, period_end, source, evidence_ref,
             extractor, confidence)
        values (%s, %s, %s, %s, %s, %s, now(), '2025-09-27', '10k-extraction',
                'accession:0000320193-25-000079 span:segment-note', 'rule:exhaustive-partition:v1', 0.85)
        """,  # noqa: S608 - TABLE is a module constant, not input
        (CIK, segment, Decimal(value), partition, Decimal("250"), Decimal(residual)),
    )


def test_a_partition_lands_one_row_per_segment(connection) -> None:
    for segment, value in (("Americas", "100"), ("Europe", "90"), ("Rest of world", "60")):
        _insert(connection, segment=segment, value=value)
    rows = connection.execute(
        f"select segment_name, segment_revenue from {TABLE} where cik = %s order by segment_name",  # noqa: S608
        (CIK,),
    ).fetchall()
    assert [r[0] for r in rows] == ["Americas", "Europe", "Rest of world"]
    assert sum(r[1] for r in rows) == Decimal("250"), "the parts sum to the stored partition_total"


def test_the_same_segment_cannot_be_stated_twice_in_one_partition(connection) -> None:
    """Not a correction path — a correction is a NEW partition. This blocks one extraction
    writing a segment twice, which would double-count it into every share."""
    _insert(connection, segment="Americas", value="100")
    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert(connection, segment="Americas", value="50")


def test_a_restatement_is_a_new_partition_not_an_update(connection) -> None:
    """Both partitions coexist; the reader picks by knowable_at. An UPDATE would rewrite
    history, which is what the trigger below refuses."""
    _insert(connection, segment="Americas", value="100")
    later = "segment-partition:" + "c" * 64
    _insert(connection, segment="Americas", value="110", partition=later)
    rows = connection.execute(
        f"select partition_id, segment_revenue from {TABLE} where cik = %s and segment_name = 'Americas'",  # noqa: S608
        (CIK,),
    ).fetchall()
    assert len(rows) == 2, "a restatement adds a vintage; it never replaces one"


def test_the_plane_is_append_only(connection) -> None:
    _insert(connection, segment="Americas", value="100")
    with pytest.raises(psycopg.errors.RaiseException):
        connection.execute(f"update {TABLE} set segment_revenue = 999 where cik = %s", (CIK,))  # noqa: S608
    connection.rollback()
    _insert(connection, segment="Americas", value="100")
    with pytest.raises(psycopg.errors.RaiseException):
        connection.execute(f"delete from {TABLE} where cik = %s", (CIK,))  # noqa: S608


def test_the_partition_identity_is_re_checkable_from_the_rows(connection) -> None:
    """The point of storing `partition_total` and `partition_residual`: a consumer can
    verify the set it is about to compute shares from, instead of trusting that whoever
    wrote the rows checked."""
    for segment, value in (("Americas", "100"), ("Europe", "90"), ("Rest of world", "59.8")):
        _insert(connection, segment=segment, value=value, residual="0.2")
    total, residual, parts = connection.execute(
        f"""
        select max(partition_total), max(partition_residual), sum(segment_revenue)
        from {TABLE} where partition_id = %s
        """,  # noqa: S608
        (PARTITION,),
    ).fetchone()
    assert total - parts == residual, "the stored residual must be the one the rows imply"
    assert abs(residual) <= Decimal("0.5")


def test_a_malformed_partition_id_is_refused(connection) -> None:
    """The id is content-addressed by the producer; a free-form string would let two
    different extractions claim to be the same set."""
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert(connection, segment="Americas", value="100", partition="whatever")


def test_the_writer_and_the_plane_agree(connection) -> None:
    """The join the two halves above never test: `record_segment_partition` builds the rows,
    the plane accepts them. Column names, types, the content-addressed id format and the
    `source` the standard declares are all things that can drift apart while each side's own
    tests stay green — this is where a drift shows up as a database error instead of as an
    empty table nobody notices.
    """
    from datetime import UTC, date, datetime

    from data_engine.datahub.standards.segment_extraction import (
        SEGMENT_SOURCE,
        partition_already_recorded,
        partition_id_for,
        record_segment_partition,
    )
    from truealpha_contracts.standards import STANDARDS

    period_end = date(2025, 11, 2)
    parts = [("Semiconductor solutions", Decimal("36858000000")), ("Infrastructure software", Decimal("27029000000"))]
    partition_id = partition_id_for(CIK, period_end, parts)

    assert not partition_already_recorded(connection, partition_id), "nothing written yet"
    landed = record_segment_partition(
        connection,
        cik=CIK,
        partition_id=partition_id,
        period_end=period_end,
        parts=parts,
        partition_total=Decimal("63887000000"),
        partition_residual=Decimal("0"),
        knowable_at=datetime(2025, 12, 12, tzinfo=UTC),
        evidence_ref="accession=0001730168-25-000121 form=10-K",
        extractor="rule:exhaustive-partition:v1",
        confidence=Decimal("0.85"),
    )
    assert landed == 2

    rows = connection.execute(
        f"""
        select segment_name, segment_revenue, partition_total, partition_residual,
               period_end, source, knowable_at
        from {TABLE} where partition_id = %s order by segment_name
        """,  # noqa: S608
        (partition_id,),
    ).fetchall()
    assert len(rows) == 2
    assert sum(r[1] for r in rows) + rows[0][3] == rows[0][2], "the parts and the stored identity agree"
    assert {r[4] for r in rows} == {period_end}
    assert {r[5] for r in rows} == {SEGMENT_SOURCE}
    assert SEGMENT_SOURCE in STANDARDS["segment_revenue"].plane.source_priority, (
        "a source the standard's fusion priority does not list can never be selected"
    )
    assert {r[6] for r in rows} == {datetime(2025, 12, 12, tzinfo=UTC)}
    assert partition_already_recorded(connection, partition_id), "a re-run must see what it wrote"
