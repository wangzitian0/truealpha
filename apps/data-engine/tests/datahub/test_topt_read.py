from __future__ import annotations

import os

import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub.topt_read import PostgresToptReadRepository


@pytest.fixture
def connection():
    try:
        active = psycopg.connect(settings.database_url, connect_timeout=3, autocommit=False)
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs the required integration coverage")
    try:
        active.execute("select 1")
        yield active
    finally:
        active.rollback()
        active.close()


def test_read_returns_mart_results_without_a_hash_tuple(connection) -> None:
    repo = PostgresToptReadRepository(connection)
    run_id = repo.current_run_id()
    if run_id is None:
        pytest.skip("no complete production TOPT run in this DB")
    results = repo.gppe_results(run_id)
    assert results, "expected GPPE results from mart"
    assert {"listing_id", "availability", "gppe", "confidence"} <= set(results[0])
    # every availability is a terminal value; available rows carry a numeric gppe
    for r in results:
        assert r["availability"] in {"available", "unavailable"}
        if r["availability"] == "available":
            assert r["gppe"] is not None


def test_quality_report_read(connection) -> None:
    repo = PostgresToptReadRepository(connection)
    run_id = repo.current_run_id()
    if run_id is None:
        pytest.skip("no complete production TOPT run in this DB")
    report = repo.quality_report(run_id)
    if report is not None:
        assert report["requested_count"] == 84
        assert "denominator_mean_confidence" in report


def test_limit_is_bounded(connection) -> None:
    repo = PostgresToptReadRepository(connection)
    with pytest.raises(ValueError, match="limit must be between"):
        repo.gppe_results("capture-run:" + "a" * 64, limit=999)
