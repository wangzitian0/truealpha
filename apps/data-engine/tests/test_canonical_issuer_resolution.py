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
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import dagster as dg
import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub import analyst_ratings, question_coverage
from data_engine.datahub.canonical_issuer import (
    NO_CANONICAL_ISSUER_ID,
    NOT_IN_WIDE_ROW,
    CanonicalIssuer,
    CanonicalUniverse,
    UnmappedIssuer,
    canonicalize_universe,
    is_canonical_issuer_id,
)
from data_engine.datahub.production_topt import theme_purity
from data_engine.datahub.resolve_coordinates import lookup_entity, resolve_entity
from data_engine.datahub.standards import planner, supply_chain_extraction
from data_engine.lanes import standards
from data_engine.quality import nightly_verdicts
from data_engine.sources import moomoo as moomoo_source
from truealpha_contracts.theme_purity import THEMES

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

    `commit` is counted, never run, so the head and its rows roll back with the test.
    Each connect sets a savepoint, and `rollback` returns to it. The op's own reads end.
    The head and the rows of earlier ops stay. A failed statement leaves the connection usable.
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


GHOST = "issuer:lei:ZZZZZZZZZZZZZZZZZZ01"


def _universe_with(monkeypatch: pytest.MonkeyPatch, extra: list[tuple[str, str]], *, keep_real: bool = True) -> None:
    """The real `topt` universe, plus members the entity store does not know."""
    real = planner.universe_issuers

    def issuers(connection: Any, universe: str) -> list[Any]:
        found = real(connection, universe) if keep_real else []
        return [*found, *(SimpleNamespace(issuer_id=i, ticker=t) for i, t in extra)]

    monkeypatch.setattr(planner, "universe_issuers", issuers)


class _RecordingLog:
    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def warning(self, message: str, *args: object) -> None:
        self.records.append(("warning", message % args))

    def error(self, message: str, *args: object) -> None:
        self.records.append(("error", message % args))


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
    assert summary["unvisited_written"] + summary["kept_real_rows"] == summary["unvisited_issuers"]
    assert summary["wide_row_issuers"] == len(wide)


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

    elsewhere = {"member_resolves_elsewhere": merged}
    if joined < ISSUERS / 2:
        for summary in (supply_chain, analyst):
            _check_accounts(summary, wide, joined=0)
            assert summary["unvisited_by_reason"] == {**elsewhere, "join_floor_tripped": joined}
        message = "9 of 20 wide-row issuers join, below the floor of 0.5"
        assert supply_chain["lane_failure"] == analyst["lane_failure"] == message
        with pytest.raises(RuntimeError, match=message):
            standards.fail_if_a_lane_failed(dg.build_op_context(), json.dumps(analyst), "{}")
        assert [(row["check"], row["ok"]) for row in recorded] == [("question_coverage@topt", False)]
        assert recorded[0]["summary"] == f"failed: analyst ratings: {message}"
    else:
        for summary in (supply_chain, analyst):
            _check_accounts(summary, wide, joined=joined)
            assert summary["unvisited_by_reason"] == elsewhere
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


