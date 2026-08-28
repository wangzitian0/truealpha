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
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import subprocess
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

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
from truealpha_contracts import ObligationReasonCode, RawCapture, RawIngestionEnvelope, RawObjectRef
from truealpha_contracts.datahub import CaptureWorkItem

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


def _quote() -> MarketPriceQuote:
    day = date(2026, 3, 31)
    return MarketPriceQuote(
        raw_bytes=b"bar:2026-03-31:40",
        close=Decimal("40"),
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


def _offline_routes(plan: PlannedRun, connection) -> dict[str, SourceFetchPort]:
    """The deployed adapters over fake fetchers, routed exactly as `build_routes` does.

    Stands in for `build_routes`, which resolves CIKs from SEC over the network. Every
    layer under it — executor, sink, freeze, materialize, quality report — is the real
    one, because the gate this exercises reads what those layers persisted.
    """
    cutoff_date = CUTOFF.date()
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
                cutoff=cutoff_date,
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

    price = MarketPriceAdapter(price_targets, lambda symbol, cutoff: _quote())
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
) -> None:
    """Route the tick through offline adapters, optionally failing one obligation.

    `spy` records each entry into route building. In production that step resolves CIKs
    from SEC over the network, so an empty spy is the evidence that a tick was refused
    before it reached a vendor at all.
    """
    monkeypatch.setattr(raw_store, "object_store", _InMemoryObjectStore)

    def build(plan: PlannedRun, connection=None) -> dict[str, SourceFetchPort]:
        if spy is not None:
            spy.append(plan.run_id)
        routes = _offline_routes(plan, connection)
        if sabotage is not None:
            index, reason = sabotage
            routes[plan.work_items[index].work_item_id] = _FailingPort(reason)
        return routes

    monkeypatch.setattr(composition, "build_routes", build)


def _run_tick(url: str, *, version: str, cutoff: datetime = CUTOFF):
    """One tick, shaped exactly like `dagster_defs.run_topt_live_tick`: a single
    connection, a single transaction, rolled back by the context manager when the tick
    raises. Anything that survives this survived the abort.

    Tests in this module share one database, and #635's cross-run reuse deliberately
    satisfies obligations from any run committed within twelve hours of the cutoff —
    so each test that must NOT see its predecessors' observations runs at its own
    cutoff, more than the reuse window apart. The reuse test itself runs two ticks
    at ONE cutoff, which is the feature."""
    with psycopg.connect(url) as tick:
        result = run_topt_pipeline(tick, cutoff=cutoff, version=version)
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
