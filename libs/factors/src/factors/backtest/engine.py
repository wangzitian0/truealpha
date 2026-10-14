"""The portfolio simulation of the core strategy backtest (#758, M3).

The strategy definition is the only source of execution parameters. ``execution_rules`` reads
the ``execution``, ``transaction_cost``, ``total_return`` and ``sizing`` blocks and refuses a
rule this engine does not implement. No cost, lag or price field is written here.

Rules this module enforces:

* A target portfolio decided at a cutoff fills at the open of the ``lag_sessions``-th session
  after the cutoff's calendar day (``next_eligible_market_session_open``). A session on the
  cutoff's own day opened before a 23:59:59Z cutoff, so it is never eligible.
* Prices are unadjusted (``price_basis = unadjusted``). Splits are explicit events. Each split
  multiplies the held shares once, at the open of its ex-date, before any trade of that session.
  Adjusted prices never enter a return.
* Each side of a trade pays ``basis_points / 10000`` of its notional.
* Sells execute before buys. When the cash after the sells cannot pay every buy and its cost,
  every buy shrinks by one factor and the last buy spends exactly the cash that remains. Cash
  never goes below zero, by construction.
* A target symbol with no open on the fill session stays cash until the next fill
  (``unfilled_order_behavior = remain_cash``). A held symbol with no open on the fill session
  stays held. An empty target sells every holding (``empty_selection_behavior = retain_all_cash``).
  The benchmark alone uses ``next_session_open``: such a symbol is bought at its next open, and
  the run records the delay.
* Equal weights sum to exactly 1. Each weight is ``1/N`` at the definition's sizing quantization,
  and the last symbol takes the remainder.
* The drawdown peak starts at the capital, so a loss on the first session is a drawdown.
* An order below ``MIN_ORDER_NOTIONAL`` (one cent) is Decimal rounding residue, not a decision. The
  engine records no trade for it, and the cash or the shares stay in the book.
* Shares are fractional, so a result does not depend on the notional capital. The one exception is the
  order below ``MIN_ORDER_NOTIONAL``, which the engine drops.

All arithmetic is ``Decimal``. A rerun on the same inputs gives the same digits.
"""

from __future__ import annotations

import dataclasses
from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_EVEN, Decimal

from truealpha_contracts.strategy import DecimalQuantization, LargeModelValueV0Definition

from factors.backtest.corporate_actions import SplitEvent

#: The identity of this simulation. A change to its semantics changes this id.
ENGINE_ID = "factors.backtest.engine:unadjusted-open-explicit-splits:v2"
#: How returns are priced. Recorded on each run (``mart.backtest_runs.price_basis``).
PRICE_BASIS = "unadjusted_open_explicit_splits"
#: What a result can claim. No dividend source exists, so the return omits dividends.
CLAIM_CEILING = "price_return_no_dividends"
#: The benchmark of each run: equal weight over the issuers with inputs at the first cutoff,
#: bought at the first fill (a symbol without an open there is bought at its next open), then held.
BENCHMARK_RULE = "equal_weight_buy_and_hold_first_cutoff_issuers"
#: The unfilled-order behaviors this engine implements. The definition names the first.
REMAIN_CASH = "remain_cash"
NEXT_SESSION_OPEN = "next_session_open"

#: The smallest order the engine records, in the currency of the capital: one cent. A smaller order
#: is the Decimal residue between a held weight and the same target weight, not a decision (#1270).
MIN_ORDER_NOTIONAL = Decimal("0.01")

_BASIS_POINTS_PER_UNIT = Decimal(10_000)
#: Days in a Julian year: converts a calendar span into years.
_DAYS_PER_YEAR = Decimal("365.25")
_ZERO = Decimal(0)
_ONE = Decimal(1)


class UnsupportedRuleError(ValueError):
    """The definition names a rule this engine does not implement."""


