"""A degraded tick must leave a durable record instead of erasing itself (#538).

`mart.topt_capture_status` is a view over `raw.capture_*`, and the whole tick is one
transaction (`dagster_defs.py`), so the all-or-nothing raise in the composition root took
the run's own evidence down with it. Measured consequence: 210 recorded capture runs
across Staging and Production, every single one `(84, 84, 0, 0, 0, complete)` — one
distinct row — while Staging carried 8 Dagster FAILUREs. `failed_count`,
`unavailable_count` and `skipped_count` exist to record degradation and had never held a
non-zero value.

These tests drive the deployed `run_topt_pipeline` over the real schema with fake
fetchers, sabotage exactly one of the 84 obligations, and then assert on a SEPARATE
connection, after the tick's transaction has been rolled back, that:

  * the run row survives with its true counts (`success_count = 83`);
  * the quality report states the shortfall and names the cell that did not resolve;
  * the tick still failed, nothing was materialized, and the pointer head did not move.

That last bullet is the half of #538 deliberately NOT delivered yet: the raise stays until
#536 gates the pointer, so a test that let a partial run through would be asserting a
regression.

One further test covers the price of recording: a committed run's outcomes are history, so
replaying the same tick is refused up front with the reason already on file instead of
colliding inside the sink after a wasted round of vendor calls.

The #635 reuse window lives here too, and so does its operator override (#874): a
forced tick fetches every obligation even when fresh observations sit committed inside
the window, under a capture identity of its own, and a retry of that forced launch is
as idempotent as any other.

So does what a forced tick shares with the scheduled one, its cutoff (#877): the readers
that paired rows by cutoff are driven here through the deployed op, next to the runs
that made them ambiguous.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import subprocess
import uuid
from collections import Counter
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
import pytest
from data_engine import raw_store
from data_engine.config import settings
from data_engine.datahub.production_topt import composition
from data_engine.datahub.production_topt.composition import (
    CaptureNotPublishableError,
    PlannedRun,
    run_topt_pipeline,
)
from data_engine.datahub.production_topt.executor import (
    FetchFailure,
    FetchOutcome,
    SourceFetchPort,
)
from data_engine.datahub.production_topt.headcount import PostgresHeadcountExtractor, record_headcount
from data_engine.datahub.production_topt.market_price_adapter import (
    CorroboratingOrigin,
    MarketPriceAdapter,
    MarketPriceQuote,
    MarketPriceTarget,
)
from data_engine.datahub.production_topt.release_derived_adapter import ReleaseDerivedAdapter, ReleaseDerivedRecord
from data_engine.datahub.production_topt.sec_financial_adapter import (
    FinancialFactsBundle,
    SecFinancialFactAdapter,
    SecTarget,
)
from factors.production_topt import OperatingBranch
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from truealpha_contracts.datahub import CaptureWorkItem
from truealpha_contracts.models import DataSource, RawCapture, RawIngestionEnvelope, RawObjectRef
from truealpha_contracts.obligation_reason_codes import ObligationReasonCode

REPOSITORY_ROOT = next(
    parent
    for parent in Path(__file__).resolve().parents
    if (parent / "governance" / "vision-issue-graph.json").is_file()
)
CUTOFF = datetime(2026, 4, 2, tzinfo=UTC)
OBLIGATIONS = 84
# listing-identity + universe-membership per listing — the run's own identity,
# excluded from cross-run reuse since #684.
_RELEASE_OBLIGATIONS = 42
_BANK_TICKER = "JPM"


# -- an isolated database ---------------------------------------------------------------
#
# The behaviour under test is a COMMIT that outlives the tick's abort, so the usual
# rollback-per-test fixture cannot be used: it would erase exactly what must survive, and
# the capture-control tables carry `reject_mutation` triggers that forbid cleaning up by
# DELETE. A throwaway database is the only way to assert durability and still leave
# nothing behind.


@pytest.fixture(scope="module")
def tick_database_url():
    parameters = conninfo_to_dict(settings.database_url)
    database_name = f"truealpha_degraded_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    admin_url = make_conninfo(**(parameters | {"dbname": "postgres"}))
    target_url = make_conninfo(**(parameters | {"dbname": database_name}))
    try:
        with psycopg.connect(admin_url, connect_timeout=3, autocommit=True) as admin:
            admin.execute(sql.SQL("create database {}").format(sql.Identifier(database_name)))
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs the required integration coverage")
    try:
        for migration in (*sorted((REPOSITORY_ROOT / "db/migrations").glob("*.sql")), REPOSITORY_ROOT / "db/roles.sql"):
            completed = subprocess.run(
                ["psql", target_url, "-v", "ON_ERROR_STOP=1", "-f", str(migration)],
                check=False,
                capture_output=True,
                text=True,
            )
            if completed.returncode != 0:
                pytest.fail(completed.stdout + completed.stderr, pytrace=False)
        yield target_url
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(
                "select pg_terminate_backend(pid) from pg_stat_activity where datname = %s",
                (database_name,),
            )
            admin.execute(sql.SQL("drop database if exists {}").format(sql.Identifier(database_name)))


class _InMemoryObjectStore:
    """`RawObjectStore` over a dict, so the real landing path runs without MinIO."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def store(self, capture: RawCapture) -> RawIngestionEnvelope:
        digest = hashlib.sha256(capture.body).hexdigest()
        key = f"raw/{capture.source.value}/{digest[:2]}/{digest}"
        self.objects[key] = capture.body
        return RawIngestionEnvelope(
            source=capture.source,
            source_record_id=capture.source_record_id,
            object=RawObjectRef(
                bucket="truealpha-raw",
                key=key,
                sha256=digest,
                byte_length=len(capture.body),
                content_type=capture.content_type,
            ),
            fetched_at=capture.fetched_at,
            source_published_at=capture.source_published_at,
            metadata=capture.metadata,
        )

    def get(self, ref: RawObjectRef) -> bytes:
        return self.objects[ref.key]


class _FailingPort:
    """One deliberately failing obligation: the flaky vendor cell from the issue."""

    def __init__(self, reason: ObligationReasonCode) -> None:
        self._reason = reason

    def fetch(self, work_item: CaptureWorkItem) -> FetchOutcome:
        return FetchFailure(self._reason)


class _CountingPort:
    """Records every fetch a route actually served, then delegates.

    The record is (run id, work item, semantic): which run asked, for which cell, of
    which kind. A cell #635 satisfied never reaches its port, so absence from this log
    is the evidence that no vendor was consulted for it.
    """

    def __init__(self, inner: SourceFetchPort, *, run_id: str, semantic: str, log: list[tuple[str, str, str]]):
        self._inner = inner
        self._run_id = run_id
        self._semantic = semantic
        self._log = log

    def fetch(self, work_item: CaptureWorkItem) -> FetchOutcome:
        self._log.append((self._run_id, work_item.work_item_id, self._semantic))
        return self._inner.fetch(work_item)


def _quote(day: date = date(2026, 3, 31), close: Decimal = Decimal("40")) -> MarketPriceQuote:
    return MarketPriceQuote(
        raw_bytes=f"bar:{day.isoformat()}:{close}".encode(),
        close=close,
        as_of=day,
        knowable_at=datetime.combine(day, datetime.min.time(), tzinfo=UTC),
    )


def _bundle(branch: OperatingBranch) -> FinancialFactsBundle:
    financial = branch is OperatingBranch.FINANCIAL
    return FinancialFactsBundle(
        gross_profit=Decimal("80000000") if financial else Decimal("210000000"),
        total_assets=Decimal("200000000"),
        shares_outstanding=Decimal("10000000"),
        revenue=Decimal("100000000"),
        pre_provision_profit=Decimal("80000000") if financial else None,
        raw_bytes=b'{"facts":{}}',
        knowable_at=datetime(2026, 2, 1, tzinfo=UTC),
    )


