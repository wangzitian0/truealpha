"""The comparators of `mart.oracle_outcomes` (#1523 S2): the operator listed units and the independent parse.

The first part drives the pure comparisons with hand-built snapshot inputs and stored documents. The second
part drives `record_oracle_outcomes` on a real schema over a frozen snapshot, with operator lists and stored
SEC documents of the test's own. The third part checks that the report op calls the writer. The deployed tick
is in `production_topt/test_oracle_outcomes_tick.py`.
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
    FILING_KIND,
    INDEPENDENT_COMPARATOR,
    KNOWN_ANSWERS_COMPARATOR,
    NO_OBSERVATION,
    NO_SERVED_VALUE,
    NOT_COMPARABLE,
    OPERATOR_COMPARATOR,
    PRIOR_PERIOD,
    SUPERSEDED,
    OperatorFigure,
    canonical_metric,
    compare_independent_parse,
    compare_known_answer,
    compare_listed_units,
    served_listed_units,
)
from data_engine.datahub.production_topt import PostgresToptCoreRepository
from data_engine.lanes import quality
from data_engine.quality import nightly_verdicts, vendor_oracle
from data_engine.sources import gateway
from factors.production_topt import (
    MetricAvailability,
    MetricFreshness,
    OperatingBranch,
    ToptCellQualityInput,
    ToptCoreSnapshotInput,
    ToptMarketValueComponent,
    ToptMetricInput,
)
from truealpha_contracts.environment_contract import SentinelKnownAnswer
from truealpha_contracts.models import DataSource
from truealpha_contracts.oracle_policy import load_oracle_policy
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


@pytest.fixture(autouse=True)
def stored_bytes(monkeypatch: pytest.MonkeyPatch) -> InMemoryRawObjectStore:
    """The object store of the test. A seeded raw fetch holds bytes only where the test puts them."""
    store = InMemoryRawObjectStore()
    monkeypatch.setattr(raw_store, "object_store", lambda: store)
    return store


TOLERANCE = Decimal("0.05")
KNOWN = datetime(2026, 3, 30, tzinfo=UTC)
AS_OF = date(2026, 3, 30)


# -- the pure comparison ---------------------------------------------------------------


def _metric(metric: str, value: Decimal | None, input_id: str, unit: str | None = None) -> ToptMetricInput:
    del unit
    return ToptMetricInput(
        input_id=input_id,
        metric=metric,
        value=value,
        confidence=Decimal("0.85"),
        knowable_at=KNOWN,
        freshness=MetricFreshness.FRESH,
        availability=MetricAvailability.AVAILABLE if value is not None else MetricAvailability.UNAVAILABLE,
    )


def _factor_input(
    *listings: tuple[str, Decimal | None],
    revenue: Decimal | None = None,
    gross_profit: Decimal | None = None,
    unit: str | None = None,
) -> ToptCoreSnapshotInput:
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
        gross_profit=_metric("gross_profit", gross_profit, observation_ids[0], unit),
        total_assets=None,
        headcount=None,
        revenue=_metric("revenue", revenue, observation_ids[0], unit),
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


# -- the independent parse, pure ---------------------------------------------------------

INDEPENDENT_TOLERANCE = load_oracle_policy().relative_tolerance(INDEPENDENT_COMPARATOR)
CUTOFF_DAY = date(2026, 4, 1)


def _annual(end: str, value: str, filed: str, *, start: str) -> dict:
    return {"start": start, "end": end, "val": int(value), "filed": filed}


def _document(**concepts: list[dict]) -> dict:
    """A company-facts document that holds the given us-gaap concepts in US dollars."""
    return {"facts": {"us-gaap": {name: {"units": {"USD": rows}} for name, rows in concepts.items()}}}


#: Two annual revenues under the modern tag: FY2024 (filed 2025-02-10) and FY2025 (filed 2026-02-20).
REVENUE_DOCUMENT = _document(
    RevenueFromContractWithCustomerExcludingAssessedTax=[
        _annual("2024-12-31", "800", "2025-02-10", start="2024-01-01"),
        _annual("2025-12-31", "1000", "2026-02-20", start="2025-01-01"),
    ]
)


def _independent(
    field: str, served: str | None, facts: dict | None, *, unit: str | None = None, cutoff: date = CUTOFF_DAY
):
    value = None if served is None else Decimal(served)
    factor_input = _factor_input(("listing:a", Decimal("1")), **{field: value}, unit=unit)
    return compare_independent_parse(
        factor_input, field, facts, cutoff=cutoff, tolerance=INDEPENDENT_TOLERANCE, currency=unit
    )


def test_a_served_revenue_that_the_stored_filing_repeats_agrees_and_the_row_carries_its_numbers() -> None:
    outcome = _independent("revenue", "1000", REVENUE_DOCUMENT)
    assert (outcome.result, outcome.reason, outcome.ratio) == (AGREE, None, Decimal(1))
    assert (outcome.served_value, outcome.oracle_value) == (Decimal(1000), Decimal(1000))
    assert (outcome.oracle_kind, outcome.field, outcome.source) == ("independent_parse", "revenue", "sec")
    assert outcome.period == "2025-12-31"
    assert outcome.knowable_at == datetime(2026, 2, 20, tzinfo=UTC)
    assert outcome.observation_id == "normalized-observation:" + "0" * 63 + "1"
    assert outcome.tolerance == INDEPENDENT_TOLERANCE


def test_a_revenue_ten_times_the_stored_filing_is_a_disagree_row_with_the_ratio_of_ten() -> None:
    outcome = _independent("revenue", "10000", REVENUE_DOCUMENT)
    assert (outcome.result, outcome.ratio, outcome.period) == (DISAGREE, Decimal(10), "2025-12-31")


def test_a_served_revenue_of_an_older_period_than_the_stored_filing_is_a_disagree_row() -> None:
    """The stale-period case: the issuer filed FY2025, and the served figure is the FY2024 one."""
    outcome = _independent("revenue", "800", REVENUE_DOCUMENT)
    assert (outcome.result, outcome.ratio, outcome.period) == (DISAGREE, Decimal("0.8"), "2025-12-31")


def test_a_filing_after_the_cutoff_is_no_witness() -> None:
    """The cutoff is 2026-02-01, before the FY2025 filing: the newest knowable annual figure is FY2024."""
    outcome = _independent("revenue", "800", REVENUE_DOCUMENT, cutoff=date(2026, 2, 1))
    assert (outcome.result, outcome.ratio, outcome.period) == (AGREE, Decimal(1), "2024-12-31")
    assert outcome.knowable_at == datetime(2025, 2, 10, tzinfo=UTC)


@pytest.mark.parametrize(
    ("factor", "result"),
    [
        (Decimal(1), AGREE),
        (1 + INDEPENDENT_TOLERANCE, AGREE),  # exactly at the tolerance
        (1 - INDEPENDENT_TOLERANCE, AGREE),
        (1 + 2 * INDEPENDENT_TOLERANCE, DISAGREE),
        (1 - 2 * INDEPENDENT_TOLERANCE, DISAGREE),
    ],
)
def test_the_ratio_is_judged_against_the_tolerance_of_the_policy(factor: Decimal, result: str) -> None:
    outcome = _independent("revenue", str(Decimal(1000) * factor), REVENUE_DOCUMENT)
    assert outcome.result == result and outcome.tolerance == INDEPENDENT_TOLERANCE


def test_gross_profit_is_the_difference_that_the_stored_filing_implies_when_it_reports_none() -> None:
    """No `GrossProfit` tag: revenue 1000 minus cost 400, knowable from the later filing of the two."""
    document = _document(
        Revenues=[_annual("2025-12-31", "1000", "2026-02-20", start="2025-01-01")],
        CostOfRevenue=[_annual("2025-12-31", "400", "2026-03-05", start="2025-01-01")],
    )
    outcome = _independent("gross_profit", "600", document)
    assert (outcome.result, outcome.oracle_value, outcome.field) == (AGREE, Decimal(600), "gross_profit")
    assert outcome.knowable_at == datetime(2026, 3, 5, tzinfo=UTC)


def test_the_two_fields_of_one_issuer_are_two_comparisons() -> None:
    factor_input = _factor_input(("listing:a", Decimal("1")), revenue=Decimal(1000), gross_profit=Decimal(1))
    document = _document(
        Revenues=[_annual("2025-12-31", "1000", "2026-02-20", start="2025-01-01")],
        GrossProfit=[_annual("2025-12-31", "500", "2026-02-20", start="2025-01-01")],
    )
    results = {
        field: compare_independent_parse(
            factor_input, field, document, cutoff=CUTOFF_DAY, tolerance=INDEPENDENT_TOLERANCE
        ).result
        for field in oracle_outcomes.INDEPENDENT_PARSERS
    }
    assert results == {"revenue": AGREE, "gross_profit": DISAGREE}


@pytest.mark.parametrize(
    ("served", "facts", "unit", "reason"),
    [
        ("1000", None, None, "no_document"),
        ("1000", {"facts": {}}, None, "no_oracle_value"),
        (
            "1000",
            _document(Revenues=[_annual("2025-12-31", "1000", "2026-02-20", start="2025-12-01")]),
            None,
            "no_oracle_value",
        ),
        (None, REVENUE_DOCUMENT, None, "no_served_value"),
        ("1000", REVENUE_DOCUMENT, "EUR", "unit_not_usd"),
        (
            "1000",
            _document(Revenues=[_annual("2025-12-31", "0", "2026-02-20", start="2025-01-01")]),
            None,
            "zero_oracle_value",
        ),
    ],
)
def test_a_comparison_that_cannot_be_made_says_why_and_stores_no_ratio(
    served: str | None, facts: dict | None, unit: str | None, reason: str
) -> None:
    """The third document holds a one-month figure only: no annual period, so no witness."""
    outcome = _independent("revenue", served, facts, unit=unit)
    assert (outcome.result, outcome.reason) == (NOT_COMPARABLE, reason)
    assert outcome.ratio is None and outcome.tolerance is None


def test_a_comparison_without_a_document_holds_the_cutoff_day_as_its_period() -> None:
    assert _independent("revenue", "1000", None).period == CUTOFF_DAY.isoformat()


def test_a_served_value_without_a_witness_keeps_the_witness_it_could_not_judge() -> None:
    outcome = _independent("revenue", None, REVENUE_DOCUMENT)
    assert (outcome.served_value, outcome.oracle_value) == (None, Decimal(1000))
    assert outcome.knowable_at == datetime(2026, 2, 20, tzinfo=UTC)


@pytest.mark.parametrize(
    ("served", "witness", "result", "reason", "ratio"),
    [
        ("-5", "5", DISAGREE, "sign_differs", Decimal(1)),  # the table holds no negative ratio
        ("5", "-5", DISAGREE, "sign_differs", Decimal(1)),
        ("-5", "-5", AGREE, None, Decimal(1)),  # a gross loss that the filing repeats
        ("0", "5", DISAGREE, None, Decimal(0)),
    ],
)
def test_a_figure_of_the_other_sign_is_a_disagree_row_with_a_ratio_the_table_can_hold(
    served: str, witness: str, result: str, reason: str | None, ratio: Decimal
) -> None:
    document = _document(Revenues=[_annual("2025-12-31", witness, "2026-02-20", start="2025-01-01")])
    outcome = _independent("revenue", served, document)
    assert (outcome.result, outcome.reason, outcome.ratio) == (result, reason, ratio)
    assert (outcome.served_value, outcome.oracle_value) == (Decimal(served), Decimal(witness))


# -- the known answers, pure -------------------------------------------------------------

KNOWN_ANSWERS_TOLERANCE = load_oracle_policy().relative_tolerance(KNOWN_ANSWERS_COMPARATOR)


def _known_answer(
    *,
    metric: str = "revenue",
    kind: str = "numeric",
    expected_value: str | None = "1000",
    lower_bound: str | None = None,
    upper_bound: str | None = None,
    relative_tolerance: str | None = "0.01",
    period_end: str = "2025-12-31",
) -> SentinelKnownAnswer:
    return SentinelKnownAnswer(
        answer_id=f"test-{metric}",
        ticker="TEST",
        issuer_id="issuer:cik:0000000001",
        cik="0000000001",
        metric=metric,
        period_end=period_end,
        accession="0000000001-26-000001",
        kind=kind,
        expected_value=expected_value,
        lower_bound=lower_bound,
        upper_bound=upper_bound,
        relative_tolerance=relative_tolerance,
        source_citation="Test citation",
    )


def test_a_served_revenue_that_matches_known_answer_agrees() -> None:
    answer = _known_answer(metric="revenue", expected_value="1000")
    outcome = compare_known_answer(
        answer,
        Decimal("1000"),
        actual_period_end="2025-12-31",
        observation_id="normalized-observation:1",
        knowable_at=KNOWN,
        policy_tolerance=KNOWN_ANSWERS_TOLERANCE,
    )
    assert (outcome.result, outcome.ratio, outcome.period) == (AGREE, Decimal(1), "2025-12-31")
    assert (outcome.served_value, outcome.oracle_value) == (Decimal(1000), Decimal(1000))
    assert outcome.oracle_kind == FILING_KIND
    assert outcome.field == "revenue"
    assert outcome.source == "0000000001-26-000001"
    assert outcome.tolerance == Decimal("0.01")


def test_a_revenue_ten_times_known_answer_is_one_disagree_with_ratio_ten() -> None:
    """#1523 S2 completeness criterion: Revenue x10 gives one disagree."""
    answer = _known_answer(metric="revenue", expected_value="1000")
    outcome = compare_known_answer(
        answer,
        Decimal("10000"),
        actual_period_end="2025-12-31",
        observation_id="normalized-observation:1",
        knowable_at=KNOWN,
        policy_tolerance=KNOWN_ANSWERS_TOLERANCE,
    )
    assert (outcome.result, outcome.ratio, outcome.period) == (DISAGREE, Decimal(10), "2025-12-31")
    assert outcome.tolerance == Decimal("0.01")


