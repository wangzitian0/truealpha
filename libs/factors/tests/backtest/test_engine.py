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
    MIN_ORDER_NOTIONAL,
    Bar,
    PriceBook,
    TargetPortfolio,
    UnsupportedRuleError,
    benchmark_rules,
    equal_weight_target,
    equal_weights,
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


def _cost_of(trade, rate: str) -> bool:
    """The fee is the per-side rate of the notional. A full-spend buy carries the Decimal rounding
    residual of its last digit in the fee (so cash ends at exactly zero): allow 1e-20 of the notional."""
    return abs(trade.fee - trade.notional * Decimal(rate)) <= trade.notional * Decimal("1e-20")


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
    assert _cost_of(trade, "0.0005")
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
    assert _cost_of(trade, "0.0012")
    assert not _cost_of(trade, "0.0005")


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
    sizing = _definition().sizing.quantization
    run = simulate(
        book, [equal_weight_target(["BBB", "AAA"], CUTOFF, sizing)], execution_rules(_definition()), capital=CAPITAL
    )

    assert [(trade.symbol, trade.side) for trade in run.trades] == [("AAA", "BUY"), ("BBB", "BUY")]
    assert run.trades[0].notional == run.trades[1].notional


@pytest.mark.parametrize("count", range(1, 41))
def test_equal_weights_sum_to_exactly_one_for_any_universe_size(count: int) -> None:
    """Audit finding 1: 1/N at 28 digits summed above 1 for N = 18, 19, 24, 26, 35, 36, 38."""
    sizing = _definition().sizing.quantization
    symbols = [f"S{index:02d}" for index in range(count)]

    weights = equal_weights(symbols, sizing)

    assert sum(weights.values()) == 1
    assert len(set(list(weights.values())[:-1])) <= 1, "every weight but the remainder is the same"
    assert all(abs(weight - Decimal(1) / count) <= Decimal("0.000001") * count for weight in weights.values())
    simulate(BOOK, [equal_weight_target(symbols, CUTOFF, sizing)], execution_rules(_definition()), capital=CAPITAL)


@pytest.mark.parametrize("count", [7, 9, 11, 13, 21, 37])
def test_cash_never_goes_below_zero_after_a_full_buy(count: int) -> None:
    """Audit finding 6: scaled buys, paid one by one from cash, left about -3e-22 of cash for 7
    weights of 1/7. The last buy now spends exactly what remains, so cash ends at zero."""
    weight = Decimal(1) / Decimal(count)
    bars = {
        f"S{index:02d}": {day: Bar(Decimal("97.13") + index, Decimal("98.71") + index) for day in BOOK.bars["AAA"]}
        for index in range(count)
    }
    run = simulate(
        PriceBook(bars=bars),
        [TargetPortfolio(CUTOFF, {symbol: weight for symbol in bars})],
        execution_rules(_definition()),
        capital=CAPITAL,
    )

    assert all(item.cash_weight >= 0 for item in run.valuations)
    assert run.valuations[0].cash_weight == 0, "a full buy spends all cash"
    assert sum((trade.notional + trade.fee for trade in run.trades), Decimal(0)) == CAPITAL


def test_a_loss_on_the_first_session_is_a_drawdown() -> None:
    """Audit finding 7: the drawdown peak starts at the capital, not at the first close."""
    falling = PriceBook(
        bars={
            "AAA": {
                date(2024, 10, 1): Bar(Decimal("100"), Decimal("90")),
                date(2024, 10, 2): Bar(Decimal("90"), Decimal("95")),
            }
        }
    )
    run = simulate(
        falling, [TargetPortfolio(CUTOFF, {"AAA": Decimal(1)})], execution_rules(_definition()), capital=CAPITAL
    )

    first = run.valuations[0]
    assert first.drawdown == (CAPITAL - first.value) / CAPITAL
    assert first.drawdown > Decimal("0.09")