def _offline_routes(
    plan: PlannedRun,
    connection,
    *,
    quote: Callable[[], MarketPriceQuote] = _quote,
    price_cutoff: date | None = None,
    corroborating_origins: tuple[CorroboratingOrigin, ...] = (),
    cutoff_date: date | None = None,
) -> dict[str, SourceFetchPort]:
    """The deployed adapters over fake fetchers, routed exactly as `build_routes` does.

    Stands in for `build_routes`, which resolves CIKs from SEC over the network. Every
    layer under it — executor, sink, freeze, materialize, quality report — is the real
    one, because the gate this exercises reads what those layers persisted.

    `cutoff_date` is the date the non-price routes judge look-ahead by; it defaults to the
    module's CUTOFF, and a run whose universe partition lies after that date (the QQQ
    corpus, 2026-06-30) passes its own.
    """
    cutoff_date = cutoff_date or CUTOFF.date()
    price_targets: dict[str, MarketPriceTarget] = {}
    sec_targets: dict[str, SecTarget] = {}
    release_targets: dict[str, ReleaseDerivedRecord] = {}
    cik_by_ticker: dict[str, int] = {}
    for work_item_id, binding in plan.bindings.items():
        semantic_type = binding.obligation.capture_requirement_id.removesuffix(":v1")
        issuer_id, instrument_id, listing_id, ticker = plan.coordinates[binding.obligation.subject.id]
        cik_by_ticker.setdefault(ticker, 100000 + sorted(plan.coordinates).index(listing_id))
        if semantic_type == "market-price":
            price_targets[work_item_id] = MarketPriceTarget(
                symbol=ticker,
                cutoff=price_cutoff or cutoff_date,
                issuer_id=issuer_id,
                instrument_id=instrument_id,
                listing_id=listing_id,
            )
        elif semantic_type == "financial-fact":
            sec_targets[work_item_id] = SecTarget(
                cik=cik_by_ticker[ticker],
                cutoff=cutoff_date,
                issuer_id=issuer_id,
                instrument_id=instrument_id,
                listing_id=listing_id,
                operating_branch=(
                    OperatingBranch.FINANCIAL if ticker == _BANK_TICKER else OperatingBranch.NON_FINANCIAL
                ),
            )
        else:
            release_targets[work_item_id] = ReleaseDerivedRecord(
                semantic_type=semantic_type,
                subject_id=listing_id,
                payload={
                    "issuer_id": issuer_id,
                    "instrument_id": instrument_id,
                    "listing_id": listing_id,
                    "ticker": ticker,
                },
                knowable_at=plan.timeline.partition_start,
            )

    for ticker, cik in sorted(cik_by_ticker.items()):
        record_headcount(
            connection,
            cik=cik,
            headcount=Decimal("164000"),
            knowable_at=datetime(2026, 1, 1, tzinfo=UTC),
            source="test-fixture",
            evidence_ref=f"test:{ticker}",
            confidence=Decimal("0.7"),
        )

    price = MarketPriceAdapter(
        price_targets,
        lambda symbol, cutoff: quote(),
        corroborating_origins=corroborating_origins,
    )
    financial = SecFinancialFactAdapter(
        sec_targets,
        lambda cik, cutoff, branch: _bundle(branch),
        headcount_extractor=PostgresHeadcountExtractor(connection),
    )
    release = ReleaseDerivedAdapter(release_targets, cutoff=cutoff_date)

    routes: dict[str, SourceFetchPort] = {}
    routes.update(dict.fromkeys(price_targets, price))
    routes.update(dict.fromkeys(sec_targets, financial))
    routes.update(dict.fromkeys(release_targets, release))
    return routes


def _arm(
    monkeypatch,
    *,
    sabotage: tuple[int, ObligationReasonCode] | None = None,
    spy: list[str] | None = None,
    fetched: list[tuple[str, str, str]] | None = None,
    **route_options,
) -> None:
    """Route the tick through offline adapters, optionally failing one obligation.

    `spy` records each entry into route building. In production that step resolves CIKs
    from SEC over the network, so an empty spy is the evidence that a tick was refused
    before it reached a vendor at all. `fetched` records each fetch a route served (see
    `_CountingPort`); `route_options` reach `_offline_routes`.
    """
    monkeypatch.setattr(raw_store, "object_store", _InMemoryObjectStore)

    def build(plan: PlannedRun, connection=None) -> dict[str, SourceFetchPort]:
        if spy is not None:
            spy.append(plan.run_id)
        routes = _offline_routes(plan, connection, **route_options)
        if sabotage is not None:
            index, reason = sabotage
            routes[plan.work_items[index].work_item_id] = _FailingPort(reason)
        if fetched is not None:
            routes = {
                work_item_id: _CountingPort(
                    port,
                    run_id=plan.run_id,
                    semantic=plan.bindings[work_item_id].obligation.capture_requirement_id.removesuffix(":v1"),
                    log=fetched,
                )
                for work_item_id, port in routes.items()
            }
        return routes

    monkeypatch.setattr(composition, "build_routes", build)


def _run_tick(url: str, *, version: str, cutoff: datetime = CUTOFF, force_fetch: bool = False):
    """One tick, shaped exactly like `dagster_defs.run_topt_live_tick`: a single
    connection, a single transaction, rolled back by the context manager when the tick
    raises. Anything that survives this survived the abort.

    Tests in this module share one database, and #635's cross-run reuse deliberately
    satisfies obligations from any run committed within twelve hours of the cutoff —
    so each test that must NOT see its predecessors' observations runs at its own
    cutoff, more than the reuse window apart. The reuse test itself runs two ticks
    at ONE cutoff, which is the feature."""
    with psycopg.connect(url) as tick:
        result = run_topt_pipeline(tick, cutoff=cutoff, version=version, force_fetch=force_fetch)
        tick.commit()
        return result


def _pointer_heads(url: str) -> list[tuple]:
    with psycopg.connect(url) as reader:
        return reader.execute(
            "select environment, universe_id, universe_version, factor_id, target_run_id, sequence "
            "from mart.current_pointer_head order by 1, 2, 3, 4"
        ).fetchall()


def _status_row(url: str, run_id: str):
    with psycopg.connect(url) as reader:
        return reader.execute(
            """
            select obligation_count, terminal_count, success_count, unchanged_count,
                   unavailable_count, skipped_count, failed_count, complete
            from mart.topt_capture_status where run_id = %s
            """,
            (run_id,),
        ).fetchone()


def _report_payload(url: str, run_id: str) -> dict:
    with psycopg.connect(url) as reader:
        rows = reader.execute(
            "select payload from mart.datahub_quality_report where run_id = %s order by created_at",
            (run_id,),
        ).fetchall()
    assert len(rows) == 1, f"a run must persist exactly one quality report, found {len(rows)}"
    return rows[0][0]


def _materialized(url: str, run_id: str) -> tuple[int, int, int]:
    with psycopg.connect(url) as reader:
        snapshots = reader.execute(
            "select count(*) from staging.topt_core_snapshots where run_id = %s", (run_id,)
        ).fetchone()[0]
        gppe = reader.execute("select count(*) from mart.topt_gppe_results where run_id = %s", (run_id,)).fetchone()[0]
        core = reader.execute("select count(*) from mart.topt_core_results where run_id = %s", (run_id,)).fetchone()[0]
    return snapshots, gppe, core


def test_one_unavailable_obligation_persists_a_degraded_run_record(tick_database_url, monkeypatch) -> None:
    """The issue's headline case: 83 of 84 cells resolve, one does not.

    Before this change the tick raised inside the single transaction, so `raw.capture_*`
    rolled back with it and `mart.topt_capture_status` — a view over those tables — never
    held the run at all. The assertion is deliberately made on a fresh connection AFTER
    the tick aborted: a row that only exists inside the doomed transaction is not a record.
    """
    heads_before = _pointer_heads(tick_database_url)
    _arm(monkeypatch, sabotage=(0, ObligationReasonCode.FIELD_UNAVAILABLE))

    with pytest.raises(CaptureNotPublishableError) as raised:
        _run_tick(tick_database_url, version="degraded-unavailable")

    run_id = raised.value.run_id
    assert _status_row(tick_database_url, run_id) == (
        OBLIGATIONS,  # obligation_count
        OBLIGATIONS,  # terminal_count — every cell reached a terminal state
        OBLIGATIONS - 1,  # success_count
        0,  # unchanged_count
        1,  # unavailable_count — the column that had never been non-zero
        0,  # skipped_count
        0,  # failed_count
        True,  # complete
    )

    shortfall = _report_payload(tick_database_url, run_id)["capture_shortfall"]
    assert shortfall["success_count"] == OBLIGATIONS - 1
    assert shortfall["obligation_count"] == OBLIGATIONS
    assert shortfall["unavailable_count"] == 1
    assert f"{OBLIGATIONS - 1} of {OBLIGATIONS}" in shortfall["reason"]
    assert [cell["terminal_state"] for cell in shortfall["unresolved"]] == ["unavailable"]
    assert shortfall["unresolved"][0]["reason_codes"] == [ObligationReasonCode.FIELD_UNAVAILABLE.value]

    # The protective half of the gate is intentionally untouched until #536 lands.
    assert shortfall["materialized"] is False and shortfall["published"] is False
    assert _materialized(tick_database_url, run_id) == (0, 0, 0)
    assert _pointer_heads(tick_database_url) == heads_before


