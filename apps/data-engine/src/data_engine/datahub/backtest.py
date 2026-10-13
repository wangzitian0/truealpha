"""The nightly backtest of the core strategy, ``large_model_value_v0`` (#758, M3).

One path, from staging to mart:

1. Cutoffs. The definition's schedule is calendar quarter ends at 23:59:59Z. The run evaluates
   every schedule cutoff that ``staging.strategy_backtest_inputs`` holds, from the first to the
   latest. The projector (``datahub.strategy_history``) writes those inputs as of each quarter
   end. A quarter missing between the first and the latest stops the run. The schedule's
   ``first_cutoff_at`` anchors the LIVE schedule only. It does not bound this historical window.
2. Decisions. At each cutoff, ``factors.composite.strategy_evaluator.evaluate_cutoff`` evaluates
   the inputs of that cutoff only. The gateway reads ``where cutoff_at = <cutoff>``, and the
   table CHECK holds ``knowable_at <= cutoff_at`` for each row. The labor-efficiency binding and
   the risk-free rate are the ones the live replay uses.
3. Prices. Unadjusted daily bars (``adjust = 'none'``) and split-adjusted daily bars
   (``adjust = 'splits'``), the latest vintage of each session. The ratio of the two closes
   gives the split events (``factors.backtest.corporate_actions``).
4. Simulation. ``factors.backtest.engine`` with the definition's execution, transaction-cost,
   total-return and sizing rules as its only parameters. The benchmark is an equal weight of the
   same universe, bought at the first fill and held, on the same prices and costs.
5. Persistence. ``run_id`` is the content hash of the engine id, the definition hash, the input
   snapshot, the issuer mapping, the price snapshot and the run parameters. A rerun on the same
   data writes nothing. The same ``run_id`` with a different result stops the run.

No dividend source exists, so each run is a price return. The run records that on
``mart.backtest_runs.claim_ceiling``.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import psycopg
from factors.backtest.corporate_actions import SeriesBar, SplitEvent, derive_split_events
from factors.backtest.engine import (
    BENCHMARK_RULE,
    CLAIM_CEILING,
    ENGINE_ID,
    PRICE_BASIS,
    Bar,
    Fill,
    Performance,
    PriceBook,
    Simulation,
    TargetPortfolio,
    Valuation,
    equal_weight_target,
    execution_rules,
    month_end_valuations,
    monthly_turnover,
    performance,
    simulate,
)
from factors.composite.strategy_evaluator import IssuerInput, evaluate_cutoff
from factors.forest import IssuerClass
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.strategy import GoldenDecisionOutcome, LargeModelValueV0Definition, QuarterlyCutoffSchedule

from data_engine.datahub.market_prices import SPLIT_ADJUSTED, UNADJUSTED
from data_engine.datahub.production_topt.composition import RISK_FREE_RATE
from data_engine.datahub.strategy_bridge import load_strategy_definition
from data_engine.datahub.strategy_history import (
    HistoryIssuer,
    is_schedule_cutoff,
    quarter_end_cutoffs,
    stored_history_issuers,
)
from data_engine.strategy_backtest_gateway import StrategyBacktestGateway

#: The notional scale of a run. Shares are fractional, so no result depends on it.
NOTIONAL_CAPITAL = Decimal("1000000")
#: The risk-free rate the Sharpe ratio subtracts. The page labels the ratio with it.
SHARPE_RISK_FREE_RATE = Decimal(0)

#: What a reader of a run must know about its limits. Stored on every run.
LIMITATIONS = (
    "price return only: no dividend source exists",
    "constant risk-free rate in the labor-efficiency factor (A6 decision 3)",
    "the universe is the issuers that hold strategy inputs today: survivorship bias",
    "issuer class and ticker come from today's stored lineage",
    "each session uses the latest stored vintage of its bar",
    "an input is admitted only when knowable before the cutoff's calendar date",
)


class BacktestPending(RuntimeError):
    """The run cannot judge yet: data it needs is not in this environment."""


# -- cutoffs ----------------------------------------------------------------------------


def schedule_cutoffs(connection: psycopg.Connection[Any], schedule: QuarterlyCutoffSchedule) -> list[datetime]:
    """The schedule cutoffs that hold inputs, oldest first. Raise when a quarter is missing between them."""
    rows = connection.execute("select distinct cutoff_at from staging.strategy_backtest_inputs").fetchall()
    found = sorted({row[0].astimezone(UTC) for row in rows if is_schedule_cutoff(row[0], schedule)})
    if not found:
        return []
    missing = sorted(set(quarter_end_cutoffs(schedule, start=found[0], end=found[-1])) - set(found))
    if missing:
        raise ValueError(
            "the quarter-end input history has gaps: no inputs at "
            f"{[cutoff.isoformat() for cutoff in missing]}; run strategy_history_projection_pipeline"
        )
    return found


# -- decisions --------------------------------------------------------------------------


@dataclass(frozen=True)
class CutoffDecision:
    """The target of one cutoff: symbol weights and why the other issuers were not selected."""

    cutoff_at: datetime
    weights: Mapping[str, Decimal]
    selected: tuple[str, ...]
    eligible: int
    outcomes: Mapping[str, int]


InputsAt = Callable[[datetime], Sequence[IssuerInput]]


def decide(
    cutoffs: Sequence[datetime],
    inputs_at: InputsAt,
    issuers: Mapping[str, HistoryIssuer],
    definition: LargeModelValueV0Definition,
    *,
    risk_free_rate: Decimal,
) -> list[CutoffDecision]:
    """Evaluate the strategy at each cutoff over the inputs of that cutoff only."""
    decisions: list[CutoffDecision] = []
    for cutoff in cutoffs:
        inputs = [replace(item, issuer_class=_issuer_class(issuers.get(item.issuer_id))) for item in inputs_at(cutoff)]
        evaluated = evaluate_cutoff(inputs, definition=definition, cutoff_at=cutoff, risk_free_rate=risk_free_rate)
        weights: dict[str, Decimal] = {}
        selected: list[str] = []
        outcomes: dict[str, int] = {}
        for decision in evaluated:
            key = decision.exclusion_reason.value if decision.exclusion_reason else decision.outcome.value
            outcomes[key] = outcomes.get(key, 0) + 1
            if decision.outcome is not GoldenDecisionOutcome.SELECTED:
                continue
            issuer = issuers.get(decision.issuer_id)
            if issuer is None or decision.target_weight is None:
                raise ValueError(f"{decision.issuer_id} is selected at {cutoff.isoformat()} without a listing")
            weights[issuer.ticker] = decision.target_weight
            selected.append(decision.issuer_id)
        decisions.append(
            CutoffDecision(
                cutoff_at=cutoff,
                weights=weights,
                selected=tuple(selected),
                eligible=sum(1 for item in evaluated if item.eligible),
                outcomes=outcomes,
            )
        )
    return decisions


def _issuer_class(issuer: HistoryIssuer | None) -> IssuerClass | None:
    return None if issuer is None else IssuerClass(issuer.operating_branch.value)


# -- prices -----------------------------------------------------------------------------

DailySeries = dict[str, dict[date, tuple[Decimal | None, Decimal | None]]]

_SERIES_SQL = """
select distinct on (symbol, trading_date) symbol, trading_date, open, close
from staging.market_prices_daily
where symbol = any(%s) and adjust = %s
order by symbol, trading_date, recorded_at desc
"""


def load_daily_series(connection: psycopg.Connection[Any], symbols: Sequence[str], *, adjust: str) -> DailySeries:
    """The latest vintage of each daily bar of one series, as (open, close) per symbol and session."""
    series: DailySeries = {}
    for symbol, trading_date, open_, close in connection.execute(_SERIES_SQL, (list(symbols), adjust)).fetchall():
        series.setdefault(symbol, {})[trading_date] = (open_, close)
    return series


def price_book(unadjusted: DailySeries, split_adjusted: DailySeries) -> PriceBook:
    """Unadjusted bars and the split events the two series show. Raise on an ambiguous split."""
    bars: dict[str, dict[date, Bar]] = {}
    splits: list[SplitEvent] = []
    for symbol in sorted(unadjusted):
        adjusted = split_adjusted.get(symbol, {})
        bars[symbol] = {day: Bar(open_, close) for day, (open_, close) in unadjusted[symbol].items()}
        sessions = sorted(set(unadjusted[symbol]) | set(adjusted))
        splits.extend(
            derive_split_events(
                symbol,
                [
                    SeriesBar(
                        session=day,
                        unadjusted_open=unadjusted[symbol].get(day, (None, None))[0],
                        unadjusted_close=unadjusted[symbol].get(day, (None, None))[1],
                        split_adjusted_close=adjusted.get(day, (None, None))[1],
                    )
                    for day in sessions
                ],
            )
        )
    return PriceBook(bars=bars, splits=tuple(splits))


def price_snapshot_sha256(unadjusted: DailySeries, split_adjusted: DailySeries) -> str:
    """The content hash of every bar the run read."""
    rows = [
        [adjust, symbol, day.isoformat(), str(open_), str(close)]
        for adjust, series in ((UNADJUSTED, unadjusted), (SPLIT_ADJUSTED, split_adjusted))
        for symbol, bars in series.items()
        for day, (open_, close) in bars.items()
    ]
    return canonical_sha256(sorted(rows))


# -- the run ----------------------------------------------------------------------------


@dataclass(frozen=True)
class BacktestOutcome:
    run_id: str
    inserted: bool
    strategy_key: str
    cutoffs: int
    trades: int
    strategy: Performance
    benchmark: Performance

    @property
    def summary(self) -> str:
        state = "succeeded" if self.inserted else "unchanged"
        return (
            f"{state}: {self.run_id[:25]} {self.strategy_key} cutoffs={self.cutoffs} trades={self.trades} "
            f"cagr={_text(self.strategy.cagr)} benchmark_cagr={_text(self.benchmark.cagr)} "
            f"max_dd={_text(self.strategy.max_drawdown)}"
        )


def _text(value: Decimal | None) -> str:
    return "none" if value is None else f"{value:.4f}"


def run_core_strategy_backtest(connection: psycopg.Connection[Any], *, tick: datetime) -> BacktestOutcome:
    """Backtest ``large_model_value_v0`` over the stored quarter-end history and persist the run.

    Raise ``BacktestPending`` when this environment holds no data to judge yet. The caller owns
    the transaction.
    """
    if tick.tzinfo is None or tick.utcoffset() is None:
        raise ValueError("tick must carry a time zone")
    definition = load_strategy_definition()
    rules = execution_rules(definition)
    cutoffs = schedule_cutoffs(connection, definition.schedule)
    if not cutoffs:
        raise BacktestPending("no quarter-end strategy inputs; run strategy_history_projection_pipeline")

    issuer_ids = [
        row[0]
        for row in connection.execute(
            "select distinct issuer_id from staging.strategy_backtest_inputs where cutoff_at = any(%s) order by 1",
            (cutoffs,),
        ).fetchall()
    ]
    resolved = stored_history_issuers(connection, issuer_ids, known_at=tick)
    if not resolved:
        raise RuntimeError(f"no stored lineage resolves any of the {len(issuer_ids)} issuers with quarter-end inputs")
    issuers = {issuer.issuer_id: issuer for issuer in resolved}
    tickers = [issuer.ticker for issuer in resolved]
    if len(set(tickers)) != len(tickers):
        raise RuntimeError(f"two issuers resolve to one ticker: {sorted(tickers)}")

    gateway = StrategyBacktestGateway(connection)
    decisions = decide(cutoffs, gateway.issuer_inputs, issuers, definition, risk_free_rate=RISK_FREE_RATE)
    if not any(decision.weights for decision in decisions):
        outcomes: dict[str, int] = {}
        for decision in decisions:
            for key, count in decision.outcomes.items():
                outcomes[key] = outcomes.get(key, 0) + count
        raise RuntimeError(f"no cutoff of {len(cutoffs)} selected an issuer; outcomes {dict(sorted(outcomes.items()))}")

    symbols = sorted(tickers)
    unadjusted = load_daily_series(connection, symbols, adjust=UNADJUSTED)
    if not unadjusted:
        raise BacktestPending(f"no unadjusted daily bars for {len(symbols)} symbols")
    split_adjusted = load_daily_series(connection, symbols, adjust=SPLIT_ADJUSTED)
    missing_adjusted = sorted(set(unadjusted) - set(split_adjusted))
    if missing_adjusted:
        raise RuntimeError(f"no split-adjusted daily bars, so no split events, for {missing_adjusted}")
    book = price_book(unadjusted, split_adjusted)

    strategy = simulate(
        book,
        [TargetPortfolio(decision.cutoff_at, decision.weights) for decision in decisions],
        rules,
        capital=NOTIONAL_CAPITAL,
    )
    if not strategy.valuations:
        raise BacktestPending(f"no session after the first cutoff {cutoffs[0].isoformat()} in the daily bars")
    if not strategy.trades:
        raise RuntimeError("the strategy selected issuers but no order filled: no open on any fill session")
    benchmark = simulate(book, [equal_weight_target(sorted(unadjusted), cutoffs[0])], rules, capital=NOTIONAL_CAPITAL)

    binding = {
        "engine_id": ENGINE_ID,
        "strategy_key": definition.strategy_id,
        "definition_content_sha256": definition.content_sha256,
        "cutoffs": [cutoff.isoformat() for cutoff in cutoffs],
        "input_snapshot_id": gateway.run_snapshot_id([cutoff.isoformat() for cutoff in cutoffs]),
        "issuers": sorted([issuer.issuer_id, issuer.ticker, issuer.operating_branch.value] for issuer in resolved),
        "price_snapshot_sha256": price_snapshot_sha256(unadjusted, split_adjusted),
        "risk_free_rate": str(RISK_FREE_RATE),
        "capital": str(NOTIONAL_CAPITAL),
        "benchmark_rule": BENCHMARK_RULE,
    }
    run_id = f"backtest-run:{canonical_sha256(binding)}"
    record = _record(
        run_id=run_id,
        tick=tick,
        definition=definition,
        binding=binding,
        decisions=decisions,
        strategy=strategy,
        benchmark=benchmark,
        book=book,
        unresolved=sorted(set(issuer_ids) - set(issuers)),
        cost_basis_points=definition.transaction_cost.basis_points,
    )
    inserted = persist_backtest_run(connection, record)
    return BacktestOutcome(
        run_id=run_id,
        inserted=inserted,
        strategy_key=definition.strategy_id,
        cutoffs=len(cutoffs),
        trades=len(strategy.trades),
        strategy=record.strategy_performance,
        benchmark=record.benchmark_performance,
    )


# -- the mart record --------------------------------------------------------------------


@dataclass(frozen=True)
class BacktestRecord:
    run_id: str
    strategy_key: str
    strategy_version: str
    universe_id: str
    start_date: date
    end_date: date
    executed_at: datetime
    definition_content_sha256: str
    strategy_performance: Performance
    benchmark_performance: Performance
    turnover_monthly: Decimal
    metrics_payload: dict[str, Any]
    #: (resolution, session, cum_nav, drawdown, gross_exposure, cash_weight, benchmark_cum_nav)
    valuations: tuple[tuple[str, date, Decimal, Decimal, Decimal, Decimal, Decimal], ...]
    #: (session, symbol, side, shares, price, notional, weight_before, weight_after, fee)
    trades: tuple[tuple[date, str, str, Decimal, Decimal, Decimal, Decimal, Decimal, Decimal], ...]
    result_sha256: str


def _decimal_map(value: Performance) -> dict[str, str | None]:
    return {key: None if item is None else str(item) for key, item in value.__dict__.items()}


def _valuation_rows(
    strategy: Sequence[Valuation], benchmark: Mapping[date, Valuation], resolution: str
) -> list[tuple[str, date, Decimal, Decimal, Decimal, Decimal, Decimal]]:
    rows = []
    for item in strategy:
        paired = benchmark.get(item.session)
        if paired is None:
            raise ValueError(f"the benchmark has no valuation on {item.session.isoformat()}")
        rows.append(
            (
                resolution,
                item.session,
                item.cum_nav,
                item.drawdown,
                item.gross_exposure,
                item.cash_weight,
                paired.cum_nav,
            )
        )
    return rows


def _cutoff_entry(decision: CutoffDecision, fill: Fill | None) -> dict[str, Any]:
    return {
        "cutoff_at": decision.cutoff_at.isoformat(),
        "fill_session": None if fill is None or fill.session is None else fill.session.isoformat(),
        "weights": {symbol: str(weight) for symbol, weight in sorted(decision.weights.items())},
        "selected_issuers": list(decision.selected),
        "eligible": decision.eligible,
        "outcomes": dict(sorted(decision.outcomes.items())),
        "unfilled_symbols": [] if fill is None else list(fill.unfilled_symbols),
        "turnover": None if fill is None else str(fill.turnover),
    }


def _record(
    *,
    run_id: str,
    tick: datetime,
    definition: LargeModelValueV0Definition,
    binding: dict[str, Any],
    decisions: Sequence[CutoffDecision],
    strategy: Simulation,
    benchmark: Simulation,
    book: PriceBook,
    unresolved: Sequence[str],
    cost_basis_points: Decimal,
) -> BacktestRecord:
    benchmark_by_session = {item.session: item for item in benchmark.valuations}
    valuations = _valuation_rows(month_end_valuations(strategy.valuations), benchmark_by_session, "1M")
    valuations += _valuation_rows(strategy.valuations, benchmark_by_session, "1D")
    trades = tuple(
        (
            trade.session,
            trade.symbol,
            trade.side,
            trade.shares,
            trade.price,
            trade.notional,
            trade.weight_before,
            trade.weight_after,
            trade.fee,
        )
        for trade in strategy.trades
    )
    strategy_performance = performance(strategy.valuations)
    benchmark_performance = performance(benchmark.valuations)
    first, last = strategy.valuations[0].session, strategy.valuations[-1].session
    turnover_monthly = monthly_turnover(strategy)
    fills = {fill.cutoff_at: fill for fill in strategy.fills}
    result = {
        "valuations": [[str(cell) for cell in row] for row in valuations],
        "trades": [[str(cell) for cell in row] for row in trades],
        "strategy": _decimal_map(strategy_performance),
        "benchmark": _decimal_map(benchmark_performance),
        "turnover_monthly": str(turnover_monthly),
    }
    result_sha256 = canonical_sha256(result)
    payload: dict[str, Any] = {
        "binding": binding,
        "result_sha256": result_sha256,
        "engine_id": ENGINE_ID,
        "price_basis": PRICE_BASIS,
        "claim_ceiling": CLAIM_CEILING,
        "execution": {
            "rule_id": definition.execution.rule_id,
            "lag_sessions": definition.execution.lag_sessions,
            "price_field": definition.execution.price_field,
            "unfilled_order_behavior": definition.execution.unfilled_order_behavior,
            "transaction_cost_basis_points_per_side": str(cost_basis_points),
        },
        "strategy": _decimal_map(strategy_performance),
        "benchmark": {"rule": BENCHMARK_RULE, **_decimal_map(benchmark_performance)},
        "turnover_monthly": str(turnover_monthly),
        "sharpe_risk_free_rate": str(SHARPE_RISK_FREE_RATE),
        "cutoffs": [_cutoff_entry(decision, fills.get(decision.cutoff_at)) for decision in decisions],
        "split_events": [
            {"symbol": event.symbol, "ex_date": event.ex_date.isoformat(), "factor": str(event.factor)}
            for event in book.splits
        ],
        "splits_applied": [
            {
                "symbol": item.symbol,
                "ex_date": item.ex_date.isoformat(),
                "factor": f"{item.numerator}/{item.denominator}",
                "shares_before": str(item.shares_before),
                "shares_after": str(item.shares_after),
            }
            for item in strategy.splits_applied
        ],
        "unresolved_issuers": list(unresolved),
        "limitations": list(LIMITATIONS),
    }
    symbols = sorted(book.bars)
    return BacktestRecord(
        run_id=run_id,
        strategy_key=definition.strategy_id,
        strategy_version=definition.definition_version,
        universe_id=f"strategy-backtest-inputs:{len(symbols)}-symbols:{canonical_sha256(symbols)[:16]}",
        start_date=first,
        end_date=last,
        executed_at=tick,
        definition_content_sha256=definition.content_sha256,
        strategy_performance=strategy_performance,
        benchmark_performance=benchmark_performance,
        turnover_monthly=turnover_monthly,
        metrics_payload=payload,
        valuations=tuple(valuations),
        trades=trades,
        result_sha256=result_sha256,
    )


_INSERT_RUN_SQL = """
insert into mart.backtest_runs (
    run_id, strategy_key, strategy_version, universe_id, start_date, end_date, status,
    cagr_monthly, sharpe_daily, max_dd_daily, vol_daily, turnover_monthly, calmar_daily,
    metrics_payload, error_message, executed_at,
    definition_content_sha256, price_basis, claim_ceiling,
    benchmark_rule, benchmark_cagr, benchmark_max_dd
) values (
    %s, %s, %s, %s, %s, %s, 'succeeded',
    %s, %s, %s, %s, %s, %s,
    %s, null, %s,
    %s, %s, %s,
    %s, %s, %s
)
"""


def persist_backtest_run(connection: psycopg.Connection[Any], record: BacktestRecord) -> bool:
    """Insert the run, its valuations and its trades. Return False when the run already exists.

    The mart is append-only: an existing run is never updated. A run with the same ``run_id``
    and a different result means the engine changed without a new ``ENGINE_ID``; that raises.
    """
    existing = connection.execute(
        "select metrics_payload ->> 'result_sha256' from mart.backtest_runs where run_id = %s", (record.run_id,)
    ).fetchone()
    if existing is not None:
        if existing[0] != record.result_sha256:
            raise RuntimeError(
                f"{record.run_id} exists with result {existing[0]}, this run computed {record.result_sha256}: "
                "the same inputs gave a different result; change ENGINE_ID with the engine"
            )
        return False
    strategy, benchmark = record.strategy_performance, record.benchmark_performance
    connection.execute(
        _INSERT_RUN_SQL,
        (
            record.run_id,
            record.strategy_key,
            record.strategy_version,
            record.universe_id,
            record.start_date,
            record.end_date,
            strategy.cagr,
            strategy.sharpe,
            strategy.max_drawdown,
            strategy.volatility,
            record.turnover_monthly,
            strategy.calmar,
            json.dumps(record.metrics_payload, sort_keys=True),
            record.executed_at,
            record.definition_content_sha256,
            PRICE_BASIS,
            CLAIM_CEILING,
            BENCHMARK_RULE,
            benchmark.cagr,
            benchmark.max_drawdown,
        ),
    )
    with connection.cursor() as cursor:
        cursor.executemany(
            """
            insert into mart.backtest_valuations (
                run_id, resolution, valuation_date, cum_nav, drawdown, gross_exposure, cash_weight,
                benchmark_cum_nav
            ) values (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [(record.run_id, *row) for row in record.valuations],
        )
        cursor.executemany(
            """
            insert into mart.backtest_trades (
                run_id, trade_date, symbol, side, shares, execution_price, trade_value,
                weight_before, weight_after, fee_paid
            ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [(record.run_id, *row) for row in record.trades],
        )
    return True


__all__ = [
    "LIMITATIONS",
    "NOTIONAL_CAPITAL",
    "BacktestOutcome",
    "BacktestPending",
    "BacktestRecord",
    "CutoffDecision",
    "decide",
    "load_daily_series",
    "persist_backtest_run",
    "price_book",
    "price_snapshot_sha256",
    "run_core_strategy_backtest",
    "schedule_cutoffs",
]