def test_a_newer_period_than_known_answer_gives_superseded() -> None:
    """#1523 S2 completeness criterion: a newer period gives superseded."""
    answer = _known_answer(metric="revenue", expected_value="1000", period_end="2025-09-27")
    outcome = compare_known_answer(
        answer,
        Decimal("1000"),
        actual_period_end="2025-12-31",
        observation_id="normalized-observation:1",
        knowable_at=KNOWN,
        policy_tolerance=KNOWN_ANSWERS_TOLERANCE,
    )
    assert (outcome.result, outcome.reason, outcome.period) == (NOT_COMPARABLE, SUPERSEDED, "2025-09-27")
    assert outcome.ratio is None and outcome.tolerance is None


def test_an_older_period_than_known_answer_gives_prior_period() -> None:
    answer = _known_answer(metric="revenue", expected_value="1000", period_end="2025-12-31")
    outcome = compare_known_answer(
        answer,
        Decimal("1000"),
        actual_period_end="2024-12-31",
        observation_id="normalized-observation:1",
        knowable_at=KNOWN,
        policy_tolerance=KNOWN_ANSWERS_TOLERANCE,
    )
    assert (outcome.result, outcome.reason) == (NOT_COMPARABLE, PRIOR_PERIOD)


def test_a_missing_served_value_gives_no_served_value() -> None:
    answer = _known_answer(metric="revenue", expected_value="1000")
    outcome = compare_known_answer(
        answer,
        None,
        actual_period_end="2025-12-31",
        observation_id="normalized-observation:1",
        knowable_at=KNOWN,
        policy_tolerance=KNOWN_ANSWERS_TOLERANCE,
    )
    assert (outcome.result, outcome.reason) == (NOT_COMPARABLE, NO_SERVED_VALUE)