def test_a_halted_run_persists_its_failed_cell(tick_database_url, monkeypatch) -> None:
    """A stop-disposition reason code halts the run; the record must survive that too.

    Sabotaging the LAST work item is what makes `failed_count` observable end to end:
    every earlier obligation has already resolved, so the persisted row reads 83 success
    + 1 failed instead of a run that simply stops mid-way.
    """
    heads_before = _pointer_heads(tick_database_url)
    _arm(monkeypatch, sabotage=(-1, ObligationReasonCode.AUTH_FAILED))

    with pytest.raises(CaptureNotPublishableError) as raised:
        _run_tick(tick_database_url, version="degraded-halted", cutoff=CUTOFF + timedelta(days=1))

    run_id = raised.value.run_id
    # The halting code colours the raised message; the persisted record keeps it on the
    # cell it belongs to, so the report stays derivable from the status alone.
    assert "halted on auth_failed" in raised.value.shortfall
    assert _status_row(tick_database_url, run_id) == (
        OBLIGATIONS,
        OBLIGATIONS,
        OBLIGATIONS - 1,
        0,
        0,
        0,
        1,  # failed_count — non-zero for the first time in 210 recorded runs
        True,
    )

    shortfall = _report_payload(tick_database_url, run_id)["capture_shortfall"]
    assert shortfall["failed_count"] == 1
    assert [cell["terminal_state"] for cell in shortfall["unresolved"]] == ["failed"]
    assert shortfall["unresolved"][0]["reason_codes"] == [ObligationReasonCode.AUTH_FAILED.value]
    assert _materialized(tick_database_url, run_id) == (0, 0, 0)
    assert _pointer_heads(tick_database_url) == heads_before


def test_replaying_a_recorded_tick_reports_the_record_instead_of_recapturing(tick_database_url, monkeypatch) -> None:
    """Recording a degraded run settles that tick's identity — and says so.

    Capture-control rows are append-only and one obligation holds at most one terminal
    result, so a committed outcome cannot be overwritten by a later replay of the same
    `(cutoff, version)`. Before the up-front check, that surfaced as a content conflict on
    an attempt-result identity raised deep inside the sink, AFTER the replay had already
    spent a full round of vendor calls — the "refuse the run for a reason far from its
    cause" failure `persistence.py` warns about. The replay must fail with the reason
    already on file, and must not write a second report for the same run.
    """
    _arm(monkeypatch, sabotage=(0, ObligationReasonCode.FIELD_UNAVAILABLE))
    with pytest.raises(CaptureNotPublishableError) as first:
        _run_tick(tick_database_url, version="degraded-replayed", cutoff=CUTOFF + timedelta(days=2))

    # The same tick again, this time with nothing sabotaged: the recorded outcome stands.
    calls: list[str] = []
    _arm(monkeypatch, spy=calls)
    with pytest.raises(CaptureNotPublishableError) as replayed:
        _run_tick(tick_database_url, version="degraded-replayed", cutoff=CUTOFF + timedelta(days=2))

    assert replayed.value.run_id == first.value.run_id
    assert replayed.value.quality_report_id == first.value.quality_report_id
    assert f"{OBLIGATIONS - 1} of {OBLIGATIONS}" in replayed.value.shortfall
    assert calls == [], "a settled run must be refused before any vendor call"
    # _report_payload asserts exactly one report row for the run.
    assert _report_payload(tick_database_url, first.value.run_id)["capture_shortfall"]["success_count"] == (
        OBLIGATIONS - 1
    )
    assert _status_row(tick_database_url, first.value.run_id)[2] == OBLIGATIONS - 1
    assert _materialized(tick_database_url, first.value.run_id) == (0, 0, 0)


def test_a_complete_run_still_materializes_and_reports_no_shortfall(tick_database_url, monkeypatch) -> None:
    """The control: recording degradation must not change what a healthy tick does."""
    _arm(monkeypatch)

    result = _run_tick(tick_database_url, version="complete-control", cutoff=CUTOFF + timedelta(days=3))

    assert result.core_result_count == 20
    assert "capture_shortfall" not in result.quality
    assert _status_row(tick_database_url, result.run_id) == (
        OBLIGATIONS,
        OBLIGATIONS,
        OBLIGATIONS,
        0,
        0,
        0,
        0,
        True,
    )
    snapshots, gppe, core = _materialized(tick_database_url, result.run_id)
    assert (snapshots, gppe, core) == (1, 20, 20)


def test_a_freeze_failure_no_longer_erases_the_committed_capture(tick_database_url, monkeypatch) -> None:
    """#628: three QQQ runs each captured everything over ~39 minutes and lost it all
    when freeze failed inside the same transaction. The capture now commits the moment
    it is complete and successful: a publish-side crash aborts only the cheap half."""
    _arm(monkeypatch)
    from data_engine.datahub.production_topt.materialization import PostgresToptCoreRepository

    def _boom(self, *, run_id: str, release_manifest_id: str):
        raise RuntimeError("injected freeze failure (#628)")

    monkeypatch.setattr(PostgresToptCoreRepository, "freeze_snapshot", _boom)
    with pytest.raises(RuntimeError, match="injected freeze failure"):
        _run_tick(tick_database_url, version="freeze-dies", cutoff=CUTOFF + timedelta(days=4))

    # The tick's transaction aborted — but the capture survived it, complete and
    # queryable on a fresh connection, with nothing frozen or served.
    with psycopg.connect(tick_database_url) as reader:
        # All ticks in this module share CUTOFF; the freeze-died run is the one that
        # is complete yet has no snapshot.
        run_id = reader.execute(
            "select s.run_id from mart.topt_capture_status s"
            " where s.complete and s.success_count = s.obligation_count"
            " and not exists (select 1 from staging.topt_core_snapshots c where c.run_id = s.run_id)"
        ).fetchone()[0]
    status = _status_row(tick_database_url, run_id)
    assert status is not None and status[7] is True and status[0] == status[2] == OBLIGATIONS
    snapshots, gppe, core = _materialized(tick_database_url, run_id)
    assert (snapshots, gppe, core) == (0, 0, 0)
    return None


def test_a_retry_after_freeze_failure_resumes_without_recapturing(tick_database_url, monkeypatch) -> None:
    """#628's second half: the retry of a committed-capture/failed-publish tick RESUMES —
    it freezes the EXISTING run instead of refusing (#538's refusal stays for degraded
    histories) and never re-fetches a vendor byte."""
    _arm(monkeypatch)
    from data_engine.datahub.production_topt import capture_orchestration
    from data_engine.datahub.production_topt.materialization import PostgresToptCoreRepository

    def _boom(self, *, run_id: str, release_manifest_id: str):
        raise RuntimeError("injected freeze failure (#628)")

    real_freeze = PostgresToptCoreRepository.freeze_snapshot
    monkeypatch.setattr(PostgresToptCoreRepository, "freeze_snapshot", _boom)
    with pytest.raises(RuntimeError, match="injected freeze failure"):
        _run_tick(tick_database_url, version="resume-after-freeze", cutoff=CUTOFF + timedelta(days=5))
    monkeypatch.setattr(PostgresToptCoreRepository, "freeze_snapshot", real_freeze)

    def _no_recapture(*args, **kwargs):
        raise AssertionError("resume must not re-run capture")

    monkeypatch.setattr(capture_orchestration, "run_topt_capture", _no_recapture)
    monkeypatch.setattr(composition, "run_topt_capture", _no_recapture)

    result = _run_tick(tick_database_url, version="resume-after-freeze", cutoff=CUTOFF + timedelta(days=5))
    assert result.core_result_count == 20
    snapshots, gppe, core = _materialized(tick_database_url, result.run_id)
    assert (snapshots, gppe, core) == (1, 20, 20)


def test_a_second_run_reuses_committed_observations_without_vendor_calls(tick_database_url, monkeypatch) -> None:
    """#635 as amended by #684: the 13 TOPT∩QQQ overlap names were fetched once per
    universe per day — VENDOR semantics reuse those observations, every terminal
    UNCHANGED with the reused primary vintage and both price origins re-bound.
    Release-derived semantics are the run's own identity and must NOT ride reuse
    (reusing them imported a foreign corpus's issuer keying, #684): they execute
    fresh, from this run's own coordinates, on every run."""
    _arm(monkeypatch)
    # The offline price fixture's bar is knowable 2026-03-31; a reused price must BE
    # the cutoff's settled session, so both ticks run after that session's close
    # (22:15 UTC = 18:15 EDT) — the exact production shape (TOPT 22:15 -> QQQ 23:20).
    reuse_cutoff = datetime(2026, 3, 31, 22, 15, tzinfo=UTC)
    first = _run_tick(tick_database_url, version="reuse-source", cutoff=reuse_cutoff)
    second = _run_tick(tick_database_url, version="reuse-target", cutoff=reuse_cutoff)

    assert second.run_id != first.run_id
    # 42 UNCHANGED can only come from the reuse path (vendor never executed);
    # 42 fresh SUCCESS can only come from the executor deriving release semantics.
    assert _status_row(tick_database_url, second.run_id) == (
        OBLIGATIONS,
        OBLIGATIONS,
        _RELEASE_OBLIGATIONS,
        OBLIGATIONS - _RELEASE_OBLIGATIONS,
        0,
        0,
        0,
        True,
    )
    # The #684 regression proper: identity semantics carry THIS run's own fresh
    # vintage — never a reused one from whichever run captured the subject last.
    with psycopg.connect(tick_database_url) as reader:
        foreign_identity = reader.execute(
            """
            select count(*)
            from raw.capture_obligations ob
            join raw.capture_obligation_results done on done.capture_obligation_id = ob.obligation_id
            join raw.capture_attempt_results attempt on attempt.attempt_id = done.final_attempt_id
            where ob.run_id = %s
              and regexp_replace(ob.capture_requirement_id, ':v1$', '') in ('listing-identity', 'universe-membership')
              and (done.terminal_state <> 'success' or attempt.reused_source_vintage_id is not null)
            """,
            (second.run_id,),
        ).fetchone()[0]
    assert foreign_identity == 0
    snapshots, gppe, core = _materialized(tick_database_url, second.run_id)
    assert (snapshots, gppe, core) == (1, 20, 20)


