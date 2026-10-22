"""The live tick puts a filed share count on the share basis of the close it multiplies (#1262).

The history projector fixed this shape in #1257. These tests drive the deployed Dagster op
`run_topt_live_tick` end to end. The vendor answers are offline and pass through the deployed
adapters. The capture, freeze, materialization, strategy seed, replay and plausibility gate are
real. The stored daily series (`adjust = 'none'` and `adjust = 'splits'`) carry the splits, as
the market-data lane stores them in production.

The fixture universe is the 21-listing TOPT corpus. Every issuer files 10,000,000 shares on
2026-02-01 (a cover count dated 2026-01-20), and every listing closes at 40 on 2026-03-31.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator
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
from truealpha_runtime.testing import InMemoryRawObjectStore, isolated_test_database, load_tool

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


#: The one fixture tick of this module. A symbol that this map omits closes at 40 in both series.
_EX_DATE = date(2026, 3, 2)
SHAPES: dict[str, SeriesShape] = {
    # Acceptance a: NVDA splits 10:1 after its count was filed. Both series show the split.
    "NVDA": _split(_EX_DATE, 10),
    # Acceptance b: MSFT splits 10:1 too, and only its unadjusted series is stored.
    "MSFT": lambda day: (CLOSE * 10 if day < _EX_DATE else CLOSE, None),
    # WMT's split-adjusted series steps tenfold, and its unadjusted series does not drop.
    # That is two vintages of the split-adjusted series, not a split: SplitDerivationError.
    "WMT": lambda day: (CLOSE, CLOSE / 10 if day < _EX_DATE else CLOSE),
    # JNJ's series end one session before the close, as after a winter refresh. The close holds still.
    "JNJ": lambda day: (CLOSE, CLOSE) if day < SESSION else (None, None),
    # LLY's series also end one session early, at 400. The close of 40 is a tenfold drop.
    "LLY": lambda day: (CLOSE * 10, CLOSE * 10) if day < SESSION else (None, None),
}
#: The symbols with an unknown share basis, and the reason the tick names for each.
UNAVAILABLE = {
    "MSFT": "no_split_adjusted_series",
    "WMT": "split_derivation_failed",
    "LLY": "close_jump_beyond_series",
}


class _Warnings(logging.Handler):
    """The warning messages that the strategy seed logs during the tick."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@dataclass(frozen=True)
class Tick:
    url: str
    metadata: dict
    warnings: tuple[str, ...]


@pytest.fixture(scope="module")
def tick() -> Iterator[Tick]:
    """One deployed tick over the fixture universe in an isolated database, offline, run once."""
    from data_engine.datahub import a1_evidence
    from truealpha_runtime import EnvironmentTier as RuntimeEnvironmentTier

    with isolated_test_database("sharebasis") as url, pytest.MonkeyPatch.context() as monkeypatch:
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
        _with_series(url, SHAPES)
        seed_log = logging.getLogger("data_engine.datahub.strategy_bridge")
        handler = _Warnings()
        seed_log.addHandler(handler)
        try:
            metadata = _live_tick()
        finally:
            seed_log.removeHandler(handler)
        yield Tick(url, metadata, tuple(handler.messages))


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
            (tick.metadata["strategy_run_id"], issuer_id),
        ).fetchone()
    return Served(
        shares=None if shares is None else Decimal(shares[0]),
        shares_knowable_at=None if shares is None else shares[1],
        price_to_sales=None if decision[0] is None else Decimal(decision[0]),
        exclusion_reason=decision[1],
    )


# -- acceptance a: a split between the filing and the cutoff -----------------------------


def test_a_split_after_the_filing_gives_the_live_market_value_on_the_post_split_basis(tick: Tick) -> None:
    """#1262 acceptance a. NVDA splits 10:1 on 2026-03-02, after its count was filed on 2026-02-01.

    The 2026-03-31 close is on the post-split basis. Red on main: the tick multiplied the
    pre-split count by the post-split close, and NVDA's market value was ten times too small.
    """
    nvda = _served(tick, "NVDA")
    control = _served(tick, "AAPL")

    assert nvda.market_value == CLOSE * FILED_SHARES * 10, f"post-split market value expected, got {nvda}"
    assert nvda.shares == FILED_SHARES * 10
    assert nvda.shares_knowable_at == datetime.combine(SESSION, datetime.min.time(), tzinfo=UTC), (
        "an adjusted count is knowable once the close is"
    )
    assert control.market_value == CLOSE * FILED_SHARES, "an issuer without a split keeps its filed count"
    assert control.shares_knowable_at == datetime.combine(FILED, datetime.min.time(), tzinfo=UTC)


# -- acceptance b: no split-adjusted series, no fallback to the filed count ---------------


def test_a_symbol_without_its_split_adjusted_series_makes_the_issuer_unavailable_with_a_named_reason(
    tick: Tick,
) -> None:
    """#1262 acceptance b. MSFT split 10:1 on 2026-03-02, and only its unadjusted series is stored.

    Without the split-adjusted series its splits are unknown. The tick writes no share row for
    MSFT, so the filed count never meets the post-split close. The evaluator excludes MSFT with
    `missing_market_value_input`, and the log names the reason. Red on main: the tick wrote the
    filed 10,000,000 beside the post-split close.
    """
    msft = _served(tick, "MSFT")

    assert msft.shares is None, f"no share row may stand beside a close of an unknown basis, got {msft}"
    assert msft.exclusion_reason == "missing_market_value_input"
    assert msft.market_value is None
    named = [message for message in tick.warnings if "no_split_adjusted_series" in message]
    assert len(named) == 1 and "MSFT" in named[0], tick.warnings


