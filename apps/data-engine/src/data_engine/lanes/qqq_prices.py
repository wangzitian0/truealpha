"""Daily price series refresh for QQQ listings outside TOPT symbols (#1277)."""

from collections import Counter
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock_time
from typing import Any

import dagster as dg
import psycopg
from pydantic import Field
from truealpha_contracts.environment_contract import UTILIZATION_CEILING
from truealpha_contracts.universes import lane_name

from data_engine.config import settings
from data_engine.datahub.market_prices import (
    MAX_BAR_LAG_SESSIONS,
    SPLIT_ADJUSTED,
    TWELVE_DATA_SEAT,
    UNADJUSTED,
    TwelveDataApiError,
    TwelveDataClient,
    TwelveDataRateLimitError,
    ingest_daily_series,
    stored_symbols,
)
from data_engine.datahub.price_series import load_daily_series
from data_engine.datahub.primary_listing import primary_symbols
from data_engine.datahub.production_topt.source_registrations import origin_per_key_cost
from data_engine.datahub.production_topt.universe_plane import resolve_universe_corpus
from data_engine.datahub.share_basis import SplitHistory, split_history
from data_engine.external_op import external_op
from data_engine.lanes.capture import is_universe_scheduled
from data_engine.sources import gateway

QQQ_PRICE_REFRESH_JOB_NAME = "qqq_price_refresh_pipeline"
QQQ_PRICE_REFRESH_CRON = "35 21 * * 1-5"
QQQ_PRICE_CALLER = "qqq_prices.fetch_time_series"
CREDITS_PER_SYMBOL = origin_per_key_cost("twelve-data:v1")
REFRESH_EVERY_SESSIONS = MAX_BAR_LAG_SESSIONS + 1
RESERVE_DAYS = 7


def target_symbols(
    connection: psycopg.Connection[Any],
    denominator: Mapping[str, Any],
    *,
    covered: Collection[str],
    known_at: datetime,
) -> tuple[str, ...]:
    return tuple(
        symbol for symbol in primary_symbols(connection, denominator, known_at=known_at) if symbol not in covered
    )


def refresh_order(symbols: Sequence[str], newest: Mapping[str, date]) -> list[str]:
    never = sorted(symbol for symbol in symbols if symbol not in newest)
    stored = sorted((symbol for symbol in symbols if symbol in newest), key=lambda symbol: (newest[symbol], symbol))
    return never + stored


@dataclass(frozen=True)
class CreditBudget:
    share: int
    ceiling: int
    reserve: int
    spent_today: int
    need: int
    credits: int

    @property
    def symbols(self) -> int:
        return self.credits // CREDITS_PER_SYMBOL


