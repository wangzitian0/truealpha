"""#1139: the point-in-time projector for historical strategy inputs.

The projector builds the rows `strategy_bridge.seed_strategy_inputs_from_capture` writes for
a live cutoff, for a list of past cutoffs, from stored SEC company-facts bytes.

The tests in the first half run without a database. The tests marked "database" write to
`staging.strategy_backtest_inputs` and skip without a local Postgres.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from data_engine import raw_store
from data_engine.config import settings
from data_engine.datahub.production_topt.concept_mapping import DEFAULT_RULESET
from data_engine.datahub.production_topt.headcount import PostgresHeadcountExtractor, record_headcount
from data_engine.datahub.production_topt.sec_financial_adapter import (
    HeadcountFact,
    SecFinancialFactAdapter,
    SecTarget,
    build_bundle,
)
from data_engine.datahub.strategy_history import (
    DEFAULT_MONTHS,
    UNADJUSTED_BARS,
    HistoryIssuer,
    HistoryRow,
    LastClose,
    LookAheadError,
    admit_input,
    end_of_day,
    issuer_from_observation,
    load_stored_company_facts,
    monthly_cutoffs,
    postgres_last_close,
    project_deployed_history,
    project_issuer_rows,
    run_strategy_history,
    stored_history_issuers,
)
from data_engine.strategy_backtest_gateway import StrategyBacktestGateway
from factors.production_topt import OperatingBranch
from truealpha_contracts.datahub import CaptureWorkItem
from truealpha_contracts.fiscal_period import parse_annual
from truealpha_contracts.models import DataSource, RawCapture, RawIngestionEnvelope, RawObjectRef

TICK = datetime(2026, 10, 9, 6, 15, tzinfo=UTC)
FACTS_LOADER = Callable[[int], tuple[bytes, dict[str, Any]] | None]


# -- fixtures ---------------------------------------------------------------------------


def _flow(end: str, start: str, val: int, filed: str, *, form: str = "10-K", accn: str | None = None) -> dict:
    return {
        "end": end,
        "start": start,
        "val": val,
        "filed": filed,
        "form": form,
        "fy": int(end[:4]),
        "fp": "FY",
        "accn": accn or f"0001-{filed}",
    }


def _instant(end: str, val: int, filed: str, *, form: str = "10-K") -> dict:
    return {
        "end": end,
        "val": val,
        "filed": filed,
        "form": form,
        "fy": int(end[:4]),
        "fp": "FY",
        "accn": f"0001-{filed}",
    }


def _doc(years: range = range(2018, 2026), *, base: int = 1000, extra: dict[str, list[dict]] | None = None) -> dict:
    """A company-facts document. Fiscal year Y ends on Y-12-31. Its 10-K is filed on (Y+1)-02-20."""
    series: dict[str, list[dict]] = {
        "Revenues": [],
        "GrossProfit": [],
        "NetIncomeLoss": [],
        "Assets": [],
        "CommonStockSharesOutstanding": [],
    }
    for year in years:
        end, start, filed = f"{year}-12-31", f"{year}-01-01", f"{year + 1}-02-20"
        series["Revenues"].append(_flow(end, start, base + year, filed))
        series["GrossProfit"].append(_flow(end, start, (base + year) // 2, filed))
        series["NetIncomeLoss"].append(_flow(end, start, (base + year) // 10, filed))
        series["Assets"].append(_instant(end, base * 10 + year, filed))
        series["CommonStockSharesOutstanding"].append(_instant(end, 5_000_000 + year, filed))
    for concept, entries in (extra or {}).items():
        series[concept].extend(entries)
    units = {"Assets": "USD", "CommonStockSharesOutstanding": "shares"}
    return {
        "facts": {
            "us-gaap": {concept: {"units": {units.get(concept, "USD"): entries}} for concept, entries in series.items()}
        }
    }


def _issuer(number: int, *, branch: OperatingBranch = OperatingBranch.NON_FINANCIAL) -> HistoryIssuer:
    return HistoryIssuer(
        issuer_id=f"issuer:h1139:{number:02d}",
        instrument_id=f"security:h1139:{number:02d}",
        listing_id=f"listing:xnas:h{number:02d}",
        ticker=f"H{number:02d}",
        cik=1000 + number,
        operating_branch=branch,
    )


def _loader(documents: dict[int, dict]) -> FACTS_LOADER:
    def load(cik: int) -> tuple[bytes, dict[str, Any]] | None:
        document = documents.get(cik)
        return None if document is None else (json.dumps(document).encode(), document)

    return load


def _no_price(_symbol: str, _cutoff: datetime) -> LastClose | None:
    return None


def _no_headcount(_cik: int, _as_of: date) -> HeadcountFact | None:
    return None


def _rows(
    issuer: HistoryIssuer,
    document: dict,
    cutoffs: list[datetime],
    *,
    last_close: Callable[[str, datetime], LastClose | None] = _no_price,
    headcount: Callable[[int, date], HeadcountFact | None] = _no_headcount,
) -> list[HistoryRow]:
    projection = project_issuer_rows(
        issuer,
        cutoffs,
        load_facts=_loader({issuer.cik: document}),
        headcount_extractor=headcount,
        last_close=last_close,
        ruleset=DEFAULT_RULESET,
    )
    return list(projection.rows)


def _at(cutoffs: list[HistoryRow], cutoff: datetime) -> dict[tuple[str, str | None], HistoryRow]:
    return {(row.input_key, row.fiscal_period): row for row in cutoffs if row.cutoff_at == cutoff}


def _utc(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=UTC)


@pytest.fixture
def connection():
    try:
        active = psycopg.connect(settings.database_url, connect_timeout=3, autocommit=False)
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs the required integration coverage")
    try:
        yield active
    finally:
        active.rollback()
        active.close()


# -- cutoffs ----------------------------------------------------------------------------


def test_monthly_cutoffs_are_the_first_day_of_each_of_the_last_36_months_at_midnight_utc() -> None:
    cutoffs = monthly_cutoffs(TICK)
    assert DEFAULT_MONTHS == 36
    assert len(cutoffs) == 36
    assert cutoffs[0] == _utc(2023, 11, 1)
    assert cutoffs[-1] == _utc(2026, 10, 1)
    assert all(cutoff.day == 1 and (cutoff.hour, cutoff.minute, cutoff.second) == (0, 0, 0) for cutoff in cutoffs)
    assert all(cutoff.utcoffset() == timedelta(0) for cutoff in cutoffs)
    assert cutoffs == sorted(set(cutoffs))


def test_monthly_cutoffs_follow_the_tick_and_never_pass_it() -> None:
    assert monthly_cutoffs(_utc(2026, 10, 1), 1) == [_utc(2026, 10, 1)]
    # 2026-10-01 01:00 at UTC+2 is 2026-09-30 23:00 UTC: the tick is still in September.
    plus_two = datetime(2026, 10, 1, 1, 0, tzinfo=timezone(timedelta(hours=2)))
    assert monthly_cutoffs(plus_two, 2) == [_utc(2026, 8, 1), _utc(2026, 9, 1)]
    assert monthly_cutoffs(_utc(2027, 1, 15), 3) == [_utc(2026, 11, 1), _utc(2026, 12, 1), _utc(2027, 1, 1)]
    for tick in (TICK, _utc(2026, 10, 1), _utc(2027, 1, 15)):
        assert max(monthly_cutoffs(tick)) <= tick


def test_monthly_cutoffs_reject_a_naive_tick_and_a_month_count_below_one() -> None:
    with pytest.raises(ValueError, match="time zone"):
        monthly_cutoffs(datetime(2026, 10, 9, 6, 15))
    with pytest.raises(ValueError, match="months"):
        monthly_cutoffs(TICK, 0)


# -- strict point in time ---------------------------------------------------------------


def test_h1_every_row_of_20_issuers_and_36_cutoffs_is_strictly_before_the_cutoff_date() -> None:
    cutoffs = monthly_cutoffs(TICK)
    rows: list[HistoryRow] = []
    for number in range(1, 21):
        rows.extend(_rows(_issuer(number), _doc(base=1000 + number), cutoffs))

    assert {row.issuer_id for row in rows} == {_issuer(number).issuer_id for number in range(1, 21)}
    assert {row.cutoff_at for row in rows} == set(cutoffs)
    assert len(rows) > 20 * 36 * 5, "every issuer and cutoff must carry at least five inputs"
    for row in rows:
        assert row.knowable_at.astimezone(UTC).date() < row.cutoff_at.date(), row
        assert row.knowable_at <= row.cutoff_at, row
    periodic = [row for row in rows if row.fiscal_period is not None]
    assert periodic, "net income must carry its fiscal period"
    assert {row.input_key for row in periodic} == {"net_income"}
    for row in periodic:
        period = parse_annual(row.fiscal_period or "")
        assert period is not None and period.is_annual, row.fiscal_period
        assert period.end <= row.knowable_at.date(), "a period cannot end after the filing that reports it"
    for row in rows:
        if row.input_key != "net_income":
            assert row.fiscal_period is None, row


def test_a_fact_filed_on_the_cutoff_date_is_rejected_by_the_projector() -> None:
    # The FY2025 10-K is filed on 2026-03-01: the same calendar day as the March cutoff.
    late = _doc(range(2018, 2025))
    late["facts"]["us-gaap"]["Revenues"]["units"]["USD"].append(_flow("2025-12-31", "2025-01-01", 424242, "2026-03-01"))
    late["facts"]["us-gaap"]["GrossProfit"]["units"]["USD"].append(
        _flow("2025-12-31", "2025-01-01", 212121, "2026-03-01")
    )
    rows = _rows(_issuer(1), late, [_utc(2026, 3, 1), _utc(2026, 4, 1)])

    on_cutoff_date = _at(rows, _utc(2026, 3, 1))
    assert on_cutoff_date[("revenue", None)].value == Decimal(1000 + 2024), (
        "a fact filed on the cutoff date must not win"
    )
    after = _at(rows, _utc(2026, 4, 1))
    assert after[("revenue", None)].value == Decimal(424242)
    assert after[("revenue", None)].knowable_at == end_of_day(date(2026, 3, 1))


def test_admit_input_rejects_a_row_knowable_on_the_cutoff_date() -> None:
    cutoff = _utc(2026, 3, 1)
    admit_input(cutoff, end_of_day(date(2026, 2, 28)))
    for knowable_at in (end_of_day(date(2026, 3, 1)), cutoff, cutoff + timedelta(days=3)):
        with pytest.raises(LookAheadError, match="2026-03-01"):
            admit_input(cutoff, knowable_at)


def test_a_fact_filed_after_the_cutoff_changes_no_row_of_that_cutoff() -> None:
    cutoffs = monthly_cutoffs(TICK)
    base = _doc(range(2018, 2025))
    later = _doc(range(2018, 2025))
    # A FY2025 10-K filed on 2026-02-20 and an amendment of FY2024 filed on 2025-09-10.
    later["facts"]["us-gaap"]["Revenues"]["units"]["USD"].append(_flow("2025-12-31", "2025-01-01", 9999, "2026-02-20"))
    later["facts"]["us-gaap"]["Revenues"]["units"]["USD"].append(
        _flow("2024-12-31", "2024-01-01", 7777, "2025-09-10", form="10-K/A")
    )
    before = _rows(_issuer(1), base, cutoffs)
    after = _rows(_issuer(1), later, cutoffs)

    unchanged = [cutoff for cutoff in cutoffs if cutoff < _utc(2025, 9, 10)]
    assert unchanged, "the fixture must hold cutoffs before the later filing"
    for cutoff in unchanged:
        assert _at(before, cutoff) == _at(after, cutoff), cutoff
    changed = [cutoff for cutoff in cutoffs if _at(before, cutoff) != _at(after, cutoff)]
    assert changed, "a filing before a cutoff must change that cutoff, or the test proves nothing"
    assert min(changed) > _utc(2025, 9, 10)


def test_a_restated_fact_filed_before_the_cutoff_wins_over_the_original() -> None:
    restated = _doc(range(2018, 2025))
    restated["facts"]["us-gaap"]["Revenues"]["units"]["USD"].append(
        _flow("2024-12-31", "2024-01-01", 777, "2025-09-15", form="10-K/A", accn="0001-restated")
    )
    rows = _rows(_issuer(1), restated, [_utc(2025, 9, 1), _utc(2025, 9, 15), _utc(2025, 10, 1)])

    assert _at(rows, _utc(2025, 9, 1))[("revenue", None)].value == Decimal(1000 + 2024)
    assert _at(rows, _utc(2025, 9, 15))[("revenue", None)].value == Decimal(1000 + 2024), "filed on the cutoff date"
    winner = _at(rows, _utc(2025, 10, 1))[("revenue", None)]
    assert winner.value == Decimal(777)
    assert winner.knowable_at == end_of_day(date(2025, 9, 15))


def test_each_key_carries_its_own_filed_date() -> None:
    document = _doc(range(2018, 2025))
    # A 10-Q cover page restates the share count three months after the 10-K.
    document["facts"]["us-gaap"]["CommonStockSharesOutstanding"]["units"]["shares"].append(
        _instant("2025-03-31", 6_123_456, "2025-05-09", form="10-Q")
    )
    rows = _at(_rows(_issuer(1), document, [_utc(2025, 6, 1)]), _utc(2025, 6, 1))

    assert rows[("revenue", None)].knowable_at == end_of_day(date(2025, 2, 20))
    assert rows[("total_assets", None)].knowable_at == end_of_day(date(2025, 2, 20))
    assert rows[("shares_outstanding", None)].knowable_at == end_of_day(date(2025, 5, 9))
    assert rows[("shares_outstanding", None)].value == Decimal(6_123_456), "shares are as filed (A6 decision 4)"
    assert len({row.knowable_at for row in rows.values()}) >= 2, "one blended knowable_at is the defect"
    series = [row for key, row in rows.items() if key[0] == "net_income" and key[1] is not None]
    assert {row.knowable_at for row in series} == {end_of_day(date(year + 1, 2, 20)) for year in range(2018, 2025)}


def test_a_key_without_a_filing_date_is_not_projected() -> None:
    from data_engine.datahub.strategy_history import vintage_knowable_at

    payload = {"vintage": {"revenue": {"filed": "2025-02-20"}, "total_assets": {"filed": None}, "headcount": {}}}
    knowable_at_of = vintage_knowable_at(payload)
    assert knowable_at_of("revenue", None) == end_of_day(date(2025, 2, 20))
    assert knowable_at_of("total_assets", None) is None
    assert knowable_at_of("headcount", None) is None
    assert knowable_at_of("gross_profit", None) is None
    assert knowable_at_of("net_income", "2024-12-31") is None


# -- same shape as the live bridge ------------------------------------------------------


class _Cursor:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple]:
        return self._rows


class _LiveConnection:
    """Hands one captured observation to the live writer and records its INSERT parameters."""

    def __init__(self, observation: tuple) -> None:
        self._observation = observation
        self.inserts: list[tuple] = []

    def execute(self, sql: str, params: tuple = ()) -> _Cursor:
        if sql.strip().lower().startswith("select"):
            return _Cursor([self._observation])
        assert sql.strip().lower().startswith("insert"), sql
        self.inserts.append(params)
        return _Cursor([])


def _work_item() -> CaptureWorkItem:
    return CaptureWorkItem(
        campaign_id="capture-campaign:" + "1" * 64,
        source_request_id="source-request:" + "2" * 64,
        schedule_policy_id="schedule-policy:" + "3" * 64,
    )


def test_the_projector_rows_match_the_rows_the_live_bridge_writes_for_the_same_payload() -> None:
    from data_engine.datahub.production_topt.executor import FetchSuccess
    from data_engine.datahub.strategy_bridge import seed_strategy_inputs_from_capture

    issuer, document = _issuer(7), _doc(base=3000)
    cutoff = _utc(2025, 6, 1)
    as_of = cutoff.date() - timedelta(days=1)
    item = _work_item()
    target = SecTarget(
        cik=issuer.cik,
        cutoff=as_of,
        issuer_id=issuer.issuer_id,
        instrument_id=issuer.instrument_id,
        listing_id=issuer.listing_id,
        operating_branch=issuer.operating_branch,
    )
    adapter = SecFinancialFactAdapter(
        {item.work_item_id: target},
        lambda cik, day, branch: build_bundle(document, day, branch),
        headcount_extractor=_no_headcount,
    )
    outcome = adapter.fetch(item)
    assert isinstance(outcome, FetchSuccess)

    live = _LiveConnection(
        ("financial-fact", str(outcome.confidence), outcome.record.payload, outcome.transaction_time)
    )
    assert seed_strategy_inputs_from_capture(live, "run:live", cutoff=cutoff) == len(live.inserts)
    live_rows = {(params[2], params[6], Decimal(str(params[3])), params[4]) for params in live.inserts}

    projected = {
        (row.input_key, row.fiscal_period, row.value, row.confidence) for row in _rows(issuer, document, [cutoff])
    }
    assert live_rows, "the live writer must write rows for this payload"
    assert projected == live_rows


# -- price and headcount ----------------------------------------------------------------


def test_last_close_row_carries_the_bar_close_and_the_bar_knowable_time() -> None:
    seen: list[tuple[str, datetime]] = []

    def last_close(symbol: str, cutoff: datetime) -> LastClose | None:
        seen.append((symbol, cutoff))
        return LastClose(Decimal("123.45"), Decimal("0.90"), datetime(2025, 5, 30, 20, 0, tzinfo=UTC))

    rows = _at(_rows(_issuer(1), _doc(), [_utc(2025, 6, 1)], last_close=last_close), _utc(2025, 6, 1))
    close = rows[("last_close", None)]
    assert (close.value, close.confidence) == (Decimal("123.45"), Decimal("0.90"))
    assert close.knowable_at == datetime(2025, 5, 30, 20, 0, tzinfo=UTC)
    assert seen == [("H01", _utc(2025, 6, 1))]


def test_a_price_knowable_on_the_cutoff_date_stops_the_run() -> None:
    def last_close(_symbol: str, cutoff: datetime) -> LastClose | None:
        return LastClose(Decimal("10"), Decimal("0.9"), cutoff + timedelta(hours=20))

    with pytest.raises(LookAheadError):
        _rows(_issuer(1), _doc(), [_utc(2025, 6, 1)], last_close=last_close)


def test_headcount_row_carries_the_extractor_fact_and_its_own_knowable_time() -> None:
    asked: list[tuple[int, date]] = []

    def headcount(cik: int, as_of: date) -> HeadcountFact | None:
        asked.append((cik, as_of))
        return HeadcountFact(Decimal("1500"), datetime(2025, 3, 3, 14, 30, tzinfo=UTC), source="10k-extraction")

    rows = _at(_rows(_issuer(1), _doc(), [_utc(2025, 6, 1)], headcount=headcount), _utc(2025, 6, 1))
    assert rows[("headcount", None)].value == Decimal(1500)
    assert rows[("headcount", None)].knowable_at == datetime(2025, 3, 3, 14, 30, tzinfo=UTC)
    assert asked == [(1001, date(2025, 5, 31))], "the extractor must be asked for the day before the cutoff"


# -- stored issuer resolution -----------------------------------------------------------


def test_an_observation_becomes_a_history_issuer_with_cik_ticker_and_branch() -> None:
    payload = {
        "issuer_id": "issuer:lei:AAAAAAAAAAAAAAAAAA11",
        "instrument_id": "security:cusip:037833100",
        "listing_id": "listing:xnys:brk.b",
        "operating_branch": "insurance",
    }
    issuer = issuer_from_observation(payload, "companyfacts:CIK0001067983", connection=None)
    assert issuer == HistoryIssuer(
        issuer_id="issuer:lei:AAAAAAAAAAAAAAAAAA11",
        instrument_id="security:cusip:037833100",
        listing_id="listing:xnys:brk.b",
        ticker="BRK.B",
        cik=1067983,
        operating_branch=OperatingBranch.INSURANCE,
        revenue_proxy_allowed=False,
    )


def test_the_revenue_proxy_is_allowed_only_when_the_live_payload_shows_it() -> None:
    base = {
        "issuer_id": "issuer:cik:1403161",
        "instrument_id": "security:cusip:92826C839",
        "listing_id": "listing:xnys:v",
        "operating_branch": "non_financial",
        "revenue": "32653000000",
        "gross_profit": "32653000000",
        "vintage": {
            "revenue": {"accession": "0001-26-000001", "filed": "2025-11-14"},
            "gross_profit": {"accession": "0001-26-000001", "filed": "2025-11-14"},
        },
    }
    assert issuer_from_observation(base, "companyfacts:CIK0001403161", connection=None).revenue_proxy_allowed is True
    reported = {**base, "gross_profit": "20000000000", "vintage": {**base["vintage"], "gross_profit": {"filed": "x"}}}
    assert (
        issuer_from_observation(reported, "companyfacts:CIK0001403161", connection=None).revenue_proxy_allowed is False
    )
    bank = {**base, "operating_branch": "financial"}
    assert issuer_from_observation(bank, "companyfacts:CIK0001403161", connection=None).revenue_proxy_allowed is False


def test_an_observation_without_a_company_facts_record_has_no_history_issuer() -> None:
    payload = {"issuer_id": "issuer:x", "listing_id": "listing:xnas:x", "operating_branch": "non_financial"}
    assert issuer_from_observation(payload, "production-topt-integration:3", connection=None) is None


# -- database ---------------------------------------------------------------------------


def _insert_bar(
    connection,
    symbol: str,
    trading_date: date,
    close: str,
    *,
    adjust: str = UNADJUSTED_BARS,
    recorded_offset_days: int = 0,
) -> None:
    session_close = datetime(trading_date.year, trading_date.month, trading_date.day, 20, 0, tzinfo=UTC)
    connection.execute(
        """
        insert into staging.market_prices_daily
            (symbol, trading_date, open, high, low, close, volume, adjust, transaction_time, recorded_at,
             confidence, raw_ref)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            symbol,
            trading_date,
            close,
            close,
            close,
            close,
            "1000",
            adjust,
            session_close,
            session_close + timedelta(days=recorded_offset_days),
            "0.9",
            f"test:{symbol}:{trading_date}:{adjust}:{recorded_offset_days}",
        ),
    )


