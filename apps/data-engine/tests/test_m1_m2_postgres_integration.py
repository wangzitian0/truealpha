"""Physical PostgreSQL Integration Test for M1 & M2 Materializers (#771, #772).

Tests physical database table constraints, DDL conformance, and cell extraction
against a real PostgreSQL database (localhost:5432).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal

import psycopg
import pytest
from data_engine.datahub.analyst_ratings import (
    AnalystRatingItem,
    analyst_track_record,
    materialize_analyst_ratings,
)
from data_engine.datahub.question_coverage import (
    analyst_rating_cells,
    supply_chain_cells,
)
from data_engine.datahub.standards.supply_chain_extraction import (
    SupplyChainPartner,
    materialize_supply_chain_exposure,
    supply_chain_exposure,
)

DATABASE_URL = os.environ.get("TRUEALPHA_TEST_DATABASE_URL", "postgresql://postgres@localhost:5432/truealpha_m1_m2")


_REQUIRE_RUNTIME = bool(os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"))


def _pg_ready_or_fail() -> bool:
    """Return True when M1/M2 mart tables are reachable.

    When DATABASE_URL or TRUEALPHA_REQUIRE_RUNTIME is set:
    - Connection failure → pytest.fail (unreachable configured DB).
    - Connected but mart tables absent → pytest.fail (migrations not applied).
    Otherwise: skip silently (local dev without M1/M2 DB or non-DB CI shard).
    """
    try:
        with psycopg.connect(DATABASE_URL, connect_timeout=1) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "select to_regclass('mart.issuer_analyst_ratings'), to_regclass('mart.issuer_supply_chain_exposure')"
                )
                res = cur.fetchone()
                ready = bool(res and res[0] and res[1])
        if not ready and _REQUIRE_RUNTIME:
            pytest.fail(
                "Connected to Postgres but M1/M2 mart tables are absent — run migrations before this shard.",
                pytrace=False,
            )
        return ready
    except psycopg.OperationalError as exc:
        if _REQUIRE_RUNTIME:
            pytest.fail(f"configured Postgres is unreachable: {exc}", pytrace=False)
        return False


pytestmark = pytest.mark.skipif(not _pg_ready_or_fail(), reason="PostgreSQL mart tables not available")


def test_physical_postgres_analyst_ratings_and_supply_chain_insertion() -> None:
    now = datetime.now(tz=UTC)
    run_id = f"test_run_{int(now.timestamp())}"

    with psycopg.connect(DATABASE_URL) as conn:
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
        rec_sc = supply_chain_exposure(partners, entity_id="issuer:test:pg_aapl", as_of=now)
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
