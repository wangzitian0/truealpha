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
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub import quality_report
from data_engine.datahub.evidence_graph_repository import PostgresEvidenceGraphRepository
from data_engine.datahub.production_topt import PostgresToptCoreRepository
from data_engine.datahub.production_topt.capture_orchestration import run_topt_capture
from data_engine.datahub.production_topt.composition import PlannedRun, plan_and_persist
from data_engine.datahub.production_topt.executor import FetchSuccess, NormalizedRecord, RawResponse, SourceFetchPort
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


# --- Falsifiability harness for mart.datahub_quality_report (#537) ------------------
#
# `availability` counted observation rows and `lineage_completeness` counted a
# `raw.fetches` join, so both read 1.0000 no matter what the run produced: Staging
# reported 84/84 availability for a tick with zero complete strategy inputs, and every
# Production report claimed full lineage while the bucket held exactly one object.
#
# The control below pins both metrics at 1.0000 for an intact run; each injection breaks
# exactly one cell through the deployed write path and requires the corresponding metric
# to move. Delete the control and "always below 1.0" passes; delete an injection and a
# pinned metric passes. Both halves are the check.


def _a_pointer_only_one_cell_rests_on(connection: psycopg.Connection[Any], run_id: str) -> str:
    """Find an object store URI referenced by exactly one cell in the run, so its deletion
    degrades completeness by exactly 1 without knocking out whole obligations."""
    pointers = connection.execute(
        """
        select object_uri, count(*)
        from staging.topt_observations
        where run_id = %s and object_uri is not null
        group by object_uri
        """,
        (run_id,),
    ).fetchall()
    for object_uri, count in pointers:
        if count == 1:
            return object_uri
    raise AssertionError("no pointer is exclusive to a single cell; the harness cannot isolate one")


def test_quality_report_metrics_are_perfect_only_when_the_run_is(connection) -> None:
    """Control: an intact 84-cell run scores 1.0000 on both falsifiable metrics."""
    store = _InMemoryObjectStore()
    plan = _capture(connection, version="test-537-control", object_store=store)

    report = quality_report.build_report(connection, plan.run_id, object_store=store)

    assert report["requested_count"] == 84
    assert report["available_count"] == 84
    assert report["lineage_complete_count"] == 84
    assert report["availability"] == "1.0000"
    assert report["lineage_completeness"] == "1.0000"


def test_availability_falls_when_a_payload_yields_no_usable_value(connection) -> None:
    """One financial-fact cell lands with a null operating numerator.

    Every obligation still terminally succeeds and every row is still written — the only
    thing that changed is that one payload carries no number the factor can use.
    """
    store = _InMemoryObjectStore()
    plan = _capture(
        connection,
        version="test-537-empty-payload",
        object_store=store,
        broken=_OneBrokenCell(financial_fact_numerator=True),
    )

    report = quality_report.build_report(connection, plan.run_id, object_store=store)

    assert report["requested_count"] == 84
    assert report["available_count"] < 84
    assert Decimal(report["availability"]) < 1
    # The damage is confined to availability: the bytes still landed and still dereference.
    assert report["lineage_complete_count"] == 84


def test_availability_falls_when_a_payload_cannot_be_parsed(connection) -> None:
    """One listing-identity cell lands without the `ticker` its semantic contract requires."""
    store = _InMemoryObjectStore()
    plan = _capture(
        connection,
        version="test-537-unparseable",
        object_store=store,
        broken=_OneBrokenCell(identity_payload=True),
    )

    report = quality_report.build_report(connection, plan.run_id, object_store=store)

    assert report["requested_count"] == 84
    assert report["available_count"] == 83
    assert Decimal(report["availability"]) < 1
    assert report["lineage_complete_count"] == 84


