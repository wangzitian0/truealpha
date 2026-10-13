"""#758: the simulation reads every execution parameter from the strategy definition.

Each test names the defect that turns it red: a fill at the close, a fill on the cutoff's own
session, a cost literal, a split that is ignored, a buy that spends more cash than exists.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from importlib import resources

import pytest
from factors.backtest.corporate_actions import SeriesBar, derive_split_events
from factors.backtest.engine import (
    Bar,
    PriceBook,
    TargetPortfolio,
    UnsupportedRuleError,
    equal_weight_target,
    execution_rules,
    fill_session,
    performance,
    simulate,
)
from truealpha_contracts.strategy import LargeModelValueV0Definition

CAPITAL = Decimal(1_000_000)
#: Monday 2024-09-30 is a session. The cutoff closes that day at 23:59:59Z, after its session.
CUTOFF = datetime(2024, 9, 30, 23, 59, 59, tzinfo=UTC)


def _definition() -> LargeModelValueV0Definition:
    corpus = json.loads(
        resources.files("truealpha_contracts.data").joinpath("large_model_value_v0_strategy.v1.json").read_bytes()
    )
    return LargeModelValueV0Definition.model_validate_json(json.dumps(corpus["strategy_definition"]))


def _with_cost(definition: LargeModelValueV0Definition, basis_points: str) -> LargeModelValueV0Definition:
    cost = definition.transaction_cost.model_copy(update={"basis_points": Decimal(basis_points)})
    return definition.model_copy(update={"transaction_cost": cost})


BOOK = PriceBook(
    bars={
        "AAA": {
            date(2024, 9, 27): Bar(Decimal("80"), Decimal("85")),
            date(2024, 9, 30): Bar(Decimal("90"), Decimal("95")),
            date(2024, 10, 1): Bar(Decimal("100"), Decimal("110")),
            date(2024, 10, 2): Bar(Decimal("111"), Decimal("120")),
            date(2024, 10, 3): Bar(Decimal("121"), Decimal("130")),
        }
    }
)


def test_the_definition_supplies_lag_and_cost() -> None:
    rules = execution_rules(_definition())

    assert rules.lag_sessions == 1
    assert rules.cost_rate == Decimal("0.0005"), "the definition's 5 basis points per side"


def test_an_order_fills_at_the_open_of_the_next_session_after_the_cutoff_day() -> None:
    """Red when the fill takes the cutoff day's session (09-30, open 90) or a close (110)."""
    assert fill_session(BOOK.sessions, CUTOFF, 1) == date(2024, 10, 1)

    run = simulate(
        BOOK, [TargetPortfolio(CUTOFF, {"AAA": Decimal(1)})], execution_rules(_definition()), capital=CAPITAL
    )

    (trade,) = run.trades
    assert (trade.session, trade.side, trade.price) == (date(2024, 10, 1), "BUY", Decimal("100"))
    assert trade.fee == trade.notional * Decimal("0.0005")
    assert trade.notional + trade.fee == CAPITAL, "the buy and its cost spend all cash, no more"
    assert trade.shares == trade.notional / Decimal("100")
    assert [item.session for item in run.valuations][0] == date(2024, 10, 1), "no valuation before the first fill"
    assert run.valuations[0].value == trade.shares * Decimal("110"), "the first valuation marks at the fill day's close"


def test_the_shares_bought_do_not_depend_on_any_price_after_the_open() -> None:
    """No lookahead inside the fill session: changing its close and every later bar changes no trade."""
    rules = execution_rules(_definition())
    base = simulate(BOOK, [TargetPortfolio(CUTOFF, {"AAA": Decimal(1)})], rules, capital=CAPITAL)
    later = {
        day: (
            bar
            if day <= date(2024, 9, 30)
            else Bar(bar.open if day == date(2024, 10, 1) else bar.open * 3, bar.close * 3)
        )
        for day, bar in BOOK.bars["AAA"].items()
    }
    shocked = simulate(
        PriceBook(bars={"AAA": later}), [TargetPortfolio(CUTOFF, {"AAA": Decimal(1)})], rules, capital=CAPITAL
    )

    assert shocked.trades == base.trades
    assert shocked.valuations[-1].value != base.valuations[-1].value, "the shock must reach the valuations"


def test_the_cost_is_read_from_the_definition_not_written_in_the_engine() -> None:
    """Red when the engine keeps any cost literal: 12 basis points must cost 12 basis points."""
    rules = execution_rules(_with_cost(_definition(), "12"))
    run = simulate(BOOK, [TargetPortfolio(CUTOFF, {"AAA": Decimal(1)})], rules, capital=CAPITAL)

    (trade,) = run.trades
    assert rules.cost_rate == Decimal("0.0012")
    assert trade.fee == trade.notional * Decimal("0.0012")


def test_an_empty_selection_sells_everything_at_the_next_open_and_keeps_cash() -> None:
    later_cutoff = datetime(2024, 10, 1, 23, 59, 59, tzinfo=UTC)
    run = simulate(
        BOOK,
        [TargetPortfolio(CUTOFF, {"AAA": Decimal(1)}), TargetPortfolio(later_cutoff, {})],
        execution_rules(_definition()),
        capital=CAPITAL,
    )

    buy, sell = run.trades
    assert (sell.session, sell.side, sell.price) == (date(2024, 10, 2), "SELL", Decimal("111"))
    assert sell.shares == buy.shares
    assert sell.fee == sell.notional * Decimal("0.0005")
    final = run.valuations[-1]
    assert final.cash_weight == 1 and final.gross_exposure == 0
    assert final.value == sell.notional - sell.fee, "cash after the sell: proceeds less the sell cost"