def _violations(connection, prefix: str) -> int:
    return connection.execute(
        """
        select count(*) from staging.strategy_backtest_inputs
        where issuer_id like %s and (knowable_at at time zone 'UTC')::date >= (cutoff_at at time zone 'UTC')::date
        """,
        (prefix + "%",),
    ).fetchone()[0]


def test_h1_query_over_20_issuers_and_36_cutoffs_lists_every_input_before_its_cutoff(connection) -> None:
    issuers = [_issuer(number) for number in range(1, 21)]
    documents = {issuer.cik: _doc(base=2000 + issuer.cik) for issuer in issuers}
    for issuer in issuers:
        for cutoff in monthly_cutoffs(TICK):
            month_end = cutoff.date() - timedelta(days=1)
            _insert_bar(connection, issuer.ticker, month_end, "50")

    summary = run_strategy_history(connection, tick=TICK, issuers=issuers, load_facts=_loader(documents))

    assert (summary.issuers, summary.cutoffs) == (20, 36)
    assert summary.inserted > 20 * 36 * 6
    assert summary.already_present == 0
    prefix = "issuer:h1139:"
    assert _violations(connection, prefix) == 0
    pairs = connection.execute(
        "select count(*) from (select distinct issuer_id, cutoff_at from staging.strategy_backtest_inputs "
        "where issuer_id like %s) pair",
        (prefix + "%",),
    ).fetchone()[0]
    assert pairs == 20 * 36
    assert (
        connection.execute(
            "select count(*) from staging.strategy_backtest_inputs "
            "where issuer_id like %s and fiscal_period is not null and input_key <> 'net_income'",
            (prefix + "%",),
        ).fetchone()[0]
        == 0
    )
    without_period = connection.execute(
        """
        select count(*) from (
            select issuer_id, cutoff_at from staging.strategy_backtest_inputs where issuer_id like %s
            group by issuer_id, cutoff_at
            having count(*) filter (where fiscal_period is not null) = 0
        ) bare
        """,
        (prefix + "%",),
    ).fetchone()[0]
    assert without_period == 0, "every issuer and cutoff must carry at least one fiscal period"
    closes = connection.execute(
        "select count(*) from staging.strategy_backtest_inputs where issuer_id like %s and input_key = 'last_close'",
        (prefix + "%",),
    ).fetchone()[0]
    assert closes == 20 * 36


