"""Cell confidence tick persistence and adapter zero-stamping invariants (#1517 B3 & B4)."""

from __future__ import annotations

import os
from datetime import UTC, date
from decimal import Decimal

import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub.confidence_report import evaluate_and_persist_run_cell_confidence
from data_engine.datahub.production_topt import PostgresToptCoreRepository
from data_engine.datahub.production_topt.source_registrations import (
    SOURCE_RELIABILITY_PRIORS,
    source_reliability_prior,
)
from data_engine.datahub.strategy_bridge import seed_strategy_inputs_from_capture
from factors.production_topt import GppeV0Definition

from .test_persistence import CUTOFF, _capture


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


def test_every_registered_source_declares_a_reliability_prior_in_order() -> None:
    """#1517 B1: release-derived (1) > SEC/Treasury/index (0.98) > moomoo (0.96)
    > Twelve Data (0.95) > yfinance (0.90) > single-source prose extraction (0.88)."""
    assert source_reliability_prior("release-derived") == Decimal("1")
    assert source_reliability_prior("sec-company-facts") == Decimal("0.98")
    assert source_reliability_prior("us-treasury-par-yield") == Decimal("0.98")
    assert source_reliability_prior("origin:nasdaq-index:v1") == Decimal("0.98")
    assert source_reliability_prior("origin:moomoo-kline:v1") == Decimal("0.96")
    assert source_reliability_prior("origin:moomoo-financials:v1") == Decimal("0.96")
    assert source_reliability_prior("origin:twelve-data:v1") == Decimal("0.95")
    assert source_reliability_prior("origin:yahoo:v1") == Decimal("0.90")
    assert source_reliability_prior("10k-extraction") == Decimal("0.88")
    assert (
        SOURCE_RELIABILITY_PRIORS["release-derived"]
        > SOURCE_RELIABILITY_PRIORS["sec-company-facts"]
        > SOURCE_RELIABILITY_PRIORS["moomoo"]
        > SOURCE_RELIABILITY_PRIORS["twelvedata"]
        > SOURCE_RELIABILITY_PRIORS["yahoo"]
        > SOURCE_RELIABILITY_PRIORS["10k-extraction"]
    )


def test_snapshot_member_financial_cell_confidence_bounds_by_deciding_keys_only() -> None:
    """#1517 / #36: non-deciding optional fields (such as financial_assets, financial_returns, or net_income)
    do not drag financial_cell_confidence down when input_age.deciding is present."""
    from datetime import datetime, timedelta

    from data_engine.datahub.production_topt.materialization import InputAgeLimit, _ObservationRow
    from factors.production_topt import MetricFreshness

    cutoff = datetime(2026, 4, 2, 12, 0, tzinfo=UTC)
    obs_id = f"normalized-observation:{'1' * 64}"
    obs_mem = f"normalized-observation:{'2' * 64}"
    obs_fin = f"normalized-observation:{'3' * 64}"
    obs_mkt = f"normalized-observation:{'4' * 64}"
    by_type = {
        "listing-identity": _ObservationRow(
            obligation_id="ob:id",
            listing_id="listing:xnas:aapl",
            semantic_type="listing-identity",
            observation_id=obs_id,
            freshness=MetricFreshness.FRESH,
            knowable_at=cutoff,
            payload={
                "issuer_id": "issuer:cik:0000320193",
                "instrument_id": "instrument:xnas:aapl",
                "listing_id": "listing:xnas:aapl",
                "ticker": "AAPL",
            },
        ),
        "universe-membership": _ObservationRow(
            obligation_id="ob:mem",
            listing_id="listing:xnas:aapl",
            semantic_type="universe-membership",
            observation_id=obs_mem,
            freshness=MetricFreshness.FRESH,
            knowable_at=cutoff,
            payload={
                "issuer_id": "issuer:cik:0000320193",
                "instrument_id": "instrument:xnas:aapl",
                "listing_id": "listing:xnas:aapl",
                "ticker": "AAPL",
            },
        ),
        "financial-fact": _ObservationRow(
            obligation_id="ob:fin",
            listing_id="listing:xnas:aapl",
            semantic_type="financial-fact",
            observation_id=obs_fin,
            freshness=MetricFreshness.FRESH,
            knowable_at=cutoff,
            payload={
                "issuer_id": "issuer:cik:0000320193",
                "instrument_id": "instrument:xnas:aapl",
                "listing_id": "listing:xnas:aapl",
                "operating_branch": "non_financial",
                "currency": "USD",
                "gross_profit": "180000000000",
                "total_assets": "350000000000",
                "headcount": "164000",
                "revenue": "390000000000",
                "shares_outstanding": "15000000000",
                "pre_provision_profit": None,
                "financial_assets": "50000000000",
                "financial_returns": "2000000000",
            },
        ),
        "market-price": _ObservationRow(
            obligation_id="ob:mkt",
            listing_id="listing:xnas:aapl",
            semantic_type="market-price",
            observation_id=obs_mkt,
            freshness=MetricFreshness.FRESH,
            knowable_at=cutoff,
            payload={
                "issuer_id": "issuer:cik:0000320193",
                "instrument_id": "instrument:xnas:aapl",
                "listing_id": "listing:xnas:aapl",
                "currency": "USD",
                "close": "220.00",
            },
        ),
    }
    input_age = InputAgeLimit(
        cutoff=cutoff,
        maximum_age=timedelta(days=550),
        deciding={
            "non_financial": frozenset({"gross_profit", "total_assets", "revenue", "shares_outstanding", "headcount"})
        },
    )
    listing_confidences = {
        "gross_profit": Decimal("0.85"),
        "total_assets": Decimal("0.82"),
        "revenue": Decimal("0.88"),
        "shares_outstanding": Decimal("0.625807"),
        "headcount": Decimal("0.55"),
        "financial_assets": Decimal("0.36"),
        "financial_returns": Decimal("0.36"),
        "close": Decimal("0.91"),
    }
    member = PostgresToptCoreRepository._snapshot_member(
        "listing:xnas:aapl",
        by_type,
        histories=(),
        input_age=input_age,
        listing_confidences=listing_confidences,
    )
    fin_cell = next(c for c in member.cell_inputs if c.input_id == obs_fin)
    assert fin_cell.confidence == Decimal("0.625807")

    # For OperatingBranch.FINANCIAL (banks), revenue is not consumed by PPNR/TCE GPPE and must not
    # drag financial_cell_confidence down (#1624).
    bank_by_type = dict(by_type)
    bank_by_type["financial-fact"] = _ObservationRow(
        obligation_id="ob:fin",
        listing_id="listing:xnas:aapl",
        semantic_type="financial-fact",
        observation_id=obs_fin,
        freshness=MetricFreshness.FRESH,
        knowable_at=cutoff,
        payload={
            **by_type["financial-fact"].payload,
            "operating_branch": "financial",
            "pre_provision_profit": "30000000000",
        },
    )
    bank_age = InputAgeLimit(
        cutoff=cutoff,
        maximum_age=timedelta(days=550),
        deciding={
            "financial": frozenset(
                {"pre_provision_profit", "total_assets", "revenue", "shares_outstanding", "headcount"}
            )
        },
    )
    bank_member = PostgresToptCoreRepository._snapshot_member(
        "listing:xnas:aapl",
        bank_by_type,
        histories=(),
        input_age=bank_age,
        listing_confidences={
            **listing_confidences,
            "pre_provision_profit": Decimal("0.85"),
            "revenue": Decimal("0.36"),
        },
    )
    bank_fin_cell = next(c for c in bank_member.cell_inputs if c.input_id == obs_fin)
    assert bank_fin_cell.confidence == Decimal("0.625807")


