"""The operator comparator of `mart.oracle_outcomes` (#1523 S2a).

The first half drives the pure comparison with hand-built snapshot inputs. The second half drives
`record_oracle_outcomes` on a real schema over a frozen snapshot, with operator lists of the test's
own. The third half checks that the report op calls the writer. The deployed tick is in
`production_topt/test_oracle_outcomes_tick.py`.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import dagster as dg
import psycopg
import pytest
from data_engine import raw_store
from data_engine.config import settings
from data_engine.datahub import confidence_report, oracle_outcomes
from data_engine.datahub.oracle_outcomes import (
    AGREE,
    DISAGREE,
    NOT_COMPARABLE,
    OperatorFigure,
    compare_listed_units,
    served_listed_units,
)
from data_engine.datahub.production_topt import PostgresToptCoreRepository
from data_engine.lanes import quality
from data_engine.quality import nightly_verdicts
from factors.production_topt import (
    MetricAvailability,
    MetricFreshness,
    OperatingBranch,
    ToptCellQualityInput,
    ToptCoreSnapshotInput,
    ToptMarketValueComponent,
    ToptMetricInput,
)
from truealpha_contracts.models import DataSource
from truealpha_runtime.testing import InMemoryRawObjectStore

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "support"))
from production_topt.test_materialization import _seed_complete_production_run  # noqa: E402
from split_series_seed import share_count_vintage, store_flat_topt_series  # noqa: E402


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


TOLERANCE = Decimal("0.05")
KNOWN = datetime(2026, 3, 30, tzinfo=UTC)
AS_OF = date(2026, 3, 30)


# -- the pure comparison ---------------------------------------------------------------


def _metric(metric: str, value: Decimal | None, input_id: str) -> ToptMetricInput:
    return ToptMetricInput(
        input_id=input_id,
        metric=metric,
        value=value,
        unit="shares" if metric == "shares_outstanding" else "USD_per_share",
        confidence=Decimal("0.85"),
        knowable_at=KNOWN,
        freshness=MetricFreshness.FRESH,
        availability=MetricAvailability.AVAILABLE if value is not None else MetricAvailability.UNAVAILABLE,
    )


def _factor_input(*listings: tuple[str, Decimal | None]) -> ToptCoreSnapshotInput:
    observation_ids = tuple(f"normalized-observation:{index:064x}" for index in range(1, 4 * len(listings) + 1))
    return ToptCoreSnapshotInput(
        snapshot_id="topt-core-snapshot:" + "b" * 64,
        run_id="capture-run:" + "c" * 64,
        release_manifest_id="release-manifest:" + "d" * 64,
        universe_id="universe:test",
        universe_version="v1",
        universe_sha256="e" * 64,
        cutoff=KNOWN,
        issuer_id="issuer:test",
        instrument_id=f"instrument:{listings[0][0]}",
        listing_id=listings[0][0],
        operating_branch=OperatingBranch.NON_FINANCIAL,
        observation_ids=observation_ids,
        cell_inputs=tuple(
            ToptCellQualityInput(
                input_id=observation_id, confidence=Decimal("0.85"), knowable_at=KNOWN, freshness=MetricFreshness.FRESH
            )
            for observation_id in observation_ids
        ),
        gross_profit=None,
        total_assets=None,
        headcount=None,
        revenue=None,
        pre_provision_profit=None,
        market_value_components=tuple(
            ToptMarketValueComponent(
                instrument_id=f"instrument:{listing}",
                listing_id=listing,
                market_price=_metric("market_price", Decimal("40"), observation_ids[4 * index]),
                shares_outstanding=_metric("shares_outstanding", shares, observation_ids[4 * index]),
            )
            for index, (listing, shares) in enumerate(listings)
        ),
    )


def _figure(cap: str | None, price: str | None, *, row_id: int = 7) -> OperatorFigure:
    return OperatorFigure(
        row_id=row_id,
        ticker="PDD",
        as_of=AS_OF,
        source="nasdaq-index",
        market_cap=None if cap is None else Decimal(cap),
        last_sale_price=None if price is None else Decimal(price),
        knowable_at=KNOWN,
    )


def _compare(factor_input: ToptCoreSnapshotInput, figure: OperatorFigure | None, ticker: str | None = "PDD"):
    return compare_listed_units(
        factor_input,
        {} if figure is None else {figure.ticker: figure},
        ticker=ticker,
        list_as_of=AS_OF,
        tolerance=TOLERANCE,
    )


def test_a_single_listing_serves_its_share_count() -> None:
    assert served_listed_units(_factor_input(("listing:a", Decimal("1000")))) == Decimal("1000")


def test_one_identical_count_on_several_listings_is_the_issuer_total_and_counts_once() -> None:
    assert served_listed_units(_factor_input(("listing:a", Decimal("1000")), ("listing:b", Decimal("1000")))) == 1000


def test_counts_that_differ_add_up_and_a_missing_count_leaves_no_served_value() -> None:
    assert served_listed_units(_factor_input(("listing:a", Decimal("600")), ("listing:b", Decimal("400")))) == 1000
    assert served_listed_units(_factor_input(("listing:a", Decimal("600")), ("listing:b", None))) is None


def test_the_pdd_case_is_a_disagree_with_the_ratio_of_the_cassette_row() -> None:
    """Cap 120,689,786,013 at $86.60 is 1,393,6xx,xxx units; the served ordinary count is 5,693,585,848."""
    outcome = _compare(_factor_input(("listing:pdd", Decimal("5693585848"))), _figure("120689786013", "86.60"))
    assert outcome.result == DISAGREE and outcome.reason is None
    assert outcome.oracle_value == Decimal("120689786013") / Decimal("86.60")
    assert outcome.ratio == Decimal("5693585848") / outcome.oracle_value
    assert Decimal("4.0") <= outcome.ratio <= Decimal("4.2")
    assert (outcome.tolerance, outcome.observation_id, outcome.period) == (
        TOLERANCE,
        "etf-constituent-fact:7",
        "2026-03-30",
    )


@pytest.mark.parametrize(
    ("served", "result"),
    [
        ("1000", AGREE),
        ("1050", AGREE),  # exactly at the tolerance
        ("950", AGREE),
        ("1051", DISAGREE),
        ("949", DISAGREE),
        ("2000", DISAGREE),  # a class counted twice
    ],
)
def test_the_ratio_is_judged_against_the_stored_tolerance(served: str, result: str) -> None:
    outcome = _compare(_factor_input(("listing:a", Decimal(served))), _figure("40000", "40"))  # 1000 units
    assert outcome.result == result and outcome.tolerance == TOLERANCE


@pytest.mark.parametrize(
    ("figure", "ticker", "served", "reason"),
    [
        (None, "PDD", "1000", "no_operator_row"),
        (_figure("40000", "40"), None, "1000", "no_operator_row"),
        (_figure(None, "40"), "PDD", "1000", "no_market_cap"),
        (_figure("40000", None), "PDD", "1000", "no_price"),
        (_figure("40000", "0"), "PDD", "1000", "no_price"),
        (_figure("40000", "40"), "PDD", None, "no_served_value"),
    ],
)
def test_a_comparison_that_cannot_be_made_says_why(
    figure: OperatorFigure | None, ticker: str | None, served: str | None, reason: str
) -> None:
    shares = None if served is None else Decimal(served)
    outcome = _compare(_factor_input(("listing:a", shares)), figure, ticker)
    assert (outcome.result, outcome.reason) == (NOT_COMPARABLE, reason)
    assert outcome.ratio is None and outcome.tolerance is None


# -- the writer on a real schema -------------------------------------------------------


def _frozen(connection) -> tuple[Any, dict[str, Decimal]]:
    """A frozen snapshot of the seeded TOPT corpus and the served share count of each of its issuers.

    The stored flat series and the share-count vintage let the member builder keep the filed count
    beside the close (#1277); without them every count is dropped and nothing is served.
    """
    store_flat_topt_series(connection, last=date(2026, 4, 1))
    (_, run, _, release_manifest_id, *_rest) = _seed_complete_production_run(
        connection,
        financial_vintage={
            "shares_outstanding": share_count_vintage(filed=date(2026, 3, 2), own_date=date(2026, 2, 20))
        },
    )
    PostgresToptCoreRepository(connection).freeze_snapshot(run_id=run.run_id, release_manifest_id=release_manifest_id)
    rows = connection.execute(
        """
        select member.factor_input from staging.topt_core_snapshots snapshot
        join staging.topt_core_snapshot_members member on member.snapshot_id = snapshot.snapshot_id
        where snapshot.run_id = %s
        """,
        (run.run_id,),
    ).fetchall()
    served = {
        row[0]["listing_id"]: Decimal(row[0]["market_value_components"][0]["shares_outstanding"]["value"])
        for row in rows
    }
    assert len(served) == 20 and all(count > 0 for count in served.values())
    return run, served


def _cutoff(connection, run_id: str) -> datetime:
    return connection.execute("select cutoff from mart.topt_capture_status where run_id = %s", (run_id,)).fetchone()[0]


def _operator_list(connection, figures: dict[str, tuple[str | None, str | None]], *, knowable_at: datetime) -> None:
    """One list of the operator, as the weekly refresh lands it."""
    fetch_id = raw_store.insert_fetch(
        connection,
        source=DataSource.NASDAQ_INDEX,
        source_record_id=f"index-constituents:qqq:{knowable_at.isoformat()}",
        body=json.dumps({"tickers": sorted(figures)}).encode(),
        content_type="application/json",
        fetched_at=knowable_at,
        recorded_at=knowable_at,
        store=InMemoryRawObjectStore(),
    )
    for index, (ticker, (cap, price)) in enumerate(sorted(figures.items())):
        connection.execute(
            """
            insert into staging.etf_constituent_facts
                (etf_symbol, as_of, source, raw_fetch_id, ticker, company_name, market_cap, last_sale_price,
                 cik, figi, knowable_at)
            values ('qqq', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                knowable_at.date(),
                DataSource.NASDAQ_INDEX.value,
                fetch_id,
                ticker,
                ticker,
                cap,
                price,
                900000 + index,
                f"oracle{index:07d}",
                knowable_at,
            ),
        )


