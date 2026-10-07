"""#1079: the Q3 and Q4 rows carry the wide row's issuer id, so the coverage report joins them.

Staging, 2026-10-06: `mart.topt_gppe_results` held UUID issuer ids. Q3 and Q4 held the
`issuer:lei:` ids of the universe corpus. 0 of 20 joined and the report said `no_row: 20`.

The head here is a REAL capture. `plan_and_persist` resolves the ids as the deployed tick does.
`materialize` writes the wide row. The Q3 and Q4 rows come from the deployed lane ops over
the real `topt` universe. The ids of the head are never typed by hand.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import dagster as dg
import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub import question_coverage
from data_engine.datahub.canonical_issuer import (
    NO_CANONICAL_ISSUER_ID,
    canonicalize_universe,
    is_canonical_issuer_id,
)
from data_engine.datahub.resolve_coordinates import is_uuid, lookup_entity, resolve_entity
from data_engine.datahub.standards import planner
from data_engine.lanes import standards
from data_engine.quality import nightly_verdicts
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
    """The test's connection as the ops see `psycopg.connect(...)`, and it never closes.
    `commit` is counted, never run, so the head and its rows roll back with the test."""

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


# --- the unmapped issuer: no row under the legacy id, and the run says so ---------------------

GHOST = "issuer:lei:ZZZZZZZZZZZZZZZZZZ01"


def _universe_with(monkeypatch: pytest.MonkeyPatch, extra: list[tuple[str, str]], *, keep_real: bool = True) -> None:
    """The real `topt` universe, plus members the entity store does not know."""
    real = planner.universe_issuers

    def issuers(connection: Any, universe: str) -> list[Any]:
        found = real(connection, universe) if keep_real else []
        return [*found, *(SimpleNamespace(issuer_id=i, ticker=t) for i, t in extra)]

    monkeypatch.setattr(planner, "universe_issuers", issuers)


def test_an_issuer_without_an_entity_gets_no_row_and_is_counted(
    connection: psycopg.Connection[Any], head: question_coverage.GovernedHead, monkeypatch: pytest.MonkeyPatch
) -> None:
    _universe_with(monkeypatch, [(GHOST, "GHST")])

    supply_chain, analyst = _run_lane_ops()

    assert (supply_chain["rows"], supply_chain["unmapped_issuers"]) == (ISSUERS, 1)
    assert (analyst["rows"], analyst["unmapped_issuers"]) == (ISSUERS, 1)
    assert "lane_failure" not in analyst, "one unmapped issuer of twenty is not a lane failure"
    for table in ("mart.issuer_supply_chain_exposure", "mart.issuer_analyst_ratings"):
        stored = _stored_ids(connection, table, head.run_id)
        assert stored == _wide_row_ids(connection, head.run_id), table
        assert not any(issuer_id.startswith("issuer:") for issuer_id in stored), f"{table} holds a legacy id"


def test_an_issuer_with_an_entity_outside_the_wide_row_is_written_and_counted(
    connection: psycopg.Connection[Any], head: question_coverage.GovernedHead, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The join count of #1079: rows minus `not_in_wide_row`. This issuer has an entity but no wide row."""
    outsider = "issuer:lei:YYYYYYYYYYYYYYYYYY02"
    known_at = datetime(2026, 4, 2, tzinfo=UTC)
    entity = resolve_entity(connection, outsider, "issuer", as_of=known_at.date(), known_at=known_at)
    _universe_with(monkeypatch, [(outsider, "OUTS")])

    supply_chain, analyst = _run_lane_ops()

    for summary in (supply_chain, analyst):
        assert (summary["rows"], summary["unmapped_issuers"], summary["not_in_wide_row"]) == (ISSUERS + 1, 0, 1)
    assert _stored_ids(connection, "mart.issuer_analyst_ratings", head.run_id) == _wide_row_ids(
        connection, head.run_id
    ) | {str(entity)}


def test_a_head_whose_issuers_all_join_counts_none_outside_the_wide_row(
    connection: psycopg.Connection[Any], head: question_coverage.GovernedHead
) -> None:
    supply_chain, analyst = _run_lane_ops()

    assert (supply_chain["not_in_wide_row"], analyst["not_in_wide_row"]) == (0, 0)


class _RecordingLog:
    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def warning(self, message: str, *args: object) -> None:
        self.records.append(("warning", message % args))

    def error(self, message: str, *args: object) -> None:
        self.records.append(("error", message % args))