def test_the_check_rejects_a_row_filed_on_the_cutoff_date(connection) -> None:
    insert = """
        insert into staging.strategy_backtest_inputs
            (issuer_id, cutoff_at, input_key, value, confidence, knowable_at)
        values ('issuer:h1139:check', %s, 'revenue', '1', '0.9', %s)
    """
    cutoff = _utc(2026, 3, 1)
    connection.execute(insert, (cutoff, end_of_day(date(2026, 2, 28))))
    with pytest.raises(psycopg.errors.CheckViolation, match="strategy_backtest_inputs_pit"):
        connection.execute(insert, (cutoff, end_of_day(date(2026, 3, 1))))


def test_a_rerun_adds_zero_rows(connection) -> None:
    issuers = [_issuer(number) for number in range(1, 4)]
    documents = {issuer.cik: _doc(base=500 + issuer.cik) for issuer in issuers}
    first = run_strategy_history(connection, tick=TICK, issuers=issuers, load_facts=_loader(documents))
    count = "select count(*) from staging.strategy_backtest_inputs where issuer_id like 'issuer:h1139:%'"
    after_first = connection.execute(count).fetchone()[0]
    assert first.inserted == after_first > 0

    second = run_strategy_history(connection, tick=TICK, issuers=issuers, load_facts=_loader(documents))

    assert second.inserted == 0
    assert second.already_present == first.inserted
    assert connection.execute(count).fetchone()[0] == after_first