def _rows(connection, run_id: str) -> dict[str, tuple]:
    found = connection.execute(
        "select subject_id, result, reason, ratio, tolerance, period, compared_at from mart.oracle_outcomes "
        "where run_id = %s",
        (run_id,),
    ).fetchall()
    return {row[0]: row for row in found}


def test_a_run_with_no_operator_list_writes_nothing(connection) -> None:
    run, _served = _frozen(connection)
    assert oracle_outcomes.record_oracle_outcomes(connection, run.run_id, compared_at=KNOWN) == 0
    assert _rows(connection, run.run_id) == {}


def test_an_operator_list_after_the_cutoff_is_no_witness(connection) -> None:
    run, _served = _frozen(connection)
    _operator_list(
        connection, {"AAPL": ("400000000", "40")}, knowable_at=_cutoff(connection, run.run_id) + timedelta(days=1)
    )
    assert oracle_outcomes.record_oracle_outcomes(connection, run.run_id, compared_at=KNOWN) == 0


def test_every_issuer_gets_one_row_with_its_result_and_a_replay_writes_nothing(connection) -> None:
    run, served = _frozen(connection)
    cutoff = _cutoff(connection, run.run_id)
    tickers = oracle_outcomes.run_tickers(connection, run.run_id)
    by_ticker = {ticker: listing for listing, ticker in tickers.items() if listing in served}
    price = Decimal("40")
    _operator_list(
        connection,
        {
            # units equal to the served count: ratio 1
            "AAPL": (str(served[by_ticker["AAPL"]] * price), "40"),
            # units four times smaller than the served count: ratio 4
            "NVDA": (str(served[by_ticker["NVDA"]] * price / 4), "40"),
            "MSFT": ("400000000", None),
            "COST": (None, "40"),
        },
        knowable_at=cutoff - timedelta(days=1),
    )

    written = oracle_outcomes.record_oracle_outcomes(connection, run.run_id, compared_at=KNOWN)
    rows = _rows(connection, run.run_id)

    assert written == 20 == len(rows)
    subject = {ticker: rows[_issuer_of(connection, run.run_id, by_ticker[ticker])] for ticker in by_ticker}
    assert subject["AAPL"][1] == AGREE and subject["AAPL"][3] == Decimal(1)
    assert subject["NVDA"][1] == DISAGREE and subject["NVDA"][3] == Decimal(4)
    assert (subject["MSFT"][1], subject["MSFT"][2]) == (NOT_COMPARABLE, "no_price")
    assert (subject["COST"][1], subject["COST"][2]) == (NOT_COMPARABLE, "no_market_cap")
    absent = {ticker for ticker, row in subject.items() if row[2] == "no_operator_row"}
    assert absent == set(by_ticker) - {"AAPL", "NVDA", "MSFT", "COST"}
    # Each compared row stores the policy tolerance; the others store none. The run clock dates the row.
    assert {row[4] for row in rows.values() if row[1] != NOT_COMPARABLE} == {TOLERANCE}
    assert {row[4] for row in rows.values() if row[1] == NOT_COMPARABLE} == {None}
    assert {row[6] for row in rows.values()} == {KNOWN}
    # A replay of the same run, even with another clock, writes nothing.
    assert oracle_outcomes.record_oracle_outcomes(connection, run.run_id, compared_at=KNOWN + timedelta(days=1)) == 0
    assert len(_rows(connection, run.run_id)) == 20


