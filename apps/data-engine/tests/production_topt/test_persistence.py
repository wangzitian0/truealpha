"""The executor path populates the capture-control tables the mart reads (#171 A1).

The load-bearing property this milestone turns on: driving the planned obligations
through the generic executor must leave `raw.capture_*` /
`staging.capture_normalized_observations` populated well enough that
`freeze_snapshot` → `materialize` reconstructs the 20-issuer TOPT core — exactly what
the retired monolith did, but with persistence injected instead of inlined.

Real schema, no network: the adapters are the deployed ones, wired to fake fetchers.
"""

from __future__ import annotations

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
from data_engine.datahub.production_topt.executor import SourceFetchPort
from data_engine.datahub.production_topt.headcount import STOPGAP_HEADCOUNTS, StopgapHeadcountExtractor
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
from truealpha_contracts.datahub import ObligationTerminalState

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


def _routes(plan: PlannedRun, *, corroborate: bool) -> dict[str, SourceFetchPort]:
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
        headcount_extractor=StopgapHeadcountExtractor(
            {
                cik_by_ticker[ticker]: Decimal(value)
                for ticker, value in STOPGAP_HEADCOUNTS.items()
                if ticker in cik_by_ticker
            }
        ),
    )
    release = ReleaseDerivedAdapter(release_targets, cutoff=cutoff_date)

    routes: dict[str, SourceFetchPort] = {}
    routes.update(dict.fromkeys(price_targets, price))
    routes.update(dict.fromkeys(sec_targets, financial))
    routes.update(dict.fromkeys(release_targets, release))
    return routes


def _capture(connection, *, version: str, corroborate: bool = False) -> PlannedRun:
    plan = plan_and_persist(connection, cutoff=CUTOFF, version=version)
    sink = PostgresCaptureControlSink(
        connection,
        plan.bindings,
        source_label=plan.source_label,
        timeline=plan.timeline,
        retry=plan.retry,
    )
    report = run_topt_capture(
        plan.run_id,
        plan.work_items,
        _routes(plan, corroborate=corroborate),
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
    plan = _capture(connection, version="test-a1-evidence")
    counts = dict(
        connection.execute(
            """
            select kind, count(*) from staging.evidence_nodes
            where node_id = %s
               or node_id in (
                   select 'raw-fetch:' || payload_sha256 from raw.fetches where source = %s
               )
            group by kind
            """,
            (plan.run_id, plan.source_label),
        ).fetchall()
    )
    assert counts.get("capture_run") == 1
    assert counts.get("raw_fetch", 0) >= 21


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