def test_a_restated_value_lands_as_a_new_row_only_for_the_cutoffs_after_its_filing(connection) -> None:
    issuer = _issuer(1)
    original = _doc(range(2018, 2026))
    restated = _doc(range(2018, 2026))
    restated["facts"]["us-gaap"]["Revenues"]["units"]["USD"].append(
        _flow("2024-12-31", "2024-01-01", 777, "2025-09-15", form="10-K/A", accn="0001-restated")
    )
    run_strategy_history(connection, tick=TICK, issuers=[issuer], load_facts=_loader({issuer.cik: original}))

    second = run_strategy_history(connection, tick=TICK, issuers=[issuer], load_facts=_loader({issuer.cik: restated}))

    new_rows = connection.execute(
        """
        select cutoff_at, input_key, value from staging.strategy_backtest_inputs
        where issuer_id = %s and value = 777 order by cutoff_at
        """,
        (issuer.issuer_id,),
    ).fetchall()
    # The FY2025 10-K is filed on 2026-02-20 and then supersedes FY2024 revenue.
    expected_cutoffs = [
        _utc(2025, 10, 1),
        _utc(2025, 11, 1),
        _utc(2025, 12, 1),
        _utc(2026, 1, 1),
        _utc(2026, 2, 1),
    ]
    assert [row[0] for row in new_rows] == expected_cutoffs
    assert {row[1] for row in new_rows} == {"revenue"}
    assert second.inserted == len(expected_cutoffs)
    seen = StrategyBacktestGateway(connection).issuer_inputs(_utc(2025, 10, 1))
    assert {item.issuer_id: item.records["revenue"][0] for item in seen}[issuer.issuer_id] == Decimal(777)