def _issuer_of(connection, run_id: str, listing_id: str) -> str:
    return connection.execute(
        """
        select member.factor_input ->> 'issuer_id' from staging.topt_core_snapshots snapshot
        join staging.topt_core_snapshot_members member on member.snapshot_id = snapshot.snapshot_id
        where snapshot.run_id = %s and member.factor_input ->> 'listing_id' = %s
        """,
        (run_id, listing_id),
    ).fetchone()[0]


class _Log:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def warning(self, message: str) -> None:
        self.messages.append(message)


def test_a_failing_writer_is_logged_and_leaves_the_connection_usable(connection, monkeypatch) -> None:
    run, _served = _frozen(connection)

    def broken(*_args: object, **_kwargs: object) -> int:
        raise RuntimeError("the writer is broken")

    monkeypatch.setattr(oracle_outcomes, "record_oracle_outcomes", broken)
    log = _Log()
    assert oracle_outcomes.record_oracle_outcomes_safely(connection, run.run_id, compared_at=KNOWN, log=log) is None
    assert len(log.messages) == 1 and "RuntimeError" in log.messages[0] and run.run_id in log.messages[0]
    assert connection.execute("select 1").fetchone() == (1,), "the transaction of the failed write was rolled back"


# -- the report op ---------------------------------------------------------------------


