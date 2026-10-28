"""#1523 S2a and S2b through the deployed op: the QQQ tick records the outside comparisons of its run.

The op is `run_qqq_live_tick`, as the schedule launches it. The vendor answers are offline and pass
through the deployed adapters. The capture, freeze, materialization, plausibility gate, pointer
advance and the writer are real, over a real Postgres schema.

The head holds two names with the operator's own figures from the Nasdaq-100 cassette (the bytes of
2026-08-17). PDD files the ordinary share count of the PDD case (#1515): 5,693,585,848. The operator
quotes the American depositary shares: a market value of 120,689,786,013 at a last sale price of
$86.60 makes 1,393,6xx,xxx units. The served count is about four times the witness. AAPL files a count
within the tolerance of its operator figure.

The stored SEC bytes of each name carry a revenue and a gross profit. The adapter serves 100,000,000 and
210,000,000. The stored AAPL document says the same. The stored PDD document says revenue 1,000,000,000:
the served revenue is a tenth of what an independent parse of the same bytes finds (S2b).
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import dagster as dg
import psycopg
import pytest
from data_engine import raw_store
from data_engine.config import settings
from data_engine.datahub import oracle_outcomes
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
from data_engine.datahub.production_topt.universe_plane import parse_nasdaq_index_rows
from data_engine.lanes.capture import ToptLiveTickConfig, run_qqq_live_tick
from factors.production_topt import OperatingBranch
from psycopg.rows import dict_row
from truealpha_runtime import EnvironmentTier as RuntimeEnvironmentTier
from truealpha_runtime.testing import InMemoryRawObjectStore, isolated_test_database

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "support"))
from macro_seed import seed_rate_for_cutoff  # noqa: E402
from qqq_head_seed import publish_qqq_head  # noqa: E402
from split_series_seed import share_count_vintage, store_daily_series  # noqa: E402

SESSION = date(2026, 3, 31)
TICK = datetime(2026, 4, 1, 22, 15, tzinfo=UTC)
CLOSE = Decimal("40")
COVER_DATE = date(2026, 1, 20)
FILED = date(2026, 2, 1)
ANNUAL_END = date(2025, 12, 31)
ANNUAL_FILED = date(2026, 2, 20)
SERVED_REVENUE = Decimal("100000000")
SERVED_GROSS_PROFIT = Decimal("210000000")
SERIES_START = date(2025, 12, 1)
_CASSETTE = Path(__file__).parent / "cassettes" / "nasdaq100_index.bcc3fb15.json"

#: ticker -> (CIK, filed share count). PDD files the ordinary count; AAPL a count near its operator figure.
MEMBERS = {"PDD": (1737806, Decimal("5693585848")), "AAPL": (320193, Decimal("14700000000"))}
#: CIK -> the annual revenue that the stored document holds. PDD's is ten times the served revenue.
DOCUMENT_REVENUE = {1737806: Decimal("1000000000"), 320193: SERVED_REVENUE}


def _operator_figures() -> dict[str, tuple[str, str]]:
    """The cassette's (market cap, last sale price) of each member."""
    rows = {row["ticker"]: row for row in parse_nasdaq_index_rows(_CASSETTE.read_bytes())}
    return {ticker: (rows[ticker]["market_cap"] or "", rows[ticker]["last_sale_price"] or "") for ticker in MEMBERS}


def _annual(value: Decimal) -> dict:
    """One annual us-gaap figure of the stored document: the year ended 2025-12-31, filed 2026-02-20."""
    annual = {"start": "2025-01-01", "end": ANNUAL_END.isoformat(), "val": int(value), "filed": str(ANNUAL_FILED)}
    return {"units": {"USD": [annual]}}