def test_each_unmapped_issuer_is_logged_with_its_reason_code(connection: psycopg.Connection[Any]) -> None:
    universe = canonicalize_universe(connection, {GHOST: "GHST"}, cutoff=datetime(2026, 4, 2, tzinfo=UTC))
    log = _RecordingLog()

    standards._report_unmapped(SimpleNamespace(log=log), "analyst ratings", universe)  # type: ignore[arg-type]

    warnings = [text for level, text in log.records if level == "warning"]
    assert warnings == [f"analyst ratings: no row for GHST ({GHOST}): {NO_CANONICAL_ISSUER_ID}"]
    assert [level for level, _ in log.records if level == "error"] == ["error"], "a universe with no entity at all"


def test_a_universe_without_any_entity_fails_the_run_after_the_report(
    connection: psycopg.Connection[Any], head: question_coverage.GovernedHead, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every row would be missing and the report would read `no_row` for all of them: the
    #1079 symptom. The analyst op names it in its summary, and the terminal op ends the run red."""
    _universe_with(monkeypatch, [(GHOST, "GHST")], keep_real=False)
    recorded: list[dict[str, Any]] = []
    monkeypatch.setattr(nightly_verdicts, "record", lambda name, **row: recorded.append({"check": name, **row}))

    supply_chain, analyst = _run_lane_ops()

    assert (supply_chain["rows"], supply_chain["unmapped_issuers"]) == (0, 1)
    assert (analyst["rows"], analyst["unmapped_issuers"]) == (0, 1)
    assert "1 issuers have no entity" in analyst["lane_failure"]
    assert _stored_ids(connection, "mart.issuer_analyst_ratings", head.run_id) == set()

    with pytest.raises(RuntimeError, match="1 issuers have no entity"):
        standards.fail_if_a_lane_failed(dg.build_op_context(), json.dumps(analyst), "{}")
    assert [(row["check"], row["ok"]) for row in recorded] == [("question_coverage@topt", False)]
    assert recorded[0]["summary"] == "failed: analyst ratings: 1 issuers have no entity, no row written"


# --- the resolver: one function for the wide row and for these rows ---------------------------


def test_lookup_entity_finds_what_the_capture_minted_and_mints_nothing(
    connection: psycopg.Connection[Any], head: question_coverage.GovernedHead
) -> None:
    known_at = datetime(2026, 4, 2, tzinfo=UTC)
    corpus = [issuer for issuer in planner.universe_issuers(connection, UNIVERSE)]
    entities_before = connection.execute("select count(*) from staging.entities").fetchone()[0]

    found = {
        issuer.issuer_id: lookup_entity(
            connection, issuer.issuer_id, "issuer", as_of=known_at.date(), known_at=known_at
        )
        for issuer in corpus
    }
    ghost = lookup_entity(connection, GHOST, "issuer", as_of=known_at.date(), known_at=known_at)

    assert set(str(entity) for entity in found.values()) == _wide_row_ids(connection, head.run_id)
    assert ghost is None
    assert connection.execute("select count(*) from staging.entities").fetchone()[0] == entities_before
    for legacy_id, entity in found.items():
        assert resolve_entity(connection, legacy_id, "issuer", as_of=known_at.date(), known_at=known_at) == entity


def test_two_names_of_one_issuer_make_one_row_under_the_first(
    connection: psycopg.Connection[Any], head: question_coverage.GovernedHead
) -> None:
    corpus = planner.universe_issuers(connection, UNIVERSE)
    first = corpus[0]
    entity = lookup_entity(
        connection,
        first.issuer_id,
        "issuer",
        as_of=datetime(2026, 4, 2).date(),
        known_at=datetime(2026, 4, 2, tzinfo=UTC),
    )

    universe = canonicalize_universe(
        connection,
        [(first.issuer_id, first.ticker), (str(entity), first.ticker)],
        cutoff=datetime(2026, 4, 2, tzinfo=UTC),
    )

    assert [(i.issuer_id, i.legacy_id) for i in universe.issuers] == [(str(entity), first.issuer_id)]
    assert universe.unmapped == ()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("02587046-dc99-5a44-b811-e2d086a58ccb", True),
        ("02587046-DC99-5A44-B811-E2D086A58CCB", False),
        ("{02587046-dc99-5a44-b811-e2d086a58ccb}", False),
        ("issuer:lei:29DX7H14B9S6O3FD6V18", False),
        ("", False),
    ],
)
def test_the_wide_row_form_is_the_lower_case_hyphenated_uuid(value: str, expected: bool) -> None:
    assert is_canonical_issuer_id(value) is expected


# --- the guard: every run-scoped issuer table stores the wide row's form ----------------------