def test_a_rerun_keeps_the_row_of_an_issuer_the_universe_has_lost(
    connection: psycopg.Connection[Any], lane_world: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first run joined the issuer. A later publication drops it. Its row stays what it was."""
    head = _capture_qqq_head(connection, monkeypatch)
    removed = _qqq_issuers()[-1]
    removed_entity = lookup_entity(connection, removed, "issuer", as_of=QQQ_REPORT_DATE, known_at=QQQ_CUTOFF)
    _publish_universe(monkeypatch, QQQ_REPORT_DATE)
    _run_lane_ops("universe-list:qqq")
    before = {
        table: connection.execute(
            f"select availability_status, reason_codes from {table} where run_id = %s and issuer_id = %s",  # noqa: S608
            (head.run_id, str(removed_entity)),
        ).fetchone()
        for table in ("mart.issuer_supply_chain_exposure", "mart.issuer_analyst_ratings")
    }
    assert before["mart.issuer_analyst_ratings"] == ("available", [])
    _publish_universe(monkeypatch, date(2026, 7, 4), without=removed)

    supply_chain, analyst = _run_lane_ops("universe-list:qqq")

    for table, row in before.items():
        after = connection.execute(
            f"select availability_status, reason_codes from {table} where run_id = %s and issuer_id = %s",  # noqa: S608
            (head.run_id, str(removed_entity)),
        ).fetchone()
        assert after == row, table
    for summary in (supply_chain, analyst):
        assert (summary["unvisited_issuers"], summary["unvisited_written"], summary["kept_real_rows"]) == (1, 0, 1)
        assert summary["unvisited_by_reason"] == {}, "no fill row was written"
        assert summary["rows"] == summary["wide_row_issuers"]


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
        "order by ran_at desc, recorded_at desc limit 1"
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


# --- round 5: the floor is judged before any fetch, errors in the wide row are wrapped, fills refresh


class _Vendor:
    """Counts the OpenD connects and the consensus calls of one test."""

    def __init__(self) -> None:
        self.connects = 0
        self.fetches = 0


@pytest.fixture
def vendor(monkeypatch: pytest.MonkeyPatch) -> _Vendor:
    counted = _Vendor()

    @contextmanager
    def opend() -> Iterator[object]:
        counted.connects += 1
        yield object()

    def consensus(_ctx: object, _code: str, **_kwargs: object) -> dict[str, Any]:
        counted.fetches += 1
        return {"rating": 4, "total": 10, "buy": 60.0, "hold": 30.0, "sell": 10.0}

    monkeypatch.setattr(moomoo_source, "connect", opend)
    monkeypatch.setattr(moomoo_source, "get_analyst_consensus", consensus)
    return counted


def _newest_verdict(connection: psycopg.Connection[Any]) -> tuple[bool | None, str]:
    row = connection.execute(
        "select ok, summary from mart.nightly_verdicts where check_name = 'question_coverage@topt' "
        "order by ran_at desc, recorded_at desc limit 1"
    ).fetchone()
    assert row is not None
    return row[0], row[1]


def test_a_tripped_floor_spends_no_vendor_call_and_still_ends_red_after_the_report(
    connection: psycopg.Connection[Any], head: question_coverage.GovernedHead, vendor: _Vendor
) -> None:
    """The floor depends on the head and the universe only. A retry would repeat it every night."""
    wide = _wide_row_ids(connection, head.run_id)
    _merge_away(connection, sorted(wide)[:11])

    result = _run_head_reports_job()

    message = "9 of 20 wide-row issuers join, below the floor of 0.5"
    assert (vendor.connects, vendor.fetches) == (0, 0)
    assert not result.success
    assert [e.step_key for e in result.all_events if e.is_step_failure] == ["fail_if_a_lane_failed"]
    assert json.loads(result.output_for_node("run_analyst_ratings"))["lane_failure"] == message
    assert _newest_verdict(connection) == (False, f"failed: analyst ratings: {message}")
    assert _stored_reasons(connection, "mart.issuer_analyst_ratings", head.run_id) == {
        ("unavailable", ("member_resolves_elsewhere",)): 11,
        ("unavailable", ("join_floor_tripped",)): 9,
    }
    reports = connection.execute("select count(*) from mart.question_coverage_report where run_id = %s", (head.run_id,))
    assert reports.fetchone() == (1,)


def test_a_universe_with_nothing_to_fetch_does_not_open_opend(
    connection: psycopg.Connection[Any],
    head: question_coverage.GovernedHead,
    monkeypatch: pytest.MonkeyPatch,
    vendor: _Vendor,
) -> None:
    _universe_with(monkeypatch, [], keep_real=False)

    _, analyst = _run_lane_ops()

    assert (vendor.connects, vendor.fetches) == (0, 0)
    assert analyst["lane_failure"] == "0 of 20 wide-row issuers join"


def test_a_head_without_a_wide_row_and_without_members_opens_no_opend(
    connection: psycopg.Connection[Any],
    head: question_coverage.GovernedHead,
    monkeypatch: pytest.MonkeyPatch,
    vendor: _Vendor,
) -> None:
    """Nothing fails and nothing is left to fetch. The floor is not what keeps OpenD closed here."""
    monkeypatch.setattr(question_coverage, "gppe_cells", lambda _c, _run: ())
    _universe_with(monkeypatch, [], keep_real=False)

    supply_chain, analyst = _run_lane_ops()

    assert (vendor.connects, vendor.fetches) == (0, 0)
    for summary in (supply_chain, analyst):
        assert (summary["rows"], summary["wide_row_issuers"]) == (0, 0)
        assert "lane_failure" not in summary


def test_a_head_that_fetches_opens_opend_once_for_every_ticker(
    connection: psycopg.Connection[Any], head: question_coverage.GovernedHead, vendor: _Vendor
) -> None:
    _run_lane_ops()

    assert (vendor.connects, vendor.fetches) == (1, ISSUERS)


def test_a_wide_row_issuer_id_that_is_not_a_uuid_ends_the_run_red_after_the_report(
    connection: psycopg.Connection[Any], head: question_coverage.GovernedHead
) -> None:
    """`mart.topt_gppe_results.issuer_id` is plain text. A legacy id there must not crash the ops."""
    connection.execute("set local session_replication_role = replica")  # the wide row is append-only
    connection.execute(
        "update mart.topt_gppe_results set issuer_id = 'issuer:lei:XXXXXXXXXXXXXXXXXX77' "
        "where run_id = %s and issuer_id = (select min(issuer_id) from mart.topt_gppe_results where run_id = %s)",
        (head.run_id, head.run_id),
    )
    connection.execute("set local session_replication_role = origin")

    result = _run_head_reports_job()

    failure = "canonicalization failed: ValueError"
    assert not result.success
    assert [e.step_key for e in result.all_events if e.is_step_failure] == ["fail_if_a_lane_failed"]
    for node in ("run_supply_chain_exposure", "run_analyst_ratings"):
        assert json.loads(result.output_for_node(node))["lane_failure"] == failure, node
    for table in ("mart.issuer_supply_chain_exposure", "mart.issuer_analyst_ratings"):
        assert _stored_ids(connection, table, head.run_id) == set(), table
    assert _stored_ids(connection, "mart.issuer_theme_purity", head.run_id) == set(), "Q6 writes no fill either"
    assert json.loads(result.output_for_node("run_theme_purity"))[standards.UNVISITED_ISSUERS] == 0
    reports = connection.execute("select count(*) from mart.question_coverage_report where run_id = %s", (head.run_id,))
    assert reports.fetchone() == (1,)
    assert _newest_verdict(connection) == (False, f"failed: analyst ratings: {failure}")


def test_a_failure_of_the_supply_chain_op_alone_ends_the_run_red(
    connection: psycopg.Connection[Any], head: question_coverage.GovernedHead, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the supply chain op cannot resolve its ids. The analyst op is healthy and keeps its rows."""
    real = standards._canonical_universe

    def resolve(context: Any, lane: str, *args: Any) -> Any:
        if lane == "supply chain exposure":
            raise ValueError("the supply chain lane cannot resolve")
        return real(context, lane, *args)

    monkeypatch.setattr(standards, "_canonical_universe", resolve)

    result = _run_head_reports_job()

    failure = "canonicalization failed: ValueError"
    assert not result.success
    assert [e.step_key for e in result.all_events if e.is_step_failure] == ["fail_if_a_lane_failed"]
    supply_chain = json.loads(result.output_for_node("run_supply_chain_exposure"))
    analyst = json.loads(result.output_for_node("run_analyst_ratings"))
    assert supply_chain["lane_failure"] == analyst["lane_failure"] == failure
    assert analyst["rows"] == ISSUERS and analyst["joined_issuers"] == ISSUERS, "the analyst op kept its rows"
    assert _stored_ids(connection, "mart.issuer_supply_chain_exposure", head.run_id) == set()
    assert _stored_ids(connection, "mart.issuer_analyst_ratings", head.run_id) == _wide_row_ids(connection, head.run_id)
    assert _newest_verdict(connection) == (False, f"failed: supply chain: {failure}")


def test_the_coverage_report_says_the_member_resolves_elsewhere_for_a_merged_issuer(
    connection: psycopg.Connection[Any], head: question_coverage.GovernedHead
) -> None:
    """The issuer is in the wide row, and Q1 answers it. Its member resolves to a survivor outside."""
    wide = _wide_row_ids(connection, head.run_id)
    _merge_away(connection, sorted(wide)[:1])
    _run_lane_ops()

    report = question_coverage.compile_report(
        connection, universe=UNIVERSE, executed_at=datetime(2026, 10, 6, tzinfo=UTC)
    )

    assert report is not None
    assert report["questions"]["q1"]["answered"] == ISSUERS
    for question in ("q3", "q4"):
        entry = report["questions"][question]
        assert question_coverage.NO_ROW not in entry["unavailable"], f"{question}: {entry}"
        assert entry["unavailable"].get("member_resolves_elsewhere") == 1, f"{question}: {entry}"
    assert report["questions"]["q4"]["answered"] == ISSUERS - 1


def test_the_summary_counts_the_rows_the_run_wrote_not_the_issuers_it_handled(
    connection: psycopg.Connection[Any],
    head: question_coverage.GovernedHead,
    monkeypatch: pytest.MonkeyPatch,
    vendor: _Vendor,
) -> None:
    """A good night writes real rows. A merge then trips the floor on a rerun. The rows stay."""
    wide = _wide_row_ids(connection, head.run_id)
    tables = ("mart.issuer_supply_chain_exposure", "mart.issuer_analyst_ratings")
    _run_lane_ops()
    good = {table: _stored_reasons(connection, table, head.run_id) for table in tables}
    assert good["mart.issuer_analyst_ratings"] == {("available", ()): ISSUERS}
    fetches = (vendor.connects, vendor.fetches)
    _merge_away(connection, sorted(wide)[:11])
    recorded: list[dict[str, Any]] = []
    monkeypatch.setattr(nightly_verdicts, "record", lambda name, **row: recorded.append({"check": name, **row}))

    supply_chain, analyst = _run_lane_ops()

    message = "9 of 20 wide-row issuers join, below the floor of 0.5"
    assert (vendor.connects, vendor.fetches) == fetches, "the rerun spends no vendor call"
    for summary in (supply_chain, analyst):
        assert summary["unvisited_by_reason"] == {}, "no fill row was written"
        assert (summary["rows"], summary["wide_row_issuers"]) == (ISSUERS, ISSUERS)
        assert (summary["unvisited_issuers"], summary["unvisited_written"], summary["kept_real_rows"]) == (20, 0, 20)
        assert summary["lane_failure"] == message
    for table in tables:
        assert _stored_reasons(connection, table, head.run_id) == good[table], table
    with pytest.raises(RuntimeError, match=message):
        standards.fail_if_a_lane_failed(dg.build_op_context(), json.dumps(analyst), "{}")
    assert recorded[0]["summary"] == f"failed: analyst ratings: {message}"


def _reasons_by_issuer(connection: psycopg.Connection[Any], table: str, run_id: str) -> dict[str, tuple[str, ...]]:
    rows = connection.execute(
        f"select issuer_id, reason_codes from {table} where run_id = %s",  # noqa: S608
        (run_id,),
    ).fetchall()
    return {issuer_id: tuple(codes) for issuer_id, codes in rows}


def test_the_supply_chain_op_and_the_analyst_op_agree_on_every_issuer_under_a_tripped_floor(
    connection: psycopg.Connection[Any], head: question_coverage.GovernedHead, vendor: _Vendor
) -> None:
    wide = _wide_row_ids(connection, head.run_id)
    _merge_away(connection, sorted(wide)[:11])

    result = _run_head_reports_job()

    assert not result.success
    supply_chain = _reasons_by_issuer(connection, "mart.issuer_supply_chain_exposure", head.run_id)
    analyst = _reasons_by_issuer(connection, "mart.issuer_analyst_ratings", head.run_id)
    assert supply_chain == analyst
    assert sorted(Counter(reason for (reason,) in analyst.values()).items()) == [
        ("join_floor_tripped", 9),
        ("member_resolves_elsewhere", 11),
    ]
    extractors = connection.execute(
        "select distinct extractor from mart.issuer_supply_chain_exposure where run_id = %s", (head.run_id,)
    ).fetchall()
    assert extractors == [("lane:unvisited:v1",)], "no real row under a tripped floor"


def test_the_reason_counts_are_sorted_and_counted_once() -> None:
    from data_engine.datahub.canonical_issuer import reason_counts

    assert list(reason_counts(["b", "a", "b"]).items()) == [("a", 1), ("b", 2)]
    assert reason_counts([]) == {}


# --- the fill upsert: a fill refreshes a fill, and a real row stays

_FILL_RUN = "capture-run:" + "9" * 64


def _write_real_purity_rows(connection: psycopg.Connection[Any], issuer: str, codes: list[str], status: str) -> None:
    """One real Q6 row per governed theme, as the member path writes it. No theme stays free for a fill."""
    available = status == "available"
    for definition in THEMES.values():
        connection.execute(
            theme_purity._INSERT_SQL,  # noqa: SLF001
            (
                _FILL_RUN,
                issuer,
                4242,
                definition.theme_id,
                definition.theme,
                definition.factor_version,
                definition.content_sha256,
                CUTOFF,
                date(2026, 3, 31),
                "segment-partition:" + "e" * 64,
                Decimal("0.5") if available else None,
                Decimal(100),
                Decimal(50) if available else Decimal(0),
                Decimal(50) if available else Decimal(0),
                Decimal(0) if available else Decimal(100),
                Decimal(0),
                2,
                Decimal("0.85") if available else Decimal(0),
                codes,
                "rule:test",
                status,
                "verified" if available else "degraded",
                "not_evaluated",
            ),
        )


_FILL_TABLES = {
    "mart.issuer_analyst_ratings": (
        analyst_ratings.materialize_unvisited_issuers,
        lambda connection, issuer, codes, status: analyst_ratings.materialize_analyst_ratings(
            connection,
            run_id=_FILL_RUN,
            cutoff=CUTOFF,
            ratings_data=[
                {
                    "issuer_id": issuer,
                    "consensus_rating": Decimal(4) if status == "available" else None,
                    "analysts_count": 10 if status == "available" else 0,
                    "availability_status": status,
                    "reason_codes": codes,
                }
            ],
        ),
    ),
    "mart.issuer_theme_purity": (theme_purity.materialize_unvisited_issuers, _write_real_purity_rows),
    "mart.issuer_supply_chain_exposure": (
        supply_chain_extraction.materialize_unvisited_issuers,
        lambda connection, issuer, codes, status: supply_chain_extraction.materialize_supply_chain_exposure(
            connection,
            run_id=_FILL_RUN,
            cutoff=CUTOFF,
            exposure_data=[
                {
                    "issuer_id": issuer,
                    "exposure_score": Decimal("0.5") if status == "available" else None,
                    "direct_partners": 1 if status == "available" else 0,
                    "availability_status": status,
                    "reason_codes": codes,
                }
            ],
        ),
    ),
}


def _fill_row(connection: psycopg.Connection[Any], table: str, issuer: str) -> tuple[str, str, list[str]] | None:
    row = connection.execute(
        f"select extractor, availability_status, reason_codes from {table} where run_id = %s and issuer_id = %s",  # noqa: S608
        (_FILL_RUN, issuer),
    ).fetchone()
    return None if row is None else (row[0], row[1], list(row[2]))


@pytest.mark.parametrize("table", sorted(_FILL_TABLES))
@pytest.mark.parametrize(
    ("status", "codes"), [("available", []), ("unavailable", ["fetch_error:OSError"])], ids=["answer", "fetch-error"]
)
def test_a_fill_row_refreshes_its_own_kind_and_never_replaces_a_real_row(
    connection: psycopg.Connection[Any], table: str, status: str, codes: list[str]
) -> None:
    fill, write_real = _FILL_TABLES[table]
    issuer = str(uuid.uuid4())

    first = fill(connection, run_id=_FILL_RUN, cutoff=CUTOFF, unvisited=[(issuer, "head_member_not_in_universe")])
    assert first == [(issuer, "head_member_not_in_universe")]
    assert _fill_row(connection, table, issuer) == ("lane:unvisited:v1", "unavailable", ["head_member_not_in_universe"])
    second = fill(connection, run_id=_FILL_RUN, cutoff=CUTOFF, unvisited=[(issuer, "member_resolves_elsewhere")])
    assert second == [(issuer, "member_resolves_elsewhere")]
    assert _fill_row(connection, table, issuer) == ("lane:unvisited:v1", "unavailable", ["member_resolves_elsewhere"])

    write_real(connection, issuer, codes, status)
    real = _fill_row(connection, table, issuer)
    assert real is not None and real[0] != "lane:unvisited:v1" and real[1:] == (status, codes)
    kept = fill(connection, run_id=_FILL_RUN, cutoff=CUTOFF, unvisited=[(issuer, "join_floor_tripped")])
    assert kept == [], "a real row stays, and the writer says it wrote nothing"
    assert _fill_row(connection, table, issuer) == real, "a real row stays"


class _Statements:
    def __init__(self) -> None:
        self.executed: list[tuple[str, object]] = []

    def execute(self, sql: str, params: object = None) -> None:
        self.executed.append((sql, params))


@pytest.mark.parametrize(
    "fill",
    [
        analyst_ratings.materialize_unvisited_issuers,
        supply_chain_extraction.materialize_unvisited_issuers,
        theme_purity.materialize_unvisited_issuers,
    ],
)
@pytest.mark.parametrize("legacy_id", ["issuer:lei:AAAAAAAAAAAAAAAAAA01", "issuer:cik:0000320193", ""])
def test_a_fill_row_is_never_written_under_a_legacy_id(fill: Any, legacy_id: str) -> None:
    connection = _Statements()

    with pytest.raises(ValueError, match="not the wide row's id"):
        fill(connection, run_id=_FILL_RUN, cutoff=CUTOFF, unvisited=[(legacy_id, "head_member_not_in_universe")])

    assert connection.executed == []


def test_a_fill_row_claims_no_answer_and_no_verification(
    connection: psycopg.Connection[Any], lane_world: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    head = _capture_qqq_head(connection, monkeypatch)
    removed = _qqq_issuers()[-1]
    removed_entity = lookup_entity(connection, removed, "issuer", as_of=QQQ_REPORT_DATE, known_at=QQQ_CUTOFF)
    _publish_universe(monkeypatch, date(2026, 7, 4), without=removed)
    _run_lane_ops("universe-list:qqq")

    for table, value in (
        ("mart.issuer_analyst_ratings", "consensus_rating"),
        ("mart.issuer_supply_chain_exposure", "exposure_score"),
    ):
        row = connection.execute(
            f"select extractor, availability_status, source_evidence_status, factor_validation_status, "  # noqa: S608
            f"confidence, reason_codes, {value}, cutoff from {table} where run_id = %s and issuer_id = %s",
            (head.run_id, str(removed_entity)),
        ).fetchone()
        assert row == (
            "lane:unvisited:v1",
            "unavailable",
            "degraded",
            "not_evaluated",
            Decimal(0),
            ["head_member_not_in_universe"],
            None,
            head.cutoff,
        ), table


# --- Q6: a wide-row issuer without a segment partition gets a row for each theme (#1117)

#: What a Q6 fill row holds. The eight columns that describe a partition are NULL. A fill has no partition.
_PURITY_FILL = {
    "extractor": "lane:unvisited:v1",
    "availability_status": "unavailable",
    "source_evidence_status": "degraded",
    "factor_validation_status": "not_evaluated",
    "reason_codes": ["no_segment_partition"],
    "segments": 0,
    "confidence": Decimal(0),
    "theme_share": None,
    "cik": None,
    "period_end": None,
    "partition_id": None,
    "consolidated_revenue": None,
    "in_theme_revenue": None,
    "out_of_theme_revenue": None,
    "unclassified_revenue": None,
    "partition_residual": None,
}


def _run_purity_op(universe: str = UNIVERSE) -> dict[str, Any]:
    config = standards.StandardBackfillConfig(executed_at=EXECUTED_AT, universe=universe)
    return json.loads(standards.run_theme_purity(dg.build_op_context(), config, "{}"))


def _purity_rows(connection: psycopg.Connection[Any], run_id: str) -> dict[str, dict[str, dict[str, Any]]]:
    """The Q6 rows of the run: issuer id, then theme id, then the columns of the row."""
    cursor = connection.execute(
        f"select issuer_id, theme_id, {', '.join(_PURITY_FILL)} from mart.issuer_theme_purity where run_id = %s",  # noqa: S608
        (run_id,),
    )
    names = [column.name for column in cursor.description or ()]
    rows: dict[str, dict[str, dict[str, Any]]] = {}
    for values in cursor.fetchall():
        row = dict(zip(names, values, strict=True))
        rows.setdefault(row.pop("issuer_id"), {})[row.pop("theme_id")] = row
    return rows


#: The eight columns that describe a segment partition. A fill row holds NULL in all of them.
_PARTITION_COLUMNS = (
    "cik",
    "period_end",
    "partition_id",
    "consolidated_revenue",
    "in_theme_revenue",
    "out_of_theme_revenue",
    "unclassified_revenue",
    "partition_residual",
)


def _purity_row(**overrides: Any) -> dict[str, Any]:
    """The columns of a real refused row: it has a partition, no share, and masses that add up."""
    row: dict[str, Any] = {
        "run_id": _FILL_RUN,
        "issuer_id": str(uuid.uuid4()),
        "theme_id": "ai-infrastructure",
        "theme": "AI infrastructure",
        "definition_version": "v0",
        "definition_sha256": "a" * 64,
        "cutoff": CUTOFF,
        "cik": 4242,
        "period_end": date(2026, 3, 31),
        "partition_id": "segment-partition:" + "e" * 64,
        "theme_share": None,
        "consolidated_revenue": Decimal(100),
        "in_theme_revenue": Decimal(0),
        "out_of_theme_revenue": Decimal(0),
        "unclassified_revenue": Decimal(100),
        "partition_residual": Decimal(0),
        "segments": 2,
        "confidence": Decimal(0),
        "reason_codes": ["below_minimum_classified_share"],
        "extractor": "rule:test",
        "availability_status": "unavailable",
        "source_evidence_status": "degraded",
        "factor_validation_status": "not_evaluated",
    }
    return row | overrides


def _insert_purity_row(connection: psycopg.Connection[Any], row: dict[str, Any]) -> None:
    """Insert `row` after a savepoint, so a refusal leaves the connection usable.

    `connection.transaction()` is not used: on an idle connection it commits, and the test would
    leave its row in the database.
    """
    columns = ", ".join(row)
    placeholders = ", ".join(["%s"] * len(row))
    connection.execute("savepoint purity_insert")
    try:
        connection.execute(
            f"insert into mart.issuer_theme_purity ({columns}) values ({placeholders})",  # noqa: S608
            list(row.values()),
        )
    except psycopg.Error:
        connection.execute("rollback to savepoint purity_insert")
        raise
    connection.execute("release savepoint purity_insert")


def test_a_real_refused_row_with_a_partition_is_accepted(connection: psycopg.Connection[Any]) -> None:
    _insert_purity_row(connection, _purity_row())


@pytest.mark.parametrize("column", _PARTITION_COLUMNS)
def test_a_real_purity_row_without_a_partition_column_is_refused(
    connection: psycopg.Connection[Any], column: str
) -> None:
    with pytest.raises(psycopg.errors.CheckViolation, match="issuer_theme_purity_partition_or_fill_check"):
        _insert_purity_row(connection, _purity_row(**{column: None}))


_FILL_COLUMN_VALUES = {
    "cik": 4242,
    "period_end": date(2026, 3, 31),
    "partition_id": "segment-partition:" + "e" * 64,
    "consolidated_revenue": Decimal(100),
    "in_theme_revenue": Decimal(0),
    "out_of_theme_revenue": Decimal(0),
    "unclassified_revenue": Decimal(100),
    "partition_residual": Decimal(0),
}


@pytest.mark.parametrize("column", _PARTITION_COLUMNS)
def test_a_fill_row_that_holds_a_partition_column_is_refused(connection: psycopg.Connection[Any], column: str) -> None:
    fill = _purity_row(**dict.fromkeys(_PARTITION_COLUMNS), extractor="lane:unvisited:v1") | {
        column: _FILL_COLUMN_VALUES[column]
    }
    with pytest.raises(psycopg.errors.CheckViolation, match="issuer_theme_purity_partition_or_fill_check"):
        _insert_purity_row(connection, fill)


@pytest.mark.parametrize(
    "claim", [{"availability_status": "available"}, {"theme_share": Decimal("0.5")}], ids=["available", "share"]
)
def test_a_fill_row_that_claims_an_answer_is_refused(
    connection: psycopg.Connection[Any], claim: dict[str, Any]
) -> None:
    fill = _purity_row(**dict.fromkeys(_PARTITION_COLUMNS), extractor="lane:unvisited:v1", **claim)
    with pytest.raises(psycopg.errors.CheckViolation, match="issuer_theme_purity_partition_or_fill_check"):
        _insert_purity_row(connection, fill)


def test_the_mass_sum_still_binds_a_refused_row_that_has_a_partition(connection: psycopg.Connection[Any]) -> None:
    """The fill needs no sum. A real row that is not `available` must still add up."""
    with pytest.raises(psycopg.errors.CheckViolation, match="issuer_theme_purity_check"):
        _insert_purity_row(connection, _purity_row(unclassified_revenue=Decimal(99)))


def test_the_purity_op_writes_a_row_for_each_theme_of_each_issuer_without_a_partition(
    connection: psycopg.Connection[Any], head: question_coverage.GovernedHead, monkeypatch: pytest.MonkeyPatch
) -> None:
    wide = _wide_row_ids(connection, head.run_id)
    assert len(wide) == ISSUERS
    partitioned = _seed_one_partition(connection, head, monkeypatch)

    summary = _run_purity_op()

    rows = _purity_rows(connection, head.run_id)
    assert set(rows) == wide, "every wide-row issuer ends with Q6 rows, not only the one with a partition"
    for issuer_id, by_theme in rows.items():
        assert set(by_theme) == set(THEMES), f"{issuer_id} has one row for each governed theme"
        if issuer_id == partitioned:
            assert all(row["extractor"] != "lane:unvisited:v1" for row in by_theme.values()), "a real row stays real"
            assert all(row["partition_id"] is not None for row in by_theme.values())
        else:
            assert all(row == _PURITY_FILL for row in by_theme.values()), issuer_id
    assert summary[standards.UNVISITED_ISSUERS] == ISSUERS - 1
    assert summary[standards.UNVISITED_WRITTEN] == ISSUERS - 1
    assert summary[standards.KEPT_REAL_ROWS] == 0
    assert summary[standards.UNVISITED_BY_REASON] == {"no_segment_partition": ISSUERS - 1}
    assert summary[standards.WIDE_ROW_ISSUERS] == ISSUERS


def test_the_coverage_report_names_no_segment_partition_instead_of_no_row_for_q6(
    connection: psycopg.Connection[Any], head: question_coverage.GovernedHead, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_one_partition(connection, head, monkeypatch)
    _run_purity_op()

    report = question_coverage.compile_report(
        connection, universe=UNIVERSE, executed_at=datetime(2026, 10, 6, tzinfo=UTC)
    )

    assert report is not None
    entry = report["questions"]["q6"]
    assert entry["denominator"] == ISSUERS
    assert question_coverage.NO_ROW not in entry["unavailable"], entry
    assert entry["unavailable"]["no_segment_partition"] == ISSUERS - 1, entry
    assert entry["answered"] + sum(entry["unavailable"].values()) == ISSUERS, entry


def test_a_rerun_of_the_purity_op_keeps_one_row_per_issuer_and_theme(
    connection: psycopg.Connection[Any], head: question_coverage.GovernedHead, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_one_partition(connection, head, monkeypatch)
    _run_purity_op()
    first = _purity_rows(connection, head.run_id)

    second_summary = _run_purity_op()

    assert _purity_rows(connection, head.run_id) == first
    assert second_summary[standards.UNVISITED_BY_REASON] == {"no_segment_partition": ISSUERS - 1}


def test_a_run_with_no_member_gets_no_fill_so_a_join_defect_still_reads_no_row(
    connection: psycopg.Connection[Any], head: question_coverage.GovernedHead, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#839: the member join found nothing, and Q6 fell from 12 of 20 to 0 of 20.
    A fill reason would call that defect a missing partition. The report must keep `no_row`.
    """
    monkeypatch.setattr(theme_purity, "governed_members", lambda _connection, *, run_id: {})

    summary = _run_purity_op()

    assert _purity_rows(connection, head.run_id) == {}
    assert summary[standards.UNVISITED_ISSUERS] == 0
    report = question_coverage.compile_report(
        connection, universe=UNIVERSE, executed_at=datetime(2026, 10, 6, tzinfo=UTC)
    )
    assert report is not None
    assert report["questions"]["q6"]["unavailable"] == {question_coverage.NO_ROW: ISSUERS}


# --- the identity wrapper catches what identity code raises, and nothing else


@pytest.mark.parametrize("error", [TypeError("a bug"), AttributeError("a bug"), KeyError("a bug")])
def test_a_bug_inside_the_identity_code_is_not_swallowed(
    connection: psycopg.Connection[Any],
    head: question_coverage.GovernedHead,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    from data_engine.datahub import canonical_issuer

    def broken(*_args: object, **_kwargs: object) -> None:
        raise error

    monkeypatch.setattr(canonical_issuer, "lookup_entity", broken)
    members = {issuer.issuer_id: issuer.ticker for issuer in planner.universe_issuers(connection, UNIVERSE)}

    with pytest.raises(type(error)):
        standards._canonical_universe_or_failure(  # type: ignore[arg-type]
            SimpleNamespace(log=_RecordingLog()), "analyst ratings", connection, head, members
        )


@pytest.mark.parametrize("value", [None, 5, b"02587046-dc99-5a44-b811-e2d086a58ccb", uuid.UUID(int=7)])
def test_only_a_string_can_be_the_wide_row_form(value: object) -> None:
    assert is_canonical_issuer_id(value) is False  # type: ignore[arg-type]


# --- the entities that a dropped member names


def test_a_dropped_member_names_the_survivor_of_its_entity_only_once_the_merge_is_known(
    connection: psycopg.Connection[Any],
) -> None:
    from data_engine.datahub.canonical_issuer import _named_entities

    loser = _mint(connection, "cik", "0000000777", at=datetime(2026, 1, 1, tzinfo=UTC))
    survivor = _mint(connection, "legacy-id", "test:survivor:777", at=datetime(2026, 1, 1, tzinfo=UTC))
    _merge(connection, loser, survivor, known_from=datetime(2026, 3, 1, tzinfo=UTC))
    member = UnmappedIssuer("issuer:cik:0000000777", "SEVEN", reason=NOT_IN_WIDE_ROW)

    after = _named_entities(connection, [member], known_at=datetime(2026, 4, 2, tzinfo=UTC))
    before = _named_entities(connection, [member], known_at=datetime(2026, 2, 1, tzinfo=UTC))

    assert after == {str(loser): NOT_IN_WIDE_ROW, str(survivor): NOT_IN_WIDE_ROW}
    assert before == {str(loser): NOT_IN_WIDE_ROW}


def test_the_first_member_to_name_an_entity_decides_its_reason(connection: psycopg.Connection[Any]) -> None:
    from data_engine.datahub.canonical_issuer import _named_entities

    entity = _mint(connection, "cik", "0000000778", at=datetime(2026, 1, 1, tzinfo=UTC))
    first = UnmappedIssuer("issuer:cik:0000000778", "ONE", reason=NO_CANONICAL_ISSUER_ID)
    second = UnmappedIssuer("issuer:cik:778", "TWO", reason=NOT_IN_WIDE_ROW)

    named = _named_entities(connection, [first, second], known_at=CUTOFF)
    reversed_named = _named_entities(connection, [second, first], known_at=CUTOFF)

    assert named == {str(entity): NO_CANONICAL_ISSUER_ID}
    assert reversed_named == {str(entity): NOT_IN_WIDE_ROW}


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
    assert [(m.legacy_id, m.reason) for m in universe.unmapped] == [(str(entity), "duplicate_corpus_id")]


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


def _seed_one_partition(
    connection: psycopg.Connection[Any], head: question_coverage.GovernedHead, monkeypatch: pytest.MonkeyPatch
) -> str:
    """Give the lowest-CIK member of the head one accepted segment partition, and seat a canned classifier.

    Returns the wide-row id of that member. Every other member of the head keeps no partition.
    """
    from data_engine.datahub.production_topt.theme_purity import governed_members
    from data_engine.sources import llm

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parent))
    from production_topt.test_theme_purity_producer import _answers, _seed  # noqa: E402

    members = governed_members(connection, run_id=head.run_id)
    assert members, "the head has members whose financials were fetched"
    cik = min(members)
    _seed(
        connection,
        cik=cik,
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
    return members[cik]


def _write_every_table(
    connection: psycopg.Connection[Any], head: question_coverage.GovernedHead, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run the real writer of each lane table on the head. The capture fixture wrote the first two."""
    from data_engine.datahub.production_topt.theme_purity import materialize_theme_purity
    from data_engine.datahub.strategy_bridge import persist_strategy_input_coverage

    persist_strategy_input_coverage(connection, head.run_id, cutoff=head.cutoff)

    _seed_one_partition(connection, head, monkeypatch)
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
