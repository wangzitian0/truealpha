"""The live tick reads one measured risk-free rate for the core engine and the strategy (#1255).

These tests drive the deployed Dagster op `run_topt_live_tick` end to end, as `test_live_input_age`
does. The vendor answers are offline and pass through the deployed adapters. The capture, freeze,
materialization, strategy seed, replay and plausibility gate are real. The rate comes from
`staging.risk_free_rates_daily`, which each test fills before the tick.

Every issuer earns 210,000,000 of gross profit on 200,000,000 of assets with 164,000 employees.
JPM, the bank, has its own metric and is not read here. The labor efficiency of the others is
(210,000,000 - 200,000,000 x rate) / 164,000. The tests compute it with their own arithmetic.

The tick runs at 22:15 UTC on 2026-04-01. The Treasury value of the day before, 2026-03-31, is the
rate that a decision may use. The table also holds three rows that no decision may use. One is an
older day. One is the day of the tick itself, which is knowable only at 2026-04-02 00:00 UTC.
One is a later day.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path

import dagster as dg
import psycopg
import pytest
from data_engine import raw_store
from data_engine.config import settings
from data_engine.datahub.market_prices import DEFAULT_TOPT_SYMBOLS
from data_engine.datahub.production_topt import composition
from data_engine.datahub.production_topt.composition import PlannedRun
from data_engine.datahub.production_topt.executor import SourceFetchPort
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
from data_engine.datahub.repository import PostgresCaptureControlRepository
from data_engine.datahub.risk_free import MissingRiskFreeRate
from factors.production_topt import OperatingBranch
from truealpha_runtime.testing import InMemoryRawObjectStore, isolated_test_database

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "support"))
from risk_free_seed import seed_risk_free_rate  # noqa: E402
from split_series_seed import share_count_vintage, store_flat_daily_series  # noqa: E402

SESSION = date(2026, 3, 31)
TICK = datetime(2026, 4, 1, 22, 15, tzinfo=UTC)
CLOSE = Decimal("40")
FILED_SHARES = Decimal("10000000")
GROSS_PROFIT = Decimal("210000000")
TOTAL_ASSETS = Decimal("200000000")
HEADCOUNT = Decimal("164000")
FILED = date(2026, 2, 1)
COVER_DATE = date(2026, 1, 20)
SERIES_START = date(2025, 12, 1)
_BANK_TICKER = "JPM"

USED = Decimal("0.0426")
#: What the table holds in the first scenario. Only 2026-03-31 may decide.
STORED = {
    date(2026, 3, 30): "0.0300",  # older: the newest knowable row wins
    date(2026, 3, 31): "0.0426",  # the day before the tick: the answer
    date(2026, 4, 1): "0.0999",  # the day of the tick: knowable at 2026-04-02 00:00 UTC
    date(2026, 4, 10): "0.0777",  # after the tick
}
#: Constants that earlier code used. No decision may equal a result computed with one of them.
OLD_CONSTANTS = (Decimal("0.05"), Decimal("0.0380"), Decimal("0.0365"))


def labor_efficiency(rate: Decimal) -> Decimal:
    """The value of the non-financial metric, from the definition's formula and 2-place quantization."""
    return ((GROSS_PROFIT - TOTAL_ASSETS * rate) / HEADCOUNT).quantize(Decimal("0.01"), ROUND_HALF_EVEN)