def test_lineage_completeness_falls_when_the_stored_object_is_gone(connection) -> None:
    """Deleting one object from the bucket must drop that run's lineage_completeness.

    The `raw.fetches` row, its vintage, and its observation all stay exactly as they were;
    only the bytes the pointer names are gone. That is the Production state the old metric
    scored 1.0000 for, 1016 rows deep.
    """
    store = _InMemoryObjectStore()
    plan = _capture(connection, version="test-537-dangling-pointer", object_store=store)
    intact = quality_report.build_report(connection, plan.run_id, object_store=store)
    assert intact["lineage_complete_count"] == 84

    object_uri = _a_pointer_only_one_cell_rests_on(connection, plan.run_id)
    del store.objects[object_uri.removeprefix("s3://").partition("/")[2]]

    report = quality_report.build_report(connection, plan.run_id, object_store=store)

    assert report["lineage_complete_count"] == 83
    assert Decimal(report["lineage_completeness"]) < 1
    # A dangling pointer says nothing about whether the payload holds a value.
    assert report["available_count"] == 84


def test_lineage_completeness_falls_when_the_stored_bytes_are_not_the_bytes_claimed(connection) -> None:
    """The pointer resolves but the object's digest no longer matches `payload_sha256`.

    A checksum that is never recomputed is a checksum nobody is checking.
    """
    store = _InMemoryObjectStore()
    plan = _capture(connection, version="test-537-checksum", object_store=store)

    object_uri = _a_pointer_only_one_cell_rests_on(connection, plan.run_id)
    store.objects[object_uri.removeprefix("s3://").partition("/")[2]] = b"not the captured bytes"

    report = quality_report.build_report(connection, plan.run_id, object_store=store)

    assert report["lineage_complete_count"] == 83
    assert Decimal(report["lineage_completeness"]) < 1


def test_the_report_and_the_mart_agree_about_the_same_broken_run(connection) -> None:
    """The report cannot call a run whole while the mart calls part of it unavailable.

    Production's report said `84/84` for a run `mart.topt_gppe_results` scored
    19 available / 1 unavailable, and the App renders the mart's number. Both now read
    the same payload fields, so one run cannot produce two answers.
    """
    store = _InMemoryObjectStore()
    plan = _capture(
        connection,
        version="test-537-agreement",
        object_store=store,
        broken=_OneBrokenCell(financial_fact_numerator=True),
    )

    core = PostgresToptCoreRepository(connection)
    snapshot = core.freeze_snapshot(run_id=plan.run_id, release_manifest_id=plan.release_manifest_id)
    results = core.materialize(snapshot, gppe_definition=GppeV0Definition(risk_free_rate="0.05"))
    unavailable = [result for result in results if result.availability.value == "unavailable"]

    report = quality_report.build_report(connection, plan.run_id, object_store=store)

    assert unavailable, "the mart must see the missing operating numerator"
    assert report["available_count"] < report["requested_count"], "so must the report"


def test_retrying_the_same_tick_is_idempotent(connection) -> None:
    """Identities derive from (cutoff, version), so a replay reuses every row."""
    plan = _capture(connection, version="test-a1-replay")
    before = connection.execute(
        "select count(*) from staging.capture_normalized_observations o "
        "join staging.capture_observation_obligations oo using (observation_id) "
        "join raw.capture_obligations ob on ob.obligation_id = oo.capture_obligation_id "
        "where ob.run_id = %s",
        (plan.run_id,),
    ).fetchone()
    replayed = _capture(connection, version="test-a1-replay")
    assert replayed.run_id == plan.run_id
    after = connection.execute(
        "select count(*) from staging.capture_normalized_observations o "
        "join staging.capture_observation_obligations oo using (observation_id) "
        "join raw.capture_obligations ob on ob.obligation_id = oo.capture_obligation_id "
        "where ob.run_id = %s",
        (plan.run_id,),
    ).fetchone()
    assert before == after