def test_a_missing_observation_id_gives_no_observation() -> None:
    answer = _known_answer(metric="revenue", expected_value="1000")
    outcome = compare_known_answer(
        answer,
        Decimal("1000"),
        actual_period_end="2025-12-31",
        observation_id=None,
        knowable_at=KNOWN,
        policy_tolerance=KNOWN_ANSWERS_TOLERANCE,
    )
    assert (outcome.result, outcome.reason) == (NOT_COMPARABLE, NO_OBSERVATION)


def test_a_band_known_answer_agrees_inside_and_disagrees_outside() -> None:
    answer = _known_answer(
        metric="gppe",
        kind="band",
        lower_bound="300000",
        upper_bound="4000000",
        expected_value=None,
        relative_tolerance=None,
    )
    inside = compare_known_answer(
        answer,
        Decimal("1070000"),
        actual_period_end="2025-12-31",
        observation_id="normalized-observation:1",
        knowable_at=KNOWN,
        policy_tolerance=KNOWN_ANSWERS_TOLERANCE,
    )
    assert inside.result == AGREE
    assert inside.field == "gross_profit_per_employee"
    assert inside.oracle_value == Decimal("2150000")

    outside = compare_known_answer(
        answer,
        Decimal("10700000"),
        actual_period_end="2025-12-31",
        observation_id="normalized-observation:1",
        knowable_at=KNOWN,
        policy_tolerance=KNOWN_ANSWERS_TOLERANCE,
    )
    assert outside.result == DISAGREE


