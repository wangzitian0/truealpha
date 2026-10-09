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
import logging
import os
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub import quality_report
from data_engine.datahub.evidence_graph_repository import PostgresEvidenceGraphRepository
from data_engine.datahub.production_topt import PostgresToptCoreRepository
from data_engine.datahub.production_topt.capture_orchestration import run_topt_capture
from data_engine.datahub.production_topt.composition import PlannedRun, plan_and_persist
from data_engine.datahub.production_topt.corroboration_audit import corroboration_tally
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
from truealpha_contracts.datahub import ObligationTerminalState
from truealpha_contracts.models import DataSource, RawCapture, RawIngestionEnvelope, RawObjectRef

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


def test_executor_run_reconstructs_the_snapshot_and_mart(connection) -> None:
    plan = _capture(connection, version="test-a1")

    core = PostgresToptCoreRepository(connection)
    snapshot = core.freeze_snapshot(run_id=plan.run_id, release_manifest_id=plan.release_manifest_id)
    assert len(snapshot.members) == 21
    results = core.materialize(snapshot, gppe_definition=GppeV0Definition(risk_free_rate="0.05"))
    assert len(results) == 20
    assert all(result.availability.value == "available" for result in results)
    # The depository-institution branch survives capture: a bank scores through the
    # pre-provision-profit numerator instead of landing missing_gross_profit.
    branches = {result.operating_branch for result in results}
    assert {"financial", "insurance"} <= branches


def test_executor_run_writes_its_own_evidence_nodes(connection) -> None:
    """The run's fetches are found through its vintages, not through a run-scoped source
    string. `raw.fetches.source` is the VENDOR now, precisely so identical bytes collapse
    across ticks — which means "this run's rows" is a linkage question, and asserting it
    through the linkage is what proves the chain is connected."""
    plan = _capture(connection, version="test-a1-evidence")
    counts = dict(
        connection.execute(
            """
            select kind, count(*) from staging.evidence_nodes
            where node_id = %s
               or node_id in (
                   select 'raw-fetch:' || landing.payload_sha256
                   from raw.capture_source_vintages vintage
                   join raw.capture_source_requests request using (source_request_id)
                   join raw.fetches landing on landing.id = vintage.raw_fetch_id
                   join raw.capture_work_items work using (source_request_id)
                   join raw.capture_obligation_work_bindings binding using (work_item_id)
                   join raw.capture_obligations obligation using (obligation_id)
                   where obligation.run_id = %s
               )
            group by kind
            """,
            (plan.run_id, plan.run_id),
        ).fetchall()
    )
    assert counts.get("capture_run") == 1
    assert counts.get("raw_fetch", 0) >= 21


def test_captured_bytes_are_readable_back_through_the_pointer(connection) -> None:
    """The point of the whole landing path: every `raw.fetches` row this run wrote must
    dereference to the bytes it claims, byte for byte.

    Production held 1016 rows and one stored object — pointers into buckets that were
    never created. A row whose object cannot be read is not evidence, so the assertion is
    the read-back, not the row count.
    """
    store = _InMemoryObjectStore()
    plan = _capture(connection, version="test-a1-readback", object_store=store)
    rows = connection.execute(
        """
        select landing.object_uri, landing.payload_sha256, landing.byte_length
        from raw.capture_source_vintages vintage
        join raw.fetches landing on landing.id = vintage.raw_fetch_id
        join raw.capture_work_items work using (source_request_id)
        join raw.capture_obligation_work_bindings binding using (work_item_id)
        join raw.capture_obligations obligation using (obligation_id)
        where obligation.run_id = %s
        """,
        (plan.run_id,),
    ).fetchall()
    assert len(rows) >= 21
    for object_uri, sha256, byte_length in rows:
        assert object_uri.startswith("s3://")
        key = object_uri.removeprefix("s3://").partition("/")[2]
        body = store.objects[key]  # KeyError here means the pointer dangles
        assert hashlib.sha256(body).hexdigest() == sha256
        assert len(body) == byte_length