def test_sink_refuses_a_success_it_cannot_persist(connection) -> None:
    """A success with no normalized record would terminally resolve the obligation with
    nothing behind it; `freeze_snapshot` would then refuse the run far from the cause."""
    plan = plan_and_persist(connection, cutoff=CUTOFF, version="test-a1-guard")
    sink = PostgresCaptureControlSink(
        connection,
        plan.bindings,
        source_label=plan.source_label,
        timeline=plan.timeline,
        retry=plan.retry,
        object_store=_InMemoryObjectStore(),
    )
    work_item = plan.work_items[0]
    recordless = FetchSuccess(
        raw=RawResponse(body=b"{}", source=DataSource.SEC, record_id="recordless"),
        normalized_sha256="b" * 64,
        confidence=Decimal("0.9"),
        valid_from=date(2026, 3, 31),
        transaction_time=datetime(2026, 3, 31, tzinfo=UTC),
    )
    with pytest.raises(ValueError, match="without a normalized record"):
        sink.record_outcome(
            work_item,
            attempt_reasons=(None,),
            terminal_state=ObligationTerminalState.SUCCESS,
            success=recordless,
        )


def test_sink_refuses_a_ledger_that_contradicts_the_served_value(connection) -> None:
    """#862: a failover success must follow a primary failure, and a primary success must
    end its attempts cleanly. Either contradiction would let the ledger hide a
    substitution or invent a failure, so the sink refuses it before writing anything."""
    plan = plan_and_persist(connection, cutoff=CUTOFF, version="test-862-ledger-guard")
    sink = PostgresCaptureControlSink(
        connection,
        plan.bindings,
        source_label=plan.source_label,
        timeline=plan.timeline,
        retry=plan.retry,
        object_store=_InMemoryObjectStore(),
    )
    work_item = next(
        item
        for item in plan.work_items
        if plan.bindings[item.work_item_id].obligation.capture_requirement_id == "market-price:v1"
    )

    def success(payload: dict, served_by_failover: str | None) -> FetchSuccess:
        return FetchSuccess(
            raw=RawResponse(body=b"bar", source=DataSource.TWELVE_DATA, record_id="twelve-data:X:2026-03-31"),
            normalized_sha256=canonical_sha256(payload),
            confidence=Decimal("0.75"),
            valid_from=date(2026, 3, 31),
            transaction_time=datetime(2026, 3, 31, tzinfo=UTC),
            record=NormalizedRecord(
                payload=payload, parser_version="twelve-data-parser:v3", mapping_version="twelve-data-map:v3"
            ),
            served_by_failover=served_by_failover,
        )

    failover = success({"close": "40.02", "served_by_failover": "twelve-data"}, "twelve-data")
    primary = success({"close": "40"}, None)
    refused = (
        ((None,), ObligationTerminalState.SUCCESS, failover, "must follow a primary failure"),
        ((ObligationReasonCode.TRANSIENT_NETWORK,), ObligationTerminalState.SUCCESS, primary, "end its attempts"),
        ((ObligationReasonCode.TIMEOUT,), ObligationTerminalState.UNAVAILABLE, failover, "under unavailable"),
    )
    for attempt_reasons, terminal_state, handed_over, message in refused:
        with pytest.raises(ValueError, match=message):
            sink.record_outcome(
                work_item, attempt_reasons=attempt_reasons, terminal_state=terminal_state, success=handed_over
            )
    assert connection.execute(
        "select count(*) from raw.capture_attempts where work_item_id = %s", (work_item.work_item_id,)
    ).fetchone() == (0,)