def test_the_price_reader_takes_unadjusted_bars_of_sessions_before_the_cutoff_date(connection) -> None:
    _insert_bar(connection, "ZZ1139", date(2025, 5, 29), "100")
    _insert_bar(connection, "ZZ1139", date(2025, 5, 30), "110")
    _insert_bar(connection, "ZZ1139", date(2025, 5, 30), "55", adjust="splits")
    _insert_bar(connection, "ZZ1139", date(2025, 6, 1), "999")
    _insert_bar(connection, "ZZ1139", date(2025, 6, 2), "888")

    friday = postgres_last_close(connection)("ZZ1139", _utc(2025, 6, 1))
    assert friday == LastClose(Decimal("110"), Decimal("0.9"), datetime(2025, 5, 30, 20, 0, tzinfo=UTC))
    assert postgres_last_close(connection)("ZZ1139", _utc(2025, 5, 30)) == LastClose(
        Decimal("100"), Decimal("0.9"), datetime(2025, 5, 29, 20, 0, tzinfo=UTC)
    )
    assert postgres_last_close(connection)("ZZ1139", _utc(2025, 5, 29)) is None


def test_the_price_reader_ignores_split_adjusted_bars(connection) -> None:
    _insert_bar(connection, "ZZ1140", date(2025, 5, 30), "55", adjust="splits")
    assert postgres_last_close(connection)("ZZ1140", _utc(2025, 6, 1)) is None


