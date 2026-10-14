"""The live tick puts a filed share count on the share basis of the close it multiplies (#1262).

The history projector fixed this shape in #1257. These tests drive the deployed Dagster op
`run_topt_live_tick` end to end: offline vendor answers through the deployed adapters, the
real capture, freeze, materialization, strategy seed, strategy replay and plausibility gate.
The stored daily series (`adjust = 'none'` and `adjust = 'splits'`) carry the splits, as the
market-data lane stores them in production.

The fixture universe is the 21-listing TOPT corpus. Every issuer files 10,000,000 shares on
2026-02-01 (a cover count dated 2026-01-20), and every listing closes at 40 on 2026-03-31.
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

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
from factors.production_topt import OperatingBranch
from truealpha_runtime.testing import InMemoryRawObjectStore, isolated_test_database

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "support"))
from split_series_seed import SeriesShape, share_count_vintage, store_daily_series  # noqa: E402

#: The settled session every listing closes on. The corpus partition starts on it.
SESSION = date(2026, 3, 31)
#: The live tick of the fixture: 22:15 UTC on the next day, like the production schedule.
TICK = datetime(2026, 4, 1, 22, 15, tzinfo=UTC)
CLOSE = Decimal("40")
FILED_SHARES = Decimal("10000000")
REVENUE = Decimal("100000000")
#: The share count is a cover-page count dated 2026-01-20 and filed on 2026-02-01.
COVER_DATE = date(2026, 1, 20)
FILED = date(2026, 2, 1)
#: The stored series start here, before the basis date of every share count.
SERIES_START = date(2025, 12, 1)
_BANK_TICKER = "JPM"


def _flat(_day: date) -> tuple[Decimal | None, Decimal | None]:
    return CLOSE, CLOSE


def _split(ex_date: date, factor: int) -> SeriesShape:
    """A split of `factor` new shares for each old share from `ex_date`.

    The split-adjusted series is on the share basis of its fetch, so it closes at 40 on every
    session. The unadjusted close before the ex-date is on the pre-split basis.
    """
    return lambda day: (CLOSE * factor if day < ex_date else CLOSE, CLOSE)


def _company_facts(shares: Decimal, *, accession: str) -> bytes:
    """The stored company-facts document: the cover count of the 10-K that carries the share count."""
    entry = {
        "end": COVER_DATE.isoformat(),
        "val": int(shares),
        "accn": accession,
        "filed": FILED.isoformat(),
        "form": "10-K",
        "fy": 2025,
        "fp": "FY",
    }
    return json.dumps(
        {"facts": {"dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [entry]}}}}}
    ).encode()


def _bundle(branch: OperatingBranch, *, cik: int) -> FinancialFactsBundle:
    financial = branch is OperatingBranch.FINANCIAL
    accession = f"0000{cik}-26-000001"
    return FinancialFactsBundle(
        gross_profit=Decimal("80000000") if financial else Decimal("210000000"),
        total_assets=Decimal("200000000"),
        shares_outstanding=FILED_SHARES,
        revenue=REVENUE,
        pre_provision_profit=Decimal("80000000") if financial else None,
        raw_bytes=_company_facts(FILED_SHARES, accession=accession),
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
            headcount=Decimal("164000"),
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


@pytest.fixture
def tick_database_url(monkeypatch):
    """One isolated database per test, armed so the deployed tick runs offline against it."""
    from data_engine.datahub import a1_evidence
    from truealpha_runtime import EnvironmentTier as RuntimeEnvironmentTier

    with isolated_test_database("sharebasis") as url:
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
        yield url


def _with_series(url: str, shapes: dict[str, SeriesShape]) -> None:
    """Both daily series of every TOPT symbol the market-data lane stores, SERIES_START to SESSION.

    A symbol that `shapes` omits closes at 40 in both series on every session.
    """
    with psycopg.connect(url) as connection:
        for symbol in DEFAULT_TOPT_SYMBOLS:
            store_daily_series(connection, symbol, shapes.get(symbol, _flat), first=SERIES_START, last=SESSION)
        connection.commit()


def _live_tick() -> dict:
    """The deployed op, as the schedule launches it."""
    import dagster as dg
    from data_engine.lanes.capture import ToptLiveTickConfig, run_topt_live_tick

    context = dg.build_op_context()
    run_topt_live_tick(context, ToptLiveTickConfig(executed_at=TICK.isoformat()))
    return context.get_output_metadata("result")


@dataclass(frozen=True)
class Served:
    """What the tick wrote for one issuer: its share row at the tick's cutoff and its decision."""

    shares: Decimal | None
    shares_knowable_at: datetime | None
    price_to_sales: Decimal | None
    exclusion_reason: str | None

    @property
    def market_value(self) -> Decimal | None:
        """The market value the decision divided by revenue."""
        return None if self.price_to_sales is None else self.price_to_sales * REVENUE


