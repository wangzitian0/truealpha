"""#496: the issuer CIK predecessor registry (migration 0037) and its
resolution precedence in planning. Skips without a local Postgres."""

import os

import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub.production_topt.composition import predecessor_ciks

_XOM_ISSUER = "issuer:lei:J3WHBG0MTS7O8ZVMDC91"
_XOM_LISTING = "listing:xnys:xom"


@pytest.fixture
def connection():
    try:
        active = psycopg.connect(settings.database_url, connect_timeout=3, autocommit=False)
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs the required integration coverage")
    try:
        yield active
    finally:
        active.rollback()
        active.close()


def test_seeded_registry_row_resolves_xom(connection) -> None:
    resolved = predecessor_ciks(connection, [_XOM_LISTING], {_XOM_LISTING: _XOM_ISSUER})
    assert resolved.get(_XOM_LISTING) == 34088, "the owner-signed 0037 seed must resolve XOM"


def test_registry_rows_are_append_only_and_signed(connection) -> None:
    reason, approver = connection.execute(
        "select reason, approved_by from staging.issuer_cik_predecessors where issuer_id = %s",
        (_XOM_ISSUER,),
    ).fetchone()
    assert "2115436" in reason and "zitian" in approver
    with pytest.raises(psycopg.errors.CheckViolation):
        connection.execute(
            "update staging.issuer_cik_predecessors set predecessor_cik = 1 where issuer_id = %s",
            (_XOM_ISSUER,),
        )


def test_unregistered_issuer_resolves_nothing(connection) -> None:
    resolved = predecessor_ciks(
        connection, ["listing:xnas:aapl"], {"listing:xnas:aapl": "issuer:lei:HWUPKR0MPOU8FGXBT394"}
    )
    assert "listing:xnas:aapl" not in resolved, "no registry row and no lineage -> honest absence"