@dataclass(frozen=True)
class ExecutionRules:
    """The execution parameters, read from one strategy definition."""

    lag_sessions: int
    cost_rate: Decimal
    #: The definition's sizing quantization: the precision of every target weight.
    weight_quantization: DecimalQuantization
    unfilled_order_behavior: str = REMAIN_CASH

    def __post_init__(self) -> None:
        if self.unfilled_order_behavior not in (REMAIN_CASH, NEXT_SESSION_OPEN):
            raise UnsupportedRuleError(f"unfilled_order_behavior {self.unfilled_order_behavior!r} is not implemented")


def _require(actual: object, expected: object, name: str) -> None:
    if actual != expected:
        raise UnsupportedRuleError(f"{name} is {actual!r}; this engine implements only {expected!r}")


def execution_rules(definition: LargeModelValueV0Definition) -> ExecutionRules:
    """The engine parameters of ``definition``. Raise for a rule the engine does not implement."""
    execution = definition.execution
    _require(execution.rule_id, "next_eligible_market_session_open", "execution.rule_id")
    _require(execution.price_field, "unadjusted_open", "execution.price_field")
    _require(execution.unfilled_order_behavior, "remain_cash", "execution.unfilled_order_behavior")
    if not isinstance(execution.lag_sessions, int) or execution.lag_sessions < 1:
        raise UnsupportedRuleError(f"execution.lag_sessions is {execution.lag_sessions!r}; it must be 1 or more")
    cost = definition.transaction_cost
    _require(cost.rule_id, "per_side_notional_basis_points", "transaction_cost.rule_id")
    total_return = definition.total_return
    _require(total_return.rule_id, "unadjusted_bars_explicit_corporate_actions", "total_return.rule_id")
    _require(total_return.price_basis, "unadjusted", "total_return.price_basis")
    _require(
        total_return.corporate_action_processing,
        "explicit_event_lifecycle_once",
        "total_return.corporate_action_processing",
    )
    _require(total_return.adjusted_prices_affect_returns, False, "total_return.adjusted_prices_affect_returns")
    sizing = definition.sizing
    _require(sizing.rule_id, "equal_weight_selected", "sizing.rule_id")
    _require(sizing.empty_selection_behavior, "retain_all_cash", "sizing.empty_selection_behavior")
    _require(sizing.quantization.rounding, "half_even", "sizing.quantization.rounding")
    return ExecutionRules(
        lag_sessions=execution.lag_sessions,
        cost_rate=Decimal(cost.basis_points) / _BASIS_POINTS_PER_UNIT,
        weight_quantization=sizing.quantization,
        unfilled_order_behavior=execution.unfilled_order_behavior,
    )


def benchmark_rules(rules: ExecutionRules) -> ExecutionRules:
    """The strategy's rules, except that an order without an open fills at the symbol's next open."""
    return dataclasses.replace(rules, unfilled_order_behavior=NEXT_SESSION_OPEN)


def equal_weights(symbols: Sequence[str], quantization: DecimalQuantization) -> dict[str, Decimal]:
    """Equal weights that sum to exactly 1.

    Each weight is ``1/N`` at ``quantization`` (half-even). The last symbol, in sorted order,
    takes ``1`` minus the sum of the others, so rounding never makes the sum differ from 1.
    """
    unique = sorted(set(symbols))
    if not unique:
        return {}
    quantum = Decimal(1).scaleb(-quantization.decimal_places)
    weight = (_ONE / Decimal(len(unique))).quantize(quantum, ROUND_HALF_EVEN)
    weights = {symbol: weight for symbol in unique[:-1]}
    remainder = _ONE - weight * Decimal(len(unique) - 1)
    if remainder < 0:
        raise ValueError(f"{len(unique)} equal weights at {quantization.decimal_places} places exceed 1")
    weights[unique[-1]] = remainder
    return weights


# -- inputs -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Bar:
    """One unadjusted session bar. A missing or non-positive price is no price."""

    open: Decimal | None
    close: Decimal | None


def _price(value: Decimal | None) -> Decimal | None:
    return value if value is not None and value > 0 else None