def _served(url: str, metadata: dict, ticker: str) -> Served:
    with psycopg.connect(url) as reader:
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
        shares = reader.execute(
            """
            select value, knowable_at from staging.strategy_backtest_inputs
            where issuer_id = %s and cutoff_at = %s and input_key = 'shares_outstanding' and fiscal_period is null
            order by knowable_at desc, recorded_at desc
            limit 1
            """,
            (issuer_id, TICK),
        ).fetchone()
        decision = reader.execute(
            "select current_price_to_sales, exclusion_reason from mart.strategy_decisions "
            "where strategy_run_id = %s and issuer_id = %s",
            (metadata["strategy_run_id"], issuer_id),
        ).fetchone()
    return Served(
        shares=None if shares is None else Decimal(shares[0]),
        shares_knowable_at=None if shares is None else shares[1],
        price_to_sales=None if decision[0] is None else Decimal(decision[0]),
        exclusion_reason=decision[1],
    )


# -- acceptance a: a split between the filing and the cutoff -----------------------------


def test_a_split_after_the_filing_gives_the_live_market_value_on_the_post_split_basis(tick_database_url) -> None:
    """#1262 acceptance a. NVDA splits 10:1 on 2026-03-02, after its count was filed on 2026-02-01.

    The 2026-03-31 close is on the post-split basis. Red on main: the tick multiplied the
    pre-split count by the post-split close, and NVDA's market value was ten times too small.
    """
    _with_series(tick_database_url, {"NVDA": _split(date(2026, 3, 2), 10)})

    metadata = _live_tick()
    nvda = _served(tick_database_url, metadata, "NVDA")
    control = _served(tick_database_url, metadata, "AAPL")

    assert nvda.market_value == CLOSE * FILED_SHARES * 10, f"post-split market value expected, got {nvda}"
    assert nvda.shares == FILED_SHARES * 10
    assert nvda.shares_knowable_at == datetime.combine(SESSION, datetime.min.time(), tzinfo=UTC), (
        "an adjusted count is knowable once the close is"
    )
    assert control.market_value == CLOSE * FILED_SHARES, "an issuer without a split keeps its filed count"
    assert control.shares_knowable_at == datetime.combine(FILED, datetime.min.time(), tzinfo=UTC)


# -- acceptance b: no split-adjusted series, no fallback to the filed count ---------------


def test_a_symbol_without_its_split_adjusted_series_makes_the_issuer_unavailable_with_a_named_reason(
    tick_database_url, caplog
) -> None:
    """#1262 acceptance b. NVDA split 10:1 on 2026-03-02, and only its unadjusted series is stored.

    Without the split-adjusted series its splits are unknown. The tick writes no share row for
    NVDA, so the filed count never meets the post-split close, and the evaluator excludes NVDA
    with `missing_market_value_input`. The log names the reason. Red on main: the tick wrote
    the filed 10,000,000 beside the post-split close.
    """
    ex_date = date(2026, 3, 2)
    _with_series(tick_database_url, {"NVDA": lambda day: (CLOSE * 10 if day < ex_date else CLOSE, None)})

    with caplog.at_level(logging.WARNING, logger="data_engine.datahub.strategy_bridge"):
        metadata = _live_tick()
    nvda = _served(tick_database_url, metadata, "NVDA")
    control = _served(tick_database_url, metadata, "AAPL")

    assert nvda.shares is None, f"no share row may stand beside a close of an unknown basis, got {nvda}"
    assert nvda.exclusion_reason == "missing_market_value_input"
    assert nvda.market_value is None
    named = [record.getMessage() for record in caplog.records if "no_split_adjusted_series" in record.getMessage()]
    assert len(named) == 1 and "NVDA" in named[0], [record.getMessage() for record in caplog.records]
    assert control.market_value == CLOSE * FILED_SHARES, "one issuer without series must not take down the others"


def test_an_ambiguous_ratio_step_of_one_symbol_makes_only_its_issuer_unavailable(tick_database_url, caplog) -> None:
    """A SplitDerivationError of one symbol names that symbol and leaves every other issuer alone.

    NVDA's split-adjusted series steps tenfold on 2026-03-02, and its unadjusted series does not
    drop: two vintages of the split-adjusted series, not a split. Red on main: the tick wrote the
    filed count for NVDA as if no question existed.
    """
    ex_date = date(2026, 3, 2)
    _with_series(tick_database_url, {"NVDA": lambda day: (CLOSE, CLOSE / 10 if day < ex_date else CLOSE)})

    with caplog.at_level(logging.WARNING, logger="data_engine.datahub.strategy_bridge"):
        metadata = _live_tick()
    nvda = _served(tick_database_url, metadata, "NVDA")

    assert nvda.shares is None and nvda.exclusion_reason == "missing_market_value_input", nvda
    named = [record.getMessage() for record in caplog.records if "split_derivation_failed" in record.getMessage()]
    assert len(named) == 1 and "NVDA" in named[0], [record.getMessage() for record in caplog.records]
    others = [_served(tick_database_url, metadata, ticker) for ticker in ("AAPL", "MSFT", "WMT", "GOOGL")]
    assert all(other.market_value == CLOSE * FILED_SHARES for other in others), others
