"""The executor path populates the capture-control tables the mart reads (#171 A1).

The load-bearing property this milestone turns on: driving the planned obligations
through the generic executor must leave `raw.capture_*` /
`staging.capture_normalized_observations` populated well enough that
`freeze_snapshot` → `materialize` reconstructs the 20-issuer TOPT core — exactly what
the retired monolith did, but with persistence injected instead of inlined.

Real schema, no network: the adapters are the deployed ones, wired to fake fetchers.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub import quality_report
from data_engine.datahub.evidence_graph_repository import PostgresEvidenceGraphRepository
from data_engine.datahub.production_topt import PostgresToptCoreRepository
from data_engine.datahub.production_topt.capture_orchestration import run_topt_capture
from data_engine.datahub.production_topt.composition import PlannedRun, plan_and_persist
from data_engine.datahub.production_topt.executor import SourceFetchPort
from data_engine.datahub.production_topt.headcount import PostgresHeadcountExtractor, record_headcount
from data_engine.datahub.production_topt.market_price_adapter import (
    CorroboratingOrigin,
    MarketPriceAdapter,
    MarketPriceQuote,
    MarketPriceTarget,
    SourceUnavailableError,
)
from data_engine.datahub.production_topt.persistence import PostgresCaptureControlSink
from data_engine.datahub.production_topt.release_derived_adapter import (
    ReleaseDerivedAdapter,
    ReleaseDerivedRecord,
)
from data_engine.datahub.production_topt.sec_financial_adapter import (
    FinancialFactAssertion,
    FinancialFactCorroboratingOrigin,
    FinancialFactsBundle,
    SecFinancialFactAdapter,
    SecTarget,
)
from data_engine.datahub.production_topt.source_registrations import (
    MOOMOO_FINANCIALS_PARSER_VERSION,
    MOOMOO_KLINE_PARSER_VERSION,
)
from factors.production_topt import GppeV0Definition, OperatingBranch
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.datahub import ObligationTerminalState
from truealpha_contracts.models import DataSource, RawCapture, RawIngestionEnvelope, RawObjectRef
from truealpha_contracts.obligation_reason_codes import ObligationReasonCode

CUTOFF = datetime(2026, 4, 2, tzinfo=UTC)
# One TOPT issuer is a depository institution, one an insurer (SEC SIC 6021 / 63xx);
# the rest are non-financial. All three OperatingBranch members must flow through the
# harness: the 2026-08-14 staging tick crashed on the INSURANCE branch precisely
# because this fixture only ever emitted the other two (#534, quality_report KeyError).
_BANK_TICKER = "JPM"
_INSURANCE_TICKER = "BRK.B"


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


def _quote(close: str, day: date = date(2026, 3, 31)) -> MarketPriceQuote:
    price = Decimal(close)
    return MarketPriceQuote(
        raw_bytes=f"bar:{day}:{close}".encode(),
        close=price,
        as_of=day,
        knowable_at=datetime.combine(day, datetime.min.time(), tzinfo=UTC),
        # The rest of the bar, derived from the close so the fixture carries no second
        # literal that has to stay consistent with it: open at the close, high/low a
        # dollar either side, one fixed share count.
        open=price,
        high=price + Decimal("1"),
        low=price - Decimal("1"),
        volume=Decimal("1000000"),
    )


@dataclass(frozen=True)
class _OneBrokenCell:
    """One deliberately broken cell, injected through the deployed write path (#537).

    The falsifiability harness needs damage that is real, singular, and invisible to
    everything except the metric under test: the run still terminally succeeds on all 84
    obligations and writes all 84 rows, so a metric that moves moved because it read the
    payload or the stored bytes rather than because the capture failed. A metric that
    stays at 1.0000 under its own failure mode is not measuring anything.

    Whichever listing sorts first is the victim, so a test never has to name one.
    """

    # The financial-fact payload lands with a null operating numerator: the row exists,
    # the obligation succeeds, and nothing downstream can score the issuer. This is the
    # Staging 2026-07-30 13:01 shape — 84/84 availability over an empty portfolio.
    financial_fact_numerator: bool = False
    # The listing-identity payload lands without its required `ticker`: bytes are stored
    # and the row is written, but the payload does not satisfy its own semantic contract.
    identity_payload: bool = False


@dataclass(frozen=True)
class _PrimaryOutage:
    """#862: the primary price vendor cannot serve ONE listing (the first by listing id);
    the registered origins are asked in fusion-policy order. Each flag says whether that
    origin holds the victim's settled close; the other listings are untouched."""

    twelve_data: bool = True
    moomoo: bool = True


# The failover origins' close for the victim, distinct from the primary's 40 so a test can
# tell which vendor's number the mart serves, and inside the 0.3% price tolerance so the
# two failover origins agree with each other.
_FAILOVER_CLOSE = "40.02"


def _victim(plan: PlannedRun) -> tuple[str, str]:
    """(listing_id, ticker) of the listing `_PrimaryOutage` and `_OneBrokenCell` target."""
    subject_id = min(plan.coordinates.keys())
    ticker = plan.coordinates[subject_id][3]
    return subject_id, ticker


def _bundle(branch: OperatingBranch, *, blank_numerator: bool = False) -> FinancialFactsBundle:
    financial = branch is OperatingBranch.FINANCIAL
    return FinancialFactsBundle(
        gross_profit=None if blank_numerator else (Decimal("80000000") if financial else Decimal("210000000")),
        total_assets=Decimal("200000000"),
        shares_outstanding=Decimal("10000000"),
        revenue=Decimal("100000000"),
        pre_provision_profit=None if blank_numerator or not financial else Decimal("80000000"),
        raw_bytes=b'{"facts":{}}' if not blank_numerator else b'{"facts":{"empty":true}}',
        knowable_at=datetime(2026, 2, 1, tzinfo=UTC),
        operating_period_end=date(2025, 12, 31),
        revenue_period_end=date(2025, 12, 31),
        shares_period_end=date(2026, 3, 15),
    )


def _moomoo_statements(ticker: str) -> FinancialFactAssertion:
    """What the moomoo statements origin asserts for the fixture issuers: the same annual
    figures the SEC bundle carries, at the same period end, so the fusion agrees on every
    dated field (the bank's gross profit follows its branch, as `_bundle` does)."""
    values: dict[str, Decimal | None] = {
        "revenue": Decimal("100000000"),
        "net_income": None,
        "gross_profit": Decimal("80000000") if ticker == _BANK_TICKER else Decimal("210000000"),
        "total_assets": Decimal("200000000"),
        "eps_basic": None,
        "eps_diluted": None,
    }
    period_end = date(2025, 12, 31)
    return FinancialFactAssertion(
        raw_bytes=f'{{"income":{{"ticker":"{ticker}"}},"balance_sheet":{{}}}}'.encode(),
        period_end=period_end,
        values=values,
        by_period_end={period_end: values},
        knowable_at=datetime(2026, 2, 1, tzinfo=UTC),
    )


def _routes(
    plan: PlannedRun,
    *,
    corroborate: bool,
    headcount_extractor=None,
    broken: _OneBrokenCell = _OneBrokenCell(),
    outage: _PrimaryOutage | None = None,
) -> dict[str, SourceFetchPort]:
    """The deployed adapters over fake fetchers, routed exactly as the composition root does."""
    # The settled session (#530 item 1): production's build_route uses
    # context.price_cutoff_date here, "the last SETTLED session, not the calendar date"
    # (market_price_adapter.py) -- CUTOFF is when the tick RUNS, not the session it
    # captures for. They used to be interchangeable because valid_from ignored both;
    # now that valid_from is the fact's own date, a fake quote dated CUTOFF (one day
    # after the obligation's actual partition, plan.timeline.partition_start) would
    # correctly be graded ineligible for this run's partition -- test the same
    # settled-session semantics production uses instead of the run's own clock.
    cutoff_date = plan.timeline.partition_start.date()
    price_targets: dict[str, MarketPriceTarget] = {}
    sec_targets: dict[str, SecTarget] = {}
    release_targets: dict[str, ReleaseDerivedRecord] = {}
    cik_by_ticker: dict[str, int] = {}
    victim_subject_id = min(plan.coordinates.keys())
    blank_ciks: set[int] = set()
    for work_item_id, binding in plan.bindings.items():
        semantic_type = binding.obligation.capture_requirement_id.removesuffix(":v1")
        subject_id = binding.obligation.subject.id
        issuer_id, instrument_id, listing_id, ticker = plan.coordinates[subject_id]
        cik = 100000 + sorted(plan.coordinates).index(subject_id)
        cik_by_ticker.setdefault(ticker, cik)
        if broken.financial_fact_numerator and subject_id == victim_subject_id:
            blank_ciks.add(cik_by_ticker[ticker])
        if semantic_type == "market-price":
            price_targets[work_item_id] = MarketPriceTarget(
                symbol=ticker,
                cutoff=cutoff_date,
                issuer_id=issuer_id,
                instrument_id=instrument_id,
                listing_id=listing_id,
                run_cutoff=CUTOFF,
            )
        elif semantic_type == "financial-fact":
            sec_targets[work_item_id] = SecTarget(
                cik=cik_by_ticker[ticker],
                cutoff=cutoff_date,
                issuer_id=issuer_id,
                instrument_id=instrument_id,
                listing_id=listing_id,
                # The deployed `build_route` carries the ticker so a symbol-keyed second
                # origin (moomoo) can be asked; a target without it is honestly single-origin.
                ticker=ticker,
                operating_branch=(
                    OperatingBranch.FINANCIAL
                    if ticker == _BANK_TICKER
                    else OperatingBranch.INSURANCE
                    if ticker == _INSURANCE_TICKER
                    else OperatingBranch.NON_FINANCIAL
                ),
            )
        else:
            payload = {
                "issuer_id": issuer_id,
                "instrument_id": instrument_id,
                "listing_id": listing_id,
                "ticker": ticker,
            }
            if broken.identity_payload and semantic_type == "listing-identity" and subject_id == victim_subject_id:
                payload.pop("ticker")
            release_targets[work_item_id] = ReleaseDerivedRecord(
                semantic_type=semantic_type,
                subject_id=listing_id,
                payload=payload,
                # #530 item 2: a governed universe's rows are knowable when its head was
                # published; the hand-curated TOPT corpus these fixtures plan against has
                # no publication event, so `plan.universe_published_at` is None here and
                # this falls back to the partition start exactly as `build_route` does.
                knowable_at=plan.universe_published_at or plan.timeline.partition_start,
            )

    victim_ticker = _victim(plan)[1]

    def origin_fetch(holds_victim: bool):
        def fetch(symbol: str, cutoff: date) -> MarketPriceQuote | None:
            if outage is None or symbol != victim_ticker:
                return _quote("40")
            # A failover serves only the target's own settled session: the one asked for.
            return _quote(_FAILOVER_CLOSE, day=cutoff) if holds_victim else None

        return fetch

    def primary_fetch(symbol: str, cutoff: date) -> MarketPriceQuote:
        if outage is not None and symbol == victim_ticker:
            raise SourceUnavailableError("chart endpoint answered 502")
        return _quote("40")

    second_origin = (
        CorroboratingOrigin(
            origin="twelve-data",
            parser_version="twelve-data-parser:v1",
            mapping_version="twelve-data-map:v1",
            value_key="price",
            confidence=Decimal("0.85"),
            fetch=lambda symbol, cutoff: _quote("40"),
        )
        if outage is None
        # The current vintage writes the close under `close`, which is what lets it
        # serve a cell (v1's `price` cannot satisfy the mart's payload contract).
        else CorroboratingOrigin(
            origin="twelve-data",
            parser_version="twelve-data-parser:v3",
            mapping_version="twelve-data-map:v3",
            value_key="close",
            confidence=Decimal("0.85"),
            fetch=origin_fetch(outage.twelve_data),
        )
    )
    # The moomoo K-line third origin and the moomoo statements second origin, over the
    # same fake vendor answers, so a corroborated run persists three price assertions
    # and two financial-fact assertions per cell through the deployed sink.
    third_origin = CorroboratingOrigin(
        origin="moomoo-kline",
        parser_version=MOOMOO_KLINE_PARSER_VERSION,
        mapping_version="moomoo-kline-map:v1",
        value_key="close",
        confidence=Decimal("0.80"),
        fetch=origin_fetch(outage.moomoo if outage is not None else True),
        raw_source=DataSource.MOOMOO,
    )
    financial_second_origin = FinancialFactCorroboratingOrigin(
        origin="moomoo-financials",
        parser_version=MOOMOO_FINANCIALS_PARSER_VERSION,
        mapping_version="moomoo-financials-map:v1",
        confidence=Decimal("0.75"),
        fetch=lambda ticker, cutoff: _moomoo_statements(ticker),
    )
    price = MarketPriceAdapter(
        price_targets,
        primary_fetch,
        corroborating_origins=(second_origin, third_origin) if corroborate else (),
    )
    financial = SecFinancialFactAdapter(
        sec_targets,
        lambda cik, cutoff, branch: _bundle(branch, blank_numerator=cik in blank_ciks),
        headcount_extractor=headcount_extractor,
        corroborating_origins=(financial_second_origin,) if corroborate else (),
    )
    release = ReleaseDerivedAdapter(release_targets, cutoff=cutoff_date)

    routes: dict[str, SourceFetchPort] = {}
    routes.update(dict.fromkeys(price_targets, price))
    routes.update(dict.fromkeys(sec_targets, financial))
    routes.update(dict.fromkeys(release_targets, release))
    return routes


class _InMemoryObjectStore:
    """A `RawObjectStore` that keeps bytes in a dict.

    The suite must exercise the real landing path — sink -> raw_store -> object store —
    because the defect this replaced was precisely that the row was written and the
    upload never happened. Stubbing at the sink would have kept that invisible; stubbing
    at the store keeps the whole path under test while staying offline.
    """

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


def _seed_headcounts(connection, plan: PlannedRun) -> None:
    """Land headcount facts the way any producer must: through the write path, into the
    table, with an evidence pointer. The capture then reads them like production does —
    a fake extractor would have skipped the plane this milestone is about."""
    for idx, _subject_id in enumerate(sorted(plan.coordinates)):
        cik = 100000 + idx
        record_headcount(
            connection,
            cik=cik,
            headcount=Decimal("164000"),
            knowable_at=datetime(2026, 1, 1, tzinfo=UTC),
            source="test-fixture",
            evidence_ref="test",
            confidence=Decimal("0.7"),
        )


def _capture(
    connection,
    *,
    version: str,
    corroborate: bool = False,
    object_store: _InMemoryObjectStore | None = None,
    broken: _OneBrokenCell = _OneBrokenCell(),
    outage: _PrimaryOutage | None = None,
    expect_complete: bool = True,
) -> PlannedRun:
    plan = plan_and_persist(connection, cutoff=CUTOFF, version=version)
    _seed_headcounts(connection, plan)
    sink = PostgresCaptureControlSink(
        connection,
        plan.bindings,
        source_label=plan.source_label,
        timeline=plan.timeline,
        retry=plan.retry,
        freshness_windows=plan.freshness_windows,
        default_freshness_max_age=plan.default_freshness_max_age,
        object_store=object_store or _InMemoryObjectStore(),
        coordinates=plan.coordinates,
        raw_coordinates=plan.raw_coordinates,
    )
    report = run_topt_capture(
        plan.run_id,
        plan.work_items,
        _routes(
            plan,
            corroborate=corroborate,
            headcount_extractor=PostgresHeadcountExtractor(connection),
            broken=broken,
            outage=outage,
        ),
        PostgresEvidenceGraphRepository(connection),
        sink=sink,
        cutoff=CUTOFF,
        recorded_at=CUTOFF,
    )
    assert not report.halted
    assert report.total == 84
    if expect_complete:
        assert all(outcome.terminal_state is ObligationTerminalState.SUCCESS for outcome in report.outcomes)
    return plan


# -- the fusion invariant judges what the snapshot SELECTED (#581, 2026-09-07) -----------


def _output_invariants_tool():
    """The suite as a module (it is a tool, not a package), registered so its dataclasses
    resolve their annotations."""
    import importlib.util
    import sys

    path = Path(__file__).resolve().parents[4] / "tools" / "output_invariants.py"
    spec = importlib.util.spec_from_file_location("output_invariants_under_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _Borrowed:
    """Lend the test's transaction to the tool's `with connect(url) as connection`
    without letting the exit commit or close it."""

    def __init__(self, connection) -> None:
        self._connection = connection

    def __enter__(self):
        return self._connection

    def __exit__(self, *_: object) -> None:
        return None


def test_fusion_invariant_judges_the_selected_observation_not_the_contest(connection, capsys) -> None:
    """Before 2026-09-07 the invariant listed every obligation with two parsers as a
    violation — every market-price cell once the second origin landed, 3,102 on
    production — while the snapshots had selected the primary for all of them. Now it
    holds when the primary is selected and turns red when a selected observation is
    not the primary's."""
    tool = _output_invariants_tool()
    fusion = next(
        invariant for invariant in tool.INVARIANTS if invariant.id == "fusion-selects-by-priority-not-recency"
    )
    plan = _capture(connection, version="test-fusion-selection", corroborate=True)
    core = PostgresToptCoreRepository(connection)
    snapshot = core.freeze_snapshot(run_id=plan.run_id, release_manifest_id=plan.release_manifest_id)
    assert snapshot.run_id == plan.run_id
    # The invariant judges the governed heads only: point the pointer at this run
    # (sequence 0, the shape register_run_evidence writes on a first advance).
    pointer_sha = canonical_sha256({"probe": plan.run_id})
    connection.execute(
        """
        insert into mart.current_pointer (pointer_id, content_sha256, environment, universe_id, universe_version,
                                          factor_id, target_run_id, sequence, previous_run_id, advanced_at)
        values (%s, %s, 'production', %s, %s, 'gross_profit_per_employee', %s, 0, null, %s)
        """,
        (
            f"current-pointer:{pointer_sha}",
            pointer_sha,
            snapshot.universe_id,
            snapshot.universe_version,
            plan.run_id,
            CUTOFF,
        ),
    )

    run = lambda: tool.check(  # noqa: E731
        "postgresql://borrowed", invariants=(fusion,), exemptions={}, connect=lambda _url: _Borrowed(connection)
    )
    assert run() == 0
    report = capsys.readouterr().out
    # Every corroborated cell is contested: 21 market-price (Yahoo + Twelve Data + moomoo
    # K-line) and 21 financial-fact (SEC + moomoo statements). The invariant's population
    # is every contested selection; its violation rule still judges market-price only.
    assert "fusion-selects-by-priority-not-recency: 42 row(s) examined" in report, report

    # Flip ONE selected market-price observation to a parser family that is not the primary
    # (a third family, so the obligation stays contested and the selection is what changes).
    # Observations are append-only by trigger (a write path can never produce this state),
    # so the red case bypasses the trigger for this transaction only; it rolls back.
    connection.execute("set local session_replication_role = replica")
    flipped = connection.execute(
        """
        with selected as (
            select sel.observation_id
            from staging.topt_core_snapshots s
            cross join lateral jsonb_array_elements(s.payload->'members') member
            cross join lateral jsonb_array_elements_text(member->'observation_ids') sel(observation_id)
            where s.run_id = %s
        )
        update staging.capture_normalized_observations o
           set parser_version = 'probe-parser:v1'
         where o.observation_id = (
            select selected.observation_id from selected
            join staging.capture_normalized_observations x on x.observation_id = selected.observation_id
            where x.semantic_type = 'market-price' limit 1
         )
        returning o.observation_id
        """,
        (plan.run_id,),
    ).fetchall()
    assert len(flipped) == 1
    assert run() == 1
    assert "fusion-selects-by-priority-not-recency: 1 violation(s)" in capsys.readouterr().err


def test_the_governed_head_selects_the_strategy_run_not_recency(connection) -> None:
    """#575: both strategy-run twins served `order by executed_at desc limit 1` — a bare
    mutable latest. Prod 2026-09-05: a manual replay at 03:10Z displaced the governed
    22:15Z run on every surface for a day. The rule now ranks the run the governed
    capture head resolves to first (mart.governed_strategy_run, keyed by the strategy
    run the tick bound to the head's capture run since #877) and falls back to recency
    only when no head resolves a run.
    Red against the old rule: the later fake run below would win."""
    from data_engine.datahub.strategy_bridge import (
        run_strategy_replay_for_cutoff,
        seed_strategy_inputs_from_capture,
    )
    from truealpha_contracts.strategy_run_postgres import LATEST_RUN_SQL

    plan = _capture(connection, version="test-governed-strategy-run")
    core = PostgresToptCoreRepository(connection)
    snapshot = core.freeze_snapshot(run_id=plan.run_id, release_manifest_id=plan.release_manifest_id)
    seed_strategy_inputs_from_capture(connection, plan.run_id, cutoff=CUTOFF)
    governed_run_id, _count, _snapshot = run_strategy_replay_for_cutoff(
        connection, cutoff=CUTOFF, executed_at=CUTOFF, risk_free_rate=Decimal("0.05"), capture_run_id=plan.run_id
    )
    env = connection.execute("select environment from mart.environment_identity").fetchone()[0]
    pointer_sha = canonical_sha256({"probe": plan.run_id})
    connection.execute(
        """
        insert into mart.current_pointer (pointer_id, content_sha256, environment, universe_id, universe_version,
                                          factor_id, target_run_id, sequence, previous_run_id, advanced_at)
        values (%s, %s, %s, %s, %s, 'gross_profit_per_employee', %s, 0, null, %s)
        """,
        (
            f"current-pointer:{pointer_sha}",
            pointer_sha,
            env,
            snapshot.universe_id,
            snapshot.universe_version,
            plan.run_id,
            datetime.now(UTC),
        ),
    )
    strategy_key = connection.execute(
        "select strategy_key from mart.strategy_runs where strategy_run_id = %s", (governed_run_id,)
    ).fetchone()[0]

    # The view resolves exactly this run for the head.
    resolved = connection.execute(
        "select strategy_run_id, universe_id from mart.governed_strategy_run where target_run_id = %s", (plan.run_id,)
    ).fetchall()
    assert resolved == [(governed_run_id, snapshot.universe_id)]

    # A newer run that no head resolves — the manual replay shape — must not displace it.
    later_sha = hashlib.sha256(b"later-unresolved-run").hexdigest()
    connection.execute(
        """
        insert into mart.strategy_runs (strategy_run_id, content_sha256, strategy_key, strategy_version,
                                        definition_content_sha256, corpus_sha256, claim_ceiling, executed_at)
        values (%s, %s, %s, 'v0', %s, %s, 'preview', %s)
        """,
        (f"strategy-run:{later_sha}", later_sha, strategy_key, later_sha, later_sha, CUTOFF + timedelta(days=1)),
    )
    row = connection.execute(LATEST_RUN_SQL, (strategy_key,)).fetchone()
    assert row[0] == governed_run_id and row[3] is True, row
    recency_only = connection.execute(
        "select strategy_run_id from mart.strategy_runs where strategy_key = %s order by executed_at desc limit 1",
        (strategy_key,),
    ).fetchone()[0]
    # Any newer run — the fake one, or a stale row another test left on a shared database
    # — is what recency alone would have served. The governed rule above ignored them all.
    assert recency_only != governed_run_id, "the red case: recency alone would serve an unresolved run"


def test_a_governed_head_with_no_bound_strategy_run_is_a_visible_gap_not_a_silent_one(connection) -> None:
    """#575/#1028: `run_production_topt_capture.py` used to advance `mart.current_pointer`
    via `register_run_evidence` without ever running the strategy bridge — the scheduled
    tick's `seed_strategy_inputs_from_capture` -> `run_strategy_replay_for_cutoff`, which
    binds a strategy run to the capture (#877). `mart.governed_strategy_run` inner-joins
    from the head to that binding, so an unbound head made the view resolve to nothing;
    readers fell back to "newest by executed_at" (the sibling test above), and the nightly
    `report_surface_proof` check found the view empty and reported it, red, on both
    2026-09-23 and 2026-09-24 in staging.

    Reproduces the pre-fix shape directly: capture + pointer-advance with NO strategy
    bridge in between (what the script used to do) must leave `mart.governed_strategy_run`
    resolving nothing for that head — the same "empty view" surface_proof.py detects.
    Reverse-verified: deleting the strategy-bridge lines the fixed script now runs turns
    this from a documented gap into a caught one — this test is what would have caught it."""
    plan = _capture(connection, version="test-unbound-strategy-gap")
    core = PostgresToptCoreRepository(connection)
    snapshot = core.freeze_snapshot(run_id=plan.run_id, release_manifest_id=plan.release_manifest_id)

    env = connection.execute("select environment from mart.environment_identity").fetchone()[0]
    pointer_sha = canonical_sha256({"probe": plan.run_id})
    connection.execute(
        """
        insert into mart.current_pointer (pointer_id, content_sha256, environment, universe_id, universe_version,
                                          factor_id, target_run_id, sequence, previous_run_id, advanced_at)
        values (%s, %s, %s, %s, %s, 'gross_profit_per_employee', %s, 0, null, %s)
        """,
        (
            f"current-pointer:{pointer_sha}",
            pointer_sha,
            env,
            snapshot.universe_id,
            snapshot.universe_version,
            plan.run_id,
            CUTOFF,
        ),
    )

    # No seed_strategy_inputs_from_capture, no run_strategy_replay_for_cutoff — the
    # pre-#575-fix manual script's exact sequence. The view must show this, not hide it.
    resolved = connection.execute(
        "select strategy_run_id from mart.governed_strategy_run where target_run_id = %s", (plan.run_id,)
    ).fetchall()
    assert resolved == [], (
        f"expected the unbound head to resolve nothing (the gap #575/#1028 describe), got {resolved} — "
        "either a strategy run bound itself to this capture with no seed/replay call, or the view's "
        "join changed shape; either way this test's premise needs re-checking before trusting it"
    )

    # Now run exactly what the fixed script runs, in order, before its own
    # register_run_evidence call — not re-implemented, the same three functions — and the
    # gap must close for this same head.
    from data_engine.datahub.strategy_bridge import (
        persist_strategy_input_coverage,
        run_strategy_replay_for_cutoff,
        seed_strategy_inputs_from_capture,
    )

    seed_strategy_inputs_from_capture(connection, plan.run_id, cutoff=CUTOFF)
    persist_strategy_input_coverage(connection, plan.run_id, cutoff=CUTOFF)
    bound_run_id, _count, _snapshot = run_strategy_replay_for_cutoff(
        connection, cutoff=CUTOFF, executed_at=CUTOFF, risk_free_rate=Decimal("0.05"), capture_run_id=plan.run_id
    )
    resolved_after = connection.execute(
        "select strategy_run_id from mart.governed_strategy_run where target_run_id = %s", (plan.run_id,)
    ).fetchall()
    assert resolved_after == [(bound_run_id,)]


def test_the_run_plan_records_which_data_engine_build_produced_it(connection, monkeypatch) -> None:
    """#712: the compose injects GIT_COMMIT_SHA and TRUEALPHA_DATA_ENGINE_IMAGE_DIGEST into
    every data-engine process; the run plan now carries them and
    `mart.data_engine_identity` projects them for /health and /admin. Read back through the
    view, which is what the consumers read, not through the payload column.

    Driven through `settings` since #784 — the deployed reader resolves these names through
    the model that declares them in `apps/data-engine/required-env.generated.json`, so this
    test sets what the deployment set, not what happened to be in `os.environ`."""
    monkeypatch.setattr(settings, "git_commit_sha", "4cf7291deadbeef")
    monkeypatch.setattr(settings, "data_engine_image_digest", "sha256:00f7")
    stamped = plan_and_persist(connection, cutoff=CUTOFF, version="test-identity-stamped")
    row = connection.execute(
        "select git_sha, image_digest from mart.data_engine_identity where run_id = %s", (stamped.run_id,)
    ).fetchone()
    assert row == ("4cf7291deadbeef", "sha256:00f7")

    # A process the deployment told nothing (local, a CI job that forgot to pass it) is
    # recorded as unknown, never as a stale value carried from somewhere else.
    monkeypatch.setattr(settings, "git_commit_sha", "")
    monkeypatch.setattr(settings, "data_engine_image_digest", "")
    bare = plan_and_persist(connection, cutoff=CUTOFF + timedelta(minutes=1), version="test-identity-bare")
    row = connection.execute(
        "select git_sha, image_digest from mart.data_engine_identity where run_id = %s", (bare.run_id,)
    ).fetchone()
    assert row == ("unknown", "unknown")


def test_the_run_plan_reads_the_build_through_settings_not_the_process_environment(connection, monkeypatch) -> None:
    """#784, red against the code this replaced: `plan_and_persist` read
    `os.environ.get("GIT_COMMIT_SHA")` and `os.environ.get("TRUEALPHA_DATA_ENGINE_IMAGE_DIGEST")`
    at the call site, so the names the environment manifest declares and the values the run
    actually stamped were two unconnected things — nothing reconciled them and boot
    validation could not require them. Both are set here, and the settings value must win."""
    monkeypatch.setattr(settings, "git_commit_sha", "v0.0.49")
    monkeypatch.setattr(settings, "data_engine_image_digest", "sha256:" + "a" * 64)
    monkeypatch.setenv("GIT_COMMIT_SHA", "v0.0.00-from-the-environment")
    monkeypatch.setenv("TRUEALPHA_DATA_ENGINE_IMAGE_DIGEST", "sha256:" + "b" * 64)
    planned = plan_and_persist(connection, cutoff=CUTOFF + timedelta(minutes=2), version="test-identity-settings")
    row = connection.execute(
        "select git_sha, image_digest from mart.data_engine_identity where run_id = %s", (planned.run_id,)
    ).fetchone()
    assert row == ("v0.0.49", "sha256:" + "a" * 64)


def test_the_release_identity_the_run_stamps_is_measured_not_minted_from_a_literal(connection, monkeypatch) -> None:
    """#784/#712: `release_manifest_id` used to be the hash of `{"kind": ...}` — one value
    for every run, every tag and both environments. It is now the artifact's measurement, so
    two releases stamp two ids, and the payload persisted beside it says what was measured.

    Also records what the DEPLOYMENT declared this release to be (`TRUEALPHA_RELEASE_MANIFEST_ID`,
    still a hand-written Vault value today), so the hand-off to infra2 is observable on the
    row rather than asserted in a PR body."""
    monkeypatch.setattr(settings, "release_manifest_id", "release-manifest:" + "c" * 64)
    monkeypatch.setattr(settings, "capture_approved_by", "zitian")

    monkeypatch.setattr(settings, "git_commit_sha", "v0.0.49")
    first = plan_and_persist(connection, cutoff=CUTOFF + timedelta(minutes=3), version="test-release-first")
    monkeypatch.setattr(settings, "git_commit_sha", "v0.0.50")
    second = plan_and_persist(connection, cutoff=CUTOFF + timedelta(minutes=4), version="test-release-second")

    assert first.release_manifest_id != second.release_manifest_id, "two releases, two identities"
    assert re.fullmatch(r"release-manifest:[0-9a-f]{64}", first.release_manifest_id)

    payload = connection.execute(
        "select payload from raw.production_topt_run_plans where run_id = %s", (first.run_id,)
    ).fetchone()[0]
    assert payload["release_manifest_id"] == first.release_manifest_id
    assert payload["declared_release_manifest_id"] == "release-manifest:" + "c" * 64
    assert payload["capture_approved_by"] == "zitian"

    # The contract object the run wrote is the measurement itself, not a literal.
    measured = connection.execute(
        "select content_sha256, payload from staging.contract_objects where contract_id = %s",
        (first.release_manifest_id,),
    ).fetchone()
    assert measured[0] == first.release_manifest_id.removeprefix("release-manifest:")
    assert measured[1]["git_commit_sha"] == "v0.0.49"
    assert measured[1]["migration_ids"] and measured[1]["environment_contract_sha256"]

    # This payload is the first content-addressed payload in the lane to carry an ARRAY, and
    # the database canonicalises payloads with its own function -- the one the run-plan and
    # snapshot triggers hash with (`raw.canonical_sha256`, migration 0023). Python and
    # Postgres must agree about what this release is called, or a row that is valid on one
    # side of the boundary is a drifted identity on the other.
    in_database = connection.execute(
        "select raw.canonical_sha256(payload) from staging.contract_objects where contract_id = %s",
        (first.release_manifest_id,),
    ).fetchone()[0]
    assert in_database == measured[0]


def test_pointer_writer_with_staging_environment_stores_staging_key(connection, monkeypatch) -> None:
    """#756: running capture with environment tier STAGING stamps STAGING onto the campaign,
    declares 'staging' in mart.environment_identity, and writes the pointer key with
    environment 'staging'."""
    from truealpha_contracts.common import CaptureEnvironment
    from truealpha_runtime import EnvironmentTier as RuntimeEnvironmentTier

    monkeypatch.setattr(settings, "environment_tier", RuntimeEnvironmentTier.STAGING)
    assert settings.capture_environment == CaptureEnvironment.STAGING

    planned = plan_and_persist(
        connection,
        cutoff=CUTOFF + timedelta(minutes=5),
        version="test-756-staging",
    )

    # 1. Assert mart.environment_identity has 'staging'
    identity = connection.execute("select environment from mart.environment_identity").fetchone()
    assert identity is not None and identity[0] == "staging"

    # 2. Assert raw.capture_campaigns has 'staging'
    campaign_env = connection.execute(
        """
        select c.environment from raw.capture_campaigns c
        join raw.capture_runs r on r.campaign_id = c.campaign_id
        where r.run_id = %s
        """,
        (planned.run_id,),
    ).fetchone()[0]
    assert campaign_env == "staging"

    # 3. Assert mart.topt_capture_status has 'staging'
    status_env = connection.execute(
        "select environment from mart.topt_capture_status where run_id = %s",
        (planned.run_id,),
    ).fetchone()[0]
    assert status_env == "staging"


# -- a cell the primary cannot serve (#862) -------------------------------------------------


def _price_obligation(connection, run_id: str, listing_id: str) -> tuple:
    """The victim's market-price obligation as the ledger and the mart record it."""
    return connection.execute(
        """
        select result.terminal_state, result.reason_codes,
               attempt.outcome, attempt.reason_codes, attempt.source_vintage_id,
               vintage.source_request_id = work.source_request_id as under_the_planned_request,
               vintage.source_record_id, landing.source,
               array(
                   select r.outcome
                   from raw.capture_attempts a
                   join raw.capture_attempt_results r using (attempt_id)
                   where a.work_item_id = work.work_item_id
                   order by a.attempt_number
               ) as attempt_outcomes
        from raw.capture_obligations ob
        join raw.capture_obligation_results result on result.capture_obligation_id = ob.obligation_id
        join raw.capture_attempt_results attempt on attempt.attempt_id = result.final_attempt_id
        join raw.capture_obligation_work_bindings binding on binding.obligation_id = ob.obligation_id
        join raw.capture_work_items work on work.work_item_id = binding.work_item_id
        left join raw.capture_source_vintages vintage on vintage.source_vintage_id = attempt.source_vintage_id
        left join raw.fetches landing on landing.id = vintage.raw_fetch_id
        where ob.run_id = %s and ob.subject_id = %s and ob.capture_requirement_id = 'market-price:v1'
        """,
        (run_id, listing_id),
    ).fetchone()


def test_a_cell_the_primary_cannot_serve_is_served_by_the_next_registered_origin(connection) -> None:
    """#862: Yahoo raises on one listing for every retry; Twelve Data holds its settled
    close. The run still resolves all 84 cells (`_capture` asserts it), and:

    * the ledger keeps the primary's failure — three attempts, the first two transport
      errors, the terminal one a success that names the failover vintage and still reads
      `transient_network` — and the obligation result says `served_by_failover`;
    * the vintage sits under the cell's PLANNED request (the one the snapshot's
      request-identity guard admits) with Twelve Data's record id and bytes;
    * the snapshot binds that vintage's observation, so the mart serves Twelve Data's
      number at Twelve Data's grade one step down, and the meta-info view and the
      strategy bridge carry the same served close.
    """
    plan = _capture(connection, version="test-862-served", corroborate=True, outage=_PrimaryOutage())
    victim_listing, victim_ticker = _victim(plan)

    (
        terminal_state,
        result_reasons,
        final_outcome,
        final_reasons,
        vintage_id,
        under_planned_request,
        record_id,
        landed_source,
        attempt_outcomes,
    ) = _price_obligation(connection, plan.run_id, victim_listing)
    assert (terminal_state, result_reasons) == ("success", ["served_by_failover", "transient_network"])
    assert (final_outcome, final_reasons) == ("success", ["transient_network"])
    assert attempt_outcomes == ["transport_error", "transport_error", "success"]
    assert vintage_id is not None and under_planned_request is True
    # The failover's own record id is stamped with the settled session it served
    # (quote.as_of), not the tick's run clock -- see _routes' cutoff_date (#530 item 1).
    settled_session = plan.timeline.partition_start.date()
    assert (record_id, landed_source) == (f"twelve-data:{victim_ticker}:{settled_session.isoformat()}", "twelvedata")
    # Every other price cell is the primary's, with an untouched ledger.
    others = connection.execute(
        """
        select distinct result.reason_codes
        from raw.capture_obligations ob
        join raw.capture_obligation_results result on result.capture_obligation_id = ob.obligation_id
        where ob.run_id = %s and ob.subject_id <> %s
        """,
        (plan.run_id, victim_listing),
    ).fetchall()
    assert others == [(["success"],)]

    core = PostgresToptCoreRepository(connection)
    snapshot = core.freeze_snapshot(run_id=plan.run_id, release_manifest_id=plan.release_manifest_id)
    victim_listing_id = plan.coordinates[victim_listing][2]
    member = next(member for member in snapshot.members if member.listing_id == victim_listing_id)
    assert member.market_price.value == Decimal(_FAILOVER_CLOSE)
    assert member.market_price.confidence == Decimal("0.75")
    bound = connection.execute(
        """
        select o.parser_version, o.source_vintage_id, p.normalized_payload->>'served_by_failover'
        from staging.capture_normalized_observations o
        join staging.capture_observation_payloads p using (observation_id)
        where o.observation_id = %s
        """,
        (member.market_price.input_id,),
    ).fetchone()
    assert bound == ("twelve-data-parser:v3", vintage_id, "twelve-data")
    assert all(m.market_price.value == Decimal("40") for m in snapshot.members if m.listing_id != victim_listing_id)
    results = core.materialize(snapshot, gppe_definition=GppeV0Definition(risk_free_rate="0.05"))
    assert len(results) == 20

    meta = connection.execute(
        """
        select parser_version, confidence, reason_codes from mart.topt_capture_meta_info
        where run_id = %s and subject_id = %s and capture_requirement_id = 'market-price:v1'
        """,
        (plan.run_id, victim_listing),
    ).fetchone()
    assert meta == ("twelve-data-parser:v3", Decimal("0.75"), ["served_by_failover", "transient_network"])

    from data_engine.datahub.strategy_bridge import seed_strategy_inputs_from_capture

    victim_issuer = plan.coordinates[victim_listing][0]
    connection.execute("delete from staging.strategy_backtest_inputs where cutoff_at = %s", (CUTOFF,))
    seed_strategy_inputs_from_capture(connection, plan.run_id, cutoff=CUTOFF)
    closes = connection.execute(
        """
        select issuer_id, value, confidence from staging.strategy_backtest_inputs
        where cutoff_at = %s and input_key = 'last_close'
        """,
        (CUTOFF,),
    ).fetchall()
    by_issuer = {issuer: (value, confidence) for issuer, value, confidence in closes}
    assert by_issuer[victim_issuer] == (Decimal(_FAILOVER_CLOSE), Decimal("0.75"))
    assert len(by_issuer) == len({c[0] for c in plan.coordinates.values()})


@pytest.mark.parametrize("corroborated", [True, False], ids=["second-origin-asserts", "single-origin"])
def test_a_failover_cell_is_graded_on_what_actually_corroborated_it(connection, corroborated: bool) -> None:
    """#862: the reports grade a failover-served cell honestly. `selected_source` is the
    real source; the cell is `agreed` only when a SECOND independent origin (moomoo) also
    asserted the served session — then the pointer gate counts it — and otherwise it is
    single-origin: `insufficient_independent_origins` in the quality report, LOW
    `served_by_failover` in the confidence report, and outside the gate's corroborated
    share. The fusion invariant holds either way: the snapshot selected the highest-
    priority origin present, and the substitution is declared."""
    from data_engine.datahub import confidence_report
    from data_engine.datahub.a1_evidence import _corroborated_share, unmet_objectives
    from data_engine.datahub.question_coverage import GovernedHead

    store = _InMemoryObjectStore()
    plan = _capture(
        connection,
        version=f"test-862-graded-{corroborated}",
        corroborate=True,
        object_store=store,
        outage=_PrimaryOutage(moomoo=corroborated),
    )
    victim_listing, _ticker = _victim(plan)
    report = quality_report.build_report(connection, plan.run_id, object_store=store)

    cells = report["reconciliation_cells"]
    cell = cells[victim_listing]
    assert cell["served_by_failover"] == "twelve-data"
    assert (cell["selected_source"], cell["selected_value"]) == ("twelve-data:v1", _FAILOVER_CLOSE)
    if corroborated:
        assert (cell["outcome"], cell["origin_groups"]) == ("agreed", 2)
    else:
        assert (cell["outcome"], cell["origin_groups"]) == ("insufficient_independent_origins", 1)
    assert all("served_by_failover" not in other for listing, other in cells.items() if listing != victim_listing)
    assert all(other["origin_groups"] == 3 for listing, other in cells.items() if listing != victim_listing)
    assert report["served_by_failover_count"] == 1
    assert (report["available_count"], report["lineage_complete_count"]) == (84, 84)
    assert report["independently_reconciled_count"] == (21 if corroborated else 20)
    share = _corroborated_share(cells, minimum_origin_groups=2)
    assert share == Decimal(21 if corroborated else 20) / Decimal(21)
    assert not [u for u in unmet_objectives(report) if u.objective == "corroborated_share"]

    quality_report.persist(connection, report)
    head = GovernedHead(universe_id="universe:topt-us-2026-03-31", run_id=plan.run_id, cutoff=CUTOFF)
    confidence = confidence_report.build_report(
        connection, universe="topt", head=head, executed_at=CUTOFF, environment="test"
    )
    close = confidence["families"]["close"]
    graded = confidence["cells"]["close"][victim_listing]
    if corroborated:
        assert (graded["band"], graded["reason"]) == ("medium", "two_origins_agree")
        assert close["reasons"] == {"independent_origins_agree": 20, "two_origins_agree": 1}
    else:
        assert (graded["band"], graded["reason"], graded["origins"]) == (
            "low",
            "served_by_failover",
            ["origin:twelve-data:v1"],
        )
        assert close["reasons"] == {"independent_origins_agree": 20, "served_by_failover": 1}
    assert confidence["accuracy"]["close"]["matches_quality_report"] is True

    # The fusion invariant judges the governed head: point the pointer at this run.
    tool = _output_invariants_tool()
    fusion = next(
        invariant for invariant in tool.INVARIANTS if invariant.id == "fusion-selects-by-priority-not-recency"
    )
    snapshot = PostgresToptCoreRepository(connection).freeze_snapshot(
        run_id=plan.run_id, release_manifest_id=plan.release_manifest_id
    )
    pointer_sha = canonical_sha256({"probe": plan.run_id})
    connection.execute(
        """
        insert into mart.current_pointer (pointer_id, content_sha256, environment, universe_id, universe_version,
                                          factor_id, target_run_id, sequence, previous_run_id, advanced_at)
        values (%s, %s, 'production', %s, %s, 'gross_profit_per_employee', %s, 0, null, %s)
        """,
        (
            f"current-pointer:{pointer_sha}",
            pointer_sha,
            snapshot.universe_id,
            snapshot.universe_version,
            plan.run_id,
            CUTOFF,
        ),
    )
    assert (
        tool.check(
            "postgresql://borrowed", invariants=(fusion,), exemptions={}, connect=lambda _url: _Borrowed(connection)
        )
        == 0
    )


def test_a_failover_substitution_the_payload_does_not_declare_is_a_fusion_violation(connection, capsys) -> None:
    """The invariant's red case for #862: strip the marker from the served observation —
    the shape of a silent substitution — and the selection no longer passes."""
    tool = _output_invariants_tool()
    fusion = next(
        invariant for invariant in tool.INVARIANTS if invariant.id == "fusion-selects-by-priority-not-recency"
    )
    plan = _capture(connection, version="test-862-silent", corroborate=True, outage=_PrimaryOutage())
    snapshot = PostgresToptCoreRepository(connection).freeze_snapshot(
        run_id=plan.run_id, release_manifest_id=plan.release_manifest_id
    )
    pointer_sha = canonical_sha256({"probe": plan.run_id})
    connection.execute(
        """
        insert into mart.current_pointer (pointer_id, content_sha256, environment, universe_id, universe_version,
                                          factor_id, target_run_id, sequence, previous_run_id, advanced_at)
        values (%s, %s, 'production', %s, %s, 'gross_profit_per_employee', %s, 0, null, %s)
        """,
        (
            f"current-pointer:{pointer_sha}",
            pointer_sha,
            snapshot.universe_id,
            snapshot.universe_version,
            plan.run_id,
            CUTOFF,
        ),
    )
    run = lambda: tool.check(  # noqa: E731
        "postgresql://borrowed", invariants=(fusion,), exemptions={}, connect=lambda _url: _Borrowed(connection)
    )
    assert run() == 0
    capsys.readouterr()
    # Payloads are append-only by trigger; the red case bypasses it for this transaction.
    connection.execute("set local session_replication_role = replica")
    stripped = connection.execute(
        """
        update staging.capture_observation_payloads
           set normalized_payload = normalized_payload - 'served_by_failover'
         where normalized_payload ? 'served_by_failover'
           and observation_id in (
               select oo.observation_id
               from raw.capture_obligations ob
               join staging.capture_observation_obligations oo on oo.capture_obligation_id = ob.obligation_id
               where ob.run_id = %s
           )
        returning observation_id
        """,
        (plan.run_id,),
    ).fetchall()
    assert len(stripped) == 1
    assert run() == 1
    assert "fusion-selects-by-priority-not-recency: 1 violation(s)" in capsys.readouterr().err


def test_a_higher_ranked_origin_passed_over_is_a_fusion_violation(connection, capsys) -> None:
    """#1061: the nightly invariant judges priority against recency. Its third clause needs
    its own red case. Twelve Data (rank 1) serves a failover cell. Relabel the corroborating
    observation of the same session as the primary family (rank 0). Now a higher-ranked
    origin asserted the cell, and the snapshot passed it over. The payload still declares
    the substitution, and the origin is still ranked. So only the passed-over clause can
    turn the invariant red."""
    tool = _output_invariants_tool()
    fusion = next(
        invariant for invariant in tool.INVARIANTS if invariant.id == "fusion-selects-by-priority-not-recency"
    )
    plan = _capture(connection, version="test-1061-passed-over", corroborate=True, outage=_PrimaryOutage())
    victim_listing, _ticker = _victim(plan)
    snapshot = PostgresToptCoreRepository(connection).freeze_snapshot(
        run_id=plan.run_id, release_manifest_id=plan.release_manifest_id
    )
    pointer_sha = canonical_sha256({"probe": plan.run_id})
    connection.execute(
        """
        insert into mart.current_pointer (pointer_id, content_sha256, environment, universe_id, universe_version,
                                          factor_id, target_run_id, sequence, previous_run_id, advanced_at)
        values (%s, %s, 'production', %s, %s, 'gross_profit_per_employee', %s, 0, null, %s)
        """,
        (
            f"current-pointer:{pointer_sha}",
            pointer_sha,
            snapshot.universe_id,
            snapshot.universe_version,
            plan.run_id,
            CUTOFF,
        ),
    )
    run = lambda: tool.check(  # noqa: E731
        "postgresql://borrowed", invariants=(fusion,), exemptions={}, connect=lambda _url: _Borrowed(connection)
    )
    assert run() == 0
    capsys.readouterr()
    victim_listing_id = plan.coordinates[victim_listing][2]
    selected_id = next(m for m in snapshot.members if m.listing_id == victim_listing_id).market_price.input_id
    # Observations are append-only by trigger; the red case bypasses it for this transaction.
    connection.execute("set local session_replication_role = replica")
    relabelled = connection.execute(
        """
        update staging.capture_normalized_observations peer
           set parser_version = %s || ':v1',
               knowable_at = selected.knowable_at
          from staging.capture_normalized_observations selected
         where selected.observation_id = %s
           and peer.observation_id <> selected.observation_id
           and peer.semantic_type = 'market-price'
           and peer.observation_id in (
               select oo.observation_id
               from staging.capture_observation_obligations oo
               join staging.capture_observation_obligations chosen
                 on chosen.capture_obligation_id = oo.capture_obligation_id
               where chosen.observation_id = selected.observation_id
           )
        returning peer.observation_id
        """,
        (tool.PRIMARY_MARKET_PRICE_PARSER, selected_id),
    ).fetchall()
    assert len(relabelled) >= 1
    assert run() == 1
    assert "fusion-selects-by-priority-not-recency: 1 violation(s)" in capsys.readouterr().err


def test_when_every_origin_fails_the_cell_stays_unavailable_as_before(connection) -> None:
    """#862: no registered origin holds the victim's close. The obligation resolves exactly
    as it did before failover existed — UNAVAILABLE after three transport errors, the
    primary's reason alone, no vintage, no observation — and the run cannot freeze."""
    plan = _capture(
        connection,
        version="test-862-all-fail",
        corroborate=True,
        outage=_PrimaryOutage(twelve_data=False, moomoo=False),
        expect_complete=False,
    )
    victim_listing, _ticker = _victim(plan)
    row = _price_obligation(connection, plan.run_id, victim_listing)
    assert row[:5] == ("unavailable", ["transient_network"], "unavailable", ["transient_network"], None)
    assert row[8] == ["transport_error", "transport_error", "unavailable"]
    bound = connection.execute(
        """
        select count(*)
        from raw.capture_obligations ob
        join staging.capture_observation_obligations oo on oo.capture_obligation_id = ob.obligation_id
        where ob.run_id = %s and ob.subject_id = %s and ob.capture_requirement_id = 'market-price:v1'
        """,
        (plan.run_id, victim_listing),
    ).fetchone()
    assert bound == (0,)
    with pytest.raises(ValueError, match="completely successful"):
        PostgresToptCoreRepository(connection).freeze_snapshot(
            run_id=plan.run_id, release_manifest_id=plan.release_manifest_id
        )


def test_every_reason_code_lands_in_the_attempt_ledger() -> None:
    """A new code (#729's `deferred_capacity`) must map to an attempt outcome, or the
    first non-terminal attempt carrying it would fail far from its cause."""
    from data_engine.datahub.production_topt import persistence

    assert set(persistence._ATTEMPT_OUTCOMES) == set(ObligationReasonCode)