def _bundle(branch: OperatingBranch, *, cik: int) -> FinancialFactsBundle:
    financial = branch is OperatingBranch.FINANCIAL
    accession = f"0000{cik}-26-000001"
    entry = {"end": COVER_DATE.isoformat(), "val": int(FILED_SHARES), "accn": accession, "filed": FILED.isoformat()}
    entry |= {"form": "10-K", "fy": 2025, "fp": "FY"}
    facts = {"facts": {"dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [entry]}}}}}
    return FinancialFactsBundle(
        gross_profit=Decimal("80000000") if financial else GROSS_PROFIT,
        total_assets=TOTAL_ASSETS,
        shares_outstanding=FILED_SHARES,
        revenue=Decimal("100000000"),
        pre_provision_profit=Decimal("80000000") if financial else None,
        raw_bytes=json.dumps(facts).encode(),
        knowable_at=datetime.combine(FILED, datetime.min.time(), tzinfo=UTC),
        shares_period_end=COVER_DATE,
        vintages={"shares_outstanding": share_count_vintage(filed=FILED, own_date=COVER_DATE, accession=accession)},
    )


def _routes(plan: PlannedRun, connection) -> dict[str, SourceFetchPort]:
    """The deployed adapters over fixed vendor answers, routed as the composition root routes them."""
    cutoff_date = plan.timeline.partition_start.date()
    price_targets: dict[str, MarketPriceTarget] = {}
    sec_targets: dict[str, SecTarget] = {}
    release_targets: dict[str, ReleaseDerivedRecord] = {}
    cik_by_ticker: dict[str, int] = {}
    for work_item_id, binding in plan.bindings.items():
        semantic_type = binding.obligation.capture_requirement_id.removesuffix(":v1")
        subject_id = binding.obligation.subject.id
        issuer_id, instrument_id, listing_id, ticker = plan.coordinates[subject_id]
        cik_by_ticker.setdefault(ticker, 100000 + sorted(plan.coordinates).index(subject_id))
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
                operating_branch=OperatingBranch.FINANCIAL if ticker == _BANK_TICKER else OperatingBranch.NON_FINANCIAL,
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
            headcount=HEADCOUNT,
            knowable_at=datetime(2026, 1, 1, tzinfo=UTC),
            source="test-fixture",
            evidence_ref=f"test:{ticker}",
            confidence=Decimal("0.7"),
        )

    def quote(symbol: str, cutoff: date) -> MarketPriceQuote:
        return MarketPriceQuote(
            raw_bytes=f"bar:{symbol}:{cutoff.isoformat()}:{CLOSE}".encode(),
            close=CLOSE,
            as_of=cutoff,
            knowable_at=datetime.combine(cutoff, datetime.min.time(), tzinfo=UTC),
        )

    price = MarketPriceAdapter(price_targets, quote)
    financial = SecFinancialFactAdapter(
        sec_targets,
        lambda cik, cutoff, branch: _bundle(branch, cik=cik),
        headcount_extractor=PostgresHeadcountExtractor(connection),
    )
    release = ReleaseDerivedAdapter(release_targets, cutoff=cutoff_date)
    routes: dict[str, SourceFetchPort] = {}
    routes.update(dict.fromkeys(price_targets, price))
    routes.update(dict.fromkeys(sec_targets, financial))
    routes.update(dict.fromkeys(release_targets, release))
    return routes


@dataclass
class World:
    """One isolated database with the offline vendors. `route_builds` counts the captures that reached a vendor."""

    url: str
    route_builds: list[int]

    def tick(self) -> dict:
        """The deployed op, as the schedule launches it."""
        from data_engine.lanes.capture import ToptLiveTickConfig, run_topt_live_tick

        context = dg.build_op_context()
        run_topt_live_tick(context, ToptLiveTickConfig(executed_at=TICK.isoformat()))
        return dict(context.get_output_metadata("result"))

    def store_rates(self, rows: dict[date, str]) -> dict[date, int]:
        """Commit the rows. Return the `raw.fetches` id behind each one."""
        with psycopg.connect(self.url) as connection:
            fetch_ids = {day: seed_risk_free_rate(connection, day, rate) for day, rate in rows.items()}
            connection.commit()
        return fetch_ids

    def run_plans(self) -> set[str]:
        """The ids of the runs that have a committed run plan, which carries the build identity."""
        with psycopg.connect(self.url) as reader:
            return {row[0] for row in reader.execute("select run_id from raw.production_topt_run_plans")}

    def scalar(self, query: str, *params: object) -> object:
        with psycopg.connect(self.url) as reader:
            return reader.execute(query, params).fetchone()[0]


@pytest.fixture
def world() -> Iterator[World]:
    from data_engine.datahub import a1_evidence
    from truealpha_runtime import EnvironmentTier as RuntimeEnvironmentTier

    with isolated_test_database("riskfreetick") as url, pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(settings, "database_url", url)
        monkeypatch.setattr(settings, "environment_tier", RuntimeEnvironmentTier.PRODUCTION)
        for name, value in {
            "twelve_data_api_key": "",
            "moomoo_kline_origin_enabled": False,
            "moomoo_financials_origin_enabled": False,
        }.items():
            monkeypatch.setattr(settings, name, value)
        store = InMemoryRawObjectStore()
        monkeypatch.setattr(raw_store, "object_store", lambda: store)
        route_builds: list[int] = []

        def build_routes(plan: PlannedRun, connection) -> dict[str, SourceFetchPort]:
            route_builds.append(1)
            return _routes(plan, connection)

        monkeypatch.setattr(composition, "build_routes", build_routes)
        monkeypatch.setattr(a1_evidence, "unmet_objectives", lambda *_args, **_kwargs: ())
        with psycopg.connect(url) as connection:
            store_flat_daily_series(connection, DEFAULT_TOPT_SYMBOLS, first=SERIES_START, last=SESSION, close=CLOSE)
            connection.commit()
        yield World(url, route_builds)


def _decision(world: World, metadata: dict, ticker: str) -> tuple[str, Decimal | None, str | None]:
    """(outcome, labor efficiency, exclusion reason) that the strategy stored for one issuer."""
    with psycopg.connect(world.url) as reader:
        issuer_id = reader.execute(
            """
            select distinct p.normalized_payload ->> 'issuer_id'
            from raw.capture_obligations ob
            join staging.capture_observation_obligations oo on oo.capture_obligation_id = ob.obligation_id
            join staging.capture_observation_payloads p on p.observation_id = oo.observation_id
            where ob.run_id = %s and p.normalized_payload ->> 'ticker' = %s
            """,
            (metadata["capture_run_id"], ticker),
        ).fetchone()[0]
        return reader.execute(
            "select outcome, capital_adjusted_labor_efficiency, exclusion_reason from mart.strategy_decisions "
            "where strategy_run_id = %s and issuer_id = %s",
            (metadata["strategy_run_id"], issuer_id),
        ).fetchone()


# -- the stored rate decides -----------------------------------------------------------------------


def test_the_tick_reports_the_lineage_of_the_rate_it_read(world: World) -> None:
    fetch_ids = world.store_rates(STORED)

    metadata = world.tick()

    assert metadata["risk_free_rate"] == "0.0426"
    assert metadata["risk_free_observation_date"] == "2026-03-31"
    assert metadata["risk_free_fetch_id"] == fetch_ids[date(2026, 3, 31)]


def test_every_strategy_decision_uses_the_stored_rate_of_the_previous_business_day(world: World) -> None:
    """Red when the tick reads a constant, because the old ones differ from 0.0426.
    Red when it reads the rate of its own day, of a later day or of an older day, because those differ too."""
    world.store_rates(STORED)
    metadata = world.tick()

    tickers = [ticker for ticker in DEFAULT_TOPT_SYMBOLS if ticker != _BANK_TICKER]
    assert len(tickers) == 19
    for ticker in tickers:
        outcome, value, reason = _decision(world, metadata, ticker)
        assert value == labor_efficiency(USED) == Decimal("1228.54"), (ticker, outcome, reason)
    for other in (*OLD_CONSTANTS, Decimal("0.0300"), Decimal("0.0999"), Decimal("0.0777")):
        assert labor_efficiency(other) != labor_efficiency(USED), f"the fixture cannot tell {other} from {USED}"


def test_the_core_engine_materialized_with_the_same_rate(world: World) -> None:
    """One tick, one rate: the GPPE definition of the core results carries the rate that the strategy used."""
    world.store_rates(STORED)
    metadata = world.tick()

    with psycopg.connect(world.url) as reader:
        rows = reader.execute(
            """
            select r.issuer_id, r.gppe, i.payload -> 'gppe_definition' ->> 'risk_free_rate'
            from mart.topt_gppe_results r join mart.topt_gppe_invocations i using (invocation_id)
            where r.run_id = %s and r.availability = 'available' and r.operating_branch = 'non_financial'
            """,
            (metadata["capture_run_id"],),
        ).fetchall()

    assert len(rows) == 19
    for _issuer, gppe, rate_in_definition in rows:
        assert Decimal(rate_in_definition) == USED
        assert abs(gppe - (GROSS_PROFIT - TOTAL_ASSETS * USED) / HEADCOUNT) < Decimal("1e-9")
    assert metadata["pointer_advanced"] is True


def test_another_stored_rate_gives_another_decision(world: World) -> None:
    """The same world with 0.0500 stored for the previous business day: the value follows the table."""
    world.store_rates({date(2026, 3, 31): "0.0500"})

    metadata = world.tick()

    assert _decision(world, metadata, "AAPL")[1] == labor_efficiency(Decimal("0.05")) == Decimal("1219.51")
    assert metadata["risk_free_rate"] == "0.0500"


# -- no usable rate: the tick fails closed after the evidence is kept --------------------------------


def test_a_tick_without_a_rate_in_ten_days_fails_with_the_named_error_and_publishes_nothing(world: World) -> None:
    """The newest row is 17 days old. The capture is committed first, so the evidence is kept and the
    build identity is stamped. Nothing is frozen, materialized, bound to a strategy run or served."""
    world.store_rates({date(2026, 3, 15): "0.0420"})
    before = {
        table: world.scalar(f"select count(*) from {table}")
        for table in ("mart.topt_core_results", "mart.strategy_runs", "mart.strategy_decisions")
    }
    heads_before = world.scalar("select count(*) from mart.current_pointer_head")
    plans_before = world.run_plans()

    with pytest.raises(
        MissingRiskFreeRate, match=r"missing_risk_free_rate_parameter: .*newest knowable observation: 2026-03-15"
    ):
        world.tick()

    (run_id,) = world.run_plans() - plans_before  # one new plan: the build identity is stamped
    with psycopg.connect(world.url) as reader:
        status = PostgresCaptureControlRepository(reader).status(run_id)
    assert status.complete and status.success_count == status.obligation_count > 0, "the capture is kept"
    after = {table: world.scalar(f"select count(*) from {table}") for table in before}
    assert after == before, "no core result, no strategy run and no decision exists"
    assert world.scalar("select count(*) from mart.current_pointer_head") == heads_before, "the pointer did not move"


def test_the_retry_of_the_same_tick_resumes_the_kept_capture_when_the_rate_has_arrived(world: World) -> None:
    world.store_rates({date(2026, 3, 15): "0.0420"})
    with pytest.raises(MissingRiskFreeRate):
        world.tick()
    assert world.route_builds == [1], "the first tick reached the vendors"

    world.store_rates({date(2026, 3, 31): "0.0426"})
    metadata = world.tick()

    assert world.route_builds == [1], "the retry built no route: the kept capture was resumed, not fetched again"
    assert metadata["risk_free_rate"] == "0.0426" and metadata["pointer_advanced"] is True
    assert _decision(world, metadata, "AAPL")[1] == labor_efficiency(USED)
