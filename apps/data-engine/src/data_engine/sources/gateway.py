"""The source gateway: every external call carries a declared capacity and lands in the
ledger (init.md rule 6 as generalized on 2026-09-04; #729).

Capacity is part of the capture logic, not an afterthought inside one adapter. A source
declares a rate window, calls per window and a daily budget; the gateway throttles inside
the window, refuses a call that would exceed the daily budget, and records every attempt
in `staging.api_call_ledger` with the caller that spent it. The moomoo ledger
(`moomoo_ledger.py`) is the first tenant of that table and keeps its own precautionary
monthly gate; this gateway is the generic seat every other source — SEC, the filing-
extraction model provider, a search provider — takes.

A refused call is `CapacityExceeded`, never a silent skip: the planner records the cell
as `deferred_capacity` and the next run picks it up.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class SourceCapacity:
    source: str
    window_seconds: float
    calls_per_window: int
    daily_budget: int

    def __post_init__(self) -> None:
        if self.window_seconds <= 0 or self.calls_per_window <= 0 or self.daily_budget <= 0:
            raise ValueError(f"capacity for {self.source!r} must be positive in every dimension")


#: Declared capacities. SEC's fair-access guidance is 10 requests/second; declaring 8
#: leaves headroom for the daily tick, which shares the same host and User-Agent. The
#: model and search seats are provider-agnostic placeholders sized for a QQQ backfill;
#: a provider's real limits replace them in the same PR that provisions it (#70, #732).
CAPACITIES: Mapping[str, SourceCapacity] = {
    "sec": SourceCapacity("sec", window_seconds=1.0, calls_per_window=8, daily_budget=5000),
    "filing-extraction-model": SourceCapacity(
        "filing-extraction-model", window_seconds=60.0, calls_per_window=30, daily_budget=2000
    ),
    "search": SourceCapacity("search", window_seconds=60.0, calls_per_window=20, daily_budget=500),
}


class CapacityExceeded(RuntimeError):
    def __init__(self, source: str, reason: str) -> None:
        super().__init__(f"{source}: {reason}")
        self.source = source
        self.reason = reason


@dataclass
class SourceGateway:
    """One path to any vendor or model. `call` throttles, budgets, records."""

    connection: Any
    caller: str
    capacities: Mapping[str, SourceCapacity] = field(default_factory=lambda: CAPACITIES)
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    _windows: dict[str, deque[float]] = field(default_factory=dict)
    # (utc day, calls) per source: a gateway that lives across midnight re-reads the
    # ledger for the new day instead of carrying yesterday's count into it.
    _spent_today: dict[str, tuple[date, int]] = field(default_factory=dict)

    def capacity(self, source: str) -> SourceCapacity:
        try:
            return self.capacities[source]
        except KeyError as error:
            raise CapacityExceeded(source, "no declared capacity — register the source before calling it") from error

    def call(self, source: str, endpoint: str, fn: Callable[[], T]) -> T:
        capacity = self.capacity(source)
        self._check_daily_budget(source, capacity)
        self._throttle(source, capacity)
        ok = False
        try:
            result = fn()
            ok = True
            return result
        finally:
            self._record(source, endpoint, ok)

    def _check_daily_budget(self, source: str, capacity: SourceCapacity) -> None:
        now = self.now()
        today = now.date()
        cached = self._spent_today.get(source)
        if cached is None or cached[0] != today:
            day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            row = self.connection.execute(
                "select count(*) from staging.api_call_ledger where source = %s and called_at >= %s",
                (source, day_start),
            ).fetchone()
            cached = (today, int(row[0]) if row else 0)
            self._spent_today[source] = cached
        if cached[1] >= capacity.daily_budget:
            raise CapacityExceeded(source, f"daily budget {capacity.daily_budget} spent ({cached[1]} calls today)")

    def _throttle(self, source: str, capacity: SourceCapacity) -> None:
        window = self._windows.setdefault(source, deque())
        now = self.clock()
        while window and now - window[0] >= capacity.window_seconds:
            window.popleft()
        if len(window) >= capacity.calls_per_window:
            wait = capacity.window_seconds - (now - window[0])
            if wait > 0:
                self.sleep(wait)
            now = self.clock()
            while window and now - window[0] >= capacity.window_seconds:
                window.popleft()
        window.append(now)

    def _record(self, source: str, endpoint: str, ok: bool) -> None:
        day, spent = self._spent_today.get(source, (self.now().date(), 0))
        self._spent_today[source] = (day, spent + 1)
        self.connection.execute(
            "insert into staging.api_call_ledger (source, endpoint, caller, ok) values (%s, %s, %s, %s)",
            (source, endpoint, self.caller, ok),
        )