def test_observation_valid_from_is_the_adapters_real_date_not_the_partition_anchor(connection) -> None:
    """#530 item 1: a fact's valid_from is its own real date, not the capturing tick's
    partition anchor -- otherwise a fact is only eligible starting from whenever it
    happened to be captured rather than from when it became real-world true (the defect
    the 2010 Visa share count exposed: captured in 2026, it should have been eligible
    for any replay since 2010, not only from its capture tick's own partition onward)."""
    plan = plan_and_persist(connection, cutoff=CUTOFF, version="test-530-item1-valid-from")
    sink = PostgresCaptureControlSink(
        connection,
        plan.bindings,
        source_label=plan.source_label,
        timeline=plan.timeline,
        retry=plan.retry,
        object_store=_InMemoryObjectStore(),
    )
    work_item = next(
        item
        for item in plan.work_items
        if plan.bindings[item.work_item_id].obligation.capture_requirement_id == "financial-fact:v1"
    )
    obligation_id = plan.bindings[work_item.work_item_id].obligation.obligation_id
    filed_long_before_the_capture = date(2026, 1, 15)
    payload = {"revenue": "100000000"}
    success = FetchSuccess(
        raw=RawResponse(body=b"{}", source=DataSource.SEC, record_id="sec:filed-2026-01-15"),
        normalized_sha256=canonical_sha256(payload),
        confidence=Decimal("0.9"),
        valid_from=filed_long_before_the_capture,
        transaction_time=datetime(2026, 1, 15, tzinfo=UTC),
        record=NormalizedRecord(
            payload=payload, parser_version="sec-financial-adapter-parser:v1", mapping_version="sec-map:v1"
        ),
    )
    sink.record_outcome(
        work_item, attempt_reasons=(None,), terminal_state=ObligationTerminalState.SUCCESS, success=success
    )
    stored = connection.execute(
        """
        select o.valid_from
        from staging.capture_observation_obligations oo
        join staging.capture_normalized_observations o on o.observation_id = oo.observation_id
        where oo.capture_obligation_id = %s
        """,
        (obligation_id,),
    ).fetchone()
    assert stored is not None
    # The column round-trips as an aware datetime at midnight UTC (timestamptz); a bare
    # `date` never compares equal to a `datetime` in Python even for the same day, so
    # normalize before asserting -- an unnormalized comparison here would stay red
    # forever regardless of the fix, not just before it.
    stored_valid_from = stored[0].date() if isinstance(stored[0], datetime) else stored[0]
    partition_start = plan.timeline.partition_start
    partition_start_date = partition_start.date() if isinstance(partition_start, datetime) else partition_start
    assert stored_valid_from == filed_long_before_the_capture
    assert stored_valid_from != partition_start_date


def test_sink_refuses_more_attempts_than_the_retry_policy_permits(connection) -> None:
    plan = plan_and_persist(connection, cutoff=CUTOFF, version="test-a1-attempts")
    sink = PostgresCaptureControlSink(
        connection,
        plan.bindings,
        source_label=plan.source_label,
        timeline=plan.timeline,
        retry=plan.retry,
        object_store=_InMemoryObjectStore(),
    )
    with pytest.raises(ValueError, match="beyond the 3"):
        sink.record_outcome(
            plan.work_items[0],
            attempt_reasons=(ObligationReasonCode.TIMEOUT,) * 4,
            terminal_state=ObligationTerminalState.UNAVAILABLE,
            success=None,
        )


def test_vintage_and_fetch_stamps_are_source_truth_not_cutoff_arithmetic(connection) -> None:
    """#530 slice 1: the sink persists the adapter's own time, not `cutoff - constant`.

    The fabricated stamps produced a false diagnosis in #531 (fetch rows read as
    pre-deploy output because recorded_at sat 58 minutes before the tick that wrote
    them). Financial-fact vintages must carry the adapter's transaction_time (the
    fixture's knowable_at, 2026-02-01 — a real filed-derived date, months before the
    cutoff), and raw.fetches audit stamps must be the ingestion clock.
    """
    import datetime as _dt

    before = _dt.datetime.now(_dt.UTC)
    plan = _capture(connection, version="test-530-stamps")
    after = _dt.datetime.now(_dt.UTC)

    rows = connection.execute(
        """
        select v.source_published_at, f.fetched_at, f.recorded_at
        from raw.capture_obligations ob
        join staging.capture_observation_obligations oo on oo.capture_obligation_id = ob.obligation_id
        join staging.capture_normalized_observations o on o.observation_id = oo.observation_id
        join raw.capture_source_vintages v on v.source_vintage_id = o.source_vintage_id
        join raw.fetches f on f.id = v.raw_fetch_id
        where ob.run_id = %s and o.semantic_type = 'financial-fact'
        """,
        (plan.run_id,),
    ).fetchall()
    assert rows, "the capture must have landed financial-fact vintages"
    for source_published_at, fetched_at, recorded_at in rows:
        # The adapter's transaction_time (bundle knowable_at), not CUTOFF - 2h.
        assert source_published_at == _dt.datetime(2026, 2, 1, tzinfo=_dt.UTC), source_published_at
        # Audit clocks: within this test's own wall-clock window, never cutoff-derived.
        assert before <= fetched_at <= after, (before, fetched_at, after)
        assert before <= recorded_at <= after, (before, recorded_at, after)


