"""The two daily price series of the QQQ listings outside the TOPT symbols (#1277).

The core engine values an issuer through the stored daily series of its listings
(`production_topt.materialization._snapshot_member`, `share_basis.live_on_close_basis`).
`lanes.market_data` stores the series for the TOPT symbols only. Since #1309 a QQQ issuer without
them is unavailable. This lane stores both series (`adjust = 'splits'` and `adjust = 'none'`) for
the other QQQ listings.

Symbols. The listings of the governed QQQ head (`capture.QQQ_UNIVERSE_HEAD`), which the QQQ tick
captures. The lane keeps one listing per issuer: the primary class (`canonical_issuer.ticker_sort_key`).
It skips an issuer whose primary listing is a TOPT symbol, because the market-data lane stores it.
So Alphabet adds no symbol (GOOGL is a TOPT symbol), and the lane never stores GOOG.

Cadence. The core accepts series that end at most `MAX_BAR_LAG_SESSIONS` sessions before the close
it values. A run after the close of session D stores D. That run serves the ticks that value D and
the next `MAX_BAR_LAG_SESSIONS` sessions. So each weekday run refreshes the stalest
ceil(N / `REFRESH_EVERY_SESSIONS`) symbols, and symbols without both series come first.

Budget. One credit per request and one request per series. The run spends at most
`floor(0.85 * share) - reserve - spent today by this job` (`credit_budget`). The reserve is the
largest daily spend of the other Twelve Data callers over the last seven complete UTC days in the
ledger. init.md section 5 records the measured numbers.

Schedule. Weekdays 21:35 UTC: after the market-data refresh (21:15 UTC, about 15 minutes in
production) and before the TOPT tick (22:15 UTC) and the QQQ tick (23:20 UTC).

The run commits each symbol alone. A symbol that fails writes nothing and keeps its place, so the
next run fetches it first. A vendor error, a rate limit or a gate refusal makes the run red after it
commits what it stored.

Manual replay: launch `qqq_price_refresh_pipeline` from the Dagster UI launchpad or through GraphQL.
`dry_run` logs the plan and the budget and calls nothing. `max_symbols` caps the symbols of the run.

    ops:
      refresh_qqq_prices_op:
        config:
          dry_run: true
          max_symbols: 5
"""

from collections import Counter
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock_time
from typing import Any

import dagster as dg
import psycopg
from pydantic import Field

from data_engine.config import settings
from data_engine.datahub.canonical_issuer import ticker_sort_key
from data_engine.datahub.market_prices import (
    DAILY_SERIES,
    DEFAULT_TOPT_SYMBOLS,
    MAX_BAR_LAG_SESSIONS,
    SPLIT_ADJUSTED,
    TWELVE_DATA_SEAT,
    UNADJUSTED,
    TwelveDataApiError,
    TwelveDataClient,
    TwelveDataRateLimitError,
    ingest_daily_series,
)
from data_engine.datahub.price_series import load_daily_series
from data_engine.datahub.production_topt.universe_plane import resolve_universe_corpus
from data_engine.datahub.share_basis import SplitHistory, split_history
from data_engine.lanes.backtest import schedule_status
from data_engine.lanes.capture import QQQ_UNIVERSE_HEAD
from data_engine.sources import gateway

QQQ_PRICE_REFRESH_JOB_NAME = "qqq_price_refresh_pipeline"
#: Weekdays 21:35 UTC. The market-data refresh starts at 21:15 UTC and makes 60 calls at 4 a minute.
QQQ_PRICE_REFRESH_CRON = "35 21 * * 1-5"
#: The ledger `caller` of this lane's requests. The budget tells this lane's calls from the others by it.
QQQ_PRICE_CALLER = "qqq_prices.fetch_time_series"
#: One credit per request, one request per daily series.
CREDITS_PER_SYMBOL = len(DAILY_SERIES)
#: A run after the close of session D serves the ticks of D and of the next `MAX_BAR_LAG_SESSIONS` sessions.
REFRESH_EVERY_SESSIONS = MAX_BAR_LAG_SESSIONS + 1
#: The percentage of the environment's daily share that all callers together may plan to spend.
HEADROOM_PERCENT = 85
#: The complete UTC days before today whose busiest day sets the reserve of the other callers.
RESERVE_DAYS = 7