def need_credits(targets: int) -> int:
    return CREDITS_PER_SYMBOL * -(-targets // REFRESH_EVERY_SESSIONS)


def credit_budget(*, share: int | None, reserve: int, spent_today: int, targets: int) -> CreditBudget:
    granted = share or 0
    ceiling = granted * int(round(UTILIZATION_CEILING * 100)) // 100
    need = need_credits(targets)
    credits = max(0, min(need, ceiling - reserve - spent_today))
    return CreditBudget(
        share=granted, ceiling=ceiling, reserve=reserve, spent_today=spent_today, need=need, credits=credits
    )


@dataclass(frozen=True)
class LedgerSpend:
    reserve: int
    spent_today: int


def ledger_spend(today: date) -> LedgerSpend:
    since = datetime.combine(today - timedelta(days=RESERVE_DAYS), clock_time.min, tzinfo=UTC)
    until = datetime.combine(today + timedelta(days=1), clock_time.min, tzinfo=UTC)
    others: Counter[date] = Counter()
    spent_today = 0
    for (caller, day), calls in gateway.ledger_daily_calls(TWELVE_DATA_SEAT, since, until).items():
        if caller == QQQ_PRICE_CALLER:
            spent_today += calls if day == today else 0
        elif day < today:
            others[day] += calls
    return LedgerSpend(reserve=max(others.values(), default=0), spent_today=spent_today)


def environment_share(environment: str) -> int | None:
    capacity = gateway.CAPACITIES[TWELVE_DATA_SEAT].in_environment(environment)
    return None if capacity is None else capacity.daily_budget


def newest_sessions(connection: psycopg.Connection, symbols: Sequence[str]) -> dict[str, date]:
    unadjusted = load_daily_series(connection, symbols, adjust=UNADJUSTED)
    split_adjusted = load_daily_series(connection, symbols, adjust=SPLIT_ADJUSTED)
    histories = {symbol: split_history(symbol, unadjusted, split_adjusted) for symbol in symbols}
    return {symbol: history.last_session for symbol, history in histories.items() if isinstance(history, SplitHistory)}


@dataclass
class RefreshOutcome:
    environment: str
    targets: int
    budget: CreditBudget
    planned: tuple[str, ...]
    dry_run: bool = False
    stored: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    stopped_by: str | None = None
    calls: int = 0
    skipped_reason: str | None = None

    def summary(self) -> str:
        b = self.budget
        return (
            f"qqq prices in {self.environment}: {self.targets} target symbols, need {b.need} credits, "
            f"ceiling {b.ceiling} of share {b.share}, reserve {b.reserve}, spent today {b.spent_today}, "
            f"budget {b.credits}; planned {len(self.planned)} {list(self.planned)}; "
            f"stored {len(self.stored)}, failed {len(self.failed)}, calls {self.calls}"
            + (" (dry run, no call)" if self.dry_run else "")
            + (f"; stopped by {self.stopped_by}" if self.stopped_by else "")
            + (f"; {self.skipped_reason}" if self.skipped_reason else "")
        )

    def metadata(self) -> dict[str, Any]:
        return {
            "environment": self.environment,
            "target_symbols": self.targets,
            "need_credits": self.budget.need,
            "share_credits": self.budget.share,
            "ceiling_credits": self.budget.ceiling,
            "reserve_credits": self.budget.reserve,
            "spent_today_credits": self.budget.spent_today,
            "budget_credits": self.budget.credits,
            "planned": ", ".join(self.planned),
            "stored": ", ".join(self.stored),
            "failed": "; ".join(f"{symbol}: {error}" for symbol, error in self.failed.items()),
            "stopped_by": self.stopped_by or "",
            "calls": self.calls,
            "dry_run": self.dry_run,
            "skipped_reason": self.skipped_reason or "",
        }


def _fetch(
    outcome: RefreshOutcome,
    *,
    connection: psycopg.Connection,
    client: TwelveDataClient,
    now: datetime,
    commit: Callable[[], None],
) -> None:
    for symbol in outcome.planned:
        if client.call_count + CREDITS_PER_SYMBOL > outcome.budget.credits:
            outcome.stopped_by = "budget"
            return
        try:
            ingest_daily_series(symbol, client=client, connection=connection, now=now)
        except TwelveDataApiError as error:
            outcome.failed[symbol] = str(error)
            continue
        except (TwelveDataRateLimitError, gateway.CapacityExceeded) as error:
            outcome.failed[symbol] = str(error)
            outcome.stopped_by = type(error).__name__
            return
        finally:
            outcome.calls = client.call_count
        commit()
        outcome.stored.append(symbol)


def _refresh_qqq_prices(
    context: dg.OpExecutionContext,
    connection: psycopg.Connection,
    *,
    now: datetime,
    environment: str,
    client: TwelveDataClient,
    commit: Callable[[], None],
    dry_run: bool = False,
    max_symbols: int | None = None,
) -> RefreshOutcome:
    corpus = resolve_universe_corpus(connection, lane_name("qqq"))
    covered = stored_symbols(connection, known_at=now)
    symbols = target_symbols(connection, corpus["topt_denominator"], covered=covered, known_at=now)
    spend = ledger_spend(now.astimezone(UTC).date())
    budget = credit_budget(
        share=environment_share(environment),
        reserve=spend.reserve,
        spent_today=spend.spent_today,
        targets=len(symbols),
    )
    limit = budget.symbols if max_symbols is None else min(budget.symbols, max_symbols)
    planned = tuple(refresh_order(symbols, newest_sessions(connection, symbols))[:limit])
    outcome = RefreshOutcome(
        environment=environment, targets=len(symbols), budget=budget, planned=planned, dry_run=dry_run
    )
    if not dry_run:
        if planned:
            _fetch(outcome, connection=connection, client=client, now=now, commit=commit)
        else:
            outcome.skipped_reason = (
                f"budget {budget.credits} credits is less than the {CREDITS_PER_SYMBOL} credits of one symbol; "
                "nothing fetched"
            )
    context.log.info(outcome.summary())
    return outcome


class QqqPriceRefreshConfig(dg.Config):
    dry_run: bool = False
    max_symbols: int | None = Field(default=None, ge=1)


@external_op
def refresh_qqq_prices_op(context: dg.OpExecutionContext, config: QqqPriceRefreshConfig) -> None:
    with gateway.capacity_scope(), psycopg.connect(settings.database_url) as connection:
        outcome = _refresh_qqq_prices(
            context,
            connection,
            now=datetime.now(UTC),
            environment=gateway.capacity_environment(),
            client=TwelveDataClient(caller=QQQ_PRICE_CALLER),
            commit=connection.commit,
            dry_run=config.dry_run,
            max_symbols=config.max_symbols,
        )
    if outcome.failed:
        raise dg.Failure(
            description=(
                f"qqq prices: {len(outcome.failed)} symbols failed and keep their place in the next run: "
                + "; ".join(f"{symbol}: {error}" for symbol, error in outcome.failed.items())
            ),
            metadata=outcome.metadata(),
        )
    context.add_output_metadata(outcome.metadata())


@dg.job(name=QQQ_PRICE_REFRESH_JOB_NAME)
def qqq_price_refresh_pipeline_job() -> None:
    refresh_qqq_prices_op()


@dg.schedule(
    job=qqq_price_refresh_pipeline_job,
    cron_schedule=QQQ_PRICE_REFRESH_CRON,
    execution_timezone="UTC",
    default_status=(
        dg.DefaultScheduleStatus.RUNNING
        if is_universe_scheduled("qqq", settings.app_env)
        else dg.DefaultScheduleStatus.STOPPED
    ),
)
def qqq_price_refresh_schedule(context: dg.ScheduleEvaluationContext) -> dg.RunRequest:
    return dg.RunRequest(run_key=context.scheduled_execution_time.isoformat())


defs = dg.Definitions(jobs=[qqq_price_refresh_pipeline_job], schedules=[qqq_price_refresh_schedule])
