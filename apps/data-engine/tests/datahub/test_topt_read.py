from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub.topt_read import PostgresToptReadRepository
from truealpha_contracts.universes import SERVED_UNIVERSE_PREFIX


class _FakeCursor:
    def __init__(self, row: Any = None) -> None:
        self._row = row

    def fetchone(self) -> Any:
        return self._row


class _FakeConnection:
    def __init__(self, responder: Callable[[str, Any], Any]) -> None:
        self._responder = responder
        self.calls: list[tuple[str, Any]] = []

    def execute(self, sql: str, params: Any = None) -> _FakeCursor:
        self.calls.append((sql, params))
        return _FakeCursor(self._responder(sql, params))


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


def test_current_head_is_acceptance_gated(connection) -> None:
    # The governed head is resolved by joining the quality report, so any run it returns
    # must carry an accepted quality report — never a captured-but-unreported run.
    repo = PostgresToptReadRepository(connection)
    run_id = repo.current_run_id()
    if run_id is None:
        pytest.skip("no accepted production TOPT run in this DB")
    assert repo.quality_report(run_id) is not None


def test_limit_is_bounded(connection) -> None:
    repo = PostgresToptReadRepository(connection)
    with pytest.raises(ValueError, match="limit must be between"):
        repo.gppe_results("capture-run:" + "a" * 64, limit=999)


def test_fallback_head_scopes_to_served_universe() -> None:
    run_id = "capture-run:" + "a" * 64

    def responder(sql: str, params: Any) -> Any:
        if "current_pointer_head" in sql:
            return None
        if "topt_capture_status" in sql and "datahub_quality_report" in sql:
            if params and params == (f"{SERVED_UNIVERSE_PREFIX}%",):
                return (run_id,)
            return None
        raise AssertionError(f"unexpected query: {sql}")

    conn = _FakeConnection(responder)
    repo = PostgresToptReadRepository(conn)  # type: ignore[arg-type]
    resolved = repo.current_run_id()

    assert resolved == run_id
    fallback_call = next((s, p) for s, p in conn.calls if "topt_capture_status" in s)
    assert "s.universe_id like %s" in fallback_call[0]
    assert fallback_call[1] == (f"{SERVED_UNIVERSE_PREFIX}%",)


def test_fallback_head_returns_none_when_only_canary_run_exists() -> None:
    def responder(sql: str, params: Any) -> Any:
        if "current_pointer_head" in sql:
            return None
        if "topt_capture_status" in sql and "datahub_quality_report" in sql:
            # Query filters by universe:topt-%, so canary run is excluded
            if params and params == (f"{SERVED_UNIVERSE_PREFIX}%",):
                return None
            return ("capture-run:canary",)
        raise AssertionError(f"unexpected query: {sql}")

    conn = _FakeConnection(responder)
    repo = PostgresToptReadRepository(conn)  # type: ignore[arg-type]
    resolved = repo.current_run_id()

    assert resolved is None
    fallback_call = next((s, p) for s, p in conn.calls if "topt_capture_status" in s)
    assert "s.universe_id like %s" in fallback_call[0]
    assert fallback_call[1] == (f"{SERVED_UNIVERSE_PREFIX}%",)
