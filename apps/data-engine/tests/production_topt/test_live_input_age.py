"""The live tick enforces the input age limit of the strategy definition (#1255).

The definition declares `eligibility.maximum_input_age_days` (455). These tests drive the deployed
Dagster op `run_topt_live_tick` end to end. The vendor answers are offline and pass through the deployed
adapters. The capture, freeze, materialization, strategy seed, replay and plausibility gate are real.

Four issuers file their statements a fixed number of days before the tick: 454, 455, 456 and 700 days.
Every other issuer files 59 days before the tick. The filing time sets the `knowable_at` of each input
that comes from the filing. The stored daily series are flat, so no split changes a share count.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from data_engine import raw_store
from data_engine.config import settings
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
from factors.production_topt import OperatingBranch
from truealpha_runtime.testing import InMemoryRawObjectStore, isolated_test_database

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "support"))
from macro_seed import seed_rate_for_cutoff  # noqa: E402
from split_series_seed import share_count_vintage, store_flat_daily_series  # noqa: E402
from topt_symbols import TOPT_SYMBOLS  # noqa: E402

#: The settled session every listing closes on. The corpus partition starts on it.
SESSION = date(2026, 3, 31)
#: The live tick of the fixture: 22:15 UTC on the next day, like the production schedule.
TICK = datetime(2026, 4, 1, 22, 15, tzinfo=UTC)
#: The limit that the live definition declares. The first test proves the fixture uses the live value.
LIMIT_DAYS = 455
CLOSE = Decimal("40")
FILED_SHARES = Decimal("10000000")
#: The stored series start before the cover date of every share count in this module.
SERIES_START = date(2024, 1, 2)
#: Days between a filing and the tick, per ticker. A ticker that this map omits files 59 days before the tick.
AGE_DAYS = {"AAPL": LIMIT_DAYS - 1, "MSFT": LIMIT_DAYS, "GOOGL": LIMIT_DAYS + 1, "AMZN": 700}
DEFAULT_AGE_DAYS = 59
_BANK_TICKER = "JPM"


def _filed_at(ticker: str) -> datetime:
    return TICK - timedelta(days=AGE_DAYS.get(ticker, DEFAULT_AGE_DAYS))


def _company_facts(shares: Decimal, *, filed: date, cover: date, accession: str) -> bytes:
    """The stored company-facts document: the cover count of the 10-K that carries the share count."""
    entry = {
        "end": cover.isoformat(),
        "val": int(shares),
        "accn": accession,
        "filed": filed.isoformat(),
        "form": "10-K",
        "fy": filed.year - 1,
        "fp": "FY",
    }
    return json.dumps(
        {"facts": {"dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [entry]}}}}}
    ).encode()


def _bundle(branch: OperatingBranch, *, cik: int, ticker: str) -> FinancialFactsBundle:
    financial = branch is OperatingBranch.FINANCIAL
    accession = f"0000{cik}-26-000001"
    filed_at = _filed_at(ticker)
    cover = filed_at.date() - timedelta(days=12)
    return FinancialFactsBundle(
        gross_profit=Decimal("80000000") if financial else Decimal("210000000"),
        total_assets=Decimal("200000000"),
        shares_outstanding=FILED_SHARES,
        revenue=Decimal("100000000"),
        pre_provision_profit=Decimal("80000000") if financial else None,
        raw_bytes=_company_facts(FILED_SHARES, filed=filed_at.date(), cover=cover, accession=accession),
        knowable_at=filed_at,
        shares_period_end=cover,
        vintages={
            "shares_outstanding": share_count_vintage(filed=filed_at.date(), own_date=cover, accession=accession)
        },
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
    ticker_by_cik = {cik: ticker for ticker, cik in cik_by_ticker.items()}
    for ticker, cik in sorted(cik_by_ticker.items()):
        record_headcount(
            connection,
            cik=cik,
            headcount=Decimal("164000"),
            # The headcount is one day older than the filing, so the filing alone sets the `knowable_at` of the facts.
            knowable_at=_filed_at(ticker) - timedelta(days=1),
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
        lambda cik, cutoff, branch: _bundle(branch, cik=cik, ticker=ticker_by_cik[cik]),
        headcount_extractor=PostgresHeadcountExtractor(connection),
    )
    release = ReleaseDerivedAdapter(release_targets, cutoff=cutoff_date)
    routes: dict[str, SourceFetchPort] = {}
    routes.update(dict.fromkeys(price_targets, price))
    routes.update(dict.fromkeys(sec_targets, financial))
    routes.update(dict.fromkeys(release_targets, release))
    return routes


def _live_tick() -> dict:
    """The deployed op, as the schedule launches it."""
    import dagster as dg
    from data_engine.lanes.capture import ToptLiveTickConfig, run_topt_live_tick

    context = dg.build_op_context()
    run_topt_live_tick(context, ToptLiveTickConfig(executed_at=TICK.isoformat()))
    return context.get_output_metadata("result")


@dataclass(frozen=True)
class Tick:
    url: str
    metadata: dict


@pytest.fixture(scope="module")
def tick() -> Iterator[Tick]:
    """One deployed tick over the fixture universe in an isolated database, offline, run once."""
    from data_engine.datahub import a1_evidence
    from truealpha_runtime import EnvironmentTier as RuntimeEnvironmentTier

    with isolated_test_database("inputage") as url, pytest.MonkeyPatch.context() as monkeypatch:
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
        monkeypatch.setattr(composition, "build_routes", _routes)
        monkeypatch.setattr(a1_evidence, "unmet_objectives", lambda *_args, **_kwargs: ())
        with psycopg.connect(url) as connection:
            store_flat_daily_series(connection, TOPT_SYMBOLS, first=SERIES_START, last=SESSION, close=CLOSE)
            # The tick reads the risk-free rate of its cutoff from the stored series (#1255).
            seed_rate_for_cutoff(connection, TICK)
            connection.commit()
        yield Tick(url, _live_tick())


@dataclass(frozen=True)
class Served:
    """What the tick wrote for one issuer: its decision and the `knowable_at` of its winning inputs."""

    outcome: str
    exclusion_reason: str | None
    knowable_at: dict[str, datetime]


def _served(tick: Tick, ticker: str) -> Served:
    with psycopg.connect(tick.url) as reader:
        issuer_id = reader.execute(
            """
            select distinct p.normalized_payload ->> 'issuer_id'
            from raw.capture_obligations ob
            join staging.capture_observation_obligations oo on oo.capture_obligation_id = ob.obligation_id
            join staging.capture_observation_payloads p on p.observation_id = oo.observation_id
            where ob.run_id = %s and p.normalized_payload ->> 'ticker' = %s
            """,
            (tick.metadata["capture_run_id"], ticker),
        ).fetchone()[0]
        decision = reader.execute(
            "select outcome, exclusion_reason from mart.strategy_decisions where strategy_run_id = %s and issuer_id = %s",
            (tick.metadata["strategy_run_id"], issuer_id),
        ).fetchone()
        inputs = reader.execute(
            """
            select distinct on (input_key) input_key, knowable_at from staging.strategy_backtest_inputs
            where issuer_id = %s and cutoff_at = %s and fiscal_period is null
            order by input_key, knowable_at desc, recorded_at desc
            """,
            (issuer_id, TICK),
        ).fetchall()
    return Served(outcome=decision[0], exclusion_reason=decision[1], knowable_at=dict(inputs))


def test_the_fixture_ages_the_inputs_by_the_limit_of_the_live_definition(tick: Tick) -> None:
    """The ages of this module are measured from the seeded rows and from the live definition, not assumed."""
    from truealpha_contracts.strategy import load_live_strategy_definition

    assert load_live_strategy_definition().eligibility.maximum_input_age_days == LIMIT_DAYS
    for ticker, days in AGE_DAYS.items():
        served = _served(tick, ticker)
        for key in ("gross_profit", "total_assets", "headcount", "revenue", "shares_outstanding"):
            assert TICK - served.knowable_at[key] == timedelta(days=days), (ticker, key, served.knowable_at)
        assert TICK - served.knowable_at["last_close"] < timedelta(days=5), "the close is recent"


@pytest.mark.parametrize("ticker", ["AAPL", "MSFT"])
def test_an_issuer_whose_inputs_are_at_most_the_limit_old_is_not_excluded(tick: Tick, ticker: str) -> None:
    """Ages of 454 and 455 days pass: the limit itself is not stale."""
    served = _served(tick, ticker)

    assert served.exclusion_reason is None, served
    assert served.outcome != "excluded", served


@pytest.mark.parametrize("ticker", ["GOOGL", "AMZN"])
def test_an_issuer_whose_inputs_are_older_than_the_limit_is_excluded_as_stale(tick: Tick, ticker: str) -> None:
    """Ages of 456 and 700 days fail. Red on main: nothing in the tick read `maximum_input_age_days`."""
    served = _served(tick, ticker)

    assert served.outcome == "excluded", served
    assert served.exclusion_reason == "stale_required_input", served


def test_the_other_issuers_stay_eligible(tick: Tick) -> None:
    """One stale issuer does not touch the others. JPM is a bank and has its own exclusion reasons."""
    others = [t for t in TOPT_SYMBOLS if t not in AGE_DAYS and t != _BANK_TICKER]

    served = {ticker: _served(tick, ticker) for ticker in others}

    assert len(others) == 15
    assert {ticker: item.exclusion_reason for ticker, item in served.items()} == dict.fromkeys(others), served
    assert all(item.outcome != "excluded" for item in served.values()), served
