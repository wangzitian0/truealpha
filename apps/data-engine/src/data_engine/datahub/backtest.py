"""Nightly backtest of the core strategy ``large_model_value_v0`` (#758, M3)."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import psycopg
from factors.backtest.corporate_actions import SplitEvent, split_factor_between
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
    benchmark_rules,
    equal_weight_target,
    equal_weights,
    execution_rules,
    month_end_valuations,
    monthly_turnover,
    performance,
    simulate,
)
from factors.composite.strategy_evaluator import IssuerInput, evaluate_cutoff
from factors.panel import IssuerClass
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.strategy import (
    GoldenDecisionOutcome,
    LargeModelValueV0Definition,
    QuarterlyCutoffSchedule,
)

from data_engine.config import settings
from data_engine.datahub.market_prices import SPLIT_ADJUSTED, UNADJUSTED
from data_engine.datahub.price_series import DailySeries, load_daily_series, split_events_by_symbol
from data_engine.datahub.risk_free import RiskFreeRate, risk_free_rate_at
from data_engine.datahub.strategy_bridge import load_strategy_definition
from data_engine.datahub.strategy_history import (
    HistoryIssuer,
    canonical_history_issuers,
    is_schedule_cutoff,
    quarter_end_cutoffs,
)
from data_engine.strategy_backtest_gateway import (
    MarketValueVintage,
    StrategyBacktestGateway,
    combine_snapshot_ids,
    issuer_inputs_from_rows,
    snapshot_id_from_rows,
)

NOTIONAL_CAPITAL = Decimal("1000000")


@dataclass(frozen=True)
class NamedRateParameter:
    name: str
    series_id: str | None
    value: Decimal
    basis: str

    def payload(self) -> dict[str, str | None]:
        return {"name": self.name, "series_id": self.series_id, "value": str(self.value), "basis": self.basis}


SHARPE_RISK_FREE = NamedRateParameter(
    name="sharpe_risk_free_rate",
    series_id=None,
    value=Decimal(0),
    basis="declared zero rate: the Sharpe ratio is the mean daily return over its standard deviation",
)

LIMITATIONS = (
    "Price return only. No dividend source exists, so dividends are not included.",
    "The labor-efficiency factor uses the 3-month U.S. Treasury par yield of the last business day before each cutoff date.",
    "The universe is the issuers that hold strategy inputs today. Issuers that left earlier are absent: survivorship bias.",
    "Issuer class and ticker come from today's stored lineage, not from each cutoff.",
    "Each bar and each input uses its latest stored vintage. A later vendor restatement can change history.",
    "A session is any date with a bar of any universe symbol. No exchange calendar is applied.",
    "An input is admitted only when it is knowable before the cutoff's calendar date.",
)

DEPLOYED_ENVIRONMENTS = ("production", "prod", "staging")
_NO_CODE_IDENTITY = ("", "unknown")


def deployed_code_identity(app_env: str, git_commit_sha: str) -> str:
    sha = (git_commit_sha or "").strip()
    if app_env.strip().lower() in DEPLOYED_ENVIRONMENTS and sha.lower() in _NO_CODE_IDENTITY:
        raise RuntimeError(
            f"the backtest refuses to run in {app_env!r} without a code identity: GIT_COMMIT_SHA is "
            f"{git_commit_sha!r}; the deployment must inject the release ref"
        )
    return sha


class BacktestPending(RuntimeError):
    """Data needed by the backtest is not yet present in this environment."""


class ShareBasisError(RuntimeError):
    """A winning share count predates a split that its close follows (#1270)."""


def schedule_cutoffs(connection: psycopg.Connection[Any], schedule: QuarterlyCutoffSchedule) -> list[datetime]:
    rows = connection.execute("select distinct cutoff_at from staging.strategy_backtest_inputs").fetchall()
    found = sorted({row[0].astimezone(UTC) for row in rows if is_schedule_cutoff(row[0], schedule)})
    return require_contiguous_quarters(found, schedule)


def require_contiguous_quarters(cutoffs: Sequence[datetime], schedule: QuarterlyCutoffSchedule) -> list[datetime]:
    found = sorted(cutoffs)
    if not found:
        return []
    missing = sorted(set(quarter_end_cutoffs(schedule, start=found[0], end=found[-1])) - set(found))
    if missing:
        raise ValueError(
            f"the quarter-end cutoffs are not contiguous: {[cutoff.isoformat() for cutoff in missing]} are "
            f"missing between {found[0].isoformat()} and {found[-1].isoformat()}; "
            "run strategy_history_projection_pipeline"
        )
    return found


@dataclass(frozen=True)
class CutoffDecision:
    cutoff_at: datetime
    weights: Mapping[str, Decimal]
    selected: tuple[str, ...]
    eligible: int
    outcomes: Mapping[str, int]


InputsAt = Callable[[datetime], Sequence[IssuerInput]]
RateAt = Callable[[datetime], Decimal]


def _issuer_class(issuer: HistoryIssuer | None) -> IssuerClass | None:
    return None if issuer is None else IssuerClass(issuer.operating_branch.value)


def decide(
    cutoffs: Sequence[datetime],
    inputs_at: InputsAt,
    issuers: Mapping[str, HistoryIssuer],
    definition: LargeModelValueV0Definition,
    *,
    rate_at: RateAt,
) -> list[CutoffDecision]:
    decisions: list[CutoffDecision] = []
    for cutoff in cutoffs:
        rate = rate_at(cutoff)
        inputs = [replace(item, issuer_class=_issuer_class(issuers.get(item.issuer_id))) for item in inputs_at(cutoff)]
        evaluated = evaluate_cutoff(inputs, definition=definition, cutoff_at=cutoff, risk_free_rate=rate)
        selected: list[str] = []
        tickers: list[str] = []
        evaluator_weights: set[Decimal | None] = set()
        outcomes: dict[str, int] = {}
        for decision in evaluated:
            key = decision.exclusion_reason.value if decision.exclusion_reason else decision.outcome.value
            outcomes[key] = outcomes.get(key, 0) + 1
            if decision.outcome is not GoldenDecisionOutcome.SELECTED:
                continue
            issuer = issuers.get(decision.issuer_id)
            if issuer is None:
                raise ValueError(f"{decision.issuer_id} is selected at {cutoff.isoformat()} without a listing")
            tickers.append(issuer.ticker)
            selected.append(decision.issuer_id)
            evaluator_weights.add(decision.target_weight)
        weights = equal_weights(tickers, definition.sizing.quantization)
        if tickers and evaluator_weights != {weights[sorted(tickers)[0]]}:
            raise ValueError(f"the evaluator weights {evaluator_weights} at {cutoff.isoformat()} are not 1/N")
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


def price_book(unadjusted: DailySeries, split_adjusted: DailySeries) -> PriceBook:
    bars = {
        symbol: {day: Bar(open_, close) for day, (open_, close) in unadjusted[symbol].items()}
        for symbol in sorted(unadjusted)
    }
    events = split_events_by_symbol(sorted(unadjusted), unadjusted, split_adjusted)
    return PriceBook(bars=bars, splits=tuple(event for symbol in sorted(events) for event in events[symbol]))


def _cutoff_symbol_dates(
    decisions: Sequence[CutoffDecision], symbols_fn: Callable[[CutoffDecision], Iterable[str]]
) -> dict[str, tuple[date, ...]]:
    found: dict[str, list[date]] = {}
    for decision in decisions:
        cutoff_day = decision.cutoff_at.astimezone(UTC).date()
        for symbol in sorted(symbols_fn(decision)):
            found.setdefault(symbol, []).append(cutoff_day)
    return {symbol: tuple(days) for symbol, days in sorted(found.items())}


def selected_without_bars(decisions: Sequence[CutoffDecision], unadjusted: DailySeries) -> dict[str, tuple[date, ...]]:
    priced = set(unadjusted)
    return _cutoff_symbol_dates(decisions, lambda d: set(d.weights) - priced)


def selected_without_open(decisions: Sequence[CutoffDecision], strategy: Simulation) -> dict[str, tuple[date, ...]]:
    fills = {fill.cutoff_at: fill for fill in strategy.fills}
    return _cutoff_symbol_dates(
        decisions,
        lambda d: (set(d.weights) & set(fills[d.cutoff_at].unfilled_symbols)) if d.cutoff_at in fills else (),
    )


def price_snapshot_sha256(unadjusted: DailySeries, split_adjusted: DailySeries) -> str:
    rows = [
        [adjust, symbol, day.isoformat(), str(open_), str(close)]
        for adjust, series in ((UNADJUSTED, unadjusted), (SPLIT_ADJUSTED, split_adjusted))
        for symbol, bars in series.items()
        for day, (open_, close) in bars.items()
    ]
    return canonical_sha256(sorted(rows))


def _stale_share_basis(
    symbol: str, cutoff: datetime, shares_known: date, close_known: date, between: Sequence[SplitEvent]
) -> str:
    splits = ", ".join(
        f"{event.numerator}/{event.denominator} split on {event.ex_date.isoformat()}" for event in between
    )
    return (
        f"{symbol} at {cutoff.isoformat()} (share count known {shares_known.isoformat()}, "
        f"close known {close_known.isoformat()}, {splits})"
    )


def require_current_share_basis(
    vintages: Mapping[datetime, Sequence[MarketValueVintage]],
    tickers: Mapping[str, str],
    splits: Sequence[SplitEvent],
) -> None:
    events: dict[str, list[SplitEvent]] = {}
    for event in splits:
        events.setdefault(event.symbol, []).append(event)
    stale: list[str] = []
    for cutoff, rows in sorted(vintages.items()):
        for row in rows:
            symbol = tickers.get(row.issuer_id)
            if symbol is None:
                continue
            shares_known = row.shares_knowable_at.astimezone(UTC).date()
            close_known = row.close_knowable_at.astimezone(UTC).date()
            if split_factor_between(events.get(symbol, ()), after=shares_known, through=close_known) == 1:
                continue
            between = [
                event
                for event in events.get(symbol, ())
                if split_factor_between([event], after=shares_known, through=close_known) != 1
            ]
            stale.append(_stale_share_basis(symbol, cutoff, shares_known, close_known, between))
    if stale:
        raise ShareBasisError(
            "A share count predates a split that its close follows, so the market value mixes two share "
            f"bases ({len(stale)} found): {'; '.join(stale)}. Run strategy_history_projection_pipeline, "
            "then run the backtest again."
        )


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


def _merge_override_dict(base: dict[str, Any], overrides: Mapping[str, Any]) -> None:
    for raw_key, value in overrides.items():
        parts = raw_key.split(".")
        cursor = base
        for part in parts[:-1]:
            child = cursor.get(part)
            if not isinstance(child, dict):
                child = {}
                cursor[part] = child
            cursor = child
        leaf = parts[-1]
        if isinstance(value, Mapping) and isinstance(cursor.get(leaf), dict):
            _merge_override_dict(cursor[leaf], value)
        else:
            cursor[leaf] = value


def apply_definition_overrides(
    definition: LargeModelValueV0Definition,
    overrides: Mapping[str, Any],
) -> LargeModelValueV0Definition:
    if not overrides:
        return definition
    payload: dict[str, Any] = json.loads(
        definition.model_dump_json(exclude={"strategy_definition_id", "content_sha256"})
    )
    _merge_override_dict(payload, overrides)
    payload.pop("strategy_definition_id", None)
    payload.pop("content_sha256", None)
    return LargeModelValueV0Definition.model_validate_json(json.dumps(payload))


def _filter_cutoff_window(
    cutoffs: Sequence[datetime],
    *,
    start_cutoff: datetime | None,
    end_cutoff: datetime | None,
) -> list[datetime]:
    start_utc = None if start_cutoff is None else start_cutoff.astimezone(UTC)
    end_utc = None if end_cutoff is None else end_cutoff.astimezone(UTC)
    return [
        cutoff
        for cutoff in cutoffs
        if (start_utc is None or cutoff >= start_utc) and (end_utc is None or cutoff <= end_utc)
    ]


def run_core_strategy_backtest(
    connection: psycopg.Connection[Any],
    *,
    tick: datetime,
    definition: LargeModelValueV0Definition | None = None,
    overrides: Mapping[str, Any] | None = None,
    start_cutoff: datetime | None = None,
    end_cutoff: datetime | None = None,
) -> BacktestOutcome:
    if tick.tzinfo is None or tick.utcoffset() is None:
        raise ValueError("tick must carry a time zone")
    code_identity = deployed_code_identity(settings.app_env, settings.git_commit_sha)
    definition = definition or load_strategy_definition()
    if overrides:
        definition = apply_definition_overrides(definition, overrides)
    rules = execution_rules(definition)
    cutoffs = _filter_cutoff_window(
        schedule_cutoffs(connection, definition.schedule),
        start_cutoff=start_cutoff,
        end_cutoff=end_cutoff,
    )
    if not cutoffs:
        raise BacktestPending("no quarter-end strategy inputs; run strategy_history_projection_pipeline")

    gateway = StrategyBacktestGateway(connection)
    vintages_by_cutoff = {cutoff: gateway.market_value_vintages_at(cutoff) for cutoff in cutoffs}
    rows_by_cutoff = {cutoff: gateway.rows_at(cutoff) for cutoff in cutoffs}
    issuer_ids = sorted({row[0] for rows in rows_by_cutoff.values() for row in rows})
    canonical = canonical_history_issuers(connection, issuer_ids, known_at=tick)
    if not canonical.issuers:
        raise RuntimeError(f"no stored lineage resolves any of the {len(issuer_ids)} issuers with quarter-end inputs")
    issuers = {issuer.issuer_id: issuer for issuer in canonical.issuers}
    tickers = [issuer.ticker for issuer in canonical.issuers]
    if len(set(tickers)) != len(tickers):
        raise RuntimeError(f"two issuers resolve to one ticker: {sorted(tickers)}")
    canonical_rows = {
        cutoff: [row for row in rows if row[0] in canonical.canonical_ids] for cutoff, rows in rows_by_cutoff.items()
    }
    cutoffs = require_contiguous_quarters(
        [cutoff for cutoff, rows in canonical_rows.items() if rows], definition.schedule
    )
    if not cutoffs:
        raise BacktestPending(
            "no quarter-end inputs under canonical issuer ids; run strategy_history_projection_pipeline"
        )
    inputs_by_cutoff = {cutoff: issuer_inputs_from_rows(canonical_rows[cutoff], cutoff_at=cutoff) for cutoff in cutoffs}
    snapshot_ids = [snapshot_id_from_rows(canonical_rows[cutoff]) for cutoff in cutoffs]
    rates: dict[datetime, RiskFreeRate] = {cutoff: risk_free_rate_at(connection, cutoff) for cutoff in cutoffs}

    decisions = decide(
        cutoffs, inputs_by_cutoff.__getitem__, issuers, definition, rate_at=lambda cutoff: rates[cutoff].value
    )
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
    unpriced = selected_without_bars(decisions, unadjusted)
    if unpriced:
        raise RuntimeError(
            "no daily bars for selected symbols: "
            + "; ".join(f"{symbol} ({', '.join(day.isoformat() for day in days)})" for symbol, days in unpriced.items())
        )
    split_adjusted = load_daily_series(connection, symbols, adjust=SPLIT_ADJUSTED)
    missing_adjusted = sorted(set(unadjusted) - set(split_adjusted))
    if missing_adjusted:
        raise RuntimeError(f"no split-adjusted daily bars, so no split events, for {missing_adjusted}")
    book = price_book(unadjusted, split_adjusted)
    require_current_share_basis(
        {cutoff: vintages_by_cutoff[cutoff] for cutoff in cutoffs},
        {issuer.issuer_id: issuer.ticker for issuer in canonical.issuers},
        book.splits,
    )

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
    first_universe = sorted(
        issuers[item.issuer_id].ticker for item in inputs_by_cutoff[cutoffs[0]] if item.issuer_id in issuers
    )
    if not first_universe:
        raise RuntimeError(f"no resolved issuer holds inputs at the first cutoff {cutoffs[0].isoformat()}")
    benchmark = simulate(
        book,
        [equal_weight_target(first_universe, cutoffs[0], definition.sizing.quantization)],
        benchmark_rules(rules),
        capital=NOTIONAL_CAPITAL,
    )

    binding = {
        "engine_id": ENGINE_ID,
        "code_git_commit_sha": code_identity,
        "strategy_key": definition.strategy_id,
        "definition_content_sha256": definition.content_sha256,
        "cutoffs": [cutoff.isoformat() for cutoff in cutoffs],
        "input_snapshot_id": combine_snapshot_ids(snapshot_ids),
        "benchmark_universe": first_universe,
        "issuers": sorted(
            [issuer.issuer_id, issuer.ticker, issuer.operating_branch.value] for issuer in canonical.issuers
        ),
        "price_snapshot_sha256": price_snapshot_sha256(unadjusted, split_adjusted),
        "risk_free_rates": [
            {
                "cutoff_at": cutoff.isoformat(),
                "series_id": rates[cutoff].series_id,
                "observation_date": rates[cutoff].observation_date.isoformat(),
                "annualized_rate": str(rates[cutoff].value),
                "source": rates[cutoff].source,
            }
            for cutoff in cutoffs
        ],
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
        unresolved=canonical.unresolved,
        ignored_legacy_ids=sorted(canonical.merged),
        benchmark_universe=first_universe,
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
    valuations: tuple[tuple[str, date, Decimal, Decimal, Decimal, Decimal, Decimal], ...]
    trades: tuple[tuple[date, str, str, Decimal, Decimal, Decimal, Decimal, Decimal, Decimal], ...]
    result_sha256: str


def benchmark_never_filled(benchmark: Simulation) -> tuple[str, ...]:
    if not benchmark.fills:
        return ()
    late = {item.symbol for item in benchmark.deferred_fills}
    return tuple(sorted(set(benchmark.fills[0].unfilled_symbols) - late))


def run_limitations(never_filled: Sequence[str], unfilled_selections: Mapping[str, Sequence[date]]) -> tuple[str, ...]:
    lines = list(LIMITATIONS)
    if never_filled:
        lines.append(
            "These benchmark symbols had no open after the first fill, so their weight stayed cash for the whole run: "
            + ", ".join(never_filled)
            + "."
        )
    for symbol, days in sorted(unfilled_selections.items()):
        lines.append(
            f"The strategy selected {symbol} at {', '.join(day.isoformat() for day in days)}, but no open existed "
            "on the fill session, so the weight it could not buy stayed cash."
        )
    return tuple(lines)


def _splits_applied(simulation: Simulation) -> list[dict[str, str]]:
    return [
        {
            "symbol": item.symbol,
            "ex_date": item.ex_date.isoformat(),
            "factor": f"{item.numerator}/{item.denominator}",
            "shares_before": str(item.shares_before),
            "shares_after": str(item.shares_after),
        }
        for item in simulation.splits_applied
    ]


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
    ignored_legacy_ids: Sequence[str],
    benchmark_universe: Sequence[str],
    cost_basis_points: Decimal,
) -> BacktestRecord:
    never_filled = benchmark_never_filled(benchmark)
    unfilled_selections = selected_without_open(decisions, strategy)
    benchmark_by_session = {item.session: item for item in benchmark.valuations}
    valuations = _valuation_rows(
        month_end_valuations(strategy.valuations), benchmark_by_session, "1M"
    ) + _valuation_rows(strategy.valuations, benchmark_by_session, "1D")
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
    result_sha256 = canonical_sha256(
        {
            "valuations": [[str(cell) for cell in row] for row in valuations],
            "trades": [[str(cell) for cell in row] for row in trades],
            "strategy": _decimal_map(strategy_performance),
            "benchmark": _decimal_map(benchmark_performance),
            "turnover_monthly": str(turnover_monthly),
        }
    )
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
        "benchmark": {
            "rule": BENCHMARK_RULE,
            "universe": list(benchmark_universe),
            "unfilled_order_behavior": "next_session_open",
            "deferred_fills": [
                {
                    "symbol": item.symbol,
                    "ordered_session": item.ordered_session.isoformat(),
                    "filled_session": item.filled_session.isoformat(),
                }
                for item in benchmark.deferred_fills
            ],
            "never_filled": list(never_filled),
            "splits_applied": _splits_applied(benchmark),
            **_decimal_map(benchmark_performance),
        },
        "turnover_monthly": str(turnover_monthly),
        "sharpe_risk_free": SHARPE_RISK_FREE.payload(),
        "cutoffs": [_cutoff_entry(decision, fills.get(decision.cutoff_at)) for decision in decisions],
        "split_events": [
            {"symbol": event.symbol, "ex_date": event.ex_date.isoformat(), "factor": str(event.factor)}
            for event in book.splits
        ],
        "splits_applied": _splits_applied(strategy),
        "unresolved_issuers": list(unresolved),
        "ignored_legacy_issuer_ids": list(ignored_legacy_ids),
        "limitations": list(run_limitations(never_filled, unfilled_selections)),
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
    existing = connection.execute(
        "select metrics_payload ->> 'result_sha256' from mart.backtest_runs where run_id = %s", (record.run_id,)
    ).fetchone()
    if existing is not None:
        if existing[0] != record.result_sha256:
            raise RuntimeError(
                f"{record.run_id} exists with result {existing[0]}, this run computed {record.result_sha256}: "
                "the same code, definition and data gave a different result, so the run is not deterministic"
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


def sweep_core_strategy_backtest(
    connection: psycopg.Connection[Any],
    *,
    tick: datetime,
    overrides_list: Sequence[Mapping[str, Any]],
    definition: LargeModelValueV0Definition | None = None,
    start_cutoff: datetime | None = None,
    end_cutoff: datetime | None = None,
) -> list[BacktestOutcome]:
    base_def = definition or load_strategy_definition()
    return [
        run_core_strategy_backtest(
            connection,
            tick=tick,
            definition=base_def,
            overrides=overrides,
            start_cutoff=start_cutoff,
            end_cutoff=end_cutoff,
        )
        for overrides in overrides_list
    ]


__all__ = [
    "LIMITATIONS",
    "NOTIONAL_CAPITAL",
    "BacktestOutcome",
    "BacktestPending",
    "BacktestRecord",
    "CutoffDecision",
    "DEPLOYED_ENVIRONMENTS",
    "apply_definition_overrides",
    "benchmark_never_filled",
    "decide",
    "deployed_code_identity",
    "persist_backtest_run",
    "price_book",
    "price_snapshot_sha256",
    "run_core_strategy_backtest",
    "run_limitations",
    "schedule_cutoffs",
    "selected_without_bars",
    "selected_without_open",
    "sweep_core_strategy_backtest",
]