def test_canonical_metric_maps_gppe_and_headcount_to_registered_names() -> None:
    assert canonical_metric("gppe") == "gross_profit_per_employee"
    assert canonical_metric("headcount") == "employees_total"
    assert canonical_metric("revenue") == "revenue"


def test_every_comparator_of_the_writer_has_a_tolerance_in_the_policy() -> None:
    policy = load_oracle_policy()
    assert {OPERATOR_COMPARATOR, INDEPENDENT_COMPARATOR, KNOWN_ANSWERS_COMPARATOR} <= set(policy.comparators)
    assert policy.relative_tolerance(INDEPENDENT_COMPARATOR) == Decimal("0.0001")
    assert policy.relative_tolerance(KNOWN_ANSWERS_COMPARATOR) == Decimal("0.01")


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
    """The operator rows of the run by issuer."""
    found = connection.execute(
        "select subject_id, result, reason, ratio, tolerance, period, compared_at from mart.oracle_outcomes "
        "where run_id = %s and oracle_kind = 'operator'",
        (run_id,),
    ).fetchall()
    return {row[0]: row for row in found}


def test_a_run_with_no_operator_list_writes_no_operator_row(connection) -> None:
    """The independent parse and filing comparisons still run: 20 issuers (40 independent parse, 6 filing)."""
    run, _served = _frozen(connection)
    assert oracle_outcomes.record_oracle_outcomes(connection, run.run_id, compared_at=KNOWN) == 46
    assert _rows(connection, run.run_id) == {}