def test_semantic_freshness_windows_round_trip_and_cast(connection) -> None:
    """#530 slice 2: the per-semantic windows land as text intervals the views can cast.

    End-to-end grading flips only in slice 3 (real knowable_at); this slice proves the
    storage leg — the map survives the repository, casts via ::interval, and an absent
    key falls back to the policy's single freshness_max_age.
    """
    import datetime as _dt

    from data_engine.datahub.control_plane import replay_retry_policy
    from data_engine.datahub.repository import PostgresCaptureControlRepository
    from truealpha_contracts.datahub import CaptureSchedulePolicy

    policy = CaptureSchedulePolicy(
        policy_version="test-530-windows",
        demanded_cadence=_dt.timedelta(days=1),
        freshness_max_age=_dt.timedelta(days=2),
        semantic_freshness_max_age={
            "market-price": _dt.timedelta(days=5),
            "financial-fact": _dt.timedelta(days=730),
        },
        provider_availability_cadence="scheduled:v1",
        retry=replay_retry_policy(3),
    )
    PostgresCaptureControlRepository(connection).put_schedule_policy(policy)

    fact_window, price_window, fallback = connection.execute(
        """
        select
          nullif(semantic_freshness_max_age->>'financial-fact', '')::interval,
          nullif(semantic_freshness_max_age->>'market-price', '')::interval,
          coalesce(nullif(semantic_freshness_max_age->>'listing-identity', '')::interval, freshness_max_age)
        from raw.capture_schedule_policies where schedule_policy_id = %s
        """,
        (policy.schedule_policy_id,),
    ).fetchone()
    assert fact_window == _dt.timedelta(days=730)
    assert price_window == _dt.timedelta(days=5)
    # The undeclared semantic falls back to the single window — existing behavior.
    assert fallback == _dt.timedelta(days=2)

    # The exact expression the materializer and both views use, on both sides of the
    # 730-day window: a 60-day-old filed date grades fresh, a 3-year-old one stale.
    fresh, stale = connection.execute(
        """
        select interval '60 days' <= x.w, interval '1100 days' <= x.w
        from (select nullif(semantic_freshness_max_age->>'financial-fact', '')::interval as w
              from raw.capture_schedule_policies where schedule_policy_id = %s) x
        """,
        (policy.schedule_policy_id,),
    ).fetchone()
    assert fresh is True and stale is False