def test_reuse_never_looks_ahead_of_its_own_cutoff(tick_database_url, monkeypatch) -> None:
    """Review on #664: a run completed AFTER this run's cutoff must not satisfy it —
    reuse without an upper bound would be a look-ahead violation. The earlier tick
    here finds only future evidence and captures fresh (every terminal SUCCESS)."""
    _arm(monkeypatch)
    # After every fixture knowable date and >12h clear of the module's other
    # cutoffs, so the ONLY reuse candidate in range is the deliberately-future one.
    future_cutoff = datetime(2026, 4, 9, 22, 15, tzinfo=UTC)  # Thursday, post-close
    _run_tick(tick_database_url, version="lookahead-source", cutoff=future_cutoff)

    earlier_cutoff = future_cutoff - timedelta(hours=2)
    result = _run_tick(tick_database_url, version="lookahead-target", cutoff=earlier_cutoff)
    assert _status_row(tick_database_url, result.run_id) == (
        OBLIGATIONS,
        OBLIGATIONS,
        OBLIGATIONS,
        0,
        0,
        0,
        0,
        True,
    )


def test_a_fully_reused_run_still_exists_on_the_evidence_plane(tick_database_url, monkeypatch) -> None:
    """The first LIVE full-reuse canary failed at publish: the capture executor is
    what appends the run's evidence node, a fully reused run skips the executor,
    and binding the release manifest to a missing node is an FK violation. The
    skip path now appends the node itself."""
    _arm(monkeypatch)
    # Same settled session as the price fixture (2026-03-31) or the market-price
    # semantic falls back to a fresh fetch and the all-UNCHANGED assertion below
    # (rightly) fails. Sharing the reuse test's cutoff is safe: versions differ,
    # and cross-test reuse inside the window is the feature itself.
    reuse_cutoff = datetime(2026, 3, 31, 22, 15, tzinfo=UTC)
    _run_tick(tick_database_url, version="evidence-source", cutoff=reuse_cutoff)
    second = _run_tick(tick_database_url, version="evidence-target", cutoff=reuse_cutoff)
    # Post-#684 a maximally-reused run still executes its release semantics fresh,
    # so the executor appends the run node on the normal path; every VENDOR
    # terminal UNCHANGED proves reuse itself still worked. (The skip-path append
    # this test was born from, #666, stays in composition as the guard for a
    # hypothetical release-free plan.)
    assert _status_row(tick_database_url, second.run_id) == (
        OBLIGATIONS,
        OBLIGATIONS,
        _RELEASE_OBLIGATIONS,
        OBLIGATIONS - _RELEASE_OBLIGATIONS,
        0,
        0,
        0,
        True,
    )
    with psycopg.connect(tick_database_url) as reader:
        node = reader.execute(
            "select count(*) from staging.evidence_nodes where node_id = %s", (second.run_id,)
        ).fetchone()[0]
    assert node == 1


def test_reuse_requires_identity_coordinate_equality(tick_database_url, monkeypatch) -> None:
    """#684's second half: every normalized payload embeds the capturing run's
    (issuer, instrument, listing) trio, keyed by THAT run's corpus. A run whose
    plan keys the same subject differently (TOPT's LEI vs the planes' CIK) must
    capture fresh — reusing the foreign trio either mis-keys mart (pre-fix) or
    trips the snapshot's payload-agreement check (the live canary FAILURE this
    encodes). Same-keyed subjects keep sharing vendor bytes."""
    _arm(monkeypatch)
    cutoff = datetime(2026, 3, 31, 22, 15, tzinfo=UTC)
    _run_tick(tick_database_url, version="coordinate-source", cutoff=cutoff)

    probe = psycopg.connect(tick_database_url)
    try:
        plan = composition.plan_and_persist(probe, cutoff=cutoff, version="coordinate-target")
        victim = sorted(plan.coordinates)[0]
        _issuer_id, instrument_id, listing_id, ticker = plan.coordinates[victim]
        patched = dataclasses.replace(
            plan,
            coordinates={
                **plan.coordinates,
                victim: ("issuer:cik:0000999999", instrument_id, listing_id, ticker),
            },
        )
        satisfied = composition._satisfy_from_recent_observations(probe, patched, cutoff=cutoff)

        reused_by_subject: dict[str, dict[str, bool]] = {}
        for work_item_id, binding in patched.bindings.items():
            semantic = binding.obligation.capture_requirement_id.removesuffix(":v1")
            cells = reused_by_subject.setdefault(binding.obligation.subject.id, {})
            cells[semantic] = work_item_id in satisfied
        assert reused_by_subject[victim]["market-price"] is False
        assert reused_by_subject[victim]["financial-fact"] is False
        others = [subject for subject in reused_by_subject if subject != victim]
        assert others, "the corpus has more than one subject"
        assert all(reused_by_subject[s]["market-price"] and reused_by_subject[s]["financial-fact"] for s in others)
        assert not any(
            cells.get("listing-identity") or cells.get("universe-membership") for cells in reused_by_subject.values()
        )
    finally:
        probe.rollback()
        probe.close()


def test_reuse_requires_parser_vintage_equality(tick_database_url, monkeypatch) -> None:
    """#788: an anchor parsed by a different primary vintage must not satisfy an
    obligation.

    The reuse predicate used to check subject, semantic, bytes, freshness and (since
    #684) identity coordinates — but not the parser vintage. So the first ad-hoc tick
    after a parser bump reused the previous vintage's observations, issued no vendor
    call, and graded COMPLETE — while `seed_strategy_inputs_from_capture` selects by the
    DEPLOYED `PARSER_VERSION` and therefore saw none of them. Every issuer missed every
    strategy input and the plausibility gate refused the run as `empty-eligible-set`,
    which reads as a data outage rather than a vintage mismatch. Observed in production
    2026-09-09 on v0.0.49 (parser v9) against v0.0.48's v8 observations.

    The cost of the rule is one re-capture on the first tick after a bump. The value is
    that any tick can be re-run on demand right after a release — which is the whole
    point of having a job you can invoke as well as schedule.
    """
    _arm(monkeypatch)
    cutoff = datetime(2026, 3, 31, 22, 15, tzinfo=UTC)
    _run_tick(tick_database_url, version="parser-vintage-source", cutoff=cutoff)

    probe = psycopg.connect(tick_database_url)
    try:
        plan = composition.plan_and_persist(probe, cutoff=cutoff, version="parser-vintage-target")

        # Same vintage as the source run: reuse is expected, and is what makes the
        # negative below mean something.
        same_vintage = composition._satisfy_from_recent_observations(probe, plan, cutoff=cutoff)
        assert same_vintage, "an unbumped parser must still reuse (#635 is not disabled)"
        probe.rollback()

        # A bumped primary parser: the previous vintage's observations no longer qualify,
        # so the tick captures fresh instead of binding what it cannot consume.
        plan = composition.plan_and_persist(probe, cutoff=cutoff, version="parser-vintage-bumped")
        monkeypatch.setattr(composition, "PARSER_VERSION", "production-topt-live-parser:v999")
        bumped = composition._satisfy_from_recent_observations(probe, plan, cutoff=cutoff)
        assert bumped == frozenset(), (
            "a parser bump must force a fresh capture; reusing the old vintage produces a "
            "COMPLETE run that seeds nothing (#788)"
        )
    finally:
        probe.rollback()
        probe.close()


# -- forced fetch: the operator's override of the reuse window (#874) -------------------

_VENDOR_SEMANTICS = ("market-price", "financial-fact")
# The reuse tests' shared window: the offline price bar is the settled session here.
_REUSE_CUTOFF = datetime(2026, 3, 31, 22, 15, tzinfo=UTC)


def _run_plan(url: str, run_id: str) -> dict:
    with psycopg.connect(url) as reader:
        return reader.execute(
            "select payload from raw.production_topt_run_plans where run_id = %s", (run_id,)
        ).fetchone()[0]


def _fetch_row_count(url: str) -> int:
    with psycopg.connect(url) as reader:
        return reader.execute("select count(*) from raw.fetches").fetchone()[0]