def test_an_operator_list_after_the_cutoff_is_no_witness(connection) -> None:
    run, _served = _frozen(connection)
    _operator_list(
        connection, {"AAPL": ("400000000", "40")}, knowable_at=_cutoff(connection, run.run_id) + timedelta(days=1)
    )
    assert oracle_outcomes.record_oracle_outcomes(connection, run.run_id, compared_at=KNOWN) == 46
    assert _rows(connection, run.run_id) == {}


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

    assert written == 66 and len(rows) == 20, "20 operator rows, 40 independent parse rows, and 6 filing rows"
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


# -- the independent parse on a real schema ---------------------------------------------

#: What the seeded financial-fact observations serve: revenue for every issuer, gross profit for all but the bank.
SEEDED_REVENUE = "100000000"
SEEDED_GROSS_PROFIT = "210000000"


def _filing(revenue: str, gross_profit: str | None = SEEDED_GROSS_PROFIT, *, refiled_later: str | None = None) -> dict:
    """A company-facts document with one annual figure of each field, filed before the seeded cutoff.

    `refiled_later` adds the next year's revenue, filed after the seeded cutoff of 2026-04-02.
    """
    revenues = [_annual("2025-12-31", revenue, "2026-02-20", start="2025-01-01")]
    if refiled_later is not None:
        revenues.append(_annual("2026-12-31", refiled_later, "2027-02-20", start="2026-01-01"))
    concepts = {"Revenues": revenues}
    if gross_profit is not None:
        concepts["GrossProfit"] = [_annual("2025-12-31", gross_profit, "2026-02-20", start="2025-01-01")]
    return _document(**concepts)


def _financial_fetches(connection, run_id: str) -> dict[str, tuple[int, str]]:
    """Listing id -> (raw fetch id, object key) of the bytes behind its financial-fact observation."""
    rows = connection.execute(
        """
        select p.normalized_payload ->> 'listing_id', stored.id, stored.object_uri
        from raw.capture_obligations ob
        join staging.capture_observation_obligations oo on oo.capture_obligation_id = ob.obligation_id
        join staging.capture_normalized_observations o on o.observation_id = oo.observation_id
        join staging.capture_observation_payloads p on p.observation_id = o.observation_id
        join raw.capture_source_vintages v on v.source_vintage_id = o.source_vintage_id
        join raw.fetches stored on stored.id = v.raw_fetch_id
        where ob.run_id = %s and o.semantic_type = 'financial-fact'
        """,
        (run_id,),
    ).fetchall()
    return {listing: (fetch_id, uri.removeprefix("s3://").partition("/")[2]) for listing, fetch_id, uri in rows}


def _store_filings(connection, run_id: str, store: InMemoryRawObjectStore, filings: dict[str, bytes]) -> None:
    """Put the bytes of each ticker's filing where its financial-fact observation cites them."""
    fetches = _financial_fetches(connection, run_id)
    for listing, ticker in oracle_outcomes.run_tickers(connection, run_id).items():
        if ticker in filings and listing in fetches:
            store.objects[fetches[listing][1]] = filings[ticker]


def _independent_rows(connection, run_id: str, served: dict[str, Decimal]) -> dict[tuple[str, str], tuple]:
    """(ticker, field) -> (result, reason, ratio, tolerance, period, served value, oracle value)."""
    ticker_of = {
        _issuer_of(connection, run_id, listing): ticker
        for listing, ticker in oracle_outcomes.run_tickers(connection, run_id).items()
        if listing in served
    }
    rows = connection.execute(
        "select subject_id, field, result, reason, ratio, tolerance, period, served_value, oracle_value "
        "from mart.oracle_outcomes where run_id = %s and oracle_kind = 'independent_parse'",
        (run_id,),
    ).fetchall()
    return {(ticker_of[row[0]], row[1]): row[2:] for row in rows}


