"""#1079: the Q3 and Q4 rows carry the wide row's issuer id, so the coverage report joins them.

Staging, 2026-10-06: `mart.topt_gppe_results` held UUID issuer ids. Q3 and Q4 held the
`issuer:lei:` ids of the universe corpus. 0 of 20 joined and the report said `no_row: 20`.

The head here is a REAL capture. `plan_and_persist` resolves the ids as the deployed tick does.
`materialize` writes the wide row. The Q3 and Q4 rows come from the deployed lane ops over
the real `topt` universe. The ids of the head are never typed by hand.
"""

from __future__ import annotations

import inspect
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
    NOT_IN_WIDE_ROW,
    CanonicalIssuer,
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

    `commit` is counted, never run, so the head and its rows roll back with the test. Each
    connect sets a savepoint, and `rollback` returns to it: the op's own reads end, and the
    head and the rows of earlier ops stay. A failed statement leaves the connection usable.
    """

    def __init__(self, connection: psycopg.Connection[Any]) -> None:
        self.connection = connection
        self.commits = 0
        self._connects = 0
        self._savepoint: str | None = None

    def __enter__(self) -> _LaneConnection:
        self._connects += 1
        self._savepoint = f"lane_{self._connects}"
        self.connection.execute(f"savepoint {self._savepoint}")
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        if self._savepoint is not None:
            self.connection.execute(f"rollback to savepoint {self._savepoint}")

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


def _advance_pointer(
    connection: psycopg.Connection[Any], snapshot: Any, run_id: str, universe_prefix: str
) -> question_coverage.GovernedHead:
    """Point the governed head at `run_id` as a first advance does, and read it back as the lane does.

    `advanced_at` is the clock, so this head is the newest of its universe in any database.
    """
    from truealpha_contracts.common import canonical_sha256

    sha = canonical_sha256({"probe": run_id})
    connection.execute(
        """
        insert into mart.current_pointer (pointer_id, content_sha256, environment, universe_id, universe_version,
                                          factor_id, target_run_id, sequence, previous_run_id, advanced_at)
        values (%s, %s, (select environment from mart.environment_identity), %s, %s,
                'gross_profit_per_employee', %s, 0, null, clock_timestamp())
        """,
        (f"current-pointer:{sha}", sha, snapshot.universe_id, snapshot.universe_version, run_id),
    )
    governed = question_coverage.governed_head(connection, universe_prefix=universe_prefix)
    assert governed is not None and governed.run_id == run_id
    return governed


def _materialize_head(
    connection: psycopg.Connection[Any], plan: Any, universe_prefix: str
) -> question_coverage.GovernedHead:
    from data_engine.datahub.production_topt import PostgresToptCoreRepository
    from factors.production_topt import GppeV0Definition

    core = PostgresToptCoreRepository(connection)
    snapshot = core.freeze_snapshot(run_id=plan.run_id, release_manifest_id=plan.release_manifest_id)
    core.materialize(snapshot, gppe_definition=GppeV0Definition(risk_free_rate="0.05"))
    return _advance_pointer(connection, snapshot, plan.run_id, universe_prefix)


def _capture_head(
    connection: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch, *, blank_one_cell: bool = False
) -> question_coverage.GovernedHead:
    """A real captured TOPT run with its wide row, advanced as the head of its universe."""
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parent))
    from production_topt.test_persistence import _capture, _OneBrokenCell  # noqa: E402

    broken = _OneBrokenCell(financial_fact_numerator=blank_one_cell)
    plan = _capture(connection, version="test-1079", broken=broken)
    return _materialize_head(connection, plan, "universe:topt-")


QQQ_REPORT_DATE = date(2026, 6, 30)
QQQ_CUTOFF = datetime(2026, 7, 2, 22, 15, tzinfo=UTC)


def _qqq_denominator(report_date: date, *, without: str | None = None) -> dict[str, Any]:
    """A small QQQ-shaped universe: 21 listings of the packaged QQQ corpus, `issuer:cik:` ids.

    `without` drops one issuer, as a later publication of the universe does.
    """
    from data_engine.datahub.production_topt.universe_corpus import _mapping_sha256, load_corpus

    instruments = load_corpus("corpus.qqq.v1.json")["topt_denominator"]["instruments"][:21]
    if without is not None:
        instruments = [row for row in instruments if row[0] != without]
    denominator: dict[str, Any] = {
        "accession": None,
        "identity_assertions": [],
        "instrument_count": len(instruments),
        "instrument_tuple_fields": ["issuer_id", "instrument_id", "listing_id", "ticker"],
        "instruments": instruments,
        "issuer_count": len({row[0] for row in instruments}),
        "list_label": "test",
        "list_version_id": "",
        "obligation_expansion": "four-semantics:v1",
        "report_date": report_date.isoformat(),
        "universe_id": f"universe:qqq-us-{report_date.isoformat()}",
    }
    denominator["instrument_mapping_sha256"] = _mapping_sha256(denominator)
    return denominator


def _capture_qqq_head(
    connection: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> question_coverage.GovernedHead:
    """A real capture of the QQQ-shaped universe. The cutoff is after its report date, so its
    inputs are stale and every wide row is `unavailable`."""
    from data_engine.datahub.production_topt import composition

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parent))
    import production_topt.test_persistence as persistence  # noqa: E402

    monkeypatch.setattr(
        composition, "_load_capture_corpus", lambda *_a, **_k: {"topt_denominator": _qqq_denominator(QQQ_REPORT_DATE)}
    )
    monkeypatch.setattr(persistence, "CUTOFF", QQQ_CUTOFF)
    plan = persistence._capture(connection, version="test-1079-qqq")
    return _materialize_head(connection, plan, "universe:qqq-")


def _publish_universe(monkeypatch: pytest.MonkeyPatch, report_date: date, *, without: str | None = None) -> None:
    """The universe as currently published: a later report date, and perhaps one issuer fewer."""
    corpus = {"topt_denominator": _qqq_denominator(report_date, without=without)}
    monkeypatch.setattr(planner, "resolve_universe_corpus", lambda _connection, _kind: corpus)


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


def _stored_reasons(
    connection: psycopg.Connection[Any], table: str, run_id: str
) -> dict[tuple[str, tuple[str, ...]], int]:
    """How many rows of the run hold each (availability, reason codes)."""
    rows = connection.execute(
        f"select availability_status, reason_codes from {table} where run_id = %s",  # noqa: S608
        (run_id,),
    ).fetchall()
    counted: dict[tuple[str, tuple[str, ...]], int] = {}
    for status, codes in rows:
        key = (status, tuple(codes))
        counted[key] = counted.get(key, 0) + 1
    return counted


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
    assert supply_chain["wide_row_issuers"] == analyst["wide_row_issuers"] == ISSUERS
    assert supply_chain["unvisited_issuers"] == analyst["unvisited_issuers"] == 0
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
        _check_accounts(summary, _wide_row_ids(connection, head.run_id), joined=ISSUERS)


class _RecordingLog:
    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def warning(self, message: str, *args: object) -> None:
        self.records.append(("warning", message % args))

    def error(self, message: str, *args: object) -> None:
        self.records.append(("error", message % args))


def test_each_unmapped_issuer_is_logged_with_its_reason_code(connection: psycopg.Connection[Any]) -> None:
    universe = canonicalize_universe(connection, {GHOST: "GHST"}, cutoff=CUTOFF, as_of=REPORT_DATE, wide_row_ids=set())
    log = _RecordingLog()

    standards._report_unmapped(SimpleNamespace(log=log), "analyst ratings", universe)  # type: ignore[arg-type]

    warnings = [text for level, text in log.records if level == "warning"]
    assert warnings == [f"analyst ratings: no row for GHST ({GHOST}): {NO_CANONICAL_ISSUER_ID}"]
    assert [level for level, _ in log.records if level == "error"] == ["error"], "a universe with no entity at all"


def test_a_universe_without_any_entity_fails_the_run_after_the_report(
    connection: psycopg.Connection[Any], head: question_coverage.GovernedHead, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The universe holds one unknown member and none of the head's. Nothing joins.

    Each wide-row issuer still gets an unavailable row with the reason. The terminal op ends the run red.
    """
    _universe_with(monkeypatch, [(GHOST, "GHST")], keep_real=False)
    recorded: list[dict[str, Any]] = []
    monkeypatch.setattr(nightly_verdicts, "record", lambda name, **row: recorded.append({"check": name, **row}))

    supply_chain, analyst = _run_lane_ops()

    for summary in (supply_chain, analyst):
        _check_accounts(summary, _wide_row_ids(connection, head.run_id), joined=0)
        assert summary["unmapped_by_reason"] == {NO_CANONICAL_ISSUER_ID: 1}
        assert summary["unvisited_by_reason"] == {"head_member_not_in_universe": ISSUERS}
    assert analyst["lane_failure"] == "0 of 20 wide-row issuers join"
    assert supply_chain["lane_failure"] == "0 of 20 wide-row issuers join"
    assert _stored_reasons(connection, "mart.issuer_analyst_ratings", head.run_id) == {
        ("unavailable", ("head_member_not_in_universe",)): ISSUERS
    }

    with pytest.raises(RuntimeError, match="0 of 20 wide-row issuers join"):
        standards.fail_if_a_lane_failed(dg.build_op_context(), json.dumps(analyst), "{}")
    assert [(row["check"], row["ok"]) for row in recorded] == [("question_coverage@topt", False)]
    assert recorded[0]["summary"] == "failed: analyst ratings: 0 of 20 wide-row issuers join"