@dataclass(frozen=True)
class PriceBook:
    """Unadjusted bars per symbol and the split events of those symbols."""

    bars: Mapping[str, Mapping[date, Bar]]
    splits: Sequence[SplitEvent] = ()
    #: Every session that holds a bar of any symbol, oldest first.
    sessions: tuple[date, ...] = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "sessions", tuple(sorted({day for series in self.bars.values() for day in series})))
        seen: set[tuple[str, date]] = set()
        for event in self.splits:
            key = (event.symbol, event.ex_date)
            if key in seen:
                raise ValueError(f"two split events for {event.symbol} on {event.ex_date.isoformat()}")
            seen.add(key)


@dataclass(frozen=True)
class TargetPortfolio:
    """The weights decided at one cutoff. Empty weights mean all cash."""

    cutoff_at: datetime
    weights: Mapping[str, Decimal]

    def __post_init__(self) -> None:
        if self.cutoff_at.tzinfo is None:
            raise ValueError("cutoff_at must carry a time zone")
        if any(weight < 0 for weight in self.weights.values()):
            raise ValueError("a target weight cannot be negative")
        if sum(self.weights.values(), _ZERO) > _ONE:
            raise ValueError("target weights sum to more than 1")


def fill_session(sessions: Sequence[date], cutoff_at: datetime, lag_sessions: int) -> date | None:
    """The ``lag_sessions``-th session after the cutoff's calendar day, or None when none exists yet."""
    if lag_sessions < 1:
        raise ValueError("lag_sessions must be 1 or more")
    start = bisect_right(sessions, cutoff_at.astimezone(UTC).date())
    index = start + lag_sessions - 1
    return sessions[index] if index < len(sessions) else None


# -- outputs ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Trade:
    cutoff_at: datetime
    session: date
    symbol: str
    side: str
    shares: Decimal
    price: Decimal
    notional: Decimal
    fee: Decimal
    weight_before: Decimal
    weight_after: Decimal


@dataclass(frozen=True)
class Fill:
    """What happened to the target of one cutoff."""

    cutoff_at: datetime
    #: The fill session, or None when the price book holds no session after the cutoff yet.
    session: date | None
    #: Target or held symbols that had no open on the fill session.
    unfilled_symbols: tuple[str, ...] = ()
    #: One-way turnover: (sells + buys) / 2 / portfolio value at the open.
    turnover: Decimal = _ZERO


@dataclass(frozen=True)
class DeferredFill:
    """A buy without an open on its fill session, filled at a later open (benchmark only)."""

    symbol: str
    cutoff_at: datetime
    ordered_session: date
    filled_session: date


@dataclass(frozen=True)
class AppliedSplit:
    symbol: str
    ex_date: date
    numerator: int
    denominator: int
    shares_before: Decimal
    shares_after: Decimal


@dataclass(frozen=True)
class Valuation:
    """The portfolio at the close of one session."""

    session: date
    value: Decimal
    cum_nav: Decimal
    drawdown: Decimal
    gross_exposure: Decimal
    cash_weight: Decimal


@dataclass(frozen=True)
class Simulation:
    valuations: tuple[Valuation, ...]
    trades: tuple[Trade, ...]
    fills: tuple[Fill, ...]
    splits_applied: tuple[AppliedSplit, ...]
    deferred_fills: tuple[DeferredFill, ...] = ()


# -- simulation -------------------------------------------------------------------------


@dataclass
class _Book:
    cash: Decimal
    shares: dict[str, Decimal] = field(default_factory=dict)
    #: The latest known price of each symbol, on the current share basis.
    last: dict[str, Decimal] = field(default_factory=dict)

    def market_value(self) -> Decimal:
        return sum((held * self.last[symbol] for symbol, held in self.shares.items()), _ZERO)


def _apply_split(book: _Book, event: SplitEvent) -> AppliedSplit | None:
    numerator, denominator = Decimal(event.numerator), Decimal(event.denominator)
    if event.symbol in book.last:
        book.last[event.symbol] = book.last[event.symbol] * denominator / numerator
    held = book.shares.get(event.symbol)
    if not held:
        return None
    after = held * numerator / denominator
    book.shares[event.symbol] = after
    return AppliedSplit(event.symbol, event.ex_date, event.numerator, event.denominator, held, after)


