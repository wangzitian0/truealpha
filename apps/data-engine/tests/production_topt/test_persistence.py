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
from datetime import UTC, date, datetime
from decimal import Decimal

import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub import quality_report
from data_engine.datahub.evidence_graph_repository import PostgresEvidenceGraphRepository
from data_engine.datahub.production_topt import PostgresToptCoreRepository
from data_engine.datahub.production_topt.capture_orchestration import run_topt_capture
from data_engine.datahub.production_topt.composition import PlannedRun, plan_and_persist
from data_engine.datahub.production_topt.executor import FetchSuccess, RawResponse, SourceFetchPort
from data_engine.datahub.production_topt.headcount import PostgresHeadcountExtractor, record_headcount
from data_engine.datahub.production_topt.market_price_adapter import (
    CorroboratingOrigin,
    MarketPriceAdapter,
    MarketPriceQuote,
    MarketPriceTarget,
)
from data_engine.datahub.production_topt.persistence import PostgresCaptureControlSink
from data_engine.datahub.production_topt.release_derived_adapter import (
    ReleaseDerivedAdapter,
    ReleaseDerivedRecord,
)
from data_engine.datahub.production_topt.sec_financial_adapter import (
    FinancialFactsBundle,
    SecFinancialFactAdapter,
    SecTarget,
)
from factors.production_topt import GppeV0Definition, OperatingBranch
from truealpha_contracts import ObligationReasonCode, RawCapture, RawIngestionEnvelope, RawObjectRef
from truealpha_contracts.datahub import ObligationTerminalState
from truealpha_contracts.models import DataSource

CUTOFF = datetime(2026, 4, 2, tzinfo=UTC)
# One TOPT issuer is a depository institution; the rest are not (SEC SIC 6021).
_BANK_TICKER = "JPM"


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


def _quote(close: str) -> MarketPriceQuote:
    day = date(2026, 3, 31)
    return MarketPriceQuote(
        raw_bytes=f"bar:{day}:{close}".encode(),
        close=Decimal(close),
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


def _routes(plan: PlannedRun, *, corroborate: bool, headcount_extractor=None) -> dict[str, SourceFetchPort]:
    """The deployed adapters over fake fetchers, routed exactly as the composition root does."""
    cutoff_date = CUTOFF.date()
    price_targets: dict[str, MarketPriceTarget] = {}
    sec_targets: dict[str, SecTarget] = {}
    release_targets: dict[str, ReleaseDerivedRecord] = {}
    cik_by_ticker: dict[str, int] = {}
    for work_item_id, binding in plan.bindings.items():
        semantic_type = binding.obligation.capture_requirement_id.removesuffix(":v1")
        issuer_id, instrument_id, listing_id, ticker = plan.coordinates[binding.obligation.subject.id]
        cik = 100000 + sorted(plan.coordinates).index(listing_id)
        cik_by_ticker.setdefault(ticker, cik)
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

    second_origin = CorroboratingOrigin(
        origin="twelve-data",
        parser_version="twelve-data-parser:v1",
        mapping_version="twelve-data-map:v1",
        value_key="price",
        confidence=Decimal("0.85"),
        fetch=lambda symbol, cutoff: _quote("40"),
    )
    price = MarketPriceAdapter(
        price_targets,
        lambda symbol, cutoff: _quote("40"),
        corroborating_origins=(second_origin,) if corroborate else (),
    )
    financial = SecFinancialFactAdapter(
        sec_targets,
        lambda cik, cutoff, branch: _bundle(branch),
        headcount_extractor=headcount_extractor,
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
    for listing_id in sorted({coordinate[2] for coordinate in plan.coordinates.values()}):
        cik = 100000 + sorted(plan.coordinates).index(listing_id)
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
    connection, *, version: str, corroborate: bool = False, object_store: _InMemoryObjectStore | None = None
) -> PlannedRun:
    plan = plan_and_persist(connection, cutoff=CUTOFF, version=version)
    _seed_headcounts(connection, plan)
    sink = PostgresCaptureControlSink(
        connection,
        plan.bindings,
        source_label=plan.source_label,
        timeline=plan.timeline,
        retry=plan.retry,
        object_store=object_store or _InMemoryObjectStore(),
    )
    report = run_topt_capture(
        plan.run_id,
        plan.work_items,
        _routes(plan, corroborate=corroborate, headcount_extractor=PostgresHeadcountExtractor(connection)),
        PostgresEvidenceGraphRepository(connection),
        sink=sink,
        cutoff=CUTOFF,
        recorded_at=CUTOFF,
    )
    assert not report.halted
    assert report.total == 84
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
    assert "financial" in branches


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