def test_the_price_reader_takes_the_latest_vintage_of_the_session(connection) -> None:
    _insert_bar(connection, "ZZ1141", date(2025, 5, 30), "110")
    _insert_bar(connection, "ZZ1141", date(2025, 5, 30), "111", recorded_offset_days=2)
    result = postgres_last_close(connection)("ZZ1141", _utc(2025, 6, 1))
    assert result is not None and result.close == Decimal("111")


def test_headcount_from_the_fact_table_obeys_the_strict_date_rule(connection) -> None:
    issuer = _issuer(1)
    record_headcount(
        connection,
        cik=issuer.cik,
        headcount=Decimal("1200"),
        knowable_at=datetime(2025, 2, 28, 9, 0, tzinfo=UTC),
        source="10k-extraction",
        evidence_ref="accession=0001-25-000001 form=10-K filed=2025-02-28 raw=raw.fetches:1",
        confidence=Decimal("0.8"),
    )
    record_headcount(
        connection,
        cik=issuer.cik,
        headcount=Decimal("1300"),
        knowable_at=datetime(2025, 6, 1, 9, 0, tzinfo=UTC),
        source="10k-extraction",
        evidence_ref="accession=0001-25-000002 form=10-K/A filed=2025-06-01 raw=raw.fetches:2",
        confidence=Decimal("0.8"),
    )
    cutoffs = [_utc(2025, 2, 28), _utc(2025, 3, 1), _utc(2025, 6, 1), _utc(2025, 6, 2)]
    projection = project_issuer_rows(
        issuer,
        cutoffs,
        load_facts=_loader({issuer.cik: _doc()}),
        headcount_extractor=PostgresHeadcountExtractor(connection),
        last_close=_no_price,
        ruleset=DEFAULT_RULESET,
    )
    rows = list(projection.rows)

    assert ("headcount", None) not in _at(rows, cutoffs[0])
    assert _at(rows, cutoffs[1])[("headcount", None)].value == Decimal(1200)
    assert _at(rows, cutoffs[1])[("headcount", None)].knowable_at == datetime(2025, 2, 28, 9, 0, tzinfo=UTC)
    assert _at(rows, cutoffs[2])[("headcount", None)].value == Decimal(1200), "a fact knowable on the cutoff date waits"
    assert _at(rows, cutoffs[3])[("headcount", None)].value == Decimal(1300)