def test_observation_knowable_at_is_the_adapters_time_and_freshness_is_graded(connection) -> None:
    """#530 slice 3: the stamped lie is gone.

    Financial-fact observations carry the adapter's transaction_time (the fixture
    bundle's 2026-02-01 filed-derived date — impossible under the old cutoff-58min
    arithmetic) and grade FRESH under their semantic's 730-day window despite being
    60 days before the cutoff, which the old single 2-day window would have called
    stale. Price observations carry their bar date. Nothing carries the fabricated
    constant."""
    import datetime as _dt

    plan = _capture(connection, version="test-530-real-time", corroborate=True)
    rows = connection.execute(
        """
        select o.semantic_type, o.knowable_at, o.freshness_state
        from raw.capture_obligations ob
        join staging.capture_observation_obligations oo on oo.capture_obligation_id = ob.obligation_id
        join staging.capture_normalized_observations o on o.observation_id = oo.observation_id
        where ob.run_id = %s
        """,
        (plan.run_id,),
    ).fetchall()
    assert rows
    fabricated = CUTOFF - _dt.timedelta(minutes=58)
    for semantic, knowable_at, freshness in rows:
        assert knowable_at != fabricated, semantic
        if semantic == "financial-fact":
            assert knowable_at == _dt.datetime(2026, 2, 1, tzinfo=_dt.UTC)
            assert freshness == "fresh", "60 days old is fresh under the 730-day facts window"
        if semantic == "market-price":
            assert knowable_at == _dt.datetime(2026, 3, 31, tzinfo=_dt.UTC)
            assert freshness == "fresh"


def _hand_driven_sink(connection, plan: PlannedRun) -> PostgresCaptureControlSink:
    """A sink over the plan's bindings, for tests that call `record_outcome` by hand."""
    return PostgresCaptureControlSink(
        connection,
        plan.bindings,
        source_label=plan.source_label,
        timeline=plan.timeline,
        retry=plan.retry,
        freshness_windows=plan.freshness_windows,
        default_freshness_max_age=plan.default_freshness_max_age,
        object_store=_InMemoryObjectStore(),
    )


def _price_obligations(plan: PlannedRun) -> list[tuple[str, object]]:
    """The plan's (work item id, binding) pairs for the market-price semantic."""
    return [(k, v) for k, v in plan.bindings.items() if v.obligation.capture_requirement_id == "market-price:v1"]


def _record_price_bar(
    plan: PlannedRun,
    sink: PostgresCaptureControlSink,
    work_item_id: str,
    binding,
    bar_time: datetime,
) -> None:
    """Record one successful price bar whose own time is `bar_time`."""
    work_item = next(w for w in plan.work_items if w.work_item_id == work_item_id)
    payload = {"listing_id": binding.obligation.subject.id, "close": "100.00", "currency": "USD"}
    success = FetchSuccess(
        raw=RawResponse(
            body=f"bar:{bar_time.date()}:100.00".encode(),
            source=DataSource.YAHOO,
            record_id=f"bar-{bar_time.date()}",
        ),
        normalized_sha256=canonical_sha256(payload),
        confidence=Decimal("0.9"),
        valid_from=bar_time.date(),
        transaction_time=bar_time,
        record=NormalizedRecord(
            payload=payload,
            parser_version="production-topt-live-parser:v4",
            mapping_version="production-topt-live-map:v4",
        ),
    )
    sink.record_outcome(
        work_item,
        attempt_reasons=(None,),
        terminal_state=ObligationTerminalState.SUCCESS,
        success=success,
    )


def test_a_stale_source_grades_stale_at_write_time(connection) -> None:
    """A price bar older than its semantic's 5-day window lands as 'stale' — the
    write-time half of the honest-freshness chain (#530 slice 3)."""
    plan = plan_and_persist(connection, cutoff=CUTOFF, version="test-530-stale-price")
    sink = _hand_driven_sink(connection, plan)
    work_item_id, binding = _price_obligations(plan)[0]
    old_bar = datetime(2026, 3, 20, tzinfo=UTC)  # 13 days before the 04-02 cutoff
    _record_price_bar(plan, sink, work_item_id, binding, old_bar)
    freshness, knowable_at = connection.execute(
        """
        select o.freshness_state, o.knowable_at from raw.capture_obligations ob
        join staging.capture_observation_obligations oo on oo.capture_obligation_id = ob.obligation_id
        join staging.capture_normalized_observations o on o.observation_id = oo.observation_id
        where ob.obligation_id = %s
        """,
        (binding.obligation.obligation_id,),
    ).fetchone()
    assert knowable_at == old_bar
    assert freshness == "stale", "13 days beyond a 5-day window must not grade fresh"


