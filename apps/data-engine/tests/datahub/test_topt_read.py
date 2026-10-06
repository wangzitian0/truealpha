from __future__ import annotations

import os
import sys
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub import quality_report
from data_engine.datahub.a1_evidence import register_run_evidence
from data_engine.datahub.production_topt import PostgresToptCoreRepository
from data_engine.datahub.topt_read import PostgresToptReadRepository
from factors.production_topt import GppeV0Definition
from truealpha_contracts.universes import SERVED_UNIVERSE_PREFIX

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from production_topt.test_materialization import _CORPUS_OBJECTIVES, _seed_complete_production_run  # noqa: E402


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


def _materialized_run(connection):
    """A complete production run, frozen and materialized, with no governed pointer in this
    transaction. Clearing the factor's pointers makes the head the same on any database. The
    delete bypasses the append-only trigger. The rollback restores every row."""
    connection.execute("set session_replication_role = replica")
    connection.execute(
        "delete from mart.current_pointer where environment = 'production' and factor_id = %s",
        ("gross_profit_per_employee",),
    )
    connection.execute("set session_replication_role = origin")
    _repository, run, _list_version, release_manifest_id, *_ = _seed_complete_production_run(connection)
    core = PostgresToptCoreRepository(connection)
    snapshot = core.freeze_snapshot(run_id=run.run_id, release_manifest_id=release_manifest_id)
    assert len(core.materialize(snapshot, gppe_definition=GppeV0Definition(risk_free_rate="0.05"))) == 20
    return run, release_manifest_id


def _accepted_run(connection):
    """The same run after its quality report persists and its pointer advances."""
    run, release_manifest_id = _materialized_run(connection)
    graded = quality_report.build_report(connection, run.run_id)
    quality_report.persist(connection, graded)
    registration = register_run_evidence(
        connection,
        run_id=run.run_id,
        release_manifest_id=release_manifest_id,
        quality_report=graded,
        objectives=_CORPUS_OBJECTIVES,
    )
    assert registration.accepted, registration.summary
    return run


def test_read_returns_mart_results_without_a_hash_tuple(connection) -> None:
    run = _accepted_run(connection)
    repo = PostgresToptReadRepository(connection)
    run_id = repo.current_run_id()
    assert run_id == run.run_id
    results = repo.gppe_results(run_id)
    assert len(results) == 20
    assert {"listing_id", "availability", "gppe", "confidence"} <= set(results[0])
    # every availability is a terminal value; available rows carry a numeric gppe
    for r in results:
        assert r["availability"] in {"available", "unavailable"}
        if r["availability"] == "available":
            assert r["gppe"] is not None
    assert {r["availability"] for r in results} == {"available"}
    assert {Decimal(r["gppe"]) for r in results} == {Decimal("2000000"), Decimal("700000")}


def test_quality_report_read(connection) -> None:
    run = _accepted_run(connection)
    repo = PostgresToptReadRepository(connection)
    report = repo.quality_report(run.run_id)
    assert report is not None
    assert report["run_id"] == run.run_id
    assert report["requested_count"] == 84
    assert "denominator_mean_confidence" in report
    assert repo.quality_report("capture-run:" + "a" * 64) is None


def test_current_head_is_acceptance_gated(connection) -> None:
    # The governed head joins the quality report. A run it returns must carry an accepted
    # quality report, never a run that was captured and not reported. Each state runs with
    # data, in the deployed order. The report persists first. Then the pointer advances.
    run, release_manifest_id = _materialized_run(connection)
    repo = PostgresToptReadRepository(connection)
    assert repo.current_run_id() != run.run_id, "a captured, materialized, unreported run must not be the head"

    graded = quality_report.build_report(connection, run.run_id)
    quality_report.persist(connection, graded)
    assert repo.current_run_id() == run.run_id, "the acceptance-gated fallback must serve the reported run"
    assert repo.quality_report(run.run_id) is not None

    registration = register_run_evidence(
        connection,
        run_id=run.run_id,
        release_manifest_id=release_manifest_id,
        quality_report=graded,
        objectives=_CORPUS_OBJECTIVES,
    )
    assert registration.accepted, registration.summary
    assert repo.current_run_id() == run.run_id, "the governed pointer must serve the accepted run"

    # Remove the report. The fallback now has nothing to serve, so only the pointer can name the
    # head. The report is append-only by trigger; the delete bypasses it in this transaction only.
    connection.execute("set local session_replication_role = replica")
    connection.execute("delete from mart.datahub_quality_report where run_id = %s", (run.run_id,))
    connection.execute("set local session_replication_role = origin")
    assert repo.current_run_id() == run.run_id, "the pointer path must not depend on the fallback"


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
