"""The moomoo quota gate and burst throttle (init.md Section 1 rule 6: every moomoo call
must go through the gate, no module decides for itself).

Per owner directive on #1411 (K2):
- There is no monthly call budget (`moomoo_monthly_call_budget` was a self-imposed fiction
  and has been deleted).
- Instead, moomoo's real quota shape is enforced:
  1. 30-day rolling distinct-stock K-line quota (`MOOMOO_KLINE_30D_STOCK_QUOTA = 2000`):
     `request_history_kline` / `get_history_kline` tracks distinct stocks (`request_uri`
     codes) over the past 30 days and raises `BudgetExceededError` if a new stock would
     exceed the 2,000-stock quota.
  2. Burst throttle (`throttle()`): at most `MOOMOO_CALLS_PER_30S` calls per 30 s, and never past the burst
     limit of the moomoo account in the environment contract (`LEDGER_CAPACITIES["moomoo"]`).
  3. Daily call anomaly alert (`is_daily_call_anomaly()`): warns when daily calls exceed
     `2 * MOOMOO_PLANNED_DAILY_P99`.

This module only reads `staging.api_call_ledger`. The one writer is `gateway.record_call`
(#1521): `sources.moomoo._call` records every OpenD call through it, so a moomoo row has the
same shape, the same failure handling and the same table as every other source's row.
"""

import logging
import time
from collections import deque

from data_engine.config import settings
from data_engine.sources import gateway
from data_engine.sources.gateway import _pg_execute  # one autocommit ledger connection, shared with the gateway

logger = logging.getLogger(__name__)

#: moomoo OpenAPI historical-candlestick quota: 2,000 distinct stocks per rolling 30-day window.
MOOMOO_KLINE_30D_STOCK_QUOTA = 2000
#: Endpoints that consume the 30-day distinct-stock candlestick quota.
KLINE_ENDPOINTS = frozenset({"request_history_kline", "get_history_kline"})
#: Planned daily p99 moomoo calls across scheduled lanes; exceeding 2x triggers an anomaly warning (#1411 K2).
MOOMOO_PLANNED_DAILY_P99 = 600


class BudgetExceededError(Exception):
    pass


# --- reads of the ledger the one writer fills ---

_MONTH_START_UTC = "date_trunc('month', now() at time zone 'utc') at time zone 'utc'"
_DAY_START_UTC = "date_trunc('day', now() at time zone 'utc') at time zone 'utc'"


def calls_this_month() -> int:
    """Return total moomoo calls recorded since the start of this UTC month."""
    row = _pg_execute(
        f"select count(*) from staging.api_call_ledger where source = 'moomoo' and called_at >= {_MONTH_START_UTC}"
    ).fetchone()
    return row[0]


def calls_today() -> int:
    """Return total moomoo calls recorded since 00:00 UTC today."""
    row = _pg_execute(
        f"select count(*) from staging.api_call_ledger where source = 'moomoo' and called_at >= {_DAY_START_UTC}"
    ).fetchone()
    return row[0]


def is_daily_call_anomaly(*, planned_p99: int = MOOMOO_PLANNED_DAILY_P99) -> bool:
    """Return True when today's moomoo call count exceeds 2x ``planned_p99`` (#1411 K2)."""
    return calls_today() > 2 * planned_p99


def normalize_stock_uri(code_or_uri: str | None) -> str | None:
    if not code_or_uri:
        return None
    text = code_or_uri.strip()
    if not text:
        return None
    return text if text.startswith("moomoo://") else f"moomoo://{text}"


def kline_distinct_stocks_30d() -> set[str]:
    """Return the set of distinct ``moomoo://<code>`` URIs requested on K-line endpoints
    within the rolling 30-day window (#1411 K2)."""
    rows = _pg_execute(
        """
        select distinct request_uri
        from staging.api_call_ledger
        where source = 'moomoo'
          and endpoint in ('request_history_kline', 'get_history_kline')
          and request_uri is not null
          and called_at >= now() - interval '30 days'
        """
    ).fetchall()
    return {uri for (raw,) in rows if (uri := normalize_stock_uri(raw)) is not None}


def gate(endpoint: str, caller: str, *, request_uri: str | None = None) -> None:
    """Enforce the 30-day rolling 2,000-stock K-line quota and log a daily anomaly warning (#1411 K2)."""
    if endpoint in KLINE_ENDPOINTS:
        stocks = kline_distinct_stocks_30d()
        target_uri = normalize_stock_uri(request_uri)
        if (target_uri is None or target_uri not in stocks) and len(stocks) >= MOOMOO_KLINE_30D_STOCK_QUOTA:
            raise BudgetExceededError(
                f"moomoo 30d K-line distinct-stock quota exhausted: {len(stocks)}/{MOOMOO_KLINE_30D_STOCK_QUOTA} "
                f"stocks used in the last 30 days (blocked call: {caller} -> {endpoint} {target_uri or ''})"
            )
    if is_daily_call_anomaly():
        logger.warning(
            "moomoo daily call anomaly: %d calls today exceeds 2x planned p99 (%d)",
            calls_today(),
            MOOMOO_PLANNED_DAILY_P99,
        )


# --- burst throttle ---

_recent_calls: deque[float] = deque()


def throttle(*, now=time.monotonic, sleep=time.sleep) -> None:
    """Block until one more call fits the moomoo seat's window.

    The window and its ceiling are the source registry's (`LEDGER_CAPACITIES["moomoo"]`: the burst limit of
    the moomoo account, rule 6); MOOMOO_CALLS_PER_30S may only LOWER the ceiling, never raise
    it past the declaration. Step 2 of #1520 replaces this pace with admission by the share of the environment.

    Process-local on purpose: sweeps are single-process, and moomoo's burst
    limits are per OpenD connection anyway — a cross-process ledger-based
    throttle would add DB churn without adding protection. Kept conservative
    and global-across-endpoints (moomoo's own limits are per endpoint group,
    so this under-uses the real allowance rather than risking it)."""
    seat = gateway.CAPACITIES["moomoo"]
    assert seat.window_seconds is not None and seat.calls_per_window is not None, "the moomoo seat is paced"
    window = seat.window_seconds
    cap = min(settings.moomoo_calls_per_30s, seat.calls_per_window)
    while True:
        t = now()
        while _recent_calls and t - _recent_calls[0] >= window:
            _recent_calls.popleft()
        if len(_recent_calls) < cap:
            _recent_calls.append(t)
            return
        sleep(window - (t - _recent_calls[0]) + 0.05)