#: Every `mart` base table with `run_id` and `issuer_id`, and who writes it for one head. A table
#: that is not here fails `test_every_run_scoped_issuer_table_is_covered`: add its real writer to
#: `_write_every_table` and name it here.
WRITERS = {
    "topt_gppe_results": "the capture tick: the wide row, the reference form",
    "topt_core_results": "the capture tick, from the same coordinates",
    "strategy_input_coverage": "persist_strategy_input_coverage, from the run's own issuers",
    "issuer_theme_purity": "materialize_theme_purity, members read from the wide row",
    "issuer_supply_chain_exposure": "run_supply_chain_exposure",
    "issuer_analyst_ratings": "run_analyst_ratings",
}


def run_scoped_issuer_tables(connection: psycopg.Connection[Any]) -> set[str]:
    """The `mart` base tables with both a `run_id` and an `issuer_id` column, from the schema."""
    rows = connection.execute(
        """
        select t.table_name
        from information_schema.tables t
        where t.table_schema = 'mart' and t.table_type = 'BASE TABLE'
          and exists (select 1 from information_schema.columns c
                      where c.table_schema = 'mart' and c.table_name = t.table_name and c.column_name = 'run_id')
          and exists (select 1 from information_schema.columns c
                      where c.table_schema = 'mart' and c.table_name = t.table_name and c.column_name = 'issuer_id')
        """
    ).fetchall()
    return {name for (name,) in rows}


def issuer_ids_outside_the_wide_row(
    connection: psycopg.Connection[Any], table: str, run_id: str, wide: set[str]
) -> set[str]:
    return _stored_ids(connection, f"mart.{table}", run_id) - wide


def _write_every_table(
    connection: psycopg.Connection[Any], head: question_coverage.GovernedHead, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run the real writer of each lane table on the head. The capture fixture wrote the first two."""
    from data_engine.datahub.production_topt.theme_purity import governed_members, materialize_theme_purity
    from data_engine.datahub.strategy_bridge import persist_strategy_input_coverage
    from data_engine.sources import llm
    from truealpha_contracts.theme_purity import THEMES

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parent))
    from production_topt.test_theme_purity_producer import _answers, _seed  # noqa: E402

    persist_strategy_input_coverage(connection, head.run_id, cutoff=head.cutoff)

    members = governed_members(connection, run_id=head.run_id)
    assert members, "the head has members whose financials were fetched"
    _seed(
        connection,
        cik=min(members),
        partition="segment-partition:" + "c" * 64,
        knowable=datetime(2026, 2, 1, tzinfo=UTC),
    )
    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    monkeypatch.setattr(settings, "llm_model", "glm-test")
    monkeypatch.setattr(
        llm,
        "_gateway_transport",
        _answers([{"index": 0, "in_theme": True, "reason": "a"}, {"index": 1, "in_theme": False, "reason": "b"}]),
    )
    materialize_theme_purity(connection, run_id=head.run_id, cutoff=head.cutoff, themes=(THEMES["ai-infrastructure"],))

    _run_lane_ops()


def test_every_run_scoped_issuer_table_is_covered(connection: psycopg.Connection[Any]) -> None:
    assert run_scoped_issuer_tables(connection) == set(WRITERS), (
        "a mart table with run_id and issuer_id is new or gone: give it a real writer in _write_every_table"
    )


def test_every_run_scoped_issuer_table_stores_the_wide_row_form(
    connection: psycopg.Connection[Any], head: question_coverage.GovernedHead, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_every_table(connection, head, monkeypatch)
    wide = _wide_row_ids(connection, head.run_id)

    for table in sorted(run_scoped_issuer_tables(connection)):
        stored = _stored_ids(connection, f"mart.{table}", head.run_id)
        assert stored, f"{table} holds no row for the head: the guard checked nothing"
        assert issuer_ids_outside_the_wide_row(connection, table, head.run_id, wide) == set(), table
        assert all(is_canonical_issuer_id(issuer_id) for issuer_id in stored), table


def test_the_guard_flags_a_table_that_stores_the_legacy_form(connection: psycopg.Connection[Any]) -> None:
    """The guard must fail on a table of the old shape, or it protects nothing."""
    legacy = "issuer:lei:29DX7H14B9S6O3FD6V18"
    wide = {"02587046-dc99-5a44-b811-e2d086a58ccb"}
    connection.execute("create table mart.zz_guard_probe (run_id text not null, issuer_id text not null)")
    connection.execute("insert into mart.zz_guard_probe values ('run:probe', %s), ('run:probe', %s)", (legacy, *wide))

    assert "zz_guard_probe" in run_scoped_issuer_tables(connection)
    assert run_scoped_issuer_tables(connection) != set(WRITERS), "the coverage test fails on it"
    assert issuer_ids_outside_the_wide_row(connection, "zz_guard_probe", "run:probe", wide) == {legacy}