def test_second_origin_reaches_two_independent_origins(connection) -> None:
    plan = _capture(connection, version="test-a1-recon", corroborate=True)
    report = quality_report.build_report(connection, plan.run_id)
    two_origin_cells = [cell for cell in report["reconciliation_cells"].values() if cell["origin_groups"] >= 2]
    assert len(two_origin_cells) == 21
    assert Decimal(report["independent_reconciliation"]) > 0


class _CorroborationBlipStore(_InMemoryObjectStore):
    """Object storage that fails under ONE vendor's bytes: the second origin's (#885).

    `object_store` is MinIO blinking while Twelve Data's bytes land — the raise the sink
    used to propagate out of `record_outcome`, failing a tick whose primary capture was
    complete. `database` is the same loss surfacing as a failed statement on the tick's
    own transaction, which aborts it: only a savepoint keeps the rest of the tick usable.
    """

    def __init__(self, connection, *, failing: DataSource, mode: str) -> None:
        super().__init__()
        self._connection = connection
        self._failing = failing
        self._mode = mode
        self.refused = 0

    def store(self, capture: RawCapture) -> RawIngestionEnvelope:
        if capture.source is not self._failing:
            return super().store(capture)
        self.refused += 1
        if self._mode == "database":
            self._connection.execute("select 1 / 0")
        raise ConnectionError("object store unavailable")