class _MemoryStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def store(self, capture: RawCapture) -> RawIngestionEnvelope:
        digest = hashlib.sha256(capture.body).hexdigest()
        ref = RawObjectRef(
            bucket="memory-bucket",
            key=f"{capture.source.value}/{digest}",
            sha256=digest,
            byte_length=len(capture.body),
            content_type=capture.content_type,
        )
        self.objects[ref.key] = capture.body
        return RawIngestionEnvelope(
            source=capture.source,
            source_record_id=capture.source_record_id,
            object=ref,
            fetched_at=capture.fetched_at,
            source_published_at=capture.source_published_at,
            metadata=capture.metadata,
        )

    def get(self, ref: RawObjectRef) -> bytes:
        return self.objects[ref.key]


def _put_vintage(connection, store: _MemoryStore, cik: int, body: bytes, *, ordinal: int) -> None:
    from data_engine.datahub.repository import PostgresCaptureControlRepository  # noqa: PLC0415
    from truealpha_contracts.datahub import SourceRequest, SourceVintage  # noqa: PLC0415
    from truealpha_contracts.universe import SubjectRef  # noqa: PLC0415

    record_id = f"companyfacts:CIK{cik:010d}"
    fetch_id = raw_store.insert_fetch(
        connection,
        source=DataSource.SEC,
        source_record_id=record_id,
        body=body,
        content_type="application/json",
        fetched_at=datetime(2026, 4, 1, tzinfo=UTC) + timedelta(days=ordinal),
        store=store,
        recorded_at=datetime(2026, 4, 1, tzinfo=UTC) + timedelta(days=ordinal),
    )
    request = SourceRequest(
        source_registry_entry_id="source-registry-entry:" + "a" * 64,
        source_policy_id="source-policy:h1139",
        request_fingerprint_version="h1139:v1",
        canonical_request_sha256="b" * 64,
        subject_refs=(SubjectRef(kind="issuer", id=f"issuer:cik:{cik}"),),
        capture_requirement_ids=("financial-fact:v1",),
        partition="2026-04-01",
    )
    repository = PostgresCaptureControlRepository(connection)
    repository.put_source_request(request)
    vintage = SourceVintage(
        source_request_id=request.source_request_id,
        source_record_id=record_id,
        source_published_at=None,
        raw_object_id=f"raw-object:{hashlib.sha256(body).hexdigest()}",
    )
    repository.put_source_vintage(vintage, raw_fetch_id=fetch_id)


