"""#1235 acceptance: the 2026-10-08 production TOPT head, replayed through the deployed path.

Inputs are the production values the owner reviewed on #1235 (capture run
``capture-run:338fc733…``, cutoff 2026-10-08): capital-adjusted labor efficiency, current
P/S, gross profit and revenue per issuer. Gross profit and revenue are rounded to 10M.

The test runs the definition the tick loads (``strategy_bridge.load_strategy_definition``)
through the evaluator entry point the tick calls (``evaluate_cutoff`` with the live
class binding). It does not call a private helper.

The table does not carry headcount, assets or price, so the inputs are back-solved to
reproduce the measured values exactly, with the production rate 0.05 and 10,000 employees:

* capital base = (gross profit - labor efficiency x 10,000) / 0.05, so the evaluator's
  own formula gives back the production labor efficiency to the cent. JPM is FINANCIAL,
  so its base is tangible common equity (goodwill, intangibles and preferred stock 0).
* last close = P/S and shares outstanding = revenue, so market value = P/S x revenue.

Expected under definition 1.0.0: WMT (+0.7056) then META (+0.1661); tiers traditional 11,
tech 5, large_model_native 1. Definition 0.1.0 selected JNJ then NFLX on 2026-10-08.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from data_engine.datahub.strategy_bridge import load_strategy_definition
from factors.composite.strategy_evaluator import IssuerInput, evaluate_cutoff
from factors.forest import IssuerClass
from truealpha_contracts.strategy import (
    GoldenDecisionOutcome,
    LargeModelValueV0Definition,
)

_CUTOFF = datetime(2026, 10, 8, tzinfo=UTC)
_RATE = Decimal("0.05")
_HEADCOUNT = Decimal("10000")
_CONFIDENCE = Decimal("0.90")

# ticker | capital-adjusted labor efficiency | current P/S | gross profit (USD) | revenue (USD)
_PRODUCTION_HEAD_2026_10_08 = """
NVDA|3272605|25.723|153460000000|215940000000
META|1804263|9.043|164790000000|200970000000
NFLX|1186574|6.596|21910000000|45180000000
AAPL|1060468|11.938|195200000000|416160000000
AVGO|1026867|26.910|43290000000|63890000000
GOOG|1017723|10.470|240300000000|402840000000
LLY|940257|16.892|54130000000|65180000000
MSFT|841014|11.694|225470000000|331840000000
MA|751418|15.863|32790000000|32790000000
ABBV|635092|7.871|42960000000|61160000000
JNJ|389898|6.562|63940000000|94190000000
AMZN|193988|3.822|360510000000|716920000000
MU|154102|31.298|14870000000|37380000000
COST|100946|1.386|38880000000|303150000000
WMT|74439|1.242|171020000000|706410000000
TSLA|68823|15.619|17090000000|94830000000
JPM|-514726|4.829|86810000000|182450000000
"""
_FINANCIAL = frozenset({"JPM"})


def _issuers() -> tuple[list[IssuerInput], dict[str, Decimal]]:
    issuers: list[IssuerInput] = []
    labor: dict[str, Decimal] = {}
    for line in _PRODUCTION_HEAD_2026_10_08.strip().splitlines():
        ticker, efficiency, price_to_sales, gross_profit, revenue = line.split("|")
        labor[ticker] = Decimal(efficiency)
        capital_base = (Decimal(gross_profit) - Decimal(efficiency) * _HEADCOUNT) / _RATE
        records = {
            "gross_profit": Decimal(gross_profit),
            "total_assets": capital_base,
            "headcount": _HEADCOUNT,
            "revenue": Decimal(revenue),
            "shares_outstanding": Decimal(revenue),
            "last_close": Decimal(price_to_sales),
        }
        if ticker in _FINANCIAL:
            records |= {
                "stockholders_equity": capital_base,
                "preferred_stock_value": Decimal(0),
                "goodwill": Decimal(0),
                "intangible_assets_net_excluding_goodwill": Decimal(0),
            }
        issuers.append(
            IssuerInput(
                issuer_id=ticker,
                records={key: (value, _CONFIDENCE) for key, value in records.items()},
                knowable_at=dict.fromkeys(records, _CUTOFF - timedelta(days=3)),
                issuer_class=IssuerClass.FINANCIAL if ticker in _FINANCIAL else IssuerClass.NON_FINANCIAL,
            )
        )
    return issuers, labor


def _replay(definition: LargeModelValueV0Definition):
    issuers, labor = _issuers()
    decisions = evaluate_cutoff(issuers, definition=definition, cutoff_at=_CUTOFF, risk_free_rate=_RATE)
    # The back-solve is exact: every issuer gets the production labor efficiency back.
    assert {item.issuer_id: item.capital_adjusted_labor_efficiency for item in decisions} == labor
    selected = sorted(
        (item for item in decisions if item.outcome is GoldenDecisionOutcome.SELECTED), key=lambda item: item.rank or 0
    )
    return decisions, selected


def test_the_tick_definition_selects_wmt_then_meta_on_the_2026_10_08_head() -> None:
    definition = load_strategy_definition()
    decisions, selected = _replay(definition)

    assert [(item.issuer_id, item.rank, item.valuation_gap) for item in selected] == [
        ("WMT", 1, Decimal("0.7056")),
        ("META", 2, Decimal("0.1661")),
    ]
    # The head was produced by 1.0.0 (#1235). 1.1.0 holds the same bands and the bank rule that the decision already used.
    assert definition.definition_version == "1.1.0"
    assert [item.target_weight for item in selected] == [Decimal("0.500000")] * 2
    assert Counter(item.tier for item in decisions) == {"traditional": 11, "tech": 5, "large_model_native": 1}
    by_ticker = {item.issuer_id: item for item in decisions}
    # The P/GP values the selection rests on: 877.36B / 171.02B and 1,817.37B / 164.79B.
    assert by_ticker["WMT"].current_price_to_gross_profit == Decimal("5.1302")
    assert by_ticker["META"].current_price_to_gross_profit == Decimal("11.0284")
    assert by_ticker["WMT"].current_price_to_sales == Decimal("1.2420")
    assert by_ticker["WMT"].target_price_to_sales is None
    assert by_ticker["NVDA"].tier == "large_model_native"
    # Every tier is eligible: a large_model_native issuer inside its band is ranked, not dropped.
    assert by_ticker["NVDA"].outcome is GoldenDecisionOutcome.RANKED_BEYOND_SELECTION_COUNT
    # P/GP above the band's upper bound is rejected: AAPL 25.45 > tech 14.29, JPM 10.15 > 10.00.
    assert by_ticker["AAPL"].outcome is GoldenDecisionOutcome.REJECTED_VALUATION_ABOVE_TIER_BAND
    assert by_ticker["JPM"].outcome is GoldenDecisionOutcome.REJECTED_VALUATION_ABOVE_TIER_BAND