def test_a_target_without_an_open_on_the_fill_session_stays_cash() -> None:
    book = PriceBook(
        bars={
            "AAA": BOOK.bars["AAA"],
            "BBB": {date(2024, 10, 2): Bar(Decimal("50"), Decimal("50"))},
        }
    )
    run = simulate(
        book,
        [TargetPortfolio(CUTOFF, {"AAA": Decimal("0.5"), "BBB": Decimal("0.5")})],
        execution_rules(_definition()),
        capital=CAPITAL,
    )

    assert [trade.symbol for trade in run.trades] == ["AAA"]
    assert run.fills[0].unfilled_symbols == ("BBB",)
    assert run.valuations[0].cash_weight > Decimal("0.45")


def test_a_cutoff_without_a_later_session_does_not_fill() -> None:
    late = datetime(2024, 10, 3, 23, 59, 59, tzinfo=UTC)
    run = simulate(BOOK, [TargetPortfolio(late, {"AAA": Decimal(1)})], execution_rules(_definition()), capital=CAPITAL)

    assert run.trades == () and run.valuations == ()
    assert run.fills[0].session is None


# -- the split ---------------------------------------------------------------------------

EX_DATE = date(2024, 6, 10)
NVDA = {
    date(2024, 6, 5): (Decimal("1186.50"), Decimal("1224.40")),
    date(2024, 6, 6): (Decimal("1240.48"), Decimal("1209.98")),
    date(2024, 6, 7): (Decimal("1197.70"), Decimal("1208.88")),
    date(2024, 6, 10): (Decimal("120.37"), Decimal("121.79")),
    date(2024, 6, 11): (Decimal("121.77"), Decimal("120.91")),
}


def _nvda_book(*, with_splits: bool) -> PriceBook:
    series = [
        SeriesBar(day, open_, close, close / 10 if day < EX_DATE else close) for day, (open_, close) in NVDA.items()
    ]
    splits = derive_split_events("NVDA", series) if with_splits else ()
    return PriceBook(bars={"NVDA": {day: Bar(*prices) for day, prices in NVDA.items()}}, splits=splits)


def test_holdings_value_is_continuous_across_a_10_for_1_split() -> None:
    """Red when the split is ignored: the holding then loses about 90% on 2024-06-10."""
    cutoff = datetime(2024, 6, 5, 23, 59, 59, tzinfo=UTC)
    run = simulate(
        _nvda_book(with_splits=True),
        [TargetPortfolio(cutoff, {"NVDA": Decimal(1)})],
        execution_rules(_definition()),
        capital=CAPITAL,
    )

    by_day = {item.session: item for item in run.valuations}
    before, after = by_day[date(2024, 6, 7)], by_day[EX_DATE]
    real_move = Decimal("121.79") / (Decimal("1208.88") / 10)
    assert after.value / before.value == real_move, "the NAV moves by the split-adjusted price move only"
    (applied,) = run.splits_applied
    assert (applied.numerator, applied.denominator) == (10, 1)
    assert applied.shares_after == applied.shares_before * 10
    assert len(run.trades) == 1, "a split is a share event, not a trade"


def test_ignoring_the_split_shows_a_false_90_percent_loss() -> None:
    """The negative control of the test above: the same run without split events."""
    cutoff = datetime(2024, 6, 5, 23, 59, 59, tzinfo=UTC)
    run = simulate(
        _nvda_book(with_splits=False),
        [TargetPortfolio(cutoff, {"NVDA": Decimal(1)})],
        execution_rules(_definition()),
        capital=CAPITAL,
    )

    by_day = {item.session: item for item in run.valuations}
    assert by_day[EX_DATE].value / by_day[date(2024, 6, 7)].value < Decimal("0.11")


# -- definition guard, benchmark, statistics ---------------------------------------------


@pytest.mark.parametrize(
    ("block", "field", "value"),
    [
        ("execution", "price_field", "unadjusted_close"),
        ("execution", "rule_id", "same_session_close"),
        ("transaction_cost", "rule_id", "flat_fee"),
        ("total_return", "adjusted_prices_affect_returns", True),
        ("total_return", "price_basis", "split_adjusted"),
        ("sizing", "empty_selection_behavior", "hold_previous"),
    ],
)
def test_a_rule_the_engine_does_not_implement_stops_the_run(block: str, field: str, value: object) -> None:
    definition = _definition()
    changed = getattr(definition, block).model_copy(update={field: value})

    with pytest.raises(UnsupportedRuleError, match=f"{block}.{field}"):
        execution_rules(definition.model_copy(update={block: changed}))


def test_the_benchmark_buys_each_symbol_once_with_equal_weight() -> None:
    book = PriceBook(
        bars={
            "AAA": BOOK.bars["AAA"],
            "BBB": {day: Bar(Decimal("10"), Decimal("10")) for day in BOOK.bars["AAA"]},
        }
    )
    run = simulate(book, [equal_weight_target(["BBB", "AAA"], CUTOFF)], execution_rules(_definition()), capital=CAPITAL)

    assert [(trade.symbol, trade.side) for trade in run.trades] == [("AAA", "BUY"), ("BBB", "BUY")]
    assert run.trades[0].notional == run.trades[1].notional


def test_statistics_use_the_measured_session_count() -> None:
    run = simulate(
        BOOK, [TargetPortfolio(CUTOFF, {"AAA": Decimal(1)})], execution_rules(_definition()), capital=CAPITAL
    )
    stats = performance(run.valuations)

    assert stats.total_return == run.valuations[-1].cum_nav - 1
    assert stats.max_drawdown == 0, "the price only rises after the fill"
    assert stats.sessions_per_year == Decimal(2) / (Decimal(2) / Decimal("365.25"))
    assert stats.cagr is not None and stats.cagr > 0
