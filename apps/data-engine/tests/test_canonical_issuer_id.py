"""#1079: the Q3 and Q4 rows carry the wide row's issuer id, so the coverage report joins them.

Staging, 2026-10-06: `mart.topt_gppe_results` held UUID issuer ids, while Q3 and Q4 held the
`issuer:lei:` ids of the universe corpus. 0 of 20 joined and the report said `no_row: 20`.

The head here is a REAL capture: `plan_and_persist` resolves the ids as the deployed tick does,
and `materialize` writes the wide row. The Q3 and Q4 rows come from the deployed lane ops over
the real `topt` universe. Nothing types an issuer id by hand.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import dagster as dg
import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub import question_coverage
from data_engine.datahub.resolve_coordinates import is_uuid
from data_engine.lanes import standards
from data_engine.sources import moomoo as moomoo_source

EXECUTED_AT = "2026-10-06T04:00:00+00:00"
UNIVERSE = "topt"
HEAD_UNIVERSE_ID = "universe:topt-us-2026-03-31"
ISSUERS = 20


@pytest.fixture
def connection() -> Iterator[psycopg.Connection[Any]]:
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


class _LaneConnection:
    """The test's connection as the ops see `psycopg.connect(...)`: no close, and `commit` is
    counted, never run, so the head and its rows roll back with the test."""

    def __init__(self, connection: psycopg.Connection[Any]) -> None:
        self.connection = connection
        self.commits = 0

    def __enter__(self) -> _LaneConnection:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def commit(self) -> None:
        self.commits += 1

    def __getattr__(self, name: str) -> Any:
        return getattr(self.connection, name)


@pytest.fixture
def head(connection: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch) -> question_coverage.GovernedHead:
    """A real captured TOPT run with its wide row, and the lane ops pointed at it."""
    from data_engine.datahub.production_topt import PostgresToptCoreRepository
    from factors.production_topt import GppeV0Definition

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parent))
    from production_topt.test_persistence import CUTOFF, _capture  # noqa: E402

    plan = _capture(connection, version="test-1079")
    core = PostgresToptCoreRepository(connection)
    snapshot = core.freeze_snapshot(run_id=plan.run_id, release_manifest_id=plan.release_manifest_id)
    core.materialize(snapshot, gppe_definition=GppeV0Definition(risk_free_rate="0.05"))
    governed = question_coverage.GovernedHead(HEAD_UNIVERSE_ID, plan.run_id, CUTOFF)

    lane = _LaneConnection(connection)
    monkeypatch.setattr(psycopg, "connect", lambda *_a, **_k: lane)
    monkeypatch.setattr(question_coverage, "governed_head", lambda _c, **_k: governed)

    @contextmanager
    def opend() -> Iterator[object]:
        yield object()

    monkeypatch.setattr(moomoo_source, "connect", opend)
    monkeypatch.setattr(
        moomoo_source,
        "get_analyst_consensus",
        lambda _ctx, _code, **_k: {"rating": 4, "total": 10, "buy": 60.0, "hold": 30.0, "sell": 10.0},
    )
    return governed


def _wide_row_ids(connection: psycopg.Connection[Any], run_id: str) -> set[str]:
    return {cell.subject_id for cell in question_coverage.gppe_cells(connection, run_id)}


def _stored_ids(connection: psycopg.Connection[Any], table: str, run_id: str) -> set[str]:
    rows = connection.execute(f"select issuer_id from {table} where run_id = %s", (run_id,)).fetchall()  # noqa: S608
    return {issuer_id for (issuer_id,) in rows}


def _run_lane_ops(universe: str = UNIVERSE) -> tuple[dict[str, Any], dict[str, Any]]:
    config = standards.StandardBackfillConfig(executed_at=EXECUTED_AT, universe=universe)
    supply_chain = standards.run_supply_chain_exposure(dg.build_op_context(), config, "{}")
    analyst = standards.run_analyst_ratings(dg.build_op_context(), config, supply_chain)
    return json.loads(supply_chain), json.loads(analyst)


def test_q3_and_q4_rows_carry_the_wide_row_issuer_id(
    connection: psycopg.Connection[Any], head: question_coverage.GovernedHead
) -> None:
    wide = _wide_row_ids(connection, head.run_id)
    assert len(wide) == ISSUERS
    assert all(is_uuid(issuer_id) for issuer_id in wide), "the reference form is the UUID"

    supply_chain, analyst = _run_lane_ops()
    assert supply_chain["rows"] == ISSUERS
    assert analyst["rows"] == ISSUERS

    assert _stored_ids(connection, "mart.issuer_supply_chain_exposure", head.run_id) == wide
    assert _stored_ids(connection, "mart.issuer_analyst_ratings", head.run_id) == wide


def test_the_coverage_report_finds_the_q3_and_q4_rows_instead_of_no_row(
    connection: psycopg.Connection[Any], head: question_coverage.GovernedHead
) -> None:
    _run_lane_ops()

    report = question_coverage.compile_report(
        connection, universe=UNIVERSE, executed_at=datetime(2026, 10, 6, tzinfo=UTC)
    )

    assert report is not None
    for question in ("q3", "q4"):
        entry = report["questions"][question]
        assert entry["denominator"] == ISSUERS
        assert question_coverage.NO_ROW not in entry["unavailable"], f"{question}: {entry}"
        assert entry["answered"] + sum(entry["unavailable"].values()) == ISSUERS, f"{question}: {entry}"
    assert report["questions"]["q4"]["answered"] == ISSUERS, "every Q4 row is available and joins"