def _bundle(shares: Decimal, *, cik: int) -> FinancialFactsBundle:
    accession = f"0000{cik}-26-000001"
    entry = {"end": COVER_DATE.isoformat(), "val": int(shares), "accn": accession, "filed": FILED.isoformat()}
    entry |= {"form": "10-K", "fy": 2025, "fp": "FY"}
    document = {
        "facts": {
            "dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [entry]}}},
            "us-gaap": {"Revenues": _annual(DOCUMENT_REVENUE[cik]), "GrossProfit": _annual(SERVED_GROSS_PROFIT)},
        }
    }
    raw = json.dumps(document)
    return FinancialFactsBundle(
        gross_profit=SERVED_GROSS_PROFIT,
        total_assets=Decimal("200000000"),
        shares_outstanding=shares,
        revenue=SERVED_REVENUE,
        pre_provision_profit=None,
        raw_bytes=raw.encode(),
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
    for work_item_id, binding in plan.bindings.items():
        semantic_type = binding.obligation.capture_requirement_id.removesuffix(":v1")
        issuer_id, instrument_id, listing_id, ticker = plan.coordinates[binding.obligation.subject.id]
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
                cik=MEMBERS[ticker][0],
                cutoff=cutoff_date,
                issuer_id=issuer_id,
                instrument_id=instrument_id,
                listing_id=listing_id,
                operating_branch=OperatingBranch.NON_FINANCIAL,
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
    for ticker, (cik, _shares) in MEMBERS.items():
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

    shares_by_cik = {cik: shares for cik, shares in MEMBERS.values()}
    price = MarketPriceAdapter(price_targets, quote)
    financial = SecFinancialFactAdapter(
        sec_targets,
        lambda cik, _cutoff, _branch: _bundle(shares_by_cik[cik], cik=cik),
        headcount_extractor=PostgresHeadcountExtractor(connection),
    )
    release = ReleaseDerivedAdapter(release_targets, cutoff=cutoff_date)
    routes: dict[str, SourceFetchPort] = {}
    routes.update(dict.fromkeys(price_targets, price))
    routes.update(dict.fromkeys(sec_targets, financial))
    routes.update(dict.fromkeys(release_targets, release))
    return routes


@dataclass(frozen=True)
class Tick:
    url: str
    metadata: dict


def _tick(prepare: Callable[[pytest.MonkeyPatch], None] | None = None) -> Iterator[Tick]:
    """One deployed QQQ tick over the two-name head in an isolated database, offline, run once."""
    from data_engine.datahub import a1_evidence

    with isolated_test_database("oracletick") as url, pytest.MonkeyPatch.context() as monkeypatch:
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
        if prepare is not None:
            prepare(monkeypatch)
        with psycopg.connect(url) as connection:
            publish_qqq_head(
                connection,
                [(ticker, cik) for ticker, (cik, _shares) in MEMBERS.items()],
                as_of=SESSION,
                figures=_operator_figures(),
            )
            for ticker in MEMBERS:
                store_daily_series(connection, ticker, lambda _day: (CLOSE, CLOSE), first=SERIES_START, last=SESSION)
            seed_rate_for_cutoff(connection, TICK)
            connection.commit()
        context = dg.build_op_context()
        run_qqq_live_tick(context, ToptLiveTickConfig(executed_at=TICK.isoformat()))
        yield Tick(url, context.get_output_metadata("result"))


@pytest.fixture(scope="module")
def tick() -> Iterator[Tick]:
    yield from _tick()


@pytest.fixture(scope="module")
def failing_tick() -> Iterator[Tick]:
    """The same tick with a writer that raises."""

    def prepare(monkeypatch: pytest.MonkeyPatch) -> None:
        def broken(*_args: object, **_kwargs: object) -> int:
            raise RuntimeError("the writer is broken")

        monkeypatch.setattr(oracle_outcomes, "record_oracle_outcomes", broken)

    yield from _tick(prepare)


def _all_rows(url: str, run_id: str) -> dict[tuple[str, str, str], dict]:
    """The rows of the run by (ticker, oracle kind, field), each a column dict."""
    with psycopg.connect(url, row_factory=dict_row) as reader:
        found = reader.execute(
            """
            select distinct o.*, p.normalized_payload ->> 'ticker' as ticker
            from mart.oracle_outcomes o
            join staging.capture_observation_payloads p on p.normalized_payload ->> 'issuer_id' = o.subject_id
            where o.run_id = %s and p.normalized_payload ? 'ticker'
            """,
            (run_id,),
        ).fetchall()
    return {(row["ticker"], row["oracle_kind"], row["field"]): row for row in found}


def _rows(url: str, run_id: str) -> dict[str, tuple]:
    """The operator rows of the run by ticker: (subject, ticker, result, ratio, tolerance, kind, served, witness, at, reason)."""
    columns = ("subject_id", "ticker", "result", "ratio", "tolerance", "oracle_kind")
    columns += ("served_value", "oracle_value", "compared_at", "reason")
    return {
        ticker: tuple(row[column] for column in columns)
        for (ticker, kind, _field), row in _all_rows(url, run_id).items()
        if kind == "operator"
    }


def test_the_pdd_case_is_one_disagree_row_with_a_ratio_of_about_four(tick: Tick) -> None:
    rows = _rows(tick.url, tick.metadata["capture_run_id"])
    _issuer, _ticker, result, ratio, tolerance, kind, served, witness, compared_at, reason = rows["PDD"]
    assert (result, kind, reason) == ("disagree", "operator", None)
    assert Decimal("4.0") <= ratio <= Decimal("4.2"), ratio
    assert served == MEMBERS["PDD"][1]
    assert witness == Decimal("120689786013") / Decimal("86.60")
    assert tolerance == Decimal("0.05")
    assert compared_at == TICK


def test_a_count_within_the_tolerance_is_an_agree_row(tick: Tick) -> None:
    rows = _rows(tick.url, tick.metadata["capture_run_id"])
    assert rows["AAPL"][2] == "agree"
    assert abs(rows["AAPL"][3] - 1) <= Decimal("0.05")
    assert len(rows) == 2


def test_a_revenue_a_tenth_of_the_stored_filing_is_one_independent_parse_disagree_row(tick: Tick) -> None:
    """The adapter serves 100,000,000 and the stored document of PDD says 1,000,000,000 (S2b)."""
    row = _all_rows(tick.url, tick.metadata["capture_run_id"])[("PDD", "independent_parse", "revenue")]
    assert (row["result"], row["reason"]) == ("disagree", None)
    assert (row["served_value"], row["oracle_value"], row["ratio"]) == (
        SERVED_REVENUE,
        Decimal("1000000000"),
        Decimal("0.1"),
    )
    assert row["tolerance"] == Decimal("0.0001")
    assert (row["period"], row["compared_at"], row["source"]) == (str(ANNUAL_END), TICK, "sec")
    assert row["knowable_at"] == datetime.combine(ANNUAL_FILED, datetime.min.time(), tzinfo=UTC)
    assert row["observation_id"].startswith("normalized-observation:")


def test_a_figure_that_the_stored_filing_repeats_is_an_independent_parse_agree_row(tick: Tick) -> None:
    rows = _all_rows(tick.url, tick.metadata["capture_run_id"])
    for ticker, field in [("AAPL", "revenue"), ("AAPL", "gross_profit"), ("PDD", "gross_profit")]:
        row = rows[(ticker, "independent_parse", field)]
        assert (row["result"], row["ratio"]) == ("agree", Decimal(1)), (ticker, field)
    assert len([key for key in rows if key[1] == "independent_parse"]) == 4


def test_the_pointer_advances_whatever_the_comparison_says(tick: Tick) -> None:
    """A `disagree` row is data. The run committed, and the governed pointer heads it."""
    assert tick.metadata["pointer_advanced"] is True
    assert tick.metadata["oracle_outcomes_written"] is True and tick.metadata["oracle_outcomes_rows"] == 6
    with psycopg.connect(tick.url) as reader:
        head = reader.execute(
            "select count(*) from mart.current_pointer_head where target_run_id = %s",
            (tick.metadata["capture_run_id"],),
        ).fetchone()
    assert head == (1,)


def test_a_second_run_of_the_same_run_id_writes_no_second_row(tick: Tick) -> None:
    run_id = tick.metadata["capture_run_id"]
    with psycopg.connect(tick.url) as connection:
        before = connection.execute("select count(*) from mart.oracle_outcomes").fetchone()
        again = oracle_outcomes.record_oracle_outcomes(
            connection, run_id, compared_at=datetime(2026, 4, 2, 0, 45, tzinfo=UTC)
        )
        connection.commit()
        after = connection.execute("select count(*) from mart.oracle_outcomes").fetchone()
    assert again == 0 and before == after == (6,)


def test_a_writer_that_raises_does_not_stop_the_tick(failing_tick: Tick) -> None:
    """The tick commits and the pointer advances; the metadata says the comparison was not written."""
    run_id = failing_tick.metadata["capture_run_id"]
    assert failing_tick.metadata["pointer_advanced"] is True
    assert failing_tick.metadata["oracle_outcomes_written"] is False
    with psycopg.connect(failing_tick.url) as reader:
        committed = reader.execute(
            "select complete from mart.topt_capture_status where run_id = %s", (run_id,)
        ).fetchone()
        written = reader.execute("select count(*) from mart.oracle_outcomes").fetchone()
        head = reader.execute(
            "select count(*) from mart.current_pointer_head where target_run_id = %s", (run_id,)
        ).fetchone()
    assert committed is not None and head == (1,) and written == (0,)