def test_capture_leaves_observation_confidence_null_and_persists_cell_confidence_to_mart(
    connection,
) -> None:
    """#1517 B3 & B4: new capture rows leave `staging.capture_normalized_observations.confidence`
    NULL; `evaluate_and_persist_run_cell_confidence` populates `mart.cell_confidence` with
    policy_hash, input_hash, and reason_codes; core snapshot and strategy bridge consume those
    evaluated cell confidences."""
    from split_series_seed import store_flat_topt_series

    plan = _capture(connection, version="test-1517-b3-b4", corroborate=True)
    store_flat_topt_series(connection, last=date(2026, 4, 2))

    non_null_obs = connection.execute(
        """
        select count(*)
        from raw.capture_obligations ob
        join staging.capture_observation_obligations oo on oo.capture_obligation_id = ob.obligation_id
        join staging.capture_normalized_observations o on o.observation_id = oo.observation_id
        where ob.run_id = %s and o.confidence is not null
        """,
        (plan.run_id,),
    ).fetchone()[0]
    assert non_null_obs == 0

    cell_confs = evaluate_and_persist_run_cell_confidence(connection, plan.run_id, cutoff=CUTOFF)
    assert cell_confs

    persisted_count, null_hash_count = connection.execute(
        """
        select count(*),
               count(*) filter (where policy_hash = '' or input_hash = '' or cardinality(reason_codes) = 0)
        from mart.cell_confidence
        where run_id = %s
        """,
        (plan.run_id,),
    ).fetchone()
    assert persisted_count >= 21 * 8
    assert null_hash_count == 0

    core = PostgresToptCoreRepository(connection)
    snapshot = core.freeze_snapshot(run_id=plan.run_id, release_manifest_id=plan.release_manifest_id)
    results = core.materialize(snapshot, gppe_definition=GppeV0Definition(risk_free_rate="0.05"))
    assert len(results) == 20

    for member in snapshot.members:
        expected_close_conf = cell_confs[(member.listing_id, "close")]
        assert member.market_price.confidence == expected_close_conf
        assert Decimal("0.5") < member.market_price.confidence < Decimal("1.0")
    gppe_confs = connection.execute(
        "select confidence from mart.topt_gppe_results where run_id = %s and availability = 'available'",
        (plan.run_id,),
    ).fetchall()
    assert gppe_confs and all(Decimal("0.5") < c < Decimal("1.0") for (c,) in gppe_confs)

    connection.execute("delete from staging.strategy_backtest_inputs where cutoff_at = %s", (CUTOFF,))
    written = seed_strategy_inputs_from_capture(connection, plan.run_id, cutoff=CUTOFF)
    assert written > 0
    seeded_closes = connection.execute(
        """
        select value, confidence
        from staging.strategy_backtest_inputs
        where cutoff_at = %s and input_key = 'last_close'
        """,
        (CUTOFF,),
    ).fetchall()
    assert len(seeded_closes) == 20
    assert all(Decimal("0.5") < conf < Decimal("1.0") for _val, conf in seeded_closes)