def _served_closes(url: str, run_id: str) -> set[str]:
    """The primary close every market-price cell of the run is bound to."""
    with psycopg.connect(url) as reader:
        rows = reader.execute(
            """
            select distinct p.normalized_payload->>'close'
            from raw.capture_obligations ob
            join staging.capture_observation_obligations link on link.capture_obligation_id = ob.obligation_id
            join staging.capture_normalized_observations o on o.observation_id = link.observation_id
            join staging.capture_observation_payloads p on p.observation_id = o.observation_id
            where ob.run_id = %s and o.semantic_type = 'market-price'
            """,
            (run_id,),
        ).fetchall()
    return {row[0] for row in rows}


def _fetches_by_semantic(fetched: list[tuple[str, str, str]], run_id: str) -> Counter[str]:
    return Counter(semantic for run, _work_item, semantic in fetched if run == run_id)


def test_a_forced_run_fetches_every_obligation_despite_fresh_observations(tick_database_url, monkeypatch) -> None:
    """#874, the issue's own evidence: a same-day re-run reused the 03:38Z canary's
    observations for twelve hours, so a new origin could not be proven by hand. A
    forced run of the SAME tick (same executed_at, same version) is its own capture:
    every obligation reaches its route exactly once, and the run says it was forced."""
    fetched: list[tuple[str, str, str]] = []
    _arm(monkeypatch, fetched=fetched)
    _run_tick(tick_database_url, version="forced-seed", cutoff=_REUSE_CUTOFF)

    # The control: fresh observations inside the window, not forced -> no vendor call.
    control = _run_tick(tick_database_url, version="forced-same-tick", cutoff=_REUSE_CUTOFF)
    control_fetches = _fetches_by_semantic(fetched, control.run_id)
    assert not any(control_fetches[semantic] for semantic in _VENDOR_SEMANTICS), control_fetches
    assert _status_row(tick_database_url, control.run_id)[3] == OBLIGATIONS - _RELEASE_OBLIGATIONS

    fetch_rows_before = _fetch_row_count(tick_database_url)
    forced = _run_tick(tick_database_url, version="forced-same-tick", cutoff=_REUSE_CUTOFF, force_fetch=True)

    # Its own identity: the unforced run at this executed_at is settled history (#538),
    # so a forced capture that shared it would resume or refuse instead of fetching.
    assert forced.run_id != control.run_id
    per_cell = Counter(work_item for run, work_item, _semantic in fetched if run == forced.run_id)
    assert len(per_cell) == OBLIGATIONS and set(per_cell.values()) == {1}, "each obligation fetched exactly once"
    assert _fetches_by_semantic(fetched, forced.run_id) == {
        "market-price": 21,
        "financial-fact": 21,
        "listing-identity": 21,
        "universe-membership": 21,
    }
    # Every terminal a fresh SUCCESS; nothing rode the reuse path.
    assert _status_row(tick_database_url, forced.run_id) == (
        OBLIGATIONS,
        OBLIGATIONS,
        OBLIGATIONS,
        0,
        0,
        0,
        0,
        True,
    )
    assert _materialized(tick_database_url, forced.run_id) == (1, 20, 20)

    # Recorded: the run plan and the quality report both say which kind of run this was.
    assert _run_plan(tick_database_url, forced.run_id)["forced_fetch"] is True
    assert _run_plan(tick_database_url, control.run_id)["forced_fetch"] is False
    assert forced.forced_fetch is True and control.forced_fetch is False
    assert forced.quality["forced_fetch"] is True
    assert _report_payload(tick_database_url, forced.run_id)["forced_fetch"] is True
    assert _report_payload(tick_database_url, control.run_id)["forced_fetch"] is False

    # Identity rules unchanged: the vendor sent the same bytes, so they collapse onto the
    # raw.fetches rows already on file instead of landing a second copy.
    assert _fetch_row_count(tick_database_url) == fetch_rows_before


def test_a_forced_fetch_of_changed_bytes_serves_the_new_vintage(tick_database_url, monkeypatch) -> None:
    """The recovery half of #874: a bad capture (#622's overnight null close is the
    precedent) could not be overwritten for twelve hours because a re-run reused it. A
    forced re-run of that tick lands the vendor's corrected bytes as a new vintage and
    its snapshot serves them."""
    day = date(2026, 4, 16)  # a Thursday; nothing else in this module captures near it
    cutoff = datetime(2026, 4, 16, 22, 15, tzinfo=UTC)
    _arm(monkeypatch, quote=lambda: _quote(day, Decimal("40")), price_cutoff=day)
    first = _run_tick(tick_database_url, version="corrected-bytes", cutoff=cutoff)
    assert _served_closes(tick_database_url, first.run_id) == {"40"}

    fetch_rows_before = _fetch_row_count(tick_database_url)
    _arm(monkeypatch, quote=lambda: _quote(day, Decimal("41.5")), price_cutoff=day)
    forced = _run_tick(tick_database_url, version="corrected-bytes", cutoff=cutoff, force_fetch=True)

    assert forced.run_id != first.run_id
    assert _served_closes(tick_database_url, forced.run_id) == {"41.5"}
    assert _materialized(tick_database_url, forced.run_id) == (1, 20, 20)
    # One new raw landing per listing: 21 changed price bodies. The financial bytes did
    # not change and collapse onto their existing rows.
    assert _fetch_row_count(tick_database_url) == fetch_rows_before + 21
    # The earlier run is history, untouched.
    assert _served_closes(tick_database_url, first.run_id) == {"40"}


def test_retrying_a_forced_launch_resumes_instead_of_fetching_again(tick_database_url, monkeypatch) -> None:
    """The same forced launch retried (Dagster's run retry replays the same config) is
    the same capture: a complete one resumes (#628) with zero route builds and zero
    fetches, and the report collapses onto the row already on file."""
    cutoff = CUTOFF + timedelta(days=6)
    fetched: list[tuple[str, str, str]] = []
    _arm(monkeypatch, fetched=fetched)
    first = _run_tick(tick_database_url, version="forced-retry", cutoff=cutoff, force_fetch=True)
    assert len([entry for entry in fetched if entry[0] == first.run_id]) == OBLIGATIONS

    fetched.clear()
    routes_built: list[str] = []
    _arm(monkeypatch, spy=routes_built, fetched=fetched)
    retried = _run_tick(tick_database_url, version="forced-retry", cutoff=cutoff, force_fetch=True)

    assert retried.run_id == first.run_id
    assert retried.quality_report_id == first.quality_report_id
    assert retried.forced_fetch is True
    assert routes_built == [] and fetched == [], "a retried forced launch must not fetch again"
    # _report_payload asserts exactly one report row for the run.
    assert _report_payload(tick_database_url, first.run_id)["forced_fetch"] is True
    assert _materialized(tick_database_url, first.run_id) == (1, 20, 20)


def test_retrying_a_degraded_forced_launch_reports_the_record(tick_database_url, monkeypatch) -> None:
    """#538 holds for forced runs too: a degraded forced capture is settled history, so
    its retry is refused with the reason on file before any vendor call. A NEW forced
    launch (another executed_at) is how an operator fetches again."""
    cutoff = CUTOFF + timedelta(days=9)
    _arm(monkeypatch, sabotage=(0, ObligationReasonCode.FIELD_UNAVAILABLE))
    with pytest.raises(CaptureNotPublishableError) as first:
        _run_tick(tick_database_url, version="forced-degraded", cutoff=cutoff, force_fetch=True)

    routes_built: list[str] = []
    _arm(monkeypatch, spy=routes_built)
    with pytest.raises(CaptureNotPublishableError) as replayed:
        _run_tick(tick_database_url, version="forced-degraded", cutoff=cutoff, force_fetch=True)

    assert replayed.value.run_id == first.value.run_id
    assert replayed.value.quality_report_id == first.value.quality_report_id
    assert routes_built == [], "a settled forced run must be refused before any vendor call"
    shortfall_report = _report_payload(tick_database_url, first.value.run_id)
    assert shortfall_report["forced_fetch"] is True
    assert shortfall_report["capture_shortfall"]["success_count"] == OBLIGATIONS - 1

    fresh = _run_tick(
        tick_database_url, version="forced-degraded", cutoff=cutoff + timedelta(minutes=1), force_fetch=True
    )
    assert fresh.run_id != first.value.run_id
    assert _status_row(tick_database_url, fresh.run_id)[2] == OBLIGATIONS