def target_symbols(denominator: Mapping[str, Any], *, covered: Collection[str]) -> tuple[str, ...]:
    """The primary listing of each issuer in the universe, less the symbols in `covered`, by symbol.

    `ticker_sort_key` ranks the listings of an issuer; the listing id breaks a tie. A listing
    without a ticker names no price series.
    """
    fields = list(denominator["instrument_tuple_fields"])
    issuer_at, listing_at, ticker_at = (fields.index(name) for name in ("issuer_id", "listing_id", "ticker"))
    listings: dict[str, list[tuple[str | None, str]]] = {}
    for row in denominator["instruments"]:
        listings.setdefault(str(row[issuer_at]), []).append((row[ticker_at] or None, str(row[listing_at])))
    primaries = (min(rows, key=lambda item: (ticker_sort_key(item[0]), item[1]))[0] for rows in listings.values())
    return tuple(sorted({ticker for ticker in primaries if ticker and ticker not in covered}))


def refresh_order(symbols: Sequence[str], newest: Mapping[str, date]) -> list[str]:
    """The order a run fetches `symbols` in.

    First the symbols without both stored series, by symbol. Then the others, the oldest newest
    session first, then by symbol. A symbol that failed keeps its stored state, so it keeps its place.
    """
    never = sorted(symbol for symbol in symbols if symbol not in newest)
    stored = sorted((symbol for symbol in symbols if symbol in newest), key=lambda symbol: (newest[symbol], symbol))
    return never + stored


@dataclass(frozen=True)
class CreditBudget:
    """The credits of one run and the terms that set them."""

    #: The environment's daily share of the Twelve Data seat. 0 when the environment has no share.
    share: int
    #: floor(HEADROOM_PERCENT % of the share): what all callers together may plan to spend in a day.
    ceiling: int
    #: The largest daily spend of the other callers over the last `RESERVE_DAYS` complete UTC days.
    reserve: int
    #: The calls this lane made today (UTC).
    spent_today: int
    #: What the run needs: `CREDITS_PER_SYMBOL` * ceil(targets / `REFRESH_EVERY_SESSIONS`).
    need: int
    #: What the run may spend: max(0, min(need, ceiling - reserve - spent_today)).
    credits: int

    @property
    def symbols(self) -> int:
        """How many symbols the credits pay for."""
        return self.credits // CREDITS_PER_SYMBOL


