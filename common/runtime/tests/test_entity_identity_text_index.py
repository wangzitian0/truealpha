"""mart.entity_identity serves `entity_id::text = ...` lookups from an index (#1699).

mart keys hold entity ids as text, so readers join on `entity_id::text`. Without an
expression index each lookup scans the whole view; on staging the wide panel read took
9.2 s and failed /research/rankings and /research/coverage on the 5 s statement_timeout.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Generator

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from truealpha_runtime.testing import apply_migration_chain, guard_db_write

#: The lookup shape that panel_service._load_wide_panel and strategy_run_postgres use.
LOOKUP = "select ei.legacy_id from mart.entity_identity ei where ei.entity_id::text = %s order by ei.legacy_id limit 1"


@pytest.fixture(scope="module")
def chain_db() -> Generator[str]:
    params = conninfo_to_dict(guard_db_write())
    admin_url = make_conninfo(**(params | {"dbname": "postgres"}))
    name = f"truealpha_eitext_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    try:
        with psycopg.connect(admin_url, connect_timeout=3, autocommit=True) as admin:
            admin.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs required DB tests")
    db_url = make_conninfo(**(params | {"dbname": name}))
    try:
        apply_migration_chain(db_url)
        yield db_url
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(name)))


def test_text_key_lookup_uses_an_index_scan(chain_db: str) -> None:
    with psycopg.connect(chain_db) as conn:
        # With sequential scans priced out, the planner picks an index whenever one can serve
        # the predicate. Without the expression index the plan stays a Seq Scan.
        conn.execute("set enable_seqscan = off")
        plan = "\n".join(row[0] for row in conn.execute("explain (costs off) " + LOOKUP, (str(uuid.uuid4()),)))
    assert "ix_mart_entity_identity_entity_text" in plan, plan
    assert "Seq Scan" not in plan, plan
