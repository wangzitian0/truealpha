from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError
from truealpha_contracts import (
    AnalystRatingEvent,
    AsOfQuery,
    BacktestDataset,
    DataSource,
    FinancialFact,
    FundHolding,
)

NOW = datetime(2026, 7, 10, tzinfo=UTC)


def test_sec_fact_preserves_vintage_and_decimal_value():
    fact = FinancialFact(
        entity_id="company:ddog",
        metric="revenue",
        value=Decimal("100761000"),
        unit="USD",
        fiscal_period="2017FY",
        valid_from=date(2017, 1, 1),
        valid_to=date(2017, 12, 31),
        knowable_at=datetime(2020, 2, 25, tzinfo=UTC),
        recorded_at=NOW,
        confidence=Decimal("1"),
        raw_ref="raw.fetches:1",
        source_metric="RevenueFromContractWithCustomerExcludingAssessedTax",
        accession="0001564590-20-006422",
        form="10-K",
    )
    assert fact.value == Decimal("100761000")
    assert fact.knowable_at < fact.recorded_at


def test_rating_backfill_cannot_be_known_before_recommendation():
    with pytest.raises(ValidationError, match="knowable_at must not precede"):
        AnalystRatingEvent(
            analyst_id="analyst:yi-fu-lee",
            company_id="company:ddog",
            recommendation_at=NOW,
            knowable_at=NOW - timedelta(days=1),
            recorded_at=NOW,
            rating=4,
            confidence=Decimal("0.8"),
            raw_ref="raw.fetches:2",
        )


def test_nport_holding_supports_foreign_identifier_fallback():
    holding = FundHolding(
        fund_id="etf:arkk",
        holding_name="CRISPR Therapeutics AG",
        report_period=date(2026, 4, 30),
        knowable_at=NOW,
        recorded_at=NOW,
        cusip="000000000",
        isin="CH0334081137",
        value_usd=Decimal("123.45"),
        percent_of_net_assets=Decimal("0.42"),
        confidence=Decimal("1"),
        raw_ref="raw.fetches:3",
    )
    assert holding.isin == "CH0334081137"


def test_as_of_query_requires_timezone_and_entity():
    with pytest.raises(ValidationError):
        AsOfQuery(entity_ids=("company:ddog",), as_of=datetime(2026, 7, 10))
    with pytest.raises(ValidationError):
        AsOfQuery(entity_ids=(), as_of=NOW)


def test_source_values_are_stable_wire_names():
    assert DataSource.NPORT.value == "nport"


def test_backtest_dataset_keeps_one_as_of_boundary():
    query = AsOfQuery(entity_ids=("company:ddog",), as_of=NOW)
    dataset = BacktestDataset(query=query)
    assert dataset.query.as_of == NOW


def test_backtest_dataset_rejects_lookahead_fact():
    query = AsOfQuery(entity_ids=("company:ddog",), as_of=datetime(2020, 2, 24, tzinfo=UTC))
    fact = FinancialFact(
        entity_id="company:ddog",
        metric="revenue",
        value=Decimal("100761000"),
        unit="USD",
        fiscal_period="2017FY",
        valid_from=date(2017, 1, 1),
        valid_to=date(2017, 12, 31),
        knowable_at=datetime(2020, 2, 25, tzinfo=UTC),
        recorded_at=NOW,
        confidence=Decimal("1"),
        raw_ref="raw.fetches:1",
        source_metric="RevenueFromContractWithCustomerExcludingAssessedTax",
    )
    with pytest.raises(ValidationError, match="not knowable"):
        BacktestDataset(query=query, financial_facts=(fact,))
