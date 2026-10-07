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
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
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
    CanonicalUniverse,
    UnmappedIssuer,
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
CUTOFF = datetime(2026, 4, 2, tzinfo=UTC)
#: The `report_date` of the packaged TOPT corpus. Capture resolves every id as of this date.
REPORT_DATE = date(2026, 3, 31)
HANDOVER = date(2026, 4, 1)
NOT_IN_WIDE_ROW = "not_in_wide_row"


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
def lane_world(connection: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """The lane ops run on the test connection, and moomoo answers every ticker."""
    lane = _LaneConnection(connection)
    monkeypatch.setattr(psycopg, "connect", lambda *_a, **_k: lane)

    @contextmanager
    def opend() -> Iterator[object]:
        yield object()

    monkeypatch.setattr(moomoo_source, "connect", opend)
    monkeypatch.setattr(
        moomoo_source,
        "get_analyst_consensus",
        lambda _ctx, _code, **_k: {"rating": 4, "total": 10, "buy": 60.0, "hold": 30.0, "sell": 10.0},
    )


def _capture_head(
    connection: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> question_coverage.GovernedHead:
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
    monkeypatch.setattr(question_coverage, "governed_head", lambda _c, **_k: governed)
    return governed


@pytest.fixture
def head(
    connection: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch, lane_world: None
) -> question_coverage.GovernedHead:
    return _capture_head(connection, monkeypatch)


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
    assert supply_chain["unmapped_by_reason"] == analyst["unmapped_by_reason"] == {NO_CANONICAL_ISSUER_ID: 1}
    assert "lane_failure" not in analyst, "one unmapped issuer of twenty is not a lane failure"
    for table in ("mart.issuer_supply_chain_exposure", "mart.issuer_analyst_ratings"):
        stored = _stored_ids(connection, table, head.run_id)
        assert stored == _wide_row_ids(connection, head.run_id), table
        assert not any(issuer_id.startswith("issuer:") for issuer_id in stored), f"{table} holds a legacy id"


def test_an_issuer_with_an_entity_outside_the_wide_row_gets_no_row_and_is_counted(
    connection: psycopg.Connection[Any], head: question_coverage.GovernedHead, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wide row is the authority. This issuer has an entity, and the head has no row for it."""
    outsider = "issuer:lei:YYYYYYYYYYYYYYYYYY02"
    entity = resolve_entity(connection, outsider, "issuer", as_of=REPORT_DATE, known_at=CUTOFF)
    _universe_with(monkeypatch, [(outsider, "OUTS")])

    supply_chain, analyst = _run_lane_ops()

    for summary in (supply_chain, analyst):
        assert (summary["rows"], summary["unmapped_issuers"]) == (ISSUERS, 1)
        assert summary["unmapped_by_reason"] == {NOT_IN_WIDE_ROW: 1}
    for table in ("mart.issuer_supply_chain_exposure", "mart.issuer_analyst_ratings"):
        assert _stored_ids(connection, table, head.run_id) == _wide_row_ids(connection, head.run_id)
        assert str(entity) not in _stored_ids(connection, table, head.run_id)


def test_a_head_whose_issuers_all_join_has_nothing_unmapped(
    connection: psycopg.Connection[Any], head: question_coverage.GovernedHead
) -> None:
    supply_chain, analyst = _run_lane_ops()

    for summary in (supply_chain, analyst):
        assert (summary["unmapped_issuers"], summary["unmapped_by_reason"]) == (0, {})
        assert "lane_failure" not in summary


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
    assert "1 issuers get no row" in analyst["lane_failure"]
    assert _stored_ids(connection, "mart.issuer_analyst_ratings", head.run_id) == set()

    with pytest.raises(RuntimeError, match="1 issuers get no row"):
        standards.fail_if_a_lane_failed(dg.build_op_context(), json.dumps(analyst), "{}")
    assert [(row["check"], row["ok"]) for row in recorded] == [("question_coverage@topt", False)]
    assert recorded[0]["summary"] == "failed: analyst ratings: 1 issuers get no row"


# --- the wide row is the authority (round 2) --------------------------------------------------
#
# The store below is built the way a backfilled one looks: a claim ended by a retraction, a
# successor that holds the value from the handover date, and a merge recorded after the capture.


def _alias(
    connection: psycopg.Connection[Any], entity: uuid.UUID, scheme: str, value: str, *, valid_from: str, at: datetime
) -> int:
    row = connection.execute(
        """
        insert into staging.entity_aliases
            (entity_id, scheme, value, valid_from, transaction_time, source, raw_ref,
             method, confidence, mapping_version)
        values (%s, %s, %s, %s, %s, 'test', 'test', 'asserted', 1, 'test')
        returning alias_id
        """,
        (entity, scheme, value, valid_from, at),
    ).fetchone()
    assert row is not None
    return int(row[0])


def _mint(connection: psycopg.Connection[Any], scheme: str, value: str, *, at: datetime) -> uuid.UUID:
    row = connection.execute("select staging.entity_mint('issuer', %s, %s, 'test')", (scheme, value)).fetchone()
    assert row is not None
    _alias(connection, row[0], scheme, value, valid_from="-infinity", at=at)
    return row[0]  # type: ignore[no-any-return]


def _hand_over_lei(connection: psycopg.Connection[Any], lei: str) -> tuple[uuid.UUID, uuid.UUID]:
    """The LEI belongs to one entity until HANDOVER and to its successor from then on."""
    recorded = datetime(2026, 3, 15, tzinfo=UTC)
    old = _mint(connection, "lei", lei, at=datetime(2026, 1, 1, tzinfo=UTC))
    claim = connection.execute(
        "select alias_id from staging.entity_aliases where entity_id = %s and scheme = 'lei'", (old,)
    ).fetchone()
    assert claim is not None
    connection.execute(
        "insert into staging.entity_retractions (alias_id, valid_to, reason, transaction_time, source, raw_ref) "
        "values (%s, %s, 'handed over', %s, 'test', 'test')",
        (claim[0], HANDOVER, recorded),
    )
    new = _mint(connection, "legacy-id", f"test:successor:{lei}", at=recorded)
    _alias(connection, new, "lei", lei, valid_from=HANDOVER.isoformat(), at=recorded)
    return old, new


def _merge(connection: psycopg.Connection[Any], loser: uuid.UUID, survivor: uuid.UUID, *, known_from: datetime) -> None:
    """The evidence of a merge, recorded now and knowable from `known_from`."""
    for relation in ("same_as", "superseded_by"):
        derived = connection.execute(
            "select staging.entity_relation_uuid(%s, %s, %s, '-infinity', %s, 'test', 'asserted')",
            (relation, loser, survivor, known_from),
        ).fetchone()
        assert derived is not None
        connection.execute(
            """
            insert into staging.entity_relations
                (relation_id, relation_type, from_entity_id, to_entity_id, valid_from, transaction_time,
                 source, raw_ref, method, confidence, mapping_version)
            values (%s, %s, %s, %s, '-infinity', %s, 'test', 'test', 'asserted', 1, 'test')
            """,
            (derived[0], relation, loser, survivor, known_from),
        )


def _first_issuer(connection: psycopg.Connection[Any]) -> Any:
    return planner.universe_issuers(connection, UNIVERSE)[0]


def test_an_alias_ended_before_the_cutoff_date_still_joins_under_the_capture_as_of(
    connection: psycopg.Connection[Any], lane_world: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Capture resolves as of the corpus `report_date`. The LEI of one issuer passes to a
    successor on HANDOVER, between that date and the cutoff date, so the cutoff date names the
    successor and the wide row holds the entity that owned the LEI on the report date."""
    issuer = _first_issuer(connection)
    old, new = _hand_over_lei(connection, issuer.issuer_id.removeprefix("issuer:lei:"))
    head = _capture_head(connection, monkeypatch)
    wide = _wide_row_ids(connection, head.run_id)
    assert str(old) in wide and str(new) not in wide, "capture resolved as of the report date"

    supply_chain, analyst = _run_lane_ops()

    for summary in (supply_chain, analyst):
        assert (summary["rows"], summary["unmapped_issuers"]) == (ISSUERS, 0)
    assert _stored_ids(connection, "mart.issuer_supply_chain_exposure", head.run_id) == wide
    assert _stored_ids(connection, "mart.issuer_analyst_ratings", head.run_id) == wide


@pytest.mark.parametrize(
    ("known_from", "moves"),
    [(datetime(2026, 3, 1, tzinfo=UTC), True), (datetime(2026, 4, 5, tzinfo=UTC), False)],
    ids=["knowable-at-the-cutoff", "knowable-after-the-cutoff"],
)
def test_a_merge_recorded_after_the_capture_moves_a_row_only_when_knowable_at_the_cutoff(
    connection: psycopg.Connection[Any], head: question_coverage.GovernedHead, known_from: datetime, moves: bool
) -> None:
    """The entity backfill writes evidence with its own knowable-at time. A merge knowable at the
    cutoff moves the lookup to a survivor the wide row does not hold, so the row is not written.
    A merge knowable only after the cutoff is not visible to the head."""
    issuer = _first_issuer(connection)
    wide_entity = lookup_entity(connection, issuer.issuer_id, "issuer", as_of=REPORT_DATE, known_at=CUTOFF)
    survivor = _mint(connection, "legacy-id", f"test:survivor:{uuid.uuid4().hex}", at=datetime(2026, 1, 1, tzinfo=UTC))
    _merge(connection, wide_entity, survivor, known_from=known_from)  # type: ignore[arg-type]
    wide = _wide_row_ids(connection, head.run_id)
    assert str(wide_entity) in wide

    supply_chain, analyst = _run_lane_ops()

    expected = wide - {str(wide_entity)} if moves else wide
    for table in ("mart.issuer_supply_chain_exposure", "mart.issuer_analyst_ratings"):
        assert _stored_ids(connection, table, head.run_id) == expected, table
    assert str(survivor) not in _stored_ids(connection, "mart.issuer_analyst_ratings", head.run_id)
    for summary in (supply_chain, analyst):
        assert summary["rows"] == len(expected)
        assert summary["unmapped_by_reason"] == ({NOT_IN_WIDE_ROW: 1} if moves else {})


def test_a_head_whose_wide_row_holds_none_of_the_universe_ends_red(
    connection: psycopg.Connection[Any], head: question_coverage.GovernedHead, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every issuer maps to an entity and none is in the wide row: no row joins, the #1079 symptom."""
    foreign = tuple(question_coverage.Cell(str(uuid.uuid4()), True) for _ in range(ISSUERS))
    monkeypatch.setattr(question_coverage, "gppe_cells", lambda _c, _run: foreign)
    recorded: list[dict[str, Any]] = []
    monkeypatch.setattr(nightly_verdicts, "record", lambda name, **row: recorded.append({"check": name, **row}))

    supply_chain, analyst = _run_lane_ops()

    for summary in (supply_chain, analyst):
        assert (summary["rows"], summary["unmapped_issuers"]) == (0, ISSUERS)
        assert summary["unmapped_by_reason"] == {NOT_IN_WIDE_ROW: ISSUERS}
        assert "20 issuers get no row" in summary["lane_failure"]
    for table in ("mart.issuer_supply_chain_exposure", "mart.issuer_analyst_ratings"):
        assert _stored_ids(connection, table, head.run_id) == set(), table

    with pytest.raises(RuntimeError, match="20 issuers get no row"):
        standards.fail_if_a_lane_failed(dg.build_op_context(), json.dumps(analyst), "{}")
    assert [(row["check"], row["ok"]) for row in recorded] == [("question_coverage@topt", False)]


def test_a_uuid_corpus_id_that_the_store_does_not_hold_gets_no_row(
    connection: psycopg.Connection[Any], head: question_coverage.GovernedHead, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corpus id in UUID form resolves to itself without a lookup. It must also exist."""
    invented = str(uuid.uuid4())
    real = _wide_row_ids(connection, head.run_id)
    monkeypatch.setattr(
        question_coverage,
        "gppe_cells",
        lambda _c, _run: tuple(question_coverage.Cell(i, True) for i in (*real, invented)),
    )
    _universe_with(monkeypatch, [(invented, "INVT")], keep_real=False)

    supply_chain, analyst = _run_lane_ops()

    for table in ("mart.issuer_supply_chain_exposure", "mart.issuer_analyst_ratings"):
        assert invented not in _stored_ids(connection, table, head.run_id), table
    for summary in (supply_chain, analyst):
        assert (summary["rows"], summary["unmapped_by_reason"]) == (0, {NO_CANONICAL_ISSUER_ID: 1})


def test_a_uuid_corpus_id_that_the_store_holds_keeps_its_row(
    connection: psycopg.Connection[Any], head: question_coverage.GovernedHead, monkeypatch: pytest.MonkeyPatch
) -> None:
    issuer = _first_issuer(connection)
    entity = lookup_entity(connection, issuer.issuer_id, "issuer", as_of=REPORT_DATE, known_at=CUTOFF)
    _universe_with(monkeypatch, [(str(entity), "ONE")], keep_real=False)

    supply_chain, analyst = _run_lane_ops()

    for summary in (supply_chain, analyst):
        assert (summary["rows"], summary["unmapped_issuers"]) == (1, 0)
    assert _stored_ids(connection, "mart.issuer_analyst_ratings", head.run_id) == {str(entity)}


def test_a_cik_keyed_universe_joins_under_its_own_report_date(
    connection: psycopg.Connection[Any], lane_world: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The QQQ shape: `issuer:cik:` ids and a report date that is not the cutoff date. One
    issuer's CIK passes to a successor between the two dates."""
    from data_engine.datahub.production_topt.universe_corpus import load_corpus
    from data_engine.datahub.resolve_coordinates import resolve_coordinates

    instruments = load_corpus("corpus.qqq.v1.json")["topt_denominator"]["instruments"][:3]
    report_date, cutoff = date(2026, 6, 30), datetime(2026, 7, 2, 22, 15, tzinfo=UTC)
    corpus = {"topt_denominator": {"report_date": report_date.isoformat(), "instruments": instruments}}
    monkeypatch.setattr(planner, "resolve_universe_corpus", lambda _connection, _kind: corpus)

    cik = instruments[0][0].removeprefix("issuer:cik:").zfill(10)
    old = _mint(connection, "cik", cik, at=datetime(2026, 1, 1, tzinfo=UTC))
    claim = connection.execute("select alias_id from staging.entity_aliases where entity_id = %s", (old,)).fetchone()
    assert claim is not None
    connection.execute(
        "insert into staging.entity_retractions (alias_id, valid_to, reason, transaction_time, source, raw_ref) "
        "values (%s, '2026-07-01', 'handed over', %s, 'test', 'test')",
        (claim[0], datetime(2026, 6, 15, tzinfo=UTC)),
    )
    new = _mint(connection, "legacy-id", f"test:successor:{cik}", at=datetime(2026, 6, 15, tzinfo=UTC))
    _alias(connection, new, "cik", cik, valid_from="2026-07-01", at=datetime(2026, 6, 15, tzinfo=UTC))

    coordinates = resolve_coordinates(connection, instruments, as_of=report_date, known_at=cutoff)
    wide = {issuer for issuer, *_ in coordinates.values()}
    assert str(old) in wide and len(wide) == 3
    governed = question_coverage.GovernedHead("universe:qqq-us-2026-06-30", "capture-run:" + "7" * 64, cutoff)
    monkeypatch.setattr(question_coverage, "governed_head", lambda _c, **_k: governed)
    monkeypatch.setattr(
        question_coverage, "gppe_cells", lambda _c, _run: tuple(question_coverage.Cell(i, True) for i in sorted(wide))
    )

    supply_chain, analyst = _run_lane_ops("universe-list:qqq")

    for summary in (supply_chain, analyst):
        assert (summary["rows"], summary["unmapped_issuers"]) == (3, 0)
    assert _stored_ids(connection, "mart.issuer_supply_chain_exposure", governed.run_id) == wide
    assert _stored_ids(connection, "mart.issuer_analyst_ratings", governed.run_id) == wide


def test_the_names_of_more_unmapped_issuers_than_the_cap_are_counted_not_listed() -> None:
    unmapped = tuple(UnmappedIssuer(legacy_id=f"issuer:lei:{n:018d}01", ticker=f"T{n}") for n in range(25))
    log = _RecordingLog()

    standards._report_unmapped(
        SimpleNamespace(log=log), "analyst ratings", CanonicalUniverse(issuers=(), unmapped=unmapped)
    )  # type: ignore[arg-type]

    warnings = [text for level, text in log.records if level == "warning"]
    assert len(warnings) == standards.MAX_UNMAPPED_LOGGED + 1
    assert warnings[-1] == "analyst ratings: 5 more issuers get no row, not named here"


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
