"""Portfolio simulation of the core strategy backtest (#758, M3)."""

from __future__ import annotations

import dataclasses
from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_EVEN, Decimal

from truealpha_contracts.strategy import DecimalQuantization, LargeModelValueV0Definition

from factors.backtest.corporate_actions import SplitEvent

ENGINE_ID = "factors.backtest.engine:unadjusted-open-explicit-splits:v2"
PRICE_BASIS = "unadjusted_open_explicit_splits"
CLAIM_CEILING = "price_return_no_dividends"
BENCHMARK_RULE = "equal_weight_buy_and_hold_first_cutoff_issuers"
REMAIN_CASH = "remain_cash"
NEXT_SESSION_OPEN = "next_session_open"
MIN_ORDER_NOTIONAL = Decimal("0.01")

_BASIS_POINTS_PER_UNIT = Decimal(10_000)
_DAYS_PER_YEAR = Decimal("365.25")
_ZERO = Decimal(0)
_ONE = Decimal(1)


class UnsupportedRuleError(ValueError):
    """The definition names a rule this engine does not implement."""


@dataclass(frozen=True)
class ExecutionRules:
    lag_sessions: int
    cost_rate: Decimal
    weight_quantization: DecimalQuantization
    unfilled_order_behavior: str = REMAIN_CASH

    def __post_init__(self) -> None:
        if self.unfilled_order_behavior not in (REMAIN_CASH, NEXT_SESSION_OPEN):
            raise UnsupportedRuleError(f"unfilled_order_behavior {self.unfilled_order_behavior!r} is not implemented")


def execution_rules(definition: LargeModelValueV0Definition) -> ExecutionRules:
    """Validate and extract engine parameters from ``definition``."""
    execution = definition.execution
    cost = definition.transaction_cost
    total_return = definition.total_return
    sizing = definition.sizing
    checks: tuple[tuple[object, object, str], ...] = (
        (execution.rule_id, "next_eligible_market_session_open", "execution.rule_id"),
        (execution.price_field, "unadjusted_open", "execution.price_field"),
        (execution.unfilled_order_behavior, "remain_cash", "execution.unfilled_order_behavior"),
        (cost.rule_id, "per_side_notional_basis_points", "transaction_cost.rule_id"),
        (total_return.rule_id, "unadjusted_bars_explicit_corporate_actions", "total_return.rule_id"),
        (total_return.price_basis, "unadjusted", "total_return.price_basis"),
        (
            total_return.corporate_action_processing,
            "explicit_event_lifecycle_once",
            "total_return.corporate_action_processing",
        ),
        (total_return.adjusted_prices_affect_returns, False, "total_return.adjusted_prices_affect_returns"),
        (sizing.rule_id, "equal_weight_selected", "sizing.rule_id"),
        (sizing.empty_selection_behavior, "retain_all_cash", "sizing.empty_selection_behavior"),
        (sizing.quantization.rounding, "half_even", "sizing.quantization.rounding"),
    )
    for actual, expected, name in checks[:3]:
        if actual != expected:
            raise UnsupportedRuleError(f"{name} is {actual!r}; this engine implements only {expected!r}")
    if not isinstance(execution.lag_sessions, int) or execution.lag_sessions < 1:
        raise UnsupportedRuleError(f"execution.lag_sessions is {execution.lag_sessions!r}; it must be 1 or more")
    for actual, expected, name in checks[3:]:
        if actual != expected:
            raise UnsupportedRuleError(f"{name} is {actual!r}; this engine implements only {expected!r}")
    return ExecutionRules(
        lag_sessions=execution.lag_sessions,
        cost_rate=Decimal(cost.basis_points) / _BASIS_POINTS_PER_UNIT,
        weight_quantization=sizing.quantization,
        unfilled_order_behavior=execution.unfilled_order_behavior,
    )


def benchmark_rules(rules: ExecutionRules) -> ExecutionRules:
    return dataclasses.replace(rules, unfilled_order_behavior=NEXT_SESSION_OPEN)


def equal_weights(symbols: Sequence[str], quantization: DecimalQuantization) -> dict[str, Decimal]:
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


@dataclass(frozen=True)
class Bar:
    open: Decimal | None
    close: Decimal | None