def test_the_whole_head_is_reparsed_from_its_stored_bytes_and_a_replay_writes_nothing(
    connection, stored_bytes: InMemoryRawObjectStore
) -> None:
    run, served = _frozen(connection)
    filings = {
        "AAPL": _filing("1000000000"),  # the served revenue is a tenth of the filing
        "NVDA": _filing(SEEDED_REVENUE, refiled_later="999"),  # the later filing is after the cutoff: no witness
        "JPM": _filing(SEEDED_REVENUE, "5"),  # the bank serves no gross profit
    }
    _store_filings(connection, run.run_id, stored_bytes, {t: json.dumps(f).encode() for t, f in filings.items()})

    written = oracle_outcomes.record_oracle_outcomes(connection, run.run_id, compared_at=KNOWN)
    rows = _independent_rows(connection, run.run_id, served)
    tolerance = load_oracle_policy().relative_tolerance(INDEPENDENT_COMPARATOR)

    assert written == 46 and len(rows) == 40, "twenty issuers, two fields each plus six filing rows; no operator row"
    assert rows[("AAPL", "revenue")] == (
        DISAGREE,
        None,
        Decimal("0.1"),
        tolerance,
        "2025-12-31",
        Decimal(SEEDED_REVENUE),
        Decimal("1000000000"),
    )
    for ticker, field in [("AAPL", "gross_profit"), ("NVDA", "revenue"), ("NVDA", "gross_profit"), ("JPM", "revenue")]:
        assert rows[(ticker, field)][:3] == (AGREE, None, Decimal(1)), (ticker, field)
    assert rows[("JPM", "gross_profit")][:2] == (NOT_COMPARABLE, "no_served_value")
    cutoff_day = _cutoff(connection, run.run_id).date().isoformat()
    missing = {key: row for key, row in rows.items() if key[0] not in filings}
    assert len(missing) == 34 and {(row[0], row[1], row[4]) for row in missing.values()} == {
        (NOT_COMPARABLE, "no_document", cutoff_day)
    }
    # A replay of the same run, even with another clock, writes nothing.
    assert oracle_outcomes.record_oracle_outcomes(connection, run.run_id, compared_at=KNOWN + timedelta(days=1)) == 0


def test_both_comparators_write_through_the_one_function(connection, stored_bytes: InMemoryRawObjectStore) -> None:
    run, served = _frozen(connection)
    cutoff = _cutoff(connection, run.run_id)
    by_ticker = {t: listing for listing, t in oracle_outcomes.run_tickers(connection, run.run_id).items()}
    _operator_list(
        connection,
        {"AAPL": (str(served[by_ticker["AAPL"]] * 40), "40")},
        knowable_at=cutoff - timedelta(days=1),
    )
    _store_filings(connection, run.run_id, stored_bytes, {"AAPL": json.dumps(_filing(SEEDED_REVENUE)).encode()})

    written = oracle_outcomes.record_oracle_outcomes(connection, run.run_id, compared_at=KNOWN)

    kinds = connection.execute(
        "select oracle_kind, count(*) from mart.oracle_outcomes where run_id = %s group by 1 order by 1",
        (run.run_id,),
    ).fetchall()
    assert kinds == [("filing", 6), ("independent_parse", 40), ("operator", 20)] and written == 66
    assert oracle_outcomes.record_oracle_outcomes(connection, run.run_id, compared_at=KNOWN) == 0


def test_a_document_that_cannot_be_read_is_no_witness(connection, stored_bytes: InMemoryRawObjectStore) -> None:
    run, _served = _frozen(connection)
    fetch_id, key = next(iter(_financial_fetches(connection, run.run_id).values()))
    assert oracle_outcomes.stored_document(connection, fetch_id) is None, "the store holds no object under the key"
    for body in (b"not json", b"[1, 2]"):
        stored_bytes.objects[key] = body
        assert oracle_outcomes.stored_document(connection, fetch_id) is None, body
    stored_bytes.objects[key] = b'{"facts": {}}'
    assert oracle_outcomes.stored_document(connection, fetch_id) == {"facts": {}}