def test_reuse_binds_the_whole_bound_set_or_nothing(tick_database_url, monkeypatch) -> None:
    """#885 item 4: the reuse query's comment promised to fail closed over the WHOLE
    bound set, but its trio and look-ahead predicates filtered row by row, so an
    obligation whose bound set held one disqualified observation was reused with the
    rest. Here a second origin's bar is knowable at 23:00Z — after the target's 22:40Z
    cutoff, on the same session date, so the adapter's date-level guard admits it. The
    anchor (the primary) qualifies; the set does not, so the price cells must fetch.
    The financial cells, whose sets are clean, still reuse."""
    day = date(2026, 4, 14)  # a Tuesday; nothing else in this module captures near it
    late_origin = CorroboratingOrigin(
        origin="late-origin",
        parser_version="late-origin-parser:v1",
        mapping_version="late-origin-map:v1",
        value_key="close",
        confidence=Decimal("0.80"),
        fetch=lambda symbol, cutoff: MarketPriceQuote(
            raw_bytes=f"late:{symbol}:{day.isoformat()}".encode(),
            close=Decimal("40"),
            as_of=day,
            knowable_at=datetime(2026, 4, 14, 23, 0, tzinfo=UTC),
        ),
        raw_source=DataSource.TWELVE_DATA,
    )
    _arm(
        monkeypatch,
        quote=lambda: _quote(day),
        price_cutoff=day,
        corroborating_origins=(late_origin,),
    )
    # Completes at 22:33Z (cutoff - 57 min): inside the target's window, and newer than
    # anything else in it, so it is the anchor.
    _run_tick(tick_database_url, version="bound-set-source", cutoff=datetime(2026, 4, 14, 23, 30, tzinfo=UTC))

    target_cutoff = datetime(2026, 4, 14, 22, 40, tzinfo=UTC)
    probe = psycopg.connect(tick_database_url)
    try:
        plan = composition.plan_and_persist(probe, cutoff=target_cutoff, version="bound-set-target")
        satisfied = composition._satisfy_from_recent_observations(probe, plan, cutoff=target_cutoff)
        reused = Counter(
            binding.obligation.capture_requirement_id.removesuffix(":v1")
            for work_item_id, binding in plan.bindings.items()
            if work_item_id in satisfied
        )
        assert reused["market-price"] == 0, "a bound set with a look-ahead member must not be reused in part"
        assert reused["financial-fact"] == 21
        # Nothing was bound either: a refused candidate leaves no partial evidence.
        bound = probe.execute(
            """
            select count(*)
            from raw.capture_obligations ob
            join staging.capture_observation_obligations link on link.capture_obligation_id = ob.obligation_id
            where ob.run_id = %s and ob.capture_requirement_id = 'market-price:v1'
            """,
            (plan.run_id,),
        ).fetchone()[0]
        assert bound == 0
    finally:
        probe.rollback()
        probe.close()


def test_reuse_reads_the_session_date_in_utc_whatever_the_session_time_zone(tick_database_url, monkeypatch) -> None:
    """#885 item 2, through the database: psycopg hands a timestamptz back in the
    connection's TimeZone. The price bar's knowable_at is 00:00Z, which is the previous
    evening in New York, so `.date()` on it named the wrong session and #635 reuse
    silently stopped for every price cell (a full vendor spend per tick) on any
    connection not pinned to UTC."""
    _arm(monkeypatch)
    _run_tick(tick_database_url, version="session-zone-source", cutoff=_REUSE_CUTOFF)

    probe = psycopg.connect(tick_database_url)
    try:
        probe.execute("set time zone 'America/New_York'")
        plan = composition.plan_and_persist(probe, cutoff=_REUSE_CUTOFF, version="session-zone-target")
        satisfied = composition._satisfy_from_recent_observations(probe, plan, cutoff=_REUSE_CUTOFF)
        reused = Counter(
            binding.obligation.capture_requirement_id.removesuffix(":v1")
            for work_item_id, binding in plan.bindings.items()
            if work_item_id in satisfied
        )
        assert reused["market-price"] == 21
    finally:
        probe.rollback()
        probe.close()


def test_a_forced_capture_version_is_distinct_and_stable() -> None:
    """No database: the identity half of #874. The marker makes a forced launch its own
    capture, and applying it twice changes nothing, so a retry names the same run."""
    version = composition.live_version_for(_REUSE_CUTOFF)
    forced = composition.forced_capture_version(version)
    assert forced != version and forced.startswith(version)
    assert composition.forced_capture_version(forced) == forced


@pytest.mark.parametrize("zone", ["UTC", "America/New_York", "Asia/Singapore", "America/Los_Angeles"])
def test_the_session_check_is_utc_in_any_zone(zone: str) -> None:
    """No database: #885 item 2 at the unit level. The same instant, whatever zone it
    arrives in, names the same settled session."""
    knowable_at = datetime(2026, 3, 31, tzinfo=UTC).astimezone(ZoneInfo(zone))
    assert composition._is_settled_session(knowable_at, date(2026, 3, 31))
    assert not composition._is_settled_session(knowable_at, date(2026, 3, 30))


def test_reuse_prefers_the_forced_capture_of_the_same_tick(tick_database_url, monkeypatch) -> None:
    """#874's recovery story does not end at the forced run itself. A forced re-run of
    the scheduled tick's own executed_at completes at the same recorded instant as the
    run it corrects, so the reuse anchor order tied, and the next tick inside the window
    (QQQ at 23:20, the canary at 23:47) picked either capture by observation id. At
    that tie the forced capture is the newer look at the vendor, and it wins."""
    day = date(2026, 4, 21)  # a Tuesday; nothing else in this module captures near it
    cutoff = datetime(2026, 4, 21, 22, 15, tzinfo=UTC)
    _arm(monkeypatch, quote=lambda: _quote(day, Decimal("40")), price_cutoff=day)
    _run_tick(tick_database_url, version="anchor-choice", cutoff=cutoff)
    _arm(monkeypatch, quote=lambda: _quote(day, Decimal("39.25")), price_cutoff=day)
    _run_tick(tick_database_url, version="anchor-choice", cutoff=cutoff, force_fetch=True)

    follower_cutoff = cutoff + timedelta(minutes=5)
    probe = psycopg.connect(tick_database_url)
    try:
        plan = composition.plan_and_persist(probe, cutoff=follower_cutoff, version="anchor-choice-follower")
        satisfied = composition._satisfy_from_recent_observations(probe, plan, cutoff=follower_cutoff)
        price_cells = [
            work_item_id
            for work_item_id, binding in plan.bindings.items()
            if binding.obligation.capture_requirement_id == "market-price:v1"
        ]
        assert price_cells and all(work_item_id in satisfied for work_item_id in price_cells)
        closes = probe.execute(
            """
            select distinct p.normalized_payload->>'close'
            from raw.capture_obligations ob
            join staging.capture_observation_obligations link on link.capture_obligation_id = ob.obligation_id
            join staging.capture_observation_payloads p on p.observation_id = link.observation_id
            where ob.run_id = %s and ob.capture_requirement_id = 'market-price:v1'
            """,
            (plan.run_id,),
        ).fetchall()
        assert {row[0] for row in closes} == {"39.25"}, "every price cell must reuse the forced capture"
    finally:
        probe.rollback()
        probe.close()


# -- run scope: a cutoff is not a run (#877 PR-1) ---------------------------------------
#
# Since #874 a forced tick captures again at the SAME executed_at as the scheduled tick,
# so one cutoff carries two TOPT capture runs, two sets of core results and two strategy
# runs. Every reader that paired rows on (issuer_id, cutoff) — or picked "the" strategy run
# by cutoff — then read both. These tests drive the deployed TOPT op end to end and read
# back through the shipped readers; the statements main carried before #877 are kept
# verbatim below so each test shows what they returned on the same rows.

#: `mart.governed_strategy_run` as 20260907T0630 defined it (the head's cutoff is the link).
_MAIN_GOVERNED_STRATEGY_RUN_SQL = """
    with head as (
        select environment, universe_id, universe_version, factor_id, target_run_id, sequence, advanced_at
        from mart.current_pointer_head
        where environment = 'production'
          and factor_id = 'gross_profit_per_employee'
          and universe_id like 'universe:topt-%'
        order by advanced_at desc
        limit 1
    )
    select head.target_run_id, run.strategy_run_id
    from head
    join mart.topt_capture_status status on status.run_id = head.target_run_id
    join mart.strategy_runs run on run.executed_at = status.cutoff
"""
#: Both twins' LATEST_RUN_SQL over the view above (its `like` escaped for a bound parameter).
_MAIN_LATEST_RUN_SQL = f"""
    select r.strategy_run_id,
           exists (select 1 from ({_MAIN_GOVERNED_STRATEGY_RUN_SQL.replace("%", "%%")}) g
                   where g.strategy_run_id = r.strategy_run_id) as is_governed
    from mart.strategy_runs r
    where r.strategy_key = %s
    order by is_governed desc, r.executed_at desc, r.created_at desc, r.strategy_run_id desc
    limit 1
"""
#: Both twins' decision read (strategy_run_postgres._DECISIONS_SQL, strategy-run-repository.ts).
_MAIN_DECISIONS_SQL = """
    select d.issuer_id, t.confidence, t.run_id
    from mart.strategy_decisions d
    left join mart.topt_core_results t
      on t.issuer_id = d.issuer_id and t.cutoff = d.cutoff_at
    where d.strategy_run_id = %s
    order by d.cutoff_at, d.issuer_id
"""
#: plausibility_gate._ROWS_SQL.
_MAIN_GATE_ROWS_SQL = """
    select r.listing_id, p.value as last_close
    from mart.topt_core_results r
    left join staging.strategy_backtest_inputs p
      on p.issuer_id = r.issuer_id and p.cutoff_at = r.cutoff and p.input_key = 'last_close'
    where r.run_id = %s
    order by r.listing_id
"""
#: question_coverage.peg_cells' choice of strategy run, given the head's cutoff.
_MAIN_PEG_RUN_SQL = """
    select s.strategy_run_id
    from mart.strategy_runs s
    join mart.strategy_decisions d on d.strategy_run_id = s.strategy_run_id
    where d.cutoff_at <= %s
    group by s.strategy_run_id, s.executed_at
    order by max(d.cutoff_at) desc, s.executed_at desc
"""
_STRATEGY = "large_model_value_v0"


