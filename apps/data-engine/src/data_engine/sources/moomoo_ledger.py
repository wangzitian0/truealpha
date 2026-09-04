"""The moomoo call-budget gate (init.md Section 1 rule 6: every moomoo call must
go through the gate, no module decides for itself). The cap is a self-imposed
runaway backstop and audit trail, not a real moomoo-side quota — moomoo's own
docs only rate-limit these endpoints in ~30s bursts (init.md Section 5,
2026-07-10 correction). The burst shape is what throttle() paces.

Two interchangeable backends, selected by MOOMOO_LEDGER_BACKEND:

- 'json' (default): apps/data-engine/data/moomoo_ledger.json, the Phase -1
  stand-in — fine for manual audit sessions, needs no database. Each record
  rewrites the whole file, so it degrades at sweep volume by design.
- 'postgres': staging.api_call_ledger (db/migrations/0003) — what ingestion
  sweeps must use. Selected explicitly rather than auto-detected: a sweep that
  silently fell back to a local file would gate on state nobody audits.
"""

import json
import os
import tempfile
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

from data_engine.config import settings
from data_engine.sources import gateway
from data_engine.sources.gateway import _pg_execute  # one autocommit ledger connection, shared with the gateway

LEDGER_PATH = Path(__file__).resolve().parents[3] / "data" / "moomoo_ledger.json"


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
#
# The connection and the INSERT live in `sources.gateway` since #729: moomoo was the
# ledger's first tenant, and every other vendor now writes the same table through the
# same autocommit connection. This module keeps the moomoo-specific parts — the monthly
# backstop, the burst throttle, and the json backend for probe sessions.

_MONTH_START_UTC = "date_trunc('month', now() at time zone 'utc') at time zone 'utc'"


def _json_calls_this_month() -> int:
    state = _load()
    key = _month_key(datetime.now(UTC))
    return sum(1 for c in state["calls"] if c["month"] == key)


def calls_this_month() -> int:
    """In postgres mode this is the SUM of both stores: probe/capture sessions
    default to the json backend, and a sweep's gate must still see what they
    spent this month. The asymmetry (json mode does NOT consult postgres) is
    deliberate — json-mode sessions are small manual probes that may run where
    no warehouse is reachable, and the backstop's headroom absorbs their
    undercount; a sweep has the warehouse by definition."""
    if settings.moomoo_ledger_backend == "postgres":
        row = _pg_execute(
            f"select count(*) from staging.api_call_ledger where source = 'moomoo' and called_at >= {_MONTH_START_UTC}"
        ).fetchone()
        return row[0] + _json_calls_this_month()
    return _json_calls_this_month()


def record(
    endpoint: str,
    caller: str,
    ok: bool,
    *,
    status_code: int | None = None,
    error: str | None = None,
    duration_ms: int | None = None,
) -> None:
    """Record a call AFTER it happens — gate() below is what enforces the cap.

    A failed call carries the SDK's return code and message (#729: a failed request
    records its error); in postgres mode the row goes through the gateway writer so a
    moomoo call and a Twelve Data call are the same kind of ledger row. The postgres
    write raises on failure here (unlike the gateway's own `emit`), because `_call`
    deliberately drops an unaudited payload rather than let the gate undercount.
    """
    now = datetime.now(UTC)
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
        }
    )
    _save(state)


def gate(endpoint: str, caller: str):
    """Raise BudgetExceededError BEFORE spending a call if this month's cap is hit."""
    used = calls_this_month()
    if used >= settings.moomoo_monthly_call_budget:
        raise BudgetExceededError(
            f"moomoo monthly budget exhausted: {used}/{settings.moomoo_monthly_call_budget} "
            f"calls already made this month (blocked call: {caller} -> {endpoint})"
        )


# --- burst throttle ---

_recent_calls: deque[float] = deque()


def throttle(*, now=time.monotonic, sleep=time.sleep) -> None:
    """Block until one more call fits the per-30s window (MOOMOO_CALLS_PER_30S).

    Process-local on purpose: sweeps are single-process, and moomoo's burst
    limits are per OpenD connection anyway — a cross-process ledger-based
    throttle would add DB churn without adding protection. Kept conservative
    and global-across-endpoints (moomoo's own limits are per endpoint group,
    so this under-uses the real allowance rather than risking it)."""
    cap = settings.moomoo_calls_per_30s
    while True:
        t = now()
        while _recent_calls and t - _recent_calls[0] >= 30.0:
            _recent_calls.popleft()
        if len(_recent_calls) < cap:
            _recent_calls.append(t)
            return
        sleep(30.0 - (t - _recent_calls[0]) + 0.05)