def test_freshness_state_takes_more_than_one_value_across_one_run(connection) -> None:
    """#530 acceptance: the freshness dimension is not a constant.

    Production once held ONE distinct `freshness_state` over 21,717 observations. This
    run records one price bar 13 days before the cutoff and one bar 2 days before it.
    The 5-day price window must grade them differently, and the distinct count over the
    run's observations must show both states. A sink that stamps one literal for every
    row fails this test.
    """
    plan = plan_and_persist(connection, cutoff=CUTOFF, version="test-530-freshness-variety")
    sink = _hand_driven_sink(connection, plan)
    stale_obligation, fresh_obligation = _price_obligations(plan)[:2]
    _record_price_bar(plan, sink, *stale_obligation, datetime(2026, 3, 20, tzinfo=UTC))
    _record_price_bar(plan, sink, *fresh_obligation, datetime(2026, 3, 31, tzinfo=UTC))

    distinct_count, states = connection.execute(
        """
        select count(distinct o.freshness_state), array_agg(distinct o.freshness_state order by o.freshness_state)
        from raw.capture_obligations ob
        join staging.capture_observation_obligations oo on oo.capture_obligation_id = ob.obligation_id
        join staging.capture_normalized_observations o on o.observation_id = oo.observation_id
        where ob.run_id = %s
        """,
        (plan.run_id,),
    ).fetchone()
    assert distinct_count > 1, f"freshness_state is a constant across the run: {states}"
    assert states == ["fresh", "stale"]


def test_the_vintage_axis_reaches_the_served_mart_row(connection) -> None:
    """#530 slice 4: 'how old is the number this row serves' is a SQL question.

    The fixture's fiscal periods (FY-end 2025-12-31, shares cover date 2026-03-15)
    must arrive as typed columns on mart.topt_core_results — the V-2010 incident's
    blind spot was exactly that mart rows carried no period, so a 16-year-old share
    count was indistinguishable from a fresh one without re-deriving from the
    vendor."""
    import datetime as _dt

    plan = _capture(connection, version="test-530-periods")
    core = PostgresToptCoreRepository(connection)
    snapshot = core.freeze_snapshot(run_id=plan.run_id, release_manifest_id=plan.release_manifest_id)
    core.materialize(snapshot, gppe_definition=GppeV0Definition(risk_free_rate="0.05"))

    rows = connection.execute(
        """
        select operating_period_end, revenue_period_end, shares_period_end
        from mart.topt_core_results where run_id = %s
        """,
        (plan.run_id,),
    ).fetchall()
    assert rows
    for operating, revenue, shares in rows:
        assert operating == _dt.date(2025, 12, 31)
        assert revenue == _dt.date(2025, 12, 31)
        assert shares == _dt.date(2026, 3, 15)


def test_the_oracle_catches_the_fixtures_own_impossible_numbers(connection) -> None:
    """#578 integration — and an honest confession: the oracle's first catch is this
    suite's own corpus, in two distinct ways nobody noticed while inventing values.
    Non-financial bundles assert gross_profit 210M over revenue 100M — impossible
    accounting. The financial and insurance bundles pair an invented 80M numerator
    with the REAL seeded headcounts (JPM at 309,926 people), which is $258 per
    employee — below any legitimate issuer's floor. Fixture data that survives this
    oracle now has to be at least arithmetically possible."""
    plan = _capture(connection, version="test-578-oracle")
    report = quality_report.build_report(connection, plan.run_id)
    cells = report["plausibility_cells"]
    assert cells, "financial-fact cells must be graded"
    assert report["implausible_count"] == len(cells) == 21
    reasons = {tuple(cell["violated"]) for cell in cells.values()}
    assert reasons == {("gross_profit_exceeds_revenue",), ("per_employee_outside_domain",)}
