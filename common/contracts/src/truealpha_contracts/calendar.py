"""US Exchange Calendar SSOT (XNYS / NASDAQ) (#863)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

_HOLIDAY_TUPLES = (
    (2024, ((1, 1), (1, 15), (2, 19), (3, 29), (5, 27), (6, 19), (7, 4), (9, 2), (11, 28), (12, 25))),
    (2025, ((1, 1), (1, 20), (2, 17), (4, 18), (5, 26), (6, 19), (7, 4), (9, 1), (11, 27), (12, 25))),
    (2026, ((1, 1), (1, 19), (2, 16), (4, 3), (5, 25), (6, 19), (7, 3), (9, 7), (11, 26), (12, 25))),
    (2027, ((1, 1), (1, 18), (2, 15), (3, 26), (5, 31), (6, 18), (7, 5), (9, 6), (11, 25), (12, 24))),
)
XNYS_HOLIDAYS: frozenset[date] = frozenset(date(yr, m, d) for yr, days in _HOLIDAY_TUPLES for m, d in days)
NY_TZ = ZoneInfo("America/New_York")
MARKET_CLOSE_TIME = time(16, 0)


def is_market_session(d: date) -> bool:
    return d.weekday() < 5 and d not in XNYS_HOLIDAYS


def previous_market_session(d: date, *, strictly_before: bool = False) -> date:
    candidate = d - timedelta(days=1) if strictly_before else d
    while not is_market_session(candidate):
        candidate -= timedelta(days=1)
    return candidate


def count_market_sessions_between(start: date, end: date) -> int:
    if end <= start:
        return 0
    return sum(is_market_session(start + timedelta(days=offset)) for offset in range(1, (end - start).days + 1))


def settled_session_for_cutoff(cutoff: datetime) -> date:
    at_market = cutoff.astimezone(NY_TZ)
    d = at_market.date()
    if at_market.time() >= MARKET_CLOSE_TIME and is_market_session(d):
        return d
    return previous_market_session(d, strictly_before=True)