@dataclass(frozen=True)
class _Order:
    """One buy: ``amount`` of value to add to ``symbol`` at ``price``."""

    symbol: str
    price: Decimal
    amount: Decimal


@dataclass(frozen=True)
class _Pending:
    """A benchmark buy that waits for the symbol's next open."""

    cutoff_at: datetime
    ordered_session: date
    amount: Decimal


def _spend(
    book: _Book, orders: Sequence[_Order], rules: ExecutionRules, cutoff_at: datetime, session: date, value: Decimal
) -> list[Trade]:
    """Buy each order and pay its cost. Cash never goes below zero, by construction.

    When cash pays every order and its cost, cash falls by that exact total. Otherwise every
    order shrinks by one factor, and the last order spends exactly the cash that remains:
    its cost is that cash less its notional, and cash is then zero.
    """
    if not orders:
        return []
    rate = rules.cost_rate
    full = [(order, order.amount, order.amount * rate) for order in orders]
    need = sum((notional + fee for _order, notional, fee in full), _ZERO)
    if need < book.cash:
        legs = full
        book.cash -= need
    else:
        scale = book.cash / need
        legs = []
        spent = _ZERO
        for order, amount, _fee in full[:-1]:
            notional = amount * scale
            fee = notional * rate
            spent += notional + fee
            legs.append((order, notional, fee))
        remaining = book.cash - spent
        if remaining < 0:
            raise ValueError(f"the scaled buys on {session.isoformat()} spend more than the cash")
        last = full[-1][0]
        notional = remaining / (_ONE + rate)
        legs.append((last, notional, remaining - notional))
        book.cash = _ZERO
    trades: list[Trade] = []
    for order, notional, fee in legs:
        if notional < MIN_ORDER_NOTIONAL:
            # Too little cash was left for this order. The cash stays in the book.
            book.cash += notional + fee
            continue
        before = book.shares.get(order.symbol, _ZERO)
        bought = notional / order.price
        book.shares[order.symbol] = before + bought
        trades.append(
            Trade(
                cutoff_at,
                session,
                order.symbol,
                "BUY",
                bought,
                order.price,
                notional,
                fee,
                before * order.price / value,
                (before + bought) * order.price / value,
            )
        )
    return trades


def _rebalance(
    book: _Book,
    target: TargetPortfolio,
    session: date,
    opens: Mapping[str, Decimal],
    rules: ExecutionRules,
    pending: dict[str, _Pending],
) -> tuple[list[Trade], Fill]:
    book.last.update(opens)
    pending.clear()
    value = book.cash + book.market_value()
    if value <= 0:
        raise ValueError(f"portfolio value {value} on {session.isoformat()} is not positive")
    trades: list[Trade] = []
    unfilled: list[str] = []
    traded = _ZERO

    for symbol in sorted(book.shares):
        held = book.shares[symbol]
        weight = target.weights.get(symbol, _ZERO)
        current = held * book.last[symbol]
        goal = weight * value
        if current - goal < MIN_ORDER_NOTIONAL:
            continue
        price = opens.get(symbol)
        if price is None:
            unfilled.append(symbol)
            continue
        sold = held if weight == 0 else (current - goal) / price
        notional = sold * price
        fee = notional * rules.cost_rate
        book.cash += notional - fee
        remaining = held - sold
        if remaining == 0:
            del book.shares[symbol]
        else:
            book.shares[symbol] = remaining
        traded += notional
        trades.append(
            Trade(
                target.cutoff_at,
                session,
                symbol,
                "SELL",
                sold,
                price,
                notional,
                fee,
                current / value,
                remaining * price / value,
            )
        )

    orders: list[_Order] = []
    for symbol in sorted(target.weights):
        weight = target.weights[symbol]
        if weight <= 0:
            continue
        current = book.shares.get(symbol, _ZERO) * book.last.get(symbol, _ZERO)
        amount = weight * value - current
        if amount < MIN_ORDER_NOTIONAL:
            continue
        price = opens.get(symbol)
        if price is None:
            if symbol not in unfilled:
                unfilled.append(symbol)
            if rules.unfilled_order_behavior == NEXT_SESSION_OPEN:
                pending[symbol] = _Pending(target.cutoff_at, session, amount)
            continue
        orders.append(_Order(symbol, price, amount))
    bought = _spend(book, orders, rules, target.cutoff_at, session, value)
    trades.extend(bought)
    traded += sum((trade.notional for trade in bought), _ZERO)
    return trades, Fill(target.cutoff_at, session, tuple(sorted(unfilled)), traded / (2 * value))