@pytest.mark.parametrize("mode", ["object_store", "database"])
def test_a_corroboration_that_cannot_be_persisted_never_fails_the_primary(connection, caplog, mode: str) -> None:
    """#885 item 1: every cell's Twelve Data bytes fail to land. The capture still resolves
    all 84 obligations successfully (`_capture` asserts it), each loss is a warning and a
    count, the lost corroboration leaves nothing behind (its savepoint rolled back its
    source request), the NEXT corroboration of the same cell (moomoo) still lands, and
    the run freezes and materializes on the same transaction."""
    store = _CorroborationBlipStore(connection, failing=DataSource.TWELVE_DATA, mode=mode)
    with caplog.at_level(logging.WARNING), corroboration_tally() as tally:
        plan = _capture(connection, version=f"test-885-corroboration-{mode}", corroborate=True, object_store=store)

    assert store.refused == 21
    assert tally.summary() == "corroborations refused 21 (twelve-data persist 21)"
    expected_error = "DivisionByZero" if mode == "database" else "ConnectionError"
    warnings = [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 21
    assert all("twelve-data" in warning and expected_error in warning for warning in warnings)

    parsers = dict(
        connection.execute(
            """
            select o.parser_version, count(*)
            from raw.capture_obligations ob
            join staging.capture_observation_obligations oo on oo.capture_obligation_id = ob.obligation_id
            join staging.capture_normalized_observations o on o.observation_id = oo.observation_id
            where ob.run_id = %s and o.semantic_type = 'market-price'
            group by o.parser_version
            """,
            (plan.run_id,),
        ).fetchall()
    )
    assert "twelve-data-parser:v1" not in parsers
    assert parsers[MOOMOO_KLINE_PARSER_VERSION] == 21, "one lost corroboration must not take the next with it"

    sink = PostgresCaptureControlSink(
        connection,
        plan.bindings,
        source_label=plan.source_label,
        timeline=plan.timeline,
        retry=plan.retry,
        object_store=_InMemoryObjectStore(),
    )
    price_bindings = [
        binding
        for binding in plan.bindings.values()
        if binding.obligation.capture_requirement_id.startswith("market-price")
    ]
    assert len(price_bindings) == 21
    orphaned = [
        request.source_request_id
        for binding in price_bindings
        if (
            request := sink._corroborating_request(
                binding, origin="twelve-data", source=f"twelve-data-{plan.source_label}"
            )
        )
        and connection.execute(
            "select 1 from raw.capture_source_requests where source_request_id = %s", (request.source_request_id,)
        ).fetchone()
    ]
    assert orphaned == [], "the savepoint must roll back the lost corroboration's request"

    report = quality_report.build_report(connection, plan.run_id)
    assert {cell["origin_groups"] for cell in report["reconciliation_cells"].values()} == {2}
    core = PostgresToptCoreRepository(connection)
    snapshot = core.freeze_snapshot(run_id=plan.run_id, release_manifest_id=plan.release_manifest_id)
    assert len(core.materialize(snapshot, gppe_definition=GppeV0Definition(risk_free_rate="0.05"))) == 20


def test_every_bar_field_reaches_two_independent_origins(connection) -> None:
    """The report grades five fields per market-price cell, each from two real
    assertions written through the deployed sink and read back through the deployed
    report — not only the close. This is the standing check behind "several metrics
    at HIGH confidence": each field's `agreed` count is the number of listings whose
    two origins agreed on THAT field."""
    plan = _capture(connection, version="test-ohlcv-recon", corroborate=True)
    report = quality_report.build_report(connection, plan.run_id)
    cells = report["reconciliation_cells"]
    graded = len(cells)
    assert graded == 21
    # Every registered price origin the harness corroborates with asserts the whole bar
    # (twelve-data v3 and moomoo K-line alongside the primary), so each field sees them all.
    origins = len(quality_report.RECONCILIATION_POLICY.source_priority)
    for field in quality_report.PRICE_BAR_FIELDS:
        outcomes = {cell["fields"][field]["outcome"] for cell in cells.values()}
        assert outcomes == {"agreed"}, (field, outcomes)
        assert {cell["fields"][field]["origin_groups"] for cell in cells.values()} == {origins}, field
        assert report["field_reconciliation"][field] == {
            "agreed": graded,
            "cells": graded,
            "share": "1.0000",
            "policy_id": quality_report.FIELD_RECONCILIATION_POLICIES[field].policy_id,
        }
    assert report["field_reconciliation"]["volume"]["policy_id"] != report["field_reconciliation"]["close"]["policy_id"]
    # The headline keys are still the close's grade — what the a1 gate reads.
    assert all(cell["outcome"] == cell["fields"]["close"]["outcome"] for cell in cells.values())


def test_confidence_report_bands_the_captured_run_from_its_origins(connection) -> None:
    """The confidence report's loaders read the same persisted observations the quality
    report grades: with the second price origin wired, every bar field's cell is HIGH from
    the same origins and matches the quality report's grade of that field (#865); every
    dated fundamental the second statements origin also asserts at the primary's period is
    HIGH under the financial policy and matches the quality report's per-field grade
    (#866), while an undated one stays single-origin; headcount, written by one fixture
    producer, is LOW; and the stored confidence column is reported as measured and marked
    unused. Same real schema, same fake vendors — the SQL is what this proves."""
    from data_engine.datahub import confidence_report
    from data_engine.datahub.question_coverage import GovernedHead

    plan = _capture(connection, version="test-confidence", corroborate=True)
    quality_report.persist(connection, quality_report.build_report(connection, plan.run_id))
    head = GovernedHead(universe_id="universe:topt-us-2026-03-31", run_id=plan.run_id, cutoff=CUTOFF)
    report = confidence_report.build_report(
        connection, universe="topt", head=head, executed_at=CUTOFF, environment="test"
    )

    assert report["subjects"] == 21
    close = report["families"]["close"]
    assert (close["high"], close["cells"], close["agreement_rate"]) == (21, 21, "1.0000")
    assert close["origins"] == ["origin:moomoo-kline:v1", "origin:twelve-data:v1", "origin:yahoo:v1"]
    assert report["accuracy"]["close"]["matches_quality_report"] is True
    # Every field of the bar is its own family (#865): the harness's origins assert the whole
    # bar, so each field grades HIGH from the same three origins and matches the quality
    # report's per-field grade — five metrics at HIGH, not one; volume under its own policy.
    for name in quality_report.PRICE_BAR_FIELDS:
        family = report["families"][name]
        assert (family["high"], family["cells"], family["agreement_rate"]) == (21, 21, "1.0000"), name
        assert family["origins"] == close["origins"], name
        assert family["tolerance"] == quality_report.FIELD_RECONCILIATION_POLICIES[name].policy_id, name
        accuracy = report["accuracy"][name]
        assert accuracy["matches_quality_report"] is True and accuracy["quality_report_mismatches"] == [], name
        assert (accuracy["quality_report_cells"], accuracy["agreed"], accuracy["compared"]) == (21, 21, 21), name
    assert report["families"]["volume"]["tolerance"] != close["tolerance"]
    # The fixture bundle dates revenue and the operating numerator, and the statements origin
    # publishes both at that period: HIGH under financial-fact-fusion:v1, matching the quality
    # report field for field. total_assets is undated in the bundle, so nothing can be aligned
    # to it and it is honestly single-origin; net_income is asserted by neither origin.
    for name in ("revenue", "gross_profit"):
        family = report["families"][name]
        assert (family["low"], family["high"], family["medium"]) == (0, 0, 21), name
        assert family["origins"] == ["origin:moomoo-financials:v1", "origin:sec-company-facts:v1"], name
        assert family["tolerance"] == quality_report.FINANCIAL_FACT_RECONCILIATION_POLICY.policy_id, name
        accuracy = report["accuracy"][name]
        assert accuracy["matches_quality_report"] is True and accuracy["quality_report_cells"] == 21, name
        assert (accuracy["agreed"], accuracy["compared"]) == (21, 21), name
    total_assets = report["families"]["total_assets"]
    assert (total_assets["low"], total_assets["reasons"]) == (21, {"single_origin": 21})
    assert total_assets["origins"] == ["origin:sec-company-facts:v1"]
    assert report["accuracy"]["total_assets"]["matches_quality_report"] is None
    assert report["families"]["net_income"]["missing"] == 21
    headcount = report["families"]["headcount"]
    assert headcount["low"] == 21 and headcount["origins"] == ["origin:headcount:test-fixture"]
    # The financial branch's numerator is the only one filled for a bank; the others are
    # honest gaps, not zeros.
    assert report["families"]["pre_provision_profit"]["low"] == 1
    assert report["families"]["pre_provision_profit"]["missing"] == 20
    # TOPT is not a filing fund: neither plane family is graded, rather than graded empty.
    assert {"index_membership", "etf_weight"}.isdisjoint(report["families"])
    assert set(report["sources_connected"]) == {"moomoo", "sec-company-facts", "test-fixture", "twelve-data", "yahoo"}
    # Measured from the run's own rows (whether a semantic's stamp is constant is a fact
    # about the data, asserted on production, not here) and never read by the bands.
    stored = report["metadata"]["stored_confidence"]
    assert stored["used_for_bands"] is False
    assert set(stored["values_by_semantic"]) == {
        "financial-fact",
        "listing-identity",
        "market-price",
        "universe-membership",
    }
    # The sample names TOPT issuers the harness captured, with a value from each origin.
    sample = report["sample"]["listing:xnys:jpm"]
    assert sample["in_universe"] and sample["close"]["verdict"] == "high" and sample["volume"]["verdict"] == "high"
    assert set(sample["close"]["values"]) == {"origin:moomoo-kline:v1", "origin:twelve-data:v1", "origin:yahoo:v1"}
    assert report["accuracy"]["sec_oracle"]["reason"] == "no_sec_user_agent"
    report_id = confidence_report.persist(connection, report)
    assert report_id.startswith("datahub-confidence-report:")
    # #885: the next night's compile over the same head grades the same content — the same
    # row, not one more per night.
    recompiled = confidence_report.build_report(
        connection, universe="topt", head=head, executed_at=CUTOFF + timedelta(days=1), environment="test"
    )
    assert recompiled["generated_at"] != report["generated_at"]
    assert confidence_report.persist(connection, recompiled) == report_id
    assert connection.execute(
        "select count(*) from mart.datahub_confidence_report where run_id = %s", (plan.run_id,)
    ).fetchone() == (1,)


def test_moomoo_origins_reach_the_report_as_their_own_assertions(connection) -> None:
    """The third price origin and the second financial-fact origin land through the
    deployed sink — their own request, vintage, raw object under moomoo's prefix and
    observation — and the report reconciles them: every price cell sees three origin
    groups, every financial-fact cell agrees per dated field under the financial policy,
    and the agreed financial subjects are their own KPI beside the price one."""
    plan = _capture(connection, version="test-moomoo-origins", corroborate=True)
    report = quality_report.build_report(connection, plan.run_id)

    assert all(cell["origin_groups"] == 3 for cell in report["reconciliation_cells"].values())
    assert (
        report["financial_fact_reconciliation_policy_id"]
        == quality_report.FINANCIAL_FACT_RECONCILIATION_POLICY.policy_id
    )
    financial = report["financial_fact_reconciliation_cells"]
    assert len(financial) == 21
    for listing_id, cell in financial.items():
        assert cell["outcome"] == "agreed", (listing_id, cell)
        # The fixture bundle dates revenue and the operating numerator; total_assets and
        # net_income carry no vintage there, so exactly those two fields are compared.
        assert set(cell["fields"]) == {"revenue", "gross_profit"}, (listing_id, cell)
        assert all(
            graded["origin_groups"] == 2 and graded["period_end"] == "2025-12-31" for graded in cell["fields"].values()
        )
    # The headline KPI stays the close's (what the pointer gate and dashboards read);
    # the financial-fact agreement is reported beside it, never folded in.
    assert report["independently_reconciled_count"] == 21
    assert report["financial_fact_independently_reconciled_count"] == 21
    assert report["financial_fact_independent_reconciliation"] == "1.0000"

    landed = connection.execute(
        """
        select f.source, count(distinct o.observation_id)
        from raw.capture_obligations ob
        join staging.capture_observation_obligations oo on oo.capture_obligation_id = ob.obligation_id
        join staging.capture_normalized_observations o on o.observation_id = oo.observation_id
        join raw.capture_source_vintages v on v.source_vintage_id = o.source_vintage_id
        join raw.fetches f on f.id = v.raw_fetch_id
        where ob.run_id = %s and o.parser_version like 'moomoo-%%'
        group by f.source
        """,
        (plan.run_id,),
    ).fetchall()
    assert dict(landed) == {"moomoo": 42}, "21 K-line + 21 statements observations, each under moomoo's prefix"


def _cell_objects(connection, run_id: str) -> list[tuple[str, str]]:
    """(obligation_id, object_uri) for every landed pointer this run's cells rest on."""
    return connection.execute(
        """
        select distinct ob.obligation_id, landing.object_uri
        from raw.capture_obligations ob
        join staging.capture_observation_obligations oo on oo.capture_obligation_id = ob.obligation_id
        join staging.capture_normalized_observations o on o.observation_id = oo.observation_id
        join raw.capture_source_vintages vintage on vintage.source_vintage_id = o.source_vintage_id
        join raw.fetches landing on landing.id = vintage.raw_fetch_id
        where ob.run_id = %s
        """,
        (run_id,),
    ).fetchall()


def _a_pointer_only_one_cell_rests_on(connection, run_id: str) -> str:
    """An object URI that exactly one cell depends on, and that cell on nothing else.

    Deleting it must move `lineage_completeness` by exactly one cell, which is what makes
    the harness a measurement rather than a smoke test.
    """
    pairs = _cell_objects(connection, run_id)
    objects_per_cell: dict[str, set[str]] = {}
    cells_per_object: dict[str, set[str]] = {}
    for obligation_id, object_uri in pairs:
        objects_per_cell.setdefault(obligation_id, set()).add(object_uri)
        cells_per_object.setdefault(object_uri, set()).add(obligation_id)
    for object_uri, cells in sorted(cells_per_object.items()):
        if len(cells) == 1 and len(objects_per_cell[next(iter(cells))]) == 1:
            return object_uri
    raise AssertionError("no pointer is exclusive to a single cell; the harness cannot isolate one")
