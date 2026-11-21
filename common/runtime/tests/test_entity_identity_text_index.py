"""mart.entity_identity serves `entity_id::text = ...` lookups from an index (#1699).

mart keys hold entity ids as text, so readers join on `entity_id::text`. Without an
expression index each lookup scans the whole view; on staging the wide panel read took
9.2 s and failed /research/rankings and /research/coverage on the 5 s statement_timeout.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import psycopg
import pytest
from truealpha_runtime.testing import isolated_test_database

#: The lookup shape that panel_service._load_wide_panel and strategy_run_postgres use.
LOOKUP = "select ei.legacy_id from mart.entity_identity ei where ei.entity_id::text = %s order by ei.legacy_id limit 1"


@pytest.fixture(scope="module")
def chain_db() -> Iterator[str]:
    with isolated_test_database("eitext", template=None) as db:
        yield str(db)


def test_text_key_lookup_uses_an_index_scan(chain_db: str) -> None:
    with psycopg.connect(chain_db) as conn:
        # With sequential scans priced out, the planner picks an index whenever one can serve
        # the predicate. Without the expression index the plan stays a Seq Scan.
        conn.execute("set enable_seqscan = off")
        plan = "\n".join(row[0] for row in conn.execute("explain (costs off) " + LOOKUP, (str(uuid.uuid4()),)))
    assert "ix_mart_entity_identity_entity_text" in plan, plan
    assert "Seq Scan" not in plan, plan