def test_the_report_op_records_the_outcomes_of_the_head_it_reported(monkeypatch) -> None:
    """The nightly op is the second caller: it passes its own run clock and the run of the report."""
    executed_at = "2026-09-16T00:45:00+00:00"
    report = {
        "universe_id": "universe:test",
        "cutoff": "2026-09-15T00:00:00+00:00",
        "run_id": "capture-run:" + "9" * 64,
        "sources_connected": [],
        "families": {},
        "accuracy": {"close": {"agreement_rate": "1"}, "sec_oracle": {"issuers_compared": 0}},
    }

    class Connection:
        def __enter__(self) -> Connection:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def commit(self) -> None:
            return None

    calls: list[tuple[str, datetime]] = []

    def spy(_connection: object, run_id: str, *, compared_at: datetime, log: object) -> int:
        calls.append((run_id, compared_at))
        return 3

    monkeypatch.setattr(psycopg, "connect", lambda *_a, **_k: Connection())
    monkeypatch.setattr(nightly_verdicts, "record", lambda *_a, **_k: None)
    monkeypatch.setattr(quality, "_await_settled", lambda _context: None)
    monkeypatch.setattr(quality, "stale_served_heads", lambda *_a, **_k: [])
    monkeypatch.setattr(confidence_report, "compile_report", lambda *_a, **_k: report)
    monkeypatch.setattr(confidence_report, "persist", lambda *_a, **_k: "datahub-confidence-report:" + "1" * 64)
    monkeypatch.setattr(quality, "record_oracle_outcomes_safely", spy)
    context = dg.build_op_context()

    quality.run_confidence_report(context, quality.ConfidenceReportConfig(executed_at=executed_at, universe="topt"))

    assert calls == [(report["run_id"], datetime.fromisoformat(executed_at))]
    assert context.get_output_metadata("result")["oracle_outcomes_rows"] == 3