def test_the_benchmark_buys_a_symbol_without_an_open_at_its_next_open() -> None:
    """Audit finding 2: a benchmark symbol without an open on the first fill session waits, then fills."""
    book = PriceBook(
        bars={
            "AAA": BOOK.bars["AAA"],
            "BBB": {
                date(2024, 10, 2): Bar(Decimal("50"), Decimal("51")),
                date(2024, 10, 3): Bar(Decimal("52"), Decimal("53")),
            },
        }
    )
    sizing = _definition().sizing.quantization
    target = equal_weight_target(["AAA", "BBB"], CUTOFF, sizing)
    rules = execution_rules(_definition())

    benchmark = simulate(book, [target], benchmark_rules(rules), capital=CAPITAL)
    strategy_rule = simulate(book, [target], rules, capital=CAPITAL)

    (late,) = benchmark.deferred_fills
    assert (late.symbol, late.ordered_session, late.filled_session) == ("BBB", date(2024, 10, 1), date(2024, 10, 2))
    bought = [trade for trade in benchmark.trades if trade.symbol == "BBB"]
    assert [(trade.session, trade.price) for trade in bought] == [(date(2024, 10, 2), Decimal("50"))]
    (first_buy,) = [trade for trade in benchmark.trades if trade.symbol == "AAA"]
    assert bought[0].notional + bought[0].fee == CAPITAL - first_buy.notional - first_buy.fee, (
        "the cash left after the first fill buys BBB"
    )
    assert benchmark.valuations[-1].cash_weight == 0
    assert strategy_rule.deferred_fills == ()
    assert [trade.symbol for trade in strategy_rule.trades] == ["AAA"], (
        "the strategy's own rule, remain_cash, never buys BBB after the fill session"
    )


def test_statistics_use_the_measured_session_count() -> None:
    run = simulate(
        BOOK, [TargetPortfolio(CUTOFF, {"AAA": Decimal(1)})], execution_rules(_definition()), capital=CAPITAL
    )
    stats = performance(run.valuations)

    assert stats.total_return == run.valuations[-1].cum_nav - 1
    assert stats.max_drawdown == 0, "the price only rises after the fill"
    assert stats.sessions_per_year == Decimal(2) / (Decimal(2) / Decimal("365.25"))
    assert stats.cagr is not None and stats.cagr > 0


# -- orders below one cent (#1270) -------------------------------------------------------

#: The sessions of the book below: the first target fills on 2024-10-01, the second on 2024-10-02.
SECOND_CUTOFF = datetime(2024, 10, 1, 23, 59, 59, tzinfo=UTC)
HALF = Decimal("0.5")


def _flat_book(price: str, *symbols: str) -> PriceBook:
    """Each symbol opens and closes at one price on every session, so no price move changes a weight."""
    days = [date(2024, 9, 30), date(2024, 10, 1), date(2024, 10, 2), date(2024, 10, 3)]
    return PriceBook(bars={symbol: {day: Bar(Decimal(price), Decimal(price)) for day in days} for symbol in symbols})


def _free_rules():
    """The definition's rules at zero cost, so a fee never moves a weight away from its target."""
    return execution_rules(_with_cost(_definition(), "0"))


def _two_targets(first: Decimal, second: Decimal) -> list[TargetPortfolio]:
    return [TargetPortfolio(CUTOFF, {"AAA": first}), TargetPortfolio(SECOND_CUTOFF, {"AAA": second})]


@pytest.mark.parametrize(
    ("price", "residue"),
    [("1.18", "under target"), ("2.03", "over target"), ("1.11", "over target, then under it")],
)
def test_a_second_rebalance_to_the_held_weight_records_no_trade(price: str, residue: str) -> None:
    """Staging run backtest-run:893147d2: AMZN and META each recorded a BUY of about 4.5e-25 shares,
    because the weight before the order was 0.4999999999999999999999999999 against a target of 0.5.

    At each price below, 28 digits cannot hold shares * price exactly. The held weight then differs
    from the unchanged target by about 1e-28. Red when the engine turns that residue into an order.
    """
    notional = CAPITAL * HALF
    assert notional / Decimal(price) * Decimal(price) != notional, f"{price} must leave a Decimal residue ({residue})"

    run = simulate(_flat_book(price, "AAA"), _two_targets(HALF, HALF), _free_rules(), capital=CAPITAL)

    assert [(trade.session, trade.side) for trade in run.trades] == [(date(2024, 10, 1), "BUY")]
    assert run.fills[1].turnover == 0, "the second rebalance traded nothing"


