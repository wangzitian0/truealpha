"""The moomoo call-budget and quota gate (init.md Section 1 rule 6: every moomoo call
must go through the gate, no module decides for itself).

Per owner directive on #1411 (K2):
- There is no monthly call budget (`moomoo_monthly_call_budget` was a self-imposed fiction
  and has been deleted).
- Instead, moomoo's real quota shape is enforced:
  1. 30-day rolling distinct-stock K-line quota (`MOOMOO_KLINE_30D_STOCK_QUOTA = 2000`):
     `request_history_kline` / `get_history_kline` tracks distinct stocks (`request_uri`
     codes) over the past 30 days and raises `BudgetExceededError` if a new stock would
     exceed the 2,000-stock quota.
  2. Burst throttle (`throttle()`): 8 calls per 30 s (`LEDGER_CAPACITIES["moomoo"]`).
  3. Daily call anomaly alert (`is_daily_call_anomaly()`): warns when daily calls exceed
     `2 * MOOMOO_PLANNED_DAILY_P99`.

Two interchangeable backends, selected by MOOMOO_LEDGER_BACKEND:

- 'json' (default): apps/data-engine/data/moomoo_ledger.json, the Phase -1
  stand-in — fine for manual audit sessions, needs no database.
- 'postgres': staging.api_call_ledger (db/migrations/0003) — what ingestion
  sweeps must use.
"""

import json
import logging
import os
import tempfile
import time
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path

from data_engine.config import settings
from data_engine.sources import gateway
from data_engine.sources.gateway import _pg_execute  # one autocommit ledger connection, shared with the gateway

logger = logging.getLogger(__name__)

LEDGER_PATH = Path(__file__).resolve().parents[3] / "data" / "moomoo_ledger.json"

#: moomoo OpenAPI historical-candlestick quota: 2,000 distinct stocks per rolling 30-day window.
MOOMOO_KLINE_30D_STOCK_QUOTA = 2000
#: Endpoints that consume the 30-day distinct-stock candlestick quota.
KLINE_ENDPOINTS = frozenset({"request_history_kline", "get_history_kline"})
#: Planned daily p99 moomoo calls across scheduled lanes; exceeding 2x triggers an anomaly warning (#1411 K2).
MOOMOO_PLANNED_DAILY_P99 = 600


class BudgetExceededError(Exception):
    pass


# --- json backend (Phase -1 behavior, unchanged) ---


def _month_key(dt: datetime) -> str:
    return dt.strftime("%Y-%m")


def _load() -> dict:
    if not LEDGER_PATH.exists():
        return {"calls": []}
    return json.loads(LEDGER_PATH.read_text())


def _save(state: dict) -> None:
    """Atomic write (temp file + os.replace): a crash or interrupt mid-write must
    never leave a corrupted ledger — a broken ledger file must not be able to
    silently disable the gate."""
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=LEDGER_PATH.parent, prefix=".moomoo_ledger.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=1)
        os.replace(tmp_path, LEDGER_PATH)
    except BaseException:
        os.unlink(tmp_path)
        raise


# --- postgres backend ---

_MONTH_START_UTC = "date_trunc('month', now() at time zone 'utc') at time zone 'utc'"
_DAY_START_UTC = "date_trunc('day', now() at time zone 'utc') at time zone 'utc'"


def _json_calls_this_month() -> int:
    state = _load()
    key = _month_key(datetime.now(UTC))
    return sum(1 for c in state["calls"] if c["month"] == key)


def calls_this_month() -> int:
    """In postgres mode this is the SUM of both stores: probe/capture sessions
    default to the json backend, and a sweep's audit must still see what they
    spent this month."""
    if settings.moomoo_ledger_backend == "postgres":
        row = _pg_execute(
            f"select count(*) from staging.api_call_ledger where source = 'moomoo' and called_at >= {_MONTH_START_UTC}"
        ).fetchone()
        return row[0] + _json_calls_this_month()
    return _json_calls_this_month()