def test_an_ambiguous_ratio_step_of_one_symbol_makes_only_its_issuer_unavailable(tick: Tick) -> None:
    """A SplitDerivationError of one symbol names that symbol and leaves every other issuer alone.

    Red on main: the tick wrote the filed count for WMT as if no question existed.
    """
    wmt = _served(tick, "WMT")
    others = {ticker: _served(tick, ticker) for ticker in DEFAULT_TOPT_SYMBOLS if ticker not in {"NVDA", *UNAVAILABLE}}

    assert wmt.shares is None and wmt.exclusion_reason == "missing_market_value_input", wmt
    named = [message for message in tick.warnings if "split_derivation_failed" in message]
    assert len(named) == 1 and "WMT" in named[0], tick.warnings
    assert len(tick.warnings) == len(UNAVAILABLE), "each unavailable issuer is named once, and no other"
    assert all(any(symbol in m and reason in m for m in tick.warnings) for symbol, reason in UNAVAILABLE.items())
    assert {ticker: other.shares for ticker, other in others.items()} == dict.fromkeys(others, FILED_SHARES)


# -- the series may trail the close by one session, with a cross-check -------------------


def test_series_one_session_behind_the_close_value_the_issuer_when_the_close_holds_still(tick: Tick) -> None:
    """JNJ's series end on 2026-03-30 and its close of 2026-03-31 equals the last stored close.

    The market-data refresh can run before the vendor lists the day's bar (`MAX_BAR_LAG_SESSIONS`).
    Red without the one-session tolerance: JNJ had no share row every winter night.
    """
    jnj = _served(tick, "JNJ")

    assert jnj.shares == FILED_SHARES, jnj
    assert jnj.market_value == CLOSE * FILED_SHARES, jnj


def test_a_close_that_jumps_beyond_the_series_makes_the_issuer_unavailable_with_a_named_reason(tick: Tick) -> None:
    """LLY's series end at 400 on 2026-03-30, and its close of 2026-03-31 is 40: a split or an event.

    The stored series cannot tell which, so LLY gets no share row that night. Red without the
    cross-check: the tick wrote the filed count beside the tenfold drop.
    """
    lly = _served(tick, "LLY")

    assert lly.shares is None and lly.exclusion_reason == "missing_market_value_input", lly
    named = [message for message in tick.warnings if "close_jump_beyond_series" in message]
    assert len(named) == 1 and "LLY" in named[0] and "0.1000" in named[0], tick.warnings


# -- #1346: the persisted report of the tick agrees with the core --------------------------------


def test_the_persisted_report_of_the_tick_excludes_the_listings_whose_share_count_the_core_dropped(
    tick: Tick, capsys: pytest.CaptureFixture[str]
) -> None:
    """Production calls `build_report` after `freeze_snapshot` and `materialize` (`composition.run_topt_pipeline`).

    This test reads the report row that the deployed op persisted. MSFT, WMT and LLY have no proven
    share basis in this fixture, and their financial facts are complete, so the core marks exactly their
    issuers unavailable. The report lists none of their listings as complete, and the nightly invariant
    `quality-report-matches-core-results` holds on the rows. Red when the report is built before the
    snapshot is frozen: it then reads no verdict and lists the three listings complete.
    """
    run_id = tick.metadata["capture_run_id"]
    with psycopg.connect(tick.url) as reader:
        subjects = {
            row[0]
            for row in reader.execute("select subject_id from raw.capture_obligations where run_id = %s", (run_id,))
        }
        persisted = reader.execute(
            "select payload from mart.datahub_quality_report where run_id = %s order by created_at desc limit 1",
            (run_id,),
        ).fetchone()
        dropped = {
            row[0]
            for row in reader.execute(
                """
                select distinct ob.subject_id
                from raw.capture_obligations ob
                join staging.capture_observation_obligations oo on oo.capture_obligation_id = ob.obligation_id
                join staging.capture_observation_payloads p on p.observation_id = oo.observation_id
                where ob.run_id = %s and p.normalized_payload ->> 'ticker' = any(%s)
                """,
                (run_id, sorted(UNAVAILABLE)),
            )
        }
        unavailable_issuers = reader.execute(
            "select count(distinct issuer_id) from mart.topt_core_results where run_id = %s and availability = 'unavailable'",
            (run_id,),
        ).fetchone()
    assert persisted is not None and unavailable_issuers is not None
    complete = set(persisted[0]["factor_availability"]["gross_profit_per_employee"]["complete_subject_ids"])

    assert len(dropped) == len(UNAVAILABLE), "one listing for each symbol without a proven share basis"
    assert unavailable_issuers[0] == len(UNAVAILABLE), "the core marks exactly those issuers unavailable"
    assert subjects - complete == dropped

    tool = load_tool("output_invariants")
    parity = next(invariant for invariant in tool.INVARIANTS if invariant.id == "quality-report-matches-core-results")
    assert tool.check(tick.url, require_coverage=True, invariants=(parity,), exemptions={}) == 0, capsys.readouterr()
    assert "ok       quality-report-matches-core-results: 1 row(s) examined" in capsys.readouterr().out