def _price(value: Decimal | None) -> Decimal | None:
    return value if value is not None and value > 0 else None


@dataclass(frozen=True)
class PriceBook:
    bars: Mapping[str, Mapping[date, Bar]]
    splits: Sequence[SplitEvent] = ()
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
    if lag_sessions < 1:
        raise ValueError("lag_sessions must be 1 or more")
    index = bisect_right(sessions, cutoff_at.astimezone(UTC).date()) + lag_sessions - 1
    return sessions[index] if index < len(sessions) else None


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
    cutoff_at: datetime
    session: date | None
    unfilled_symbols: tuple[str, ...] = ()
    turnover: Decimal = _ZERO


@dataclass(frozen=True)
class DeferredFill:
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


@dataclass
class _Book:
    cash: Decimal
    shares: dict[str, Decimal] = field(default_factory=dict)
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
    symbol: str
    price: Decimal
    amount: Decimal


@dataclass(frozen=True)
class _Pending:
    cutoff_at: datetime
    ordered_session: date
    amount: Decimal


def _spend(
    book: _Book, orders: Sequence[_Order], rules: ExecutionRules, cutoff_at: datetime, session: date, value: Decimal
) -> list[Trade]:
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
        notional = remaining / (_ONE + rate)
        legs.append((full[-1][0], notional, remaining - notional))
        book.cash = _ZERO
    trades: list[Trade] = []
    for order, notional, fee in legs:
        if notional < MIN_ORDER_NOTIONAL:
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


def _build_schedule(
    sessions: Sequence[date], targets: Sequence[TargetPortfolio], lag_sessions: int
) -> tuple[dict[date, TargetPortfolio], list[Fill]]:
    fills: list[Fill] = []
    schedule: dict[date, TargetPortfolio] = {}
    for target in sorted(targets, key=lambda item: item.cutoff_at):
        session = fill_session(sessions, target.cutoff_at, lag_sessions)
        if session is None:
            fills.append(Fill(target.cutoff_at, None))
            continue
        if session in schedule:
            raise ValueError(f"two cutoffs fill on one session {session.isoformat()}")
        schedule[session] = target
    return schedule, fills


def simulate(
    book: PriceBook,
    targets: Sequence[TargetPortfolio],
    rules: ExecutionRules,
    *,
    capital: Decimal,
) -> Simulation:
    if capital <= 0:
        raise ValueError("capital must be positive")
    schedule, fills = _build_schedule(book.sessions, targets, rules.lag_sessions)
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
            if (record := _apply_split(state, event)) is not None:
                applied.append(record)
        opens = {
            symbol: opened
            for symbol, series in book.bars.items()
            if (bar := series.get(session)) is not None and (opened := _price(bar.open)) is not None
        }
        if (due := schedule.get(session)) is not None:
            made, fill = _rebalance(state, due, session, opens, rules, pending)
            trades.extend(made)
            fills.append(fill)
        else:
            state.last.update(opens)
            made, late = _fill_pending(state, pending, session, opens, rules)
            trades.extend(made)
            deferred.extend(late)
        for symbol, series in book.bars.items():
            if (bar := series.get(session)) is not None and (close := _price(bar.close)) is not None:
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
    if not symbols:
        raise ValueError("the benchmark universe is empty")
    return TargetPortfolio(cutoff_at, equal_weights(symbols, quantization))


@dataclass(frozen=True)
class Performance:
    total_return: Decimal
    cagr: Decimal | None
    volatility: Decimal | None
    sharpe: Decimal | None
    max_drawdown: Decimal
    calmar: Decimal | None
    sessions_per_year: Decimal | None


def performance(valuations: Sequence[Valuation]) -> Performance:
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
    total = sum((fill.turnover for fill in simulation.fills), _ZERO)
    if len(simulation.valuations) < 2:
        return total
    days = Decimal((simulation.valuations[-1].session - simulation.valuations[0].session).days)
    months = days * 12 / _DAYS_PER_YEAR
    return total / months if months > 0 else total


def month_end_valuations(valuations: Sequence[Valuation]) -> tuple[Valuation, ...]:
    last_of_month = {(item.session.year, item.session.month): item for item in valuations}
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