def _fill_pending(
    book: _Book, pending: dict[str, _Pending], session: date, opens: Mapping[str, Decimal], rules: ExecutionRules
) -> tuple[list[Trade], list[DeferredFill]]:
    """Buy each waiting benchmark order whose symbol opens on this session."""
    trades: list[Trade] = []
    deferred: list[DeferredFill] = []
    for symbol in sorted(pending):
        price = opens.get(symbol)
        if price is None:
            continue
        order = pending.pop(symbol)
        value = book.cash + book.market_value()
        trades.extend(_spend(book, [_Order(symbol, price, order.amount)], rules, order.cutoff_at, session, value))
        deferred.append(DeferredFill(symbol, order.cutoff_at, order.ordered_session, session))
    return trades, deferred


def simulate(
    book: PriceBook,
    targets: Sequence[TargetPortfolio],
    rules: ExecutionRules,
    *,
    capital: Decimal,
) -> Simulation:
    """Hold cash until the first fill, then trade each target at its fill session's open.

    The valuations start at the close of the first fill session and end at the last session
    of the price book.
    """
    if capital <= 0:
        raise ValueError("capital must be positive")
    fills: list[Fill] = []
    schedule: dict[date, TargetPortfolio] = {}
    for target in sorted(targets, key=lambda item: item.cutoff_at):
        session = fill_session(book.sessions, target.cutoff_at, rules.lag_sessions)
        if session is None:
            fills.append(Fill(target.cutoff_at, None))
            continue
        if session in schedule:
            raise ValueError(f"two cutoffs fill on one session {session.isoformat()}")
        schedule[session] = target
    if not schedule:
        return Simulation((), (), tuple(fills), ())

    first = min(schedule)
    splits_on: dict[date, list[SplitEvent]] = {}
    for event in book.splits:
        splits_on.setdefault(event.ex_date, []).append(event)
    state = _Book(cash=capital)
    trades: list[Trade] = []
    applied: list[AppliedSplit] = []
    deferred: list[DeferredFill] = []
    pending: dict[str, _Pending] = {}
    valuations: list[Valuation] = []
    peak = capital
    for session in book.sessions:
        for event in sorted(splits_on.get(session, ()), key=lambda item: item.symbol):
            record = _apply_split(state, event)
            if record is not None:
                applied.append(record)
        opens = {
            symbol: opened
            for symbol, series in book.bars.items()
            if (bar := series.get(session)) is not None and (opened := _price(bar.open)) is not None
        }
        due = schedule.get(session)
        if due is not None:
            made, fill = _rebalance(state, due, session, opens, rules, pending)
            trades.extend(made)
            fills.append(fill)
        else:
            state.last.update(opens)
            made, late = _fill_pending(state, pending, session, opens, rules)
            trades.extend(made)
            deferred.extend(late)
        for symbol, series in book.bars.items():
            bar = series.get(session)
            if bar is not None and (close := _price(bar.close)) is not None:
                state.last[symbol] = close
        if session < first:
            continue
        market = state.market_value()
        value = state.cash + market
        peak = max(peak, value)
        valuations.append(
            Valuation(
                session=session,
                value=value,
                cum_nav=value / capital,
                drawdown=(peak - value) / peak,
                gross_exposure=market / value,
                cash_weight=state.cash / value,
            )
        )
    fills.sort(key=lambda item: item.cutoff_at)
    return Simulation(tuple(valuations), tuple(trades), tuple(fills), tuple(applied), tuple(deferred))