def _live_topt_tick(url: str, monkeypatch, *, executed_at: datetime, force_fetch: bool = False, accept: bool) -> dict:
    """The deployed TOPT op (`lanes/capture.py`) on this module's database: capture,
    freeze, materialize, strategy seed/replay/binding, plausibility gate, pointer.

    The only verdict the test chooses is the service-objective one (`accept`): the offline
    fixtures carry no second origin, so the real grade would withhold every advance, and
    the forced-run question is precisely what happens on each side of that verdict."""
    import dagster as dg
    from data_engine.datahub import a1_evidence
    from data_engine.lanes.capture import ToptLiveTickConfig, run_topt_live_tick

    monkeypatch.setattr(settings, "database_url", url)
    unmet = (
        () if accept else (a1_evidence.UnmetObjective(objective="corroborated_share", required="0.95", observed="0"),)
    )
    monkeypatch.setattr(a1_evidence, "unmet_objectives", lambda *_args, **_kwargs: unmet)
    context = dg.build_op_context()
    run_topt_live_tick(context, ToptLiveTickConfig(executed_at=executed_at.isoformat(), force_fetch=force_fetch))
    metadata = context.get_output_metadata("result")
    assert metadata["pointer_advanced"] is accept
    assert metadata["forced_fetch"] is force_fetch
    return metadata


def _context():
    from truealpha_contracts.access import AccessContext, AuthenticationMethod, PrincipalKind

    issued = datetime(2026, 9, 16, tzinfo=UTC)
    return AccessContext(
        context_id="ctx:run-scope",
        principal_id="principal:run-scope",
        tenant_id="tenant:run-scope",
        session_id="session:run-scope",
        authentication_method=AuthenticationMethod.SERVICE_IDENTITY,
        principal_kind=PrincipalKind.SERVICE,
        issued_at=issued,
        expires_at=issued + timedelta(hours=1),
    )


def _served_report(url: str):
    from truealpha_contracts.strategy_run import StrategyRunReport
    from truealpha_contracts.strategy_run_postgres import PostgresStrategyRunRepository

    report = PostgresStrategyRunRepository(database_url=url).get_latest(strategy_id=_STRATEGY, context=_context())
    assert isinstance(report, StrategyRunReport), report
    return report


def _core_confidence(reader, run_id: str) -> dict[str, Decimal]:
    return dict(
        reader.execute(
            "select issuer_id, confidence from mart.topt_core_results where run_id = %s", (run_id,)
        ).fetchall()
    )


def _gate_closes(reader, sql: str, run_id: str) -> list[tuple[str, Decimal | None]]:
    return [(str(listing), close) for listing, close in reader.execute(sql, (run_id,)).fetchall()]


def test_a_forced_tick_that_advances_the_head_is_read_once_through_its_own_run(tick_database_url, monkeypatch) -> None:
    """#877's live defect, confirmed: a forced TOPT tick (#892) shares the scheduled tick's
    cutoff. With the forced run on the head, main's twins returned all 20 decisions twice
    (once per capture run's core result), main's view marked both strategy runs governed,
    main's gate read two `last_close` vintages per listing, and main's PEG choice tied.
    Scoped by run, each reader sees the forced run alone."""
    from data_engine.datahub.production_topt import plausibility_gate
    from data_engine.datahub.question_coverage import peg_cells
    from truealpha_contracts.strategy_run_postgres import LATEST_RUN_SQL

    day = date(2026, 5, 5)  # a Tuesday; nothing else in this module captures near it
    cutoff = datetime(2026, 5, 5, 22, 15, tzinfo=UTC)
    _arm(monkeypatch, quote=lambda: _quote(day, Decimal("40")), price_cutoff=day)
    scheduled = _live_topt_tick(tick_database_url, monkeypatch, executed_at=cutoff, accept=True)
    # The operator's correction of the same tick: the vendor's close moved.
    _arm(monkeypatch, quote=lambda: _quote(day, Decimal("41.5")), price_cutoff=day)
    forced = _live_topt_tick(tick_database_url, monkeypatch, executed_at=cutoff, force_fetch=True, accept=True)

    with psycopg.connect(tick_database_url) as reader:
        # The shape: one cutoff, two TOPT capture runs, two strategy runs, the forced one heads.
        assert scheduled["capture_run_id"] != forced["capture_run_id"]
        assert scheduled["strategy_run_id"] != forced["strategy_run_id"]
        assert forced["pointer_sequence"] == scheduled["pointer_sequence"] + 1
        core_runs = reader.execute(
            "select count(distinct run_id) from mart.topt_core_results where cutoff = %s", (cutoff,)
        ).fetchone()[0]
        assert core_runs == 2

        # BEFORE (main's statements on these rows).
        main_governed = reader.execute(_MAIN_GOVERNED_STRATEGY_RUN_SQL).fetchall()
        assert sorted(run for _head, run in main_governed) == sorted(
            [scheduled["strategy_run_id"], forced["strategy_run_id"]]
        ), "main marked BOTH strategy runs at the head's cutoff governed"
        main_decisions = reader.execute(_MAIN_DECISIONS_SQL, (forced["strategy_run_id"],)).fetchall()
        assert len(main_decisions) == 40 and len({issuer for issuer, *_ in main_decisions}) == 20, (
            "main returned every one of the 20 decisions twice"
        )
        main_gate = _gate_closes(reader, _MAIN_GATE_ROWS_SQL, forced["capture_run_id"])
        # One core result per issuer (GOOG carries Alphabet), each read against both vintages.
        assert len(main_gate) == 40 and {close for _listing, close in main_gate} == {Decimal("40"), Decimal("41.5")}
        main_peg_runs = [row[0] for row in reader.execute(_MAIN_PEG_RUN_SQL, (cutoff,)).fetchall()[:2]]
        assert set(main_peg_runs) == {scheduled["strategy_run_id"], forced["strategy_run_id"]}, (
            "main's PEG choice tied between the two runs at the head's cutoff"
        )

        # AFTER: the view resolves the head's own strategy run, and only it.
        governed = reader.execute("select target_run_id, strategy_run_id from mart.governed_strategy_run").fetchall()
        assert governed == [(forced["capture_run_id"], forced["strategy_run_id"])]
        latest = reader.execute(LATEST_RUN_SQL, (_STRATEGY,)).fetchone()
        assert (latest[0], latest[3]) == (forced["strategy_run_id"], True)

        # The gate judges each run by its own price, one row per listing.
        forced_gate = plausibility_gate._rows(reader, forced["capture_run_id"])
        scheduled_gate = plausibility_gate._rows(reader, scheduled["capture_run_id"])
        assert len(forced_gate) == 20 and {row.last_close for row in forced_gate} == {Decimal("41.5")}
        assert len(scheduled_gate) == 20 and {row.last_close for row in scheduled_gate} == {Decimal("40")}

        # PEG coverage reads the head's strategy run.
        peg = peg_cells(reader, run_id=forced["capture_run_id"])
        assert len(peg) == 20
        forced_confidence = _core_confidence(reader, forced["capture_run_id"])

    # The twin MCP ships: 20 decisions, each read against the forced run's core result.
    report = _served_report(tick_database_url)
    assert len(report.decisions) == 20
    assert {decision.issuer_id for decision in report.decisions} == set(forced_confidence)
    assert all(decision.confidence == forced_confidence[decision.issuer_id] for decision in report.decisions)