def need_credits(targets: int) -> int:
    """The credits one run needs to keep `targets` symbols within the lag: the stalest ceil(targets / cadence)."""
    return CREDITS_PER_SYMBOL * -(-targets // REFRESH_EVERY_SESSIONS)


def credit_budget(*, share: int | None, reserve: int, spent_today: int, targets: int) -> CreditBudget:
    """The credits one run may spend: its need, capped by what the other callers leave below the headroom."""
    granted = share or 0
    ceiling = granted * HEADROOM_PERCENT // 100
    need = need_credits(targets)
    credits = max(0, min(need, ceiling - reserve - spent_today))
    return CreditBudget(
        share=granted, ceiling=ceiling, reserve=reserve, spent_today=spent_today, need=need, credits=credits
    )


@dataclass(frozen=True)
class LedgerSpend:
    """The two ledger terms of `credit_budget`."""

    reserve: int
    spent_today: int


def ledger_spend(today: date) -> LedgerSpend:
    """The other callers' busiest complete day of the last `RESERVE_DAYS`, and this lane's calls on `today`.

    A day without a ledger row counts as no spend. An empty ledger gives a reserve of 0.
    """
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
    """The daily share of the Twelve Data seat that the gate grants `environment`, or None."""
    capacity = gateway.CAPACITIES[TWELVE_DATA_SEAT].in_environment(environment)
    return None if capacity is None else capacity.daily_budget


def newest_sessions(connection: psycopg.Connection, symbols: Sequence[str]) -> dict[str, date]:
    """The last session of each symbol that the core can use, read the way the core reads it.

    `share_basis.split_history` over the two stored series gives the last session with a close in both.
    A symbol without both series, or whose series show no split history, is absent: the run fetches it first.
    """
    unadjusted = load_daily_series(connection, symbols, adjust=UNADJUSTED)
    split_adjusted = load_daily_series(connection, symbols, adjust=SPLIT_ADJUSTED)
    histories = {symbol: split_history(symbol, unadjusted, split_adjusted) for symbol in symbols}
    return {symbol: history.last_session for symbol, history in histories.items() if isinstance(history, SplitHistory)}


@dataclass
class RefreshOutcome:
    """What one run planned, stored and failed, and why it stopped."""

    environment: str
    targets: int
    budget: CreditBudget
    planned: tuple[str, ...]
    dry_run: bool = False
    stored: list[str] = field(default_factory=list)
    #: Symbol -> the error. Each failed symbol keeps its place in the next run.
    failed: dict[str, str] = field(default_factory=dict)
    #: Why the run stopped before its plan ended: `budget`, or the error class that stopped it.
    stopped_by: str | None = None
    #: The requests the run made, retries included.
    calls: int = 0
    #: Why the run fetched nothing.
    skipped_reason: str | None = None

    def summary(self) -> str:
        budget = self.budget
        return (
            f"qqq prices in {self.environment}: {self.targets} target symbols, need {budget.need} credits, "
            f"ceiling {budget.ceiling} of share {budget.share}, reserve {budget.reserve}, spent today "
            f"{budget.spent_today}, budget {budget.credits}; planned {len(self.planned)} {list(self.planned)}; "
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
    """Fetch the planned symbols in order and commit each one alone.

    The run stops before a symbol whose two requests would pass the budget, retries included. A
    vendor error fails one symbol. A rate limit or a gate refusal fails the symbol and stops the run.
    """
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
    """The op body, apart from the Dagster plumbing, so a test can drive it with a fake vendor and its own connection."""
    corpus = resolve_universe_corpus(connection, QQQ_UNIVERSE_HEAD)
    symbols = target_symbols(corpus["topt_denominator"], covered=DEFAULT_TOPT_SYMBOLS)
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
    """A schedule uses the defaults. An operator sets `dry_run` to see the plan, or `max_symbols` to cap a run."""

    dry_run: bool = False
    max_symbols: int | None = Field(default=None, ge=1)


@dg.op
def refresh_qqq_prices_op(context: dg.OpExecutionContext, config: QqqPriceRefreshConfig) -> None:
    """Store both daily series of the stalest QQQ symbols within this environment's Twelve Data budget.

    Every request is attributed to this run in the ledger and admitted by the rule-6 gate first.
    """
    with (
        gateway.run_scope(f"dagster:{context.run_id}"),
        gateway.capacity_scope(),
        psycopg.connect(settings.database_url) as connection,
    ):
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
        # The stored symbols are committed. The failure carries the same metadata as a green run.
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
    # Production and staging, like the market-data refresh. The budget gives staging 0 credits
    # while its measured spend fills its share; the schedule needs no environment case.
    default_status=schedule_status(settings.app_env),
)
def qqq_price_refresh_schedule(context: dg.ScheduleEvaluationContext) -> dg.RunRequest:
    # The run key dedupes a re-evaluated tick. A schedule never plans a dry run.
    return dg.RunRequest(run_key=context.scheduled_execution_time.isoformat())


defs = dg.Definitions(jobs=[qqq_price_refresh_pipeline_job], schedules=[qqq_price_refresh_schedule])