@pytest.mark.parametrize("weight", [HALF + Decimal("1e-27"), HALF - Decimal("1e-27")], ids=["above", "below"])
def test_two_weights_that_differ_by_1e_27_record_no_trade(weight: Decimal) -> None:
    """The held weight is exactly 0.5 at a price of 100. A target 1e-27 away asks for an order of 1e-21."""
    run = simulate(_flat_book("100", "AAA"), _two_targets(HALF, weight), _free_rules(), capital=CAPITAL)

    assert [trade.side for trade in run.trades] == ["BUY"], "only the opening buy is a trade"


@pytest.mark.parametrize(
    ("weight", "side"),
    [
        ("0.50000001", "BUY"),
        ("0.5000000099", None),
        ("0.49999999", "SELL"),
        ("0.4999999901", None),
    ],
    ids=["buy-of-one-cent", "buy-below-one-cent", "sell-of-one-cent", "sell-below-one-cent"],
)
def test_an_order_of_exactly_one_cent_is_recorded_and_a_smaller_order_is_not(weight: str, side: str | None) -> None:
    """Both sides of the boundary, on both sides of the book. At a value of 1,000,000 the target
    0.50000001 is 500,000.01: the order is 0.01 = MIN_ORDER_NOTIONAL. The target 0.5000000099 is 0.0099."""
    assert MIN_ORDER_NOTIONAL == Decimal("0.01")

    run = simulate(_flat_book("100", "AAA"), _two_targets(HALF, Decimal(weight)), _free_rules(), capital=CAPITAL)

    second = [trade for trade in run.trades if trade.session == date(2024, 10, 2)]
    if side is None:
        assert second == []
        return
    (trade,) = second
    assert (trade.side, trade.notional) == (side, MIN_ORDER_NOTIONAL)
    assert trade.shares == MIN_ORDER_NOTIONAL / Decimal(100)


def test_an_order_below_one_cent_is_neither_unfilled_nor_deferred() -> None:
    """AAA has no open on the second fill session. A residue order for it is no order: the fill does
    not list it as unfilled, and the benchmark rule (next session open) does not defer it."""
    held_days = (date(2024, 9, 30), date(2024, 10, 1), date(2024, 10, 3))
    book = PriceBook(
        bars={
            "AAA": {day: Bar(Decimal(100), Decimal(100)) for day in held_days},
            "BBB": {day: Bar(Decimal(10), Decimal(10)) for day in (*held_days, date(2024, 10, 2))},
        }
    )
    rules = benchmark_rules(_free_rules())

    run = simulate(book, _two_targets(HALF, HALF + Decimal("1e-27")), rules, capital=CAPITAL)

    assert [trade.side for trade in run.trades] == ["BUY"]
    assert date(2024, 10, 2) not in book.bars["AAA"], "AAA has no bar on the second fill session"
    assert run.fills[1].session == date(2024, 10, 2) and run.fills[1].unfilled_symbols == ()
    assert run.deferred_fills == ()


def test_a_buy_that_has_no_cash_records_no_trade() -> None:
    """AAA fills the book. At the second target AAA has no open, so its sell cannot fill and no cash
    exists for the buy of BBB. The buy used to record a trade of zero shares for zero value."""
    book = PriceBook(
        bars={
            "AAA": {
                date(2024, 9, 30): Bar(Decimal("90"), Decimal("95")),
                date(2024, 10, 1): Bar(Decimal("100"), Decimal("110")),
                date(2024, 10, 3): Bar(Decimal("121"), Decimal("130")),
            },
            "BBB": {day: Bar(Decimal("50"), Decimal("50")) for day in (date(2024, 10, 1), date(2024, 10, 2))},
        }
    )
    targets = [TargetPortfolio(CUTOFF, {"AAA": Decimal(1)}), TargetPortfolio(SECOND_CUTOFF, {"BBB": Decimal(1)})]

    run = simulate(book, targets, execution_rules(_definition()), capital=CAPITAL)

    assert run.fills[1].unfilled_symbols == ("AAA",), "the sell of AAA cannot fill on 2024-10-02"
    assert [(trade.symbol, trade.side) for trade in run.trades] == [("AAA", "BUY")]
    assert all(trade.notional >= MIN_ORDER_NOTIONAL for trade in run.trades)