def equal_weight_target(
    symbols: Sequence[str], cutoff_at: datetime, quantization: DecimalQuantization
) -> TargetPortfolio:
    """The benchmark target: equal weights over the universe that sum to exactly 1."""
    if not symbols:
        raise ValueError("the benchmark universe is empty")
    return TargetPortfolio(cutoff_at, equal_weights(symbols, quantization))


# -- performance ------------------------------------------------------------------------


@dataclass(frozen=True)
class Performance:
    """Summary statistics of one valuation series. A statistic without enough data is None."""

    total_return: Decimal
    cagr: Decimal | None
    volatility: Decimal | None
    sharpe: Decimal | None
    max_drawdown: Decimal
    calmar: Decimal | None
    #: Sessions per year, measured from the series (count of returns over its span in years).
    sessions_per_year: Decimal | None


def performance(valuations: Sequence[Valuation]) -> Performance:
    """CAGR, volatility, Sharpe (risk-free rate 0), maximum drawdown and Calmar of a series.

    Annualization uses the measured sessions per year of the series, not a fixed count.
    """
    if not valuations:
        raise ValueError("no valuation to summarize")
    first, last = valuations[0], valuations[-1]
    total_return = last.cum_nav - _ONE
    max_drawdown = max(item.drawdown for item in valuations)
    years = Decimal((last.session - first.session).days) / _DAYS_PER_YEAR
    returns = [current.cum_nav / previous.cum_nav - _ONE for previous, current in zip(valuations, valuations[1:])]
    if years <= 0 or len(returns) < 2:
        return Performance(total_return, None, None, None, max_drawdown, None, None)
    cagr = last.cum_nav ** (_ONE / years) - _ONE
    sessions_per_year = Decimal(len(returns)) / years
    mean = sum(returns, _ZERO) / Decimal(len(returns))
    variance = sum(((item - mean) ** 2 for item in returns), _ZERO) / Decimal(len(returns) - 1)
    deviation = variance.sqrt()
    root = sessions_per_year.sqrt()
    volatility = deviation * root
    sharpe = mean / deviation * root if deviation > 0 else None
    calmar = cagr / max_drawdown if max_drawdown > 0 else None
    return Performance(total_return, cagr, volatility, sharpe, max_drawdown, calmar, sessions_per_year)


def monthly_turnover(simulation: Simulation) -> Decimal:
    """The one-way turnover of all fills divided by the span of the valuations in months."""
    total = sum((fill.turnover for fill in simulation.fills), _ZERO)
    if len(simulation.valuations) < 2:
        return total
    days = Decimal((simulation.valuations[-1].session - simulation.valuations[0].session).days)
    months = days * 12 / _DAYS_PER_YEAR
    return total / months if months > 0 else total


def month_end_valuations(valuations: Sequence[Valuation]) -> tuple[Valuation, ...]:
    """The last valuation of each calendar month, oldest first."""
    last_of_month: dict[tuple[int, int], Valuation] = {}
    for item in valuations:
        last_of_month[(item.session.year, item.session.month)] = item
    return tuple(last_of_month[key] for key in sorted(last_of_month))


__all__ = [
    "BENCHMARK_RULE",
    "CLAIM_CEILING",
    "ENGINE_ID",
    "MIN_ORDER_NOTIONAL",
    "PRICE_BASIS",
    "NEXT_SESSION_OPEN",
    "REMAIN_CASH",
    "AppliedSplit",
    "Bar",
    "DeferredFill",
    "ExecutionRules",
    "Fill",
    "Performance",
    "PriceBook",
    "Simulation",
    "TargetPortfolio",
    "Trade",
    "UnsupportedRuleError",
    "Valuation",
    "benchmark_rules",
    "equal_weight_target",
    "equal_weights",
    "execution_rules",
    "fill_session",
    "month_end_valuations",
    "monthly_turnover",
    "performance",
    "simulate",
]
