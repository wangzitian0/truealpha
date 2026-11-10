"""`mart.oracle_outcomes` is the record of every outside comparison (#1523 S1).

A comparison of a served value with a witness value that this repository did not write is one
row. The table is append-only. A replay of one comparison cannot write it twice, because the key
is (run, subject, field, period, oracle kind). The run clock, not an insertion default, dates a
row. Nothing writes to the table in this step; the tests insert rows by hand.

Every test builds a scratch database from the real migration chain and rolls its rows back.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import psycopg
import pytest
from psycopg import errors, sql
from truealpha_runtime.testing import isolated_test_database

COMPARED_AT = datetime(2026, 10, 27, 0, 45, tzinfo=UTC)
KNOWABLE_AT = datetime(2026, 10, 24, 8, 7, tzinfo=UTC)

#: One compared row (the PDD shape of #1523: served shares four times the witness).
DISAGREE = {
    "run_id": "capture-run:" + "a" * 64,
    "subject_id": "listing:xnas:pdd",
    "field": "listed_units",
    "period": "2026-10-23",
    "served_value": "5693585848",
    "oracle_value": "1393600000",
    "oracle_kind": "operator",
    "source": "nasdaq-index",
    "observation_id": "etf-constituent-fact:1",
    "knowable_at": KNOWABLE_AT,
    "ratio": "4.085",
    "tolerance": "0.05",
    "result": "disagree",
    "reason": None,
    "compared_at": COMPARED_AT,
}


@pytest.fixture(scope="module")
def database() -> Iterator[str]:
    """One database with the declared chain applied. Tests never commit to it."""
    with isolated_test_database("oracle_outcomes") as db:
        yield str(db)


@pytest.fixture
def conn(database: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(database) as connection:
        try:
            yield connection
        finally:
            connection.rollback()


def insert(conn: psycopg.Connection, *, conflict: str = "", **overrides: object) -> int:
    """Insert one row; returns the number of rows written (0 when a conflict clause ignored it)."""
    row = {**DISAGREE, **overrides}
    columns = sql.SQL(", ").join(sql.Identifier(name) for name in row)
    marks = sql.SQL(", ").join(sql.Placeholder() for _ in row)
    statement = sql.SQL("insert into mart.oracle_outcomes ({}) values ({}) {}").format(
        columns, marks, sql.SQL(conflict)
    )
    return conn.execute(statement, list(row.values())).rowcount


def test_a_row_cannot_be_updated_or_deleted(conn: psycopg.Connection) -> None:
    assert insert(conn) == 1
    with pytest.raises(errors.RaiseException, match="append-only"), conn.transaction():
        conn.execute("update mart.oracle_outcomes set result = 'agree'")
    with pytest.raises(errors.RaiseException, match="append-only"), conn.transaction():
        conn.execute("delete from mart.oracle_outcomes")
    assert conn.execute("select count(*) from mart.oracle_outcomes").fetchone() == (1,)


def test_one_comparison_is_written_once(conn: psycopg.Connection) -> None:
    """The key is (run, subject, field, period, oracle kind). A replay writes nothing new."""
    assert insert(conn) == 1
    with pytest.raises(errors.UniqueViolation), conn.transaction():
        insert(conn)
    # The writer of the next step uses `on conflict do nothing`: a replay is ignored by design,
    # even when the replay saw another source or another observation of the same comparison.
    assert insert(conn, conflict="on conflict do nothing", source="another-source", observation_id="x:2") == 0
    assert conn.execute("select count(*) from mart.oracle_outcomes").fetchone() == (1,)
    # Another oracle kind, field, period, subject or run is another comparison.
    assert insert(conn, oracle_kind="filing", observation_id="filing:1", source="sec-company-facts") == 1
    assert insert(conn, field="revenue") == 1
    assert insert(conn, period="2026-10-30") == 1
    assert insert(conn, subject_id="listing:xnas:aapl") == 1
    assert insert(conn, run_id="capture-run:" + "b" * 64) == 1


def test_the_run_clock_dates_a_row_not_an_insertion_default(conn: psycopg.Connection) -> None:
    row = {key: value for key, value in DISAGREE.items() if key != "compared_at"}
    columns = sql.SQL(", ").join(sql.Identifier(name) for name in row)
    marks = sql.SQL(", ").join(sql.Placeholder() for _ in row)
    with pytest.raises(errors.NotNullViolation), conn.transaction():
        conn.execute(
            sql.SQL("insert into mart.oracle_outcomes ({}) values ({})").format(columns, marks), list(row.values())
        )


@pytest.mark.parametrize(
    ("overrides", "violated"),
    [
        ({"oracle_kind": "vendor"}, "oracle_kind"),
        ({"result": "unknown"}, "result"),
        # A compared row carries the numbers it was compared by.
        ({"oracle_value": None}, "compared"),
        ({"ratio": None}, "compared"),
        ({"tolerance": None}, "compared"),
        ({"knowable_at": None}, "compared"),
        ({"observation_id": None}, "compared"),
        ({"result": "agree", "served_value": None}, "compared"),
        # A row that could not compare says why.
        ({"result": "not_comparable", "reason": None}, "not_comparable"),
        ({"result": "not_comparable", "reason": ""}, "reason"),
        ({"run_id": ""}, "run_id"),
        ({"ratio": "-1"}, "ratio"),
    ],
)
def test_a_row_that_breaks_the_shape_is_refused(
    conn: psycopg.Connection, overrides: dict[str, object], violated: str
) -> None:
    with pytest.raises(errors.CheckViolation) as refused, conn.transaction():
        insert(conn, **overrides)
    assert violated in (refused.value.diag.constraint_name or "")


def test_a_row_that_could_not_compare_may_leave_the_numbers_empty(conn: psycopg.Connection) -> None:
    assert (
        insert(
            conn,
            result="not_comparable",
            reason="no_comparator",
            served_value=None,
            oracle_value=None,
            ratio=None,
            tolerance=None,
            knowable_at=None,
            observation_id=None,
        )
        == 1
    )


@pytest.mark.parametrize("role", ["mart_readonly", "app_ops_reader"])
def test_the_dashboard_roles_can_read_and_not_write(conn: psycopg.Connection, role: str) -> None:
    """`/admin/datahub` reads as `app_ops_reader`; the Web App and MCP read as `mart_readonly`."""
    assert insert(conn) == 1
    conn.execute(sql.SQL("set local role {}").format(sql.Identifier(role)))
    assert conn.execute("select count(*), min(result) from mart.oracle_outcomes").fetchone() == (1, "disagree")
    with pytest.raises(errors.InsufficientPrivilege), conn.transaction():
        insert(conn, field="revenue")


def test_the_operator_last_sale_price_has_a_column(conn: psycopg.Connection) -> None:
    column = conn.execute(
        """
        select data_type, is_nullable from information_schema.columns
        where table_schema = 'staging' and table_name = 'etf_constituent_facts' and column_name = 'last_sale_price'
        """
    ).fetchone()
    assert column == ("numeric", "YES")