def test_stored_company_facts_come_from_the_latest_vintage_bytes(connection) -> None:
    store = _MemoryStore()
    old, new = _doc(range(2018, 2023)), _doc(range(2018, 2026))
    _put_vintage(connection, store, 424242, json.dumps(old).encode(), ordinal=0)
    _put_vintage(connection, store, 424242, json.dumps(new).encode(), ordinal=1)

    loaded = load_stored_company_facts(connection, 424242, store=store)

    assert loaded is not None
    body, document = loaded
    assert document == new
    assert body == json.dumps(new).encode()
    assert load_stored_company_facts(connection, 424243, store=store) is None


def test_the_lineage_query_finds_the_latest_financial_fact_of_each_issuer(connection) -> None:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from production_topt.test_materialization import _seed_complete_production_run  # noqa: PLC0415

    _seed_complete_production_run(connection)
    from data_engine.datahub.strategy_history import latest_financial_observations  # noqa: PLC0415

    seeded = connection.execute(
        """
        select distinct p.normalized_payload ->> 'issuer_id'
        from staging.capture_normalized_observations o
        join staging.capture_observation_payloads p on p.observation_id = o.observation_id
        where o.semantic_type = 'financial-fact' and o.parser_version = 'production-topt-integration-parser:v1'
        """
    ).fetchall()
    issuer_ids = sorted(row[0] for row in seeded)
    assert len(issuer_ids) >= 20

    found = latest_financial_observations(
        connection, issuer_ids, parser_version="production-topt-integration-parser:v1"
    )

    assert sorted(payload["issuer_id"] for payload, _record in found) == issuer_ids
    assert all(record.startswith("production-topt-integration:") for _payload, record in found)
    assert latest_financial_observations(connection, issuer_ids, parser_version="no-such-parser:v0") == []
    assert stored_history_issuers(connection, issuer_ids, parser_version="production-topt-integration-parser:v1") == []


def test_the_deployed_run_fails_when_no_issuer_resolves(connection, monkeypatch) -> None:
    import data_engine.datahub.strategy_history as history

    monkeypatch.setattr(history, "stored_history_issuers", lambda *_args, **_kwargs: [])
    with pytest.raises(RuntimeError, match="no issuer"):
        project_deployed_history(connection, tick=TICK)


def test_the_deployed_run_fails_when_the_projector_writes_and_finds_no_row(connection, monkeypatch) -> None:
    import data_engine.datahub.strategy_history as history

    monkeypatch.setattr(history, "stored_history_issuers", lambda *_args, **_kwargs: [_issuer(9)])
    monkeypatch.setattr(history, "load_stored_company_facts", lambda *_args, **_kwargs: None)
    with pytest.raises(RuntimeError, match="no row"):
        project_deployed_history(connection, tick=TICK, months=3)


def test_the_deployed_run_projects_the_issuers_the_strategy_consumes(connection, monkeypatch) -> None:
    import data_engine.datahub.strategy_history as history

    issuer = _issuer(5)
    connection.execute(
        """
        insert into staging.strategy_backtest_inputs
            (issuer_id, cutoff_at, input_key, value, confidence, knowable_at)
        values (%s, %s, 'revenue', '1', '0.9', %s)
        """,
        (issuer.issuer_id, _utc(2026, 10, 8), _utc(2026, 10, 7)),
    )
    seen: dict[str, Any] = {}

    def fake_issuers(_connection, issuer_ids, **_kwargs):
        seen["issuer_ids"] = list(issuer_ids)
        return [issuer]

    monkeypatch.setattr(history, "stored_history_issuers", fake_issuers)
    monkeypatch.setattr(
        history, "load_stored_company_facts", lambda _c, cik, store=None: _loader({issuer.cik: _doc()})(cik)
    )

    summary = project_deployed_history(connection, tick=TICK, months=6)

    assert seen["issuer_ids"] == [issuer.issuer_id]
    assert (summary.issuers, summary.cutoffs) == (1, 6)
    assert summary.inserted > 0