def test_a_withheld_forced_tick_never_displaces_the_governed_strategy_run(tick_database_url, monkeypatch) -> None:
    """The other side of the verdict: a forced tick whose pointer advance is withheld
    commits its capture, core results and strategy run, and the head stays on the
    scheduled run. Main's view still marked the forced strategy run governed (same
    cutoff), and both twins then served it because it was created last — a run the
    pointer refused. The binding keeps the served run on the head."""
    from data_engine.datahub.question_coverage import peg_cells
    from truealpha_contracts.strategy_run_postgres import LATEST_RUN_SQL

    day = date(2026, 5, 7)  # a Thursday; nothing else in this module captures near it
    cutoff = datetime(2026, 5, 7, 22, 15, tzinfo=UTC)
    _arm(monkeypatch, quote=lambda: _quote(day, Decimal("40")), price_cutoff=day)
    scheduled = _live_topt_tick(tick_database_url, monkeypatch, executed_at=cutoff, accept=True)
    _arm(monkeypatch, quote=lambda: _quote(day, Decimal("38")), price_cutoff=day)
    withheld = _live_topt_tick(tick_database_url, monkeypatch, executed_at=cutoff, force_fetch=True, accept=False)
    assert withheld["pointer_sequence"] == scheduled["pointer_sequence"], "the head did not move"
    assert withheld["strategy_run_id"] != scheduled["strategy_run_id"]

    with psycopg.connect(tick_database_url) as reader:
        head = reader.execute(
            "select target_run_id from mart.current_pointer_head "
            "where universe_id like 'universe:topt-%' order by advanced_at desc limit 1"
        ).fetchone()[0]
        assert head == scheduled["capture_run_id"]

        # BEFORE: main served the withheld run's strategy run.
        main_latest = reader.execute(_MAIN_LATEST_RUN_SQL, (_STRATEGY,)).fetchone()
        assert main_latest == (withheld["strategy_run_id"], True), main_latest

        # AFTER: the head's own strategy run, read against the head's core results.
        latest = reader.execute(LATEST_RUN_SQL, (_STRATEGY,)).fetchone()
        assert (latest[0], latest[3]) == (scheduled["strategy_run_id"], True)
        assert reader.execute("select strategy_run_id from mart.governed_strategy_run").fetchall() == [
            (scheduled["strategy_run_id"],)
        ]
        peg_run = {
            row[0]
            for row in reader.execute(
                "select distinct strategy_run_id from mart.strategy_decisions where issuer_id = any(%s)",
                ([cell.subject_id for cell in peg_cells(reader, run_id=head)],),
            ).fetchall()
        }
        assert scheduled["strategy_run_id"] in peg_run
        scheduled_confidence = _core_confidence(reader, scheduled["capture_run_id"])

    report = _served_report(tick_database_url)
    assert len(report.decisions) == 20
    assert all(decision.confidence == scheduled_confidence[decision.issuer_id] for decision in report.decisions)


def _key_topt_like_the_planes(monkeypatch) -> dict[str, tuple[str, str]]:
    """#877 PR-4's world, ahead of PR-4: TOPT's plan keys every listing it shares with the
    QQQ corpus by the QQQ corpus's (issuer, instrument) — CIK/FIGI — instead of its own
    LEI/CUSIP. Returns listing -> the shared (issuer, instrument)."""
    from data_engine.datahub.production_topt.universe_corpus import load_corpus

    plane = {
        str(row[2]): (str(row[0]), str(row[1]))
        for row in load_corpus("corpus.qqq.v1.json")["topt_denominator"]["instruments"]
    }
    real = composition.plan_and_persist

    def plan_and_persist(connection, **kwargs):
        planned = real(connection, **kwargs)
        if kwargs.get("corpus_filename", "corpus.v1.json") != "corpus.v1.json":
            return planned
        return dataclasses.replace(
            planned,
            coordinates={
                subject: (*plane.get(subject, (issuer, instrument)), listing, ticker)
                for subject, (issuer, instrument, listing, ticker) in planned.coordinates.items()
            },
        )

    monkeypatch.setattr(composition, "plan_and_persist", plan_and_persist)
    topt = {str(row[2]) for row in load_corpus("corpus.v1.json")["topt_denominator"]["instruments"]}
    return {listing: ids for listing, ids in plane.items() if listing in topt}


def test_another_universe_at_the_same_cutoff_is_neither_reused_nor_joined(tick_database_url, monkeypatch) -> None:
    """#877 H1 and H3 together, in the world where TOPT and QQQ key an issuer alike.

    H1: a QQQ tick froze its observations for partition 2026-06-30 (their `valid_from`).
    A TOPT tick inside its window matches their trio, and main reused them — resolving the
    cells UNCHANGED and then failing to freeze them, because TOPT freezes 2026-03-31. Reuse
    now requires the anchor to be valid for the obligation's partition, so TOPT fetches.

    H3: both runs then hold core results for the same issuers at the same cutoff. Main's
    twins returned each shared issuer's decision twice, and main's gate gave the QQQ run
    TOPT's seeded price. Scoped by run, neither happens."""
    from data_engine.datahub.production_topt import plausibility_gate

    day = date(2026, 7, 14)  # a Tuesday after the QQQ partition; nothing else captures near it
    cutoff = datetime(2026, 7, 14, 22, 15, tzinfo=UTC)
    shared = _key_topt_like_the_planes(monkeypatch)
    assert len(shared) == 13, "TOPT and QQQ share 13 listings"
    aapl_issuer = shared["listing:xnas:aapl"][0]

    # A QQQ run launched with the same executed_at (a manual launch of both universes, or
    # any universe a tick shares its cutoff with): its captures complete 59 minutes before
    # the cutoff, so they sit inside the TOPT tick's reuse window.
    _arm(monkeypatch, quote=lambda: _quote(day, Decimal("50")), price_cutoff=day, cutoff_date=day)
    with psycopg.connect(tick_database_url) as tick:
        qqq = run_topt_pipeline(
            tick,
            cutoff=cutoff,
            version=composition.live_version_for(cutoff),
            corpus_filename="corpus.qqq.v1.json",
            label_prefix="production-qqq",
        )
        tick.commit()

    # H1, at the reuse step itself.
    probe = psycopg.connect(tick_database_url)
    try:
        plan = composition.plan_and_persist(probe, cutoff=cutoff, version="run-scope-h1-probe")
        assert all(plan.coordinates[listing][:2] == ids for listing, ids in shared.items())
        satisfied = composition._satisfy_from_recent_observations(probe, plan, cutoff=cutoff)
        reused = [
            binding.obligation.subject.id
            for work_item_id, binding in plan.bindings.items()
            if work_item_id in satisfied
        ]
        assert reused == [], f"observations frozen for a later partition must not be reused: {sorted(set(reused))}"
    finally:
        probe.rollback()
        probe.close()

    # H1 end to end, through the deployed op: the TOPT tick fetches, freezes, publishes.
    _arm(monkeypatch, quote=lambda: _quote(day, Decimal("40")), price_cutoff=day, cutoff_date=day)
    topt = _live_topt_tick(tick_database_url, monkeypatch, executed_at=cutoff, accept=True)
    assert _status_row(tick_database_url, topt["capture_run_id"])[:4] == (OBLIGATIONS, OBLIGATIONS, OBLIGATIONS, 0)
    assert _materialized(tick_database_url, topt["capture_run_id"]) == (1, 20, 20)

    with psycopg.connect(tick_database_url) as reader:
        # The shape: the same issuer, one core result per universe.
        per_run = reader.execute(
            "select run_id from mart.topt_core_results where issuer_id = %s and cutoff = %s",
            (aapl_issuer, cutoff),
        ).fetchall()
        assert sorted(row[0] for row in per_run) == sorted([qqq.run_id, topt["capture_run_id"]])

        # BEFORE: main's join duplicated each shared issuer's decision (12 issuers: GOOG
        # and GOOGL are one), and handed the QQQ run TOPT's seeded price.
        main_decisions = reader.execute(_MAIN_DECISIONS_SQL, (topt["strategy_run_id"],)).fetchall()
        shared_issuers = {issuer for issuer, _instrument in shared.values()}
        assert len(shared_issuers) == 12
        assert len(main_decisions) == 20 + len(shared_issuers)
        main_qqq_gate = dict(_gate_closes(reader, _MAIN_GATE_ROWS_SQL, qqq.run_id))
        assert main_qqq_gate["listing:xnas:aapl"] == Decimal("40"), "main read TOPT's price into the QQQ run"

        # AFTER: each run's rows carry each run's own price.
        qqq_gate = {row.listing_id: row.last_close for row in plausibility_gate._rows(reader, qqq.run_id)}
        topt_gate = {row.listing_id: row.last_close for row in plausibility_gate._rows(reader, topt["capture_run_id"])}
        assert qqq_gate["listing:xnas:aapl"] == Decimal("50") and set(qqq_gate.values()) == {Decimal("50")}
        assert topt_gate["listing:xnas:aapl"] == Decimal("40") and len(topt_gate) == 20
        topt_confidence = _core_confidence(reader, topt["capture_run_id"])

    report = _served_report(tick_database_url)
    assert len(report.decisions) == 20
    assert len({decision.issuer_id for decision in report.decisions}) == 20
    assert aapl_issuer in {decision.issuer_id for decision in report.decisions}
    assert all(decision.confidence == topt_confidence[decision.issuer_id] for decision in report.decisions)