def test_the_writer_makes_no_vendor_call(connection, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every external request goes through the gateway or the oracle's own fetchers. The writer calls none."""

    def refused(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the writer made a vendor call")

    for name in ("fetch_once", "urlopen", "http_get"):
        monkeypatch.setattr(gateway, name, refused)
    for name in ("_http_json", "ticker_cik_index", "company_facts"):
        monkeypatch.setattr(vendor_oracle, name, refused)
    run, _served = _frozen(connection)
    assert oracle_outcomes.record_oracle_outcomes(connection, run.run_id, compared_at=KNOWN) == 46


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
        "accuracy": {"close": {"agreement_rate": "1"}},
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
    metadata = context.get_output_metadata("result")
    assert metadata["oracle_outcomes_rows"] == 3
    assert "sec_oracle_issuers_compared" not in metadata


def test_ten_outcomes_roll_up_with_deduplication_and_wilson_bound() -> None:
    """#1523 S3: 10 outcomes (8 agree, 1 disagree, 1 not_comparable) roll up to compared=9,
    disagree=1, not_comparable=1, deduplicated once per (observation_id, subject_id, field)."""
    from data_engine.datahub.oracle_outcomes import rollup_oracle_outcomes, wilson_95_upper_bound

    t1 = datetime(2026, 10, 27, 0, 0, tzinfo=UTC)
    t2 = datetime(2026, 10, 28, 0, 0, tzinfo=UTC)
    rows = [
        # Duplicate older row for (obs:0, issuer:0, revenue) that must be replaced by newer t2 row
        {
            "observation_id": "obs:0",
            "subject_id": "issuer:0",
            "field": "revenue",
            "result": "disagree",
            "reason": "beyond_tolerance",
            "oracle_kind": "filing",
            "compared_at": t1,
        },
        *[
            {
                "observation_id": f"obs:{idx}",
                "subject_id": f"issuer:{idx}",
                "field": "revenue",
                "result": "agree",
                "reason": None,
                "oracle_kind": "filing",
                "compared_at": t2,
            }
            for idx in range(8)
        ],
        {
            "observation_id": "obs:8",
            "subject_id": "issuer:8",
            "field": "revenue",
            "result": "disagree",
            "reason": "beyond_tolerance",
            "oracle_kind": "filing",
            "compared_at": t2,
        },
        {
            "observation_id": None,
            "subject_id": "issuer:9",
            "field": "revenue",
            "result": "not_comparable",
            "reason": "unserved",
            "oracle_kind": "filing",
            "compared_at": t1,
        },
    ]
    rollup = rollup_oracle_outcomes(rows)
    assert rollup["compared"] == 9
    assert rollup["agree"] == 8
    assert rollup["disagree"] == 1
    assert rollup["not_comparable"] == 1
    assert rollup["newest_compared_at"] == "2026-10-28T00:00:00Z"
    assert rollup["wilson_95_upper_bound"] == wilson_95_upper_bound(1, 9)
    assert 0.1 < rollup["wilson_95_upper_bound"] < 0.5
    assert rollup["by_field"]["revenue"]["compared"] == 9
    assert rollup["comparators"]["filing"]["compared"] == 9


def test_oracle_outcome_mass_by_origin_distinguishes_filing_independent_parse_and_operator() -> None:
    """#1517 B1, #1523 S4: filing agree/disagree add +1 success/fail; independent_parse agree adds 0
    success while disagree adds +1 fail; operator/gppe adds 0 to input origin mass."""
    from data_engine.datahub.oracle_outcomes import oracle_outcome_mass_by_origin

    t_before = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
    t_after = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
    rows = [
        # Filing agree -> +1 success
        {
            "observation_id": "obs:1",
            "subject_id": "issuer:cik:0000320193",
            "field": "revenue",
            "oracle_kind": "filing",
            "source": "sec-filing-10k",
            "result": "agree",
            "compared_at": t_before,
        },
        # Duplicate older row for same (oracle_kind, observation_id, subject_id, field) -> deduplicated
        {
            "observation_id": "obs:1",
            "subject_id": "issuer:cik:0000320193",
            "field": "revenue",
            "oracle_kind": "filing",
            "source": "sec-filing-10k",
            "result": "disagree",
            "compared_at": datetime(2026, 10, 26, 12, 0, tzinfo=UTC),
        },
        # Filing disagree -> +1 fail
        {
            "observation_id": "obs:2",
            "subject_id": "issuer:cik:0000789019",
            "field": "gross_profit",
            "oracle_kind": "filing",
            "source": "sec-filing-10k",
            "result": "disagree",
            "compared_at": t_before,
        },
        # Independent parse agree -> +0 success
        {
            "observation_id": "obs:1",
            "subject_id": "issuer:cik:0000320193",
            "field": "revenue",
            "oracle_kind": "independent_parse",
            "source": "sec-company-facts",
            "result": "agree",
            "compared_at": t_before,
        },
        # Independent parse disagree -> +1 fail
        {
            "observation_id": "obs:3",
            "subject_id": "issuer:cik:0001045810",
            "field": "revenue",
            "oracle_kind": "independent_parse",
            "source": "sec-company-facts",
            "result": "disagree",
            "compared_at": t_before,
        },
        # Operator row -> +0 to input origins
        {
            "observation_id": "etf-constituent-fact:1",
            "subject_id": "issuer:cik:0000320193",
            "field": "shares_outstanding",
            "oracle_kind": "operator",
            "source": "nasdaq_index",
            "result": "disagree",
            "compared_at": t_before,
        },
        # GPPE operator tree rebuild -> +0 to input origins
        {
            "observation_id": "res:1",
            "subject_id": "issuer:cik:0000320193",
            "field": "gross_profit_per_employee",
            "oracle_kind": "filing",
            "source": "sec-filing-10k",
            "result": "disagree",
            "compared_at": t_before,
        },
        # Future row after cutoff -> excluded
        {
            "observation_id": "obs:future",
            "subject_id": "issuer:cik:0000320193",
            "field": "total_assets",
            "oracle_kind": "filing",
            "source": "sec-filing-10k",
            "result": "disagree",
            "compared_at": t_after,
        },
    ]
    mass = oracle_outcome_mass_by_origin(rows, cutoff=datetime(2026, 10, 28, 0, 0, tzinfo=UTC))
    assert mass["origin:sec-company-facts:v1"] == (Decimal("1"), Decimal("2"))
    assert mass["origin:sec-company-facts:v1:revenue"] == (Decimal("1"), Decimal("1"))
    assert mass["origin:sec-company-facts:v1:gross_profit"] == (Decimal("0"), Decimal("1"))


def test_oracle_cell_agreement_applies_delta_curve_and_reports_oracle_none() -> None:
    """#1523 S4: comparable rows map relative error onto {1, 0.90, 0.65, 0.25}; missing/not_comparable -> (1, ('oracle:none',))."""
    from data_engine.datahub.oracle_outcomes import oracle_cell_agreement

    t_cmp = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
    rows = [
        {
            "observation_id": "obs:exact",
            "subject_id": "issuer:cik:0000320193",
            "field": "revenue",
            "oracle_kind": "filing",
            "served_value": Decimal("1000"),
            "oracle_value": Decimal("1000.5"),
            "ratio": Decimal("0.9995"),
            "result": "agree",
            "compared_at": t_cmp,
        },
        {
            "observation_id": "obs:1pct",
            "subject_id": "issuer:cik:0000320193",
            "field": "gross_profit",
            "oracle_kind": "filing",
            "served_value": Decimal("1005"),
            "oracle_value": Decimal("1000"),
            "ratio": Decimal("1.005"),
            "result": "agree",
            "compared_at": t_cmp,
        },
        {
            "observation_id": "obs:3pct",
            "subject_id": "issuer:cik:0000320193",
            "field": "total_assets",
            "oracle_kind": "filing",
            "served_value": Decimal("1030"),
            "oracle_value": Decimal("1000"),
            "ratio": Decimal("1.03"),
            "result": "disagree",
            "compared_at": t_cmp,
        },
        {
            "observation_id": "obs:10pct",
            "subject_id": "issuer:cik:0000320193",
            "field": "employees_total",
            "oracle_kind": "filing",
            "served_value": Decimal("1200"),
            "oracle_value": Decimal("1000"),
            "ratio": Decimal("1.20"),
            "result": "disagree",
            "compared_at": t_cmp,
        },
        {
            "observation_id": None,
            "subject_id": "issuer:cik:0000320193",
            "field": "shares_outstanding",
            "oracle_kind": "filing",
            "result": "not_comparable",
            "reason": "no_served_value",
            "compared_at": t_cmp,
        },
    ]

    assert oracle_cell_agreement(rows, subject_ids="issuer:cik:0000320193", field="revenue") == (
        Decimal("1"),
        ("oracle:agree",),
    )
    assert oracle_cell_agreement(rows, subject_ids="issuer:cik:0000320193", field="gross_profit") == (
        Decimal("0.90"),
        ("oracle:agree",),
    )
    assert oracle_cell_agreement(rows, subject_ids="issuer:cik:0000320193", field="total_assets") == (
        Decimal("0.65"),
        ("oracle:disagree",),
    )
    # Alias headcount -> employees_total
    assert oracle_cell_agreement(rows, subject_ids="issuer:cik:0000320193", field="headcount") == (
        Decimal("0.25"),
        ("oracle:disagree",),
    )
    assert oracle_cell_agreement(rows, subject_ids="issuer:cik:0000320193", field="shares_outstanding") == (
        Decimal("1"),
        ("oracle:none",),
    )
    assert oracle_cell_agreement(rows, subject_ids="issuer:cik:9999999999", field="revenue") == (
        Decimal("1"),
        ("oracle:none",),
    )


class _FakeFinanceConnection:
    """Returns the financial-fact rows in observation-id order, as the SQL does."""

    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def execute(self, _sql: str, _params: tuple) -> _FakeFinanceConnection:
        return self

    def fetchall(self) -> list[tuple]:
        return self._rows


def test_the_newest_knowable_financial_fact_of_an_issuer_wins_not_the_last_observation_id() -> None:
    older = datetime(2026, 3, 1, tzinfo=UTC)
    newer = datetime(2026, 6, 1, tzinfo=UTC)
    # The id of the older observation sorts last, so an id-ordered overwrite would keep the stale one.
    rows = [
        ("issuer:cik:0000000001", "aaa-newer", newer, {"vintage": "new"}),
        ("issuer:cik:0000000001", "zzz-older", older, {"vintage": "old"}),
    ]

    held = oracle_outcomes._query_fin_by_issuer(_FakeFinanceConnection(rows), "run-1")

    assert held["issuer:cik:0000000001"] == ("aaa-newer", newer, {"vintage": "new"})
    assert held["0000000001"] == ("aaa-newer", newer, {"vintage": "new"})