def calls_today(*, now: datetime | None = None) -> int:
    """Return total moomoo calls recorded since 00:00 UTC today."""
    current = now or datetime.now(UTC)
    day_prefix = current.strftime("%Y-%m-%d")
    json_today = sum(1 for c in _load()["calls"] if str(c.get("called_at", "")).startswith(day_prefix))
    if settings.moomoo_ledger_backend == "postgres":
        row = _pg_execute(
            f"select count(*) from staging.api_call_ledger where source = 'moomoo' and called_at >= {_DAY_START_UTC}"
        ).fetchone()
        return row[0] + json_today
    return json_today


def is_daily_call_anomaly(*, planned_p99: int = MOOMOO_PLANNED_DAILY_P99, now: datetime | None = None) -> bool:
    """Return True when today's moomoo call count exceeds 2x ``planned_p99`` (#1411 K2)."""
    return calls_today(now=now) > 2 * planned_p99


def _normalize_stock_uri(code_or_uri: str | None) -> str | None:
    if not code_or_uri:
        return None
    text = code_or_uri.strip()
    if not text:
        return None
    return text if text.startswith("moomoo://") else f"moomoo://{text}"


def kline_distinct_stocks_30d(*, now: datetime | None = None) -> set[str]:
    """Return the set of distinct ``moomoo://<code>`` URIs requested on K-line endpoints
    within the rolling 30-day window (#1411 K2)."""
    current = now or datetime.now(UTC)
    cutoff = current - timedelta(days=30)
    stocks: set[str] = set()
    for call in _load()["calls"]:
        if call.get("endpoint") not in KLINE_ENDPOINTS:
            continue
        uri = _normalize_stock_uri(call.get("request_uri"))
        if uri is None:
            continue
        called_at_raw = call.get("called_at")
        if isinstance(called_at_raw, str):
            try:
                called_at = datetime.fromisoformat(called_at_raw)
                if called_at.tzinfo is None:
                    called_at = called_at.replace(tzinfo=UTC)
                if called_at < cutoff:
                    continue
            except ValueError:
                pass
        stocks.add(uri)
    if settings.moomoo_ledger_backend == "postgres":
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
        for (uri,) in rows:
            norm = _normalize_stock_uri(uri)
            if norm is not None:
                stocks.add(norm)
    return stocks


def record(
    endpoint: str,
    caller: str,
    ok: bool,
    *,
    status_code: int | None = None,
    error: str | None = None,
    duration_ms: int | None = None,
    request_uri: str | None = None,
    payload_sha256: str | None = None,
    byte_length: int | None = None,
) -> None:
    """Record a call AFTER it happens — gate() below is what enforces the K-line quota."""
    now = datetime.now(UTC)
    norm_uri = _normalize_stock_uri(request_uri)
    if settings.moomoo_ledger_backend == "postgres":
        gateway.postgres_writer(
            gateway.CallRecord(
                source="moomoo",
                endpoint=endpoint,
                caller=caller,
                called_at=now,
                ok=ok,
                status_code=status_code,
                error=None if error is None else error[:500],
                duration_ms=duration_ms,
                request_uri=norm_uri,
                payload_sha256=payload_sha256,
                byte_length=byte_length,
                run_key=gateway.current_run_key(),
                capacity_window_id=gateway.capacity_window_id("moomoo", now),
            )
        )
        return
    state = _load()
    state["calls"].append(
        {
            "month": _month_key(now),
            "endpoint": endpoint,
            "caller": caller,
            "called_at": now.isoformat(),
            "ok": ok,
            "status_code": status_code,
            "error": None if error is None else error[:500],
            "duration_ms": duration_ms,
            "request_uri": norm_uri,
            "payload_sha256": payload_sha256,
            "byte_length": byte_length,
        }
    )
    _save(state)


def gate(endpoint: str, caller: str, *, request_uri: str | None = None) -> None:
    """Enforce the 30-day rolling 2,000-stock K-line quota and log a daily anomaly warning (#1411 K2)."""
    if endpoint in KLINE_ENDPOINTS:
        stocks = kline_distinct_stocks_30d()
        target_uri = _normalize_stock_uri(request_uri)
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

    The window and its ceiling are the source registry's (`LEDGER_CAPACITIES["moomoo"]`,
    rule 6: declared once); MOOMOO_CALLS_PER_30S may only LOWER the ceiling, never raise
    it past the declaration.

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