# --- the wide row is the authority (round 2) --------------------------------------------------
#
# The store below is built as a backfilled one looks. A claim ends by a retraction.
# A successor holds the value from the handover date. A merge is recorded after the capture.


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


def _merge_away(connection: psycopg.Connection[Any], entities: list[str]) -> None:
    """Merge each entity into a fresh survivor, with evidence knowable at the cutoff."""
    for entity in entities:
        survivor = _mint(connection, "legacy-id", f"test:survivor:{entity}", at=datetime(2026, 1, 1, tzinfo=UTC))
        _merge(connection, uuid.UUID(entity), survivor, known_from=datetime(2026, 3, 1, tzinfo=UTC))


def _first_issuer(connection: psycopg.Connection[Any]) -> Any:
    return planner.universe_issuers(connection, UNIVERSE)[0]


def test_an_alias_ended_before_the_cutoff_date_still_joins_under_the_capture_as_of(
    connection: psycopg.Connection[Any], lane_world: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Capture resolves as of the corpus `report_date`. One LEI passes to a successor on HANDOVER.

    HANDOVER lies between the report date and the cutoff date. The cutoff date names the successor.
    The wide row holds the entity that owned the LEI on the report date.
    """
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
    """The entity backfill writes evidence with its own knowable-at time.

    A merge knowable at the cutoff moves the lookup to a survivor the wide row lacks.
    The row is then not written. A merge knowable only after the cutoff is not visible to the head.
    """
    issuer = _first_issuer(connection)
    wide_entity = lookup_entity(connection, issuer.issuer_id, "issuer", as_of=REPORT_DATE, known_at=CUTOFF)
    survivor = _mint(connection, "legacy-id", f"test:survivor:{uuid.uuid4().hex}", at=datetime(2026, 1, 1, tzinfo=UTC))
    _merge(connection, wide_entity, survivor, known_from=known_from)  # type: ignore[arg-type]
    wide = _wide_row_ids(connection, head.run_id)
    assert str(wide_entity) in wide

    supply_chain, analyst = _run_lane_ops()

    for table in ("mart.issuer_supply_chain_exposure", "mart.issuer_analyst_ratings"):
        assert _stored_ids(connection, table, head.run_id) == wide, "every wide-row issuer has its row"
    assert str(survivor) not in _stored_ids(connection, "mart.issuer_analyst_ratings", head.run_id)
    for summary in (supply_chain, analyst):
        _check_accounts(summary, wide, joined=len(wide) - (1 if moves else 0))
        assert summary["unmapped_by_reason"] == ({NOT_IN_WIDE_ROW: 1} if moves else {})
        assert summary["unvisited_by_reason"] == ({NOT_IN_WIDE_ROW: 1} if moves else {})
    if moves:
        assert (
            _stored_reasons(connection, "mart.issuer_analyst_ratings", head.run_id)[("unavailable", (NOT_IN_WIDE_ROW,))]
            == 1
        ), "the moved issuer's row names why"


def test_a_head_whose_wide_row_holds_none_of_the_universe_ends_red(
    connection: psycopg.Connection[Any], head: question_coverage.GovernedHead, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every wide-row entity is merged into a survivor the wide row lacks. No row joins: the #1079 symptom."""
    wide = _wide_row_ids(connection, head.run_id)
    _merge_away(connection, sorted(wide))
    recorded: list[dict[str, Any]] = []
    monkeypatch.setattr(nightly_verdicts, "record", lambda name, **row: recorded.append({"check": name, **row}))

    supply_chain, analyst = _run_lane_ops()

    for summary in (supply_chain, analyst):
        _check_accounts(summary, wide, joined=0)
        assert summary["unmapped_by_reason"] == {NOT_IN_WIDE_ROW: ISSUERS}
        assert summary["unvisited_by_reason"] == {NOT_IN_WIDE_ROW: ISSUERS}
        assert summary["lane_failure"] == "0 of 20 wide-row issuers join"
    for table in ("mart.issuer_supply_chain_exposure", "mart.issuer_analyst_ratings"):
        assert _stored_ids(connection, table, head.run_id) == wide, table

    with pytest.raises(RuntimeError, match="0 of 20 wide-row issuers join"):
        standards.fail_if_a_lane_failed(dg.build_op_context(), json.dumps(analyst), "{}")
    assert [(row["check"], row["ok"]) for row in recorded] == [("question_coverage@topt", False)]
    assert recorded[0]["summary"] == "failed: analyst ratings: 0 of 20 wide-row issuers join"


def test_an_empty_current_universe_cannot_hide_the_head_and_ends_red(
    connection: psycopg.Connection[Any], head: question_coverage.GovernedHead, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The head has twenty wide rows and the current universe lists none of them."""
    _universe_with(monkeypatch, [], keep_real=False)

    supply_chain, analyst = _run_lane_ops()

    for summary in (supply_chain, analyst):
        _check_accounts(summary, _wide_row_ids(connection, head.run_id), joined=0)
        assert summary["unvisited_by_reason"] == {"head_member_not_in_universe": ISSUERS}
        assert summary["unmapped_issuers"] == 0
    assert analyst["lane_failure"] == "0 of 20 wide-row issuers join"


def test_a_uuid_corpus_id_that_the_store_does_not_hold_gets_no_available_row(
    connection: psycopg.Connection[Any], head: question_coverage.GovernedHead, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corpus id in UUID form resolves to itself without a lookup. It must also exist.

    The head's wide row names this id, so the run writes it an unavailable row and says why.
    """
    invented = str(uuid.uuid4())
    real = _wide_row_ids(connection, head.run_id)
    monkeypatch.setattr(
        question_coverage,
        "gppe_cells",
        lambda _c, _run: tuple(question_coverage.Cell(i, True) for i in (*sorted(real), invented)),
    )
    _universe_with(monkeypatch, [(invented, "INVT")])

    supply_chain, analyst = _run_lane_ops()

    for table in ("mart.issuer_supply_chain_exposure", "mart.issuer_analyst_ratings"):
        row = connection.execute(
            f"select availability_status, reason_codes from {table} where run_id = %s and issuer_id = %s",  # noqa: S608
            (head.run_id, invented),
        ).fetchone()
        assert row == ("unavailable", [NO_CANONICAL_ISSUER_ID]), table
    for summary in (supply_chain, analyst):
        _check_accounts(summary, {*real, invented}, joined=ISSUERS)
        assert summary["unmapped_by_reason"] == {NO_CANONICAL_ISSUER_ID: 1}
        assert summary["unvisited_by_reason"] == {NO_CANONICAL_ISSUER_ID: 1}


def test_a_uuid_corpus_id_that_the_store_holds_keeps_its_row(
    connection: psycopg.Connection[Any], head: question_coverage.GovernedHead, monkeypatch: pytest.MonkeyPatch
) -> None:
    issuer = _first_issuer(connection)
    entity = lookup_entity(connection, issuer.issuer_id, "issuer", as_of=REPORT_DATE, known_at=CUTOFF)
    _universe_with(monkeypatch, [(str(entity), "ONE")], keep_real=False)

    supply_chain, analyst = _run_lane_ops()

    for summary in (supply_chain, analyst):
        _check_accounts(summary, _wide_row_ids(connection, head.run_id), joined=1)
        assert summary["unvisited_by_reason"] == {"head_member_not_in_universe": ISSUERS - 1}
        assert summary["lane_failure"] == "1 of 20 wide-row issuers join, below the floor of 0.5"
    assert (
        _stored_reasons(connection, "mart.issuer_analyst_ratings", head.run_id)[
            ("unavailable", ("head_member_not_in_universe",))
        ]
        == ISSUERS - 1
    )


def _qqq_issuers() -> list[str]:
    """The distinct issuer ids of the QQQ-shaped universe, in corpus order."""
    ordered: list[str] = []
    for row in _qqq_denominator(QQQ_REPORT_DATE)["instruments"]:
        if row[0] not in ordered:
            ordered.append(row[0])
    return ordered


def _cik_of(issuer_id: str) -> str:
    return issuer_id.removeprefix("issuer:cik:").zfill(10)


def _check_accounts(summary: dict[str, Any], wide: set[str], *, joined: int) -> None:
    """Every wide-row issuer ends with one row: its own, or an unavailable one with the reason."""
    assert summary["rows"] == len(wide)
    assert summary["joined_issuers"] == joined
    assert summary["joined_issuers"] + summary["unvisited_issuers"] == len(wide)
    assert summary["wide_row_issuers"] == len(wide)


def test_a_qqq_head_joins_under_its_own_report_date_though_every_wide_row_is_unavailable(
    connection: psycopg.Connection[Any], lane_world: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The QQQ shape through a real capture: `issuer:cik:` ids and a report date that is not TOPT's.

    A CIK passes to a successor between the two dates. Every wide row is `unavailable`, and
    each issuer still gets its Q3 and Q4 row. The reader joins on the subject set, not on answers.
    """
    first = _qqq_issuers()[0]
    old = _mint(connection, "cik", _cik_of(first), at=datetime(2026, 1, 1, tzinfo=UTC))
    claim = connection.execute("select alias_id from staging.entity_aliases where entity_id = %s", (old,)).fetchone()
    assert claim is not None
    connection.execute(
        "insert into staging.entity_retractions (alias_id, valid_to, reason, transaction_time, source, raw_ref) "
        "values (%s, '2026-04-15', 'handed over', %s, 'test', 'test')",
        (claim[0], datetime(2026, 4, 1, tzinfo=UTC)),
    )
    new = _mint(connection, "legacy-id", f"test:successor:{first}", at=datetime(2026, 4, 1, tzinfo=UTC))
    _alias(connection, new, "cik", _cik_of(first), valid_from="2026-04-15", at=datetime(2026, 4, 1, tzinfo=UTC))
    head = _capture_qqq_head(connection, monkeypatch)
    _publish_universe(monkeypatch, QQQ_REPORT_DATE)
    cells = question_coverage.gppe_cells(connection, head.run_id)
    wide = {cell.subject_id for cell in cells}
    assert str(new) in wide and str(old) not in wide, "capture resolved as of its report date"
    assert {cell.answered for cell in cells} == {False}, "every wide row is unavailable"

    supply_chain, analyst = _run_lane_ops("universe-list:qqq")

    for table in ("mart.issuer_supply_chain_exposure", "mart.issuer_analyst_ratings"):
        assert _stored_ids(connection, table, head.run_id) == wide, table
    for summary in (supply_chain, analyst):
        _check_accounts(summary, wide, joined=len(wide))
        assert summary["unmapped_by_reason"] == {}


def test_an_unavailable_wide_row_still_gets_a_row(
    connection: psycopg.Connection[Any], lane_world: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One wide row is `unavailable`. The subject set holds it, so the lane writes its row too."""
    head = _capture_head(connection, monkeypatch, blank_one_cell=True)
    cells = question_coverage.gppe_cells(connection, head.run_id)
    assert sorted(cell.answered for cell in cells) == [False] + [True] * (ISSUERS - 1)
    wide = {cell.subject_id for cell in cells}

    supply_chain, analyst = _run_lane_ops()

    for table in ("mart.issuer_supply_chain_exposure", "mart.issuer_analyst_ratings"):
        assert _stored_ids(connection, table, head.run_id) == wide, table
    for summary in (supply_chain, analyst):
        _check_accounts(summary, wide, joined=ISSUERS)


def test_a_head_member_missing_from_the_current_universe_is_counted_not_dropped(
    connection: psycopg.Connection[Any], lane_world: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The universe is published again after the head, with a later report date and one issuer fewer.
    The lane lists the current universe, so the removed issuer has no member to carry its row."""
    head = _capture_qqq_head(connection, monkeypatch)
    wide = _wide_row_ids(connection, head.run_id)
    removed = _qqq_issuers()[-1]
    removed_entity = lookup_entity(connection, removed, "issuer", as_of=QQQ_REPORT_DATE, known_at=QQQ_CUTOFF)
    _publish_universe(monkeypatch, date(2026, 7, 4), without=removed)

    supply_chain, analyst = _run_lane_ops("universe-list:qqq")

    for summary in (supply_chain, analyst):
        _check_accounts(summary, wide, joined=len(wide) - 1)
        assert summary["unvisited_by_reason"] == {"head_member_not_in_universe": 1}
        assert summary["unmapped_issuers"] == 0
        assert "lane_failure" not in summary, "one head member of the universe is not a lane failure"
    for table in ("mart.issuer_supply_chain_exposure", "mart.issuer_analyst_ratings"):
        assert _stored_ids(connection, table, head.run_id) == wide, table
        assert _stored_reasons(connection, table, head.run_id)[("unavailable", ("head_member_not_in_universe",))] == 1
    assert str(removed_entity) in _stored_ids(connection, "mart.issuer_analyst_ratings", head.run_id)


def test_a_later_universe_date_cannot_move_a_row_to_another_issuer(
    connection: psycopg.Connection[Any], lane_world: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A CIK passes from issuer A to issuer B after the head's report date. Both are in the wide row.

    The current universe carries a later date. Resolved as of that date, A's id names B.
    Resolved as of the head's own report date, it names A.
    """
    head = _capture_qqq_head(connection, monkeypatch)
    wide = _wide_row_ids(connection, head.run_id)
    issuers = _qqq_issuers()
    a, b, removed = issuers[0], issuers[1], issuers[-1]
    known_at, later = QQQ_CUTOFF, date(2026, 7, 4)
    entity_a = lookup_entity(connection, a, "issuer", as_of=QQQ_REPORT_DATE, known_at=known_at)
    entity_b = lookup_entity(connection, b, "issuer", as_of=QQQ_REPORT_DATE, known_at=known_at)
    removed_entity = lookup_entity(connection, removed, "issuer", as_of=QQQ_REPORT_DATE, known_at=known_at)
    recorded = datetime(2026, 6, 15, tzinfo=UTC)
    claim = connection.execute(
        "select alias_id from staging.entity_aliases where entity_id = %s and scheme = 'cik'", (entity_a,)
    ).fetchone()
    assert claim is not None
    connection.execute(
        "insert into staging.entity_retractions (alias_id, valid_to, reason, transaction_time, source, raw_ref) "
        "values (%s, '2026-07-01', 'handed over', %s, 'test', 'test')",
        (claim[0], recorded),
    )
    _alias(connection, entity_b, "cik", _cik_of(a), valid_from="2026-07-01", at=recorded)  # type: ignore[arg-type]
    assert lookup_entity(connection, a, "issuer", as_of=later, known_at=known_at) == entity_b, "the drift exists"
    assert lookup_entity(connection, a, "issuer", as_of=QQQ_REPORT_DATE, known_at=known_at) == entity_a
    _publish_universe(monkeypatch, later, without=removed)

    supply_chain, analyst = _run_lane_ops("universe-list:qqq")

    for table in ("mart.issuer_supply_chain_exposure", "mart.issuer_analyst_ratings"):
        assert _stored_ids(connection, table, head.run_id) == wide, table
        a_row = connection.execute(
            f"select reason_codes from {table} where run_id = %s and issuer_id = %s",  # noqa: S608
            (head.run_id, str(entity_a)),
        ).fetchone()
        assert a_row is not None and "head_member_not_in_universe" not in a_row[0], "A's row is A's own"
    for summary in (supply_chain, analyst):
        _check_accounts(summary, wide, joined=len(wide) - 1)
        assert summary["unvisited_by_reason"] == {"head_member_not_in_universe": 1}
        assert summary["unmapped_issuers"] == 0
    assert removed_entity is not None


def test_the_log_names_each_head_member_missing_from_the_universe(
    connection: psycopg.Connection[Any], lane_world: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    head = _capture_qqq_head(connection, monkeypatch)
    removed = _qqq_issuers()[-1]
    _publish_universe(monkeypatch, date(2026, 7, 4), without=removed)
    members = {issuer.issuer_id: issuer.ticker for issuer in planner.universe_issuers(connection, "universe-list:qqq")}
    log = _RecordingLog()

    standards._canonical_universe(  # type: ignore[arg-type]
        SimpleNamespace(log=log), "analyst ratings", connection, head, members
    )

    warnings = [text for level, text in log.records if level == "warning"]
    assert len(warnings) == 1 and warnings[0].startswith("analyst ratings: unavailable row for ")
    assert warnings[0].endswith(": head_member_not_in_universe")
    removed_ticker = next(row[3] for row in _qqq_denominator(QQQ_REPORT_DATE)["instruments"] if row[0] == removed)
    assert removed_ticker in warnings[0], "the log names the issuer by its ticker"


def test_the_names_of_more_unmapped_issuers_than_the_cap_are_counted_not_listed() -> None:
    unmapped = tuple(UnmappedIssuer(legacy_id=f"issuer:lei:{n:018d}01", ticker=f"T{n}") for n in range(25))
    log = _RecordingLog()

    standards._report_unmapped(
        SimpleNamespace(log=log), "analyst ratings", CanonicalUniverse(issuers=(), unmapped=unmapped)
    )  # type: ignore[arg-type]

    warnings = [text for level, text in log.records if level == "warning"]
    assert len(warnings) == standards.MAX_UNMAPPED_LOGGED + 1
    assert warnings[-1] == "analyst ratings: 5 more issuers get no row, not named here"


def test_the_names_of_more_unvisited_issuers_than_the_cap_are_counted_not_listed() -> None:
    unvisited = tuple(
        UnmappedIssuer(legacy_id=f"issuer:lei:{n:018d}01", ticker=f"T{n}", reason="head_member_not_in_universe")
        for n in range(25)
    )
    log = _RecordingLog()
    universe = CanonicalUniverse(
        issuers=(CanonicalIssuer(issuer_id=str(uuid.uuid4()), legacy_id="x", ticker="X"),),
        unvisited=unvisited,
        wide_row_issuers=26,
    )

    standards._report_unmapped(SimpleNamespace(log=log), "analyst ratings", universe)  # type: ignore[arg-type]

    warnings = [text for level, text in log.records if level == "warning"]
    assert len(warnings) == standards.MAX_UNMAPPED_LOGGED + 1
    assert warnings[-1] == "analyst ratings: 5 more issuers get an unavailable row, not named here"


# --- round 4: a partial join is red, the report names the reason, errors do not break the chain


def _universe_of(joined: int, wide: int, *, others: int = 0) -> CanonicalUniverse:
    """`joined` of `wide` issuers get a row. `others` members of the current universe get none."""
    issuers = tuple(
        CanonicalIssuer(issuer_id=str(uuid.uuid4()), legacy_id=f"issuer:cik:{n:010d}", ticker=f"T{n}")
        for n in range(joined)
    )
    unmapped = tuple(
        UnmappedIssuer(legacy_id=f"issuer:cik:{n:010d}", ticker=f"U{n}", reason=NOT_IN_WIDE_ROW) for n in range(others)
    )
    return CanonicalUniverse(issuers=issuers, unmapped=unmapped, wide_row_issuers=wide)


@pytest.mark.parametrize(
    ("joined", "wide", "others", "failure"),
    [
        (9, 20, 0, "9 of 20 wide-row issuers join, below the floor of 0.5"),
        (10, 20, 0, None),
        (9, 20, 5, "9 of 20 wide-row issuers join, below the floor of 0.5"),
        (10, 20, 15, None),
        (4, 8, 0, None),
        (3, 8, 0, "3 of 8 wide-row issuers join, below the floor of 0.5"),
        (3, 5, 0, None),
        (2, 5, 0, "2 of 5 wide-row issuers join, below the floor of 0.5"),
        (1, 4, 0, None),
        (0, 4, 0, "0 of 4 wide-row issuers join"),
        (0, 0, 0, None),
        (0, 0, 3, "0 of 0 wide-row issuers join"),
    ],
    ids=[
        "9-of-20",
        "10-of-20",
        "9-of-20-extras",
        "10-of-20-many-extras",
        "4-of-8",
        "3-of-8",
        "3-of-5",
        "2-of-5",
        "1-of-4",
        "0-of-4",
        "empty",
        "members-only",
    ],
)
def test_the_join_floor_is_half_of_a_wide_row_of_five_or_more(
    joined: int, wide: int, others: int, failure: str | None
) -> None:
    assert _universe_of(joined, wide, others=others).lane_failure() == failure


@pytest.mark.parametrize(("merged", "joined"), [(11, 9), (10, 10)], ids=["9-of-20", "10-of-20"])
def test_a_partial_join_is_red_below_half_and_green_at_half(
    connection: psycopg.Connection[Any],
    head: question_coverage.GovernedHead,
    monkeypatch: pytest.MonkeyPatch,
    merged: int,
    joined: int,
) -> None:
    """Merged wide-row entities lose their members: the member resolves to a survivor the wide row lacks."""
    wide = _wide_row_ids(connection, head.run_id)
    _merge_away(connection, sorted(wide)[:merged])
    recorded: list[dict[str, Any]] = []
    monkeypatch.setattr(nightly_verdicts, "record", lambda name, **row: recorded.append({"check": name, **row}))

    supply_chain, analyst = _run_lane_ops()

    for summary in (supply_chain, analyst):
        _check_accounts(summary, wide, joined=joined)
        assert summary["unvisited_by_reason"] == {NOT_IN_WIDE_ROW: merged}
    if joined < ISSUERS / 2:
        message = "9 of 20 wide-row issuers join, below the floor of 0.5"
        assert supply_chain["lane_failure"] == analyst["lane_failure"] == message
        with pytest.raises(RuntimeError, match=message):
            standards.fail_if_a_lane_failed(dg.build_op_context(), json.dumps(analyst), "{}")
        assert [(row["check"], row["ok"]) for row in recorded] == [("question_coverage@topt", False)]
        assert recorded[0]["summary"] == f"failed: analyst ratings: {message}"
    else:
        assert "lane_failure" not in supply_chain and "lane_failure" not in analyst
        standards.fail_if_a_lane_failed(dg.build_op_context(), json.dumps(analyst), "{}")
        assert recorded == []


def test_the_coverage_report_names_the_reason_of_a_head_member_the_universe_lacks(
    connection: psycopg.Connection[Any], lane_world: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The persisted report shows the reason code, where a missing row showed `no_row`."""
    head = _capture_qqq_head(connection, monkeypatch)
    wide = _wide_row_ids(connection, head.run_id)
    _publish_universe(monkeypatch, date(2026, 7, 4), without=_qqq_issuers()[-1])
    _run_lane_ops("universe-list:qqq")

    report = question_coverage.compile_report(
        connection, universe="universe-list:qqq", executed_at=datetime(2026, 10, 6, tzinfo=UTC)
    )

    assert report is not None
    for question in ("q3", "q4"):
        entry = report["questions"][question]
        assert question_coverage.NO_ROW not in entry["unavailable"], f"{question}: {entry}"
        assert entry["unavailable"].get("head_member_not_in_universe") == 1, f"{question}: {entry}"
        assert entry["answered"] + sum(entry["unavailable"].values()) == len(wide), f"{question}: {entry}"
    assert report["questions"]["q4"]["answered"] == len(wide) - 1


def _run_head_reports_job(universe: str = UNIVERSE) -> Any:
    """The deployed job, on the test connection, with the real ops, report and verdicts."""
    run_config = standards.head_reports_request(universe, EXECUTED_AT, run_key="test", only_if_stale=False).run_config
    return standards.head_reports_pipeline_job.execute_in_process(run_config=run_config, raise_on_error=False)


def _failing_head_report_date(*_args: object) -> date:
    raise ValueError("the head has no capture obligations")


def _failing_lookup(connection: psycopg.Connection[Any], *_args: object, **_kwargs: object) -> None:
    connection.execute("select staging.entity_resolve('lei', 'X', null, now())")  # the store refuses: no date


@pytest.mark.parametrize(
    ("breaks", "error_type"),
    [("report_date", "ValueError"), ("lookup", "RaiseException")],
    ids=["report-date", "lookup"],
)
def test_an_identity_error_ends_the_run_red_after_the_coverage_report(
    connection: psycopg.Connection[Any],
    head: question_coverage.GovernedHead,
    monkeypatch: pytest.MonkeyPatch,
    breaks: str,
    error_type: str,
) -> None:
    from data_engine.datahub import canonical_issuer

    if breaks == "report_date":
        monkeypatch.setattr(question_coverage, "head_report_date", _failing_head_report_date)
    else:
        monkeypatch.setattr(canonical_issuer, "lookup_entity", _failing_lookup)

    result = _run_head_reports_job()

    failure = f"canonicalization failed: {error_type}"
    assert not result.success
    assert [e.step_key for e in result.all_events if e.is_step_failure] == ["fail_if_a_lane_failed"]
    for node in ("run_supply_chain_exposure", "run_analyst_ratings"):
        summary = json.loads(result.output_for_node(node))
        assert (summary["rows"], summary["lane_failure"]) == (0, failure), node
    for table in ("mart.issuer_supply_chain_exposure", "mart.issuer_analyst_ratings"):
        assert _stored_ids(connection, table, head.run_id) == set(), table
    reports = connection.execute("select count(*) from mart.question_coverage_report where run_id = %s", (head.run_id,))
    assert reports.fetchone() == (1,), "the coverage report exists"
    assert nightly_verdicts.newest_is_red("question_coverage@topt")
    newest = connection.execute(
        "select summary from mart.nightly_verdicts where check_name = 'question_coverage@topt' "
        "order by ran_at desc, id desc limit 1"
    ).fetchone()
    assert newest == (f"failed: analyst ratings: {failure}",), "the verdict carries the type only"


class _TracedLaneConnection(_LaneConnection):
    """The lane connection that records every statement and every rollback, in order."""

    def __init__(self, connection: psycopg.Connection[Any], events: list[str]) -> None:
        super().__init__(connection)
        self.events = events

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        self.events.append("sql")
        return self.connection.execute(*args, **kwargs)

    def rollback(self) -> None:
        self.events.append("rollback")
        super().rollback()


def test_the_analyst_op_ends_its_reads_before_the_first_vendor_call_and_writes_after_the_last(
    connection: psycopg.Connection[Any], head: question_coverage.GovernedHead, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read transaction held across the vendor calls keeps locks on the identity tables for minutes."""
    events: list[str] = []
    monkeypatch.setattr(psycopg, "connect", lambda *_a, **_k: _TracedLaneConnection(connection, events))

    def consensus(_ctx: object, _code: str, **_kwargs: object) -> dict[str, Any]:
        events.append("fetch")
        return {"rating": 4, "total": 10, "buy": 60.0, "hold": 30.0, "sell": 10.0}

    monkeypatch.setattr(moomoo_source, "get_analyst_consensus", consensus)

    _run_lane_ops()

    first, last = events.index("fetch"), len(events) - 1 - events[::-1].index("fetch")
    assert events.count("fetch") == ISSUERS
    assert events[first - 1] == "rollback", "the reads end before the first fetch"
    assert "sql" not in events[first : last + 1], "no statement runs between the fetches"
    assert "sql" in events[last + 1 :], "the rows are written after the last fetch"


def test_a_second_corpus_id_of_one_issuer_is_counted_and_logged(connection: psycopg.Connection[Any]) -> None:
    entity = resolve_entity(connection, "issuer:cik:0000000001", "issuer", as_of=REPORT_DATE, known_at=CUTOFF)

    universe = canonicalize_universe(
        connection,
        [("issuer:cik:0000000001", "ONE"), ("issuer:cik:1", "ONE")],
        cutoff=CUTOFF,
        as_of=REPORT_DATE,
        wide_row_ids={str(entity)},
    )

    assert [issuer.legacy_id for issuer in universe.issuers] == ["issuer:cik:0000000001"]
    assert universe.unmapped_by_reason() == {"duplicate_corpus_id": 1}
    log = _RecordingLog()
    standards._report_unmapped(SimpleNamespace(log=log), "analyst ratings", universe)  # type: ignore[arg-type]
    assert [text for level, text in log.records if level == "warning"] == [
        "analyst ratings: no row for ONE (issuer:cik:1): duplicate_corpus_id (same issuer as issuer:cik:0000000001)"
    ]


# --- where the lane reads the head's report date (round 3) ------------------------------------


def test_the_head_report_date_is_the_partition_its_capture_wrote(
    connection: psycopg.Connection[Any], head: question_coverage.GovernedHead
) -> None:
    """`raw.capture_obligations.partition_key` is `str(report_date)` for every obligation of the run.

    `plan_and_persist` writes `str(denominator["report_date"])` as the partition of each obligation.
    The universe id ends with the same date here. The partition is the exact source.
    """
    from data_engine.datahub.production_topt.universe_corpus import TOPT_CORPUS_FILENAME, load_corpus

    corpus_date = date.fromisoformat(load_corpus(TOPT_CORPUS_FILENAME)["topt_denominator"]["report_date"])
    assert question_coverage.head_report_date(connection, head) == corpus_date == REPORT_DATE
    assert head.universe_id.endswith(REPORT_DATE.isoformat())


class _Answers:
    def __init__(self, rows: list[tuple[str]]) -> None:
        self.rows = rows

    def execute(self, *_args: object) -> _Answers:
        return self

    def fetchall(self) -> list[tuple[str]]:
        return self.rows


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        ([], "no capture obligations"),
        ([("2026-03-31",), ("2026-04-01",)], "2 partition keys"),
        ([("latest",)], "not an ISO date"),
    ],
    ids=["none", "two", "not-a-date"],
)
def test_a_head_whose_report_date_cannot_be_read_fails_loudly(rows: list[tuple[str]], message: str) -> None:
    governed = question_coverage.GovernedHead("universe:x", "capture-run:" + "0" * 64, CUTOFF)
    with pytest.raises(ValueError, match=message):
        question_coverage.head_report_date(_Answers(rows), governed)  # type: ignore[arg-type]


def test_the_planner_and_the_capture_share_one_topt_corpus_filename() -> None:
    from data_engine.datahub.production_topt import composition, universe_corpus

    assert planner.TOPT_CORPUS_FILENAME is universe_corpus.TOPT_CORPUS_FILENAME
    for function in (composition._load_capture_corpus, composition.plan_and_persist, composition.run_topt_pipeline):
        default = inspect.signature(function).parameters["corpus_filename"].default
        assert default is universe_corpus.TOPT_CORPUS_FILENAME, function.__name__


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
        cutoff=CUTOFF,
        as_of=REPORT_DATE,
        wide_row_ids=_wide_row_ids(connection, head.run_id),
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
