"""US Exchange Calendar SSOT (XNYS / NASDAQ) — addresses #863.

Point-in-Time market calendar defining settled sessions and trading holidays.
Distinguishes real market sessions from weekends and exchange holidays so:
1. `last_settled_session_date` never asserts a holiday as a settled session.
2. `graded_price_confidence` does not penalize confidence across market holidays.
3. Ingestion skip reasons declare explicit exchange closure.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

# Checked-in XNYS (New York Stock Exchange) full holidays 2024-2027 (NYSE Rule 7.2).
# Early close sessions (e.g. Black Friday, Christmas Eve) are trading sessions and
# are NOT listed here.
XNYS_HOLIDAYS: frozenset[date] = frozenset(
    {
        # 2024
        date(2024, 1, 1),  # New Year's Day
        date(2024, 1, 15),  # MLK Day
        date(2024, 2, 19),  # Presidents' Day
        date(2024, 3, 29),  # Good Friday
        date(2024, 5, 27),  # Memorial Day
        date(2024, 6, 19),  # Juneteenth
        date(2024, 7, 4),  # Independence Day
        date(2024, 9, 2),  # Labor Day
        date(2024, 11, 28),  # Thanksgiving
        date(2024, 12, 25),  # Christmas Day
        # 2025
        date(2025, 1, 1),  # New Year's Day
        date(2025, 1, 20),  # MLK Day
        date(2025, 2, 17),  # Presidents' Day
        date(2025, 4, 18),  # Good Friday
        date(2025, 5, 26),  # Memorial Day
        date(2025, 6, 19),  # Juneteenth
        date(2025, 7, 4),  # Independence Day
        date(2025, 9, 1),  # Labor Day
        date(2025, 11, 27),  # Thanksgiving
        date(2025, 12, 25),  # Christmas Day
        # 2026
        date(2026, 1, 1),  # New Year's Day
        date(2026, 1, 19),  # MLK Day
        date(2026, 2, 16),  # Presidents' Day
        date(2026, 4, 3),  # Good Friday
        date(2026, 5, 25),  # Memorial Day
        date(2026, 6, 19),  # Juneteenth
        date(2026, 7, 3),  # Independence Day (observed)
        date(2026, 9, 7),  # Labor Day
        date(2026, 11, 26),  # Thanksgiving
        date(2026, 12, 25),  # Christmas Day
        # 2027
        date(2027, 1, 1),  # New Year's Day
        date(2027, 1, 18),  # MLK Day
        date(2027, 2, 15),  # Presidents' Day
        date(2027, 3, 26),  # Good Friday
        date(2027, 5, 31),  # Memorial Day
        date(2027, 6, 18),  # Juneteenth (observed)
        date(2027, 7, 5),  # Independence Day (observed)
        date(2027, 9, 6),  # Labor Day
        date(2027, 11, 25),  # Thanksgiving
        date(2027, 12, 24),  # Christmas Day (observed)
    }
)

NY_TZ = ZoneInfo("America/New_York")
MARKET_CLOSE_TIME = time(16, 0)


def is_market_session(d: date) -> bool:
    """Returns True if the date is an active US equity trading day (Monday-Friday and non-holiday)."""
    return d.weekday() < 5 and d not in XNYS_HOLIDAYS


def previous_market_session(d: date, *, strictly_before: bool = False) -> date:
    """Finds the latest market session on or strictly before `d`."""
    candidate = d - timedelta(days=1) if strictly_before else d
    while not is_market_session(candidate):
        candidate -= timedelta(days=1)
    return candidate


def count_market_sessions_between(start: date, end: date) -> int:
    """Counts trading sessions in the half-open interval (start, end].

    If end <= start, returns 0.
    """
    if end <= start:
        return 0
    sessions = 0
    cur = start + timedelta(days=1)
    while cur <= end:
        if is_market_session(cur):
            sessions += 1
        cur += timedelta(days=1)
    return sessions


def settled_session_for_cutoff(cutoff: datetime) -> date:
    """The newest US-session date whose CLOSE is knowable at `cutoff`.

    A session's close is knowable from 16:00 America/New_York.
    Before that, the newest settled session is the prior market session.
    Holidays and weekends automatically resolve to the previous trading day.
    """
    at_market = cutoff.astimezone(NY_TZ)
    if at_market.time() >= MARKET_CLOSE_TIME:
        candidate = at_market.date()
        # If cutoff is after 16:00 on a holiday/weekend, the settled close is the previous session
        return candidate if is_market_session(candidate) else previous_market_session(candidate, strictly_before=True)
    else:
        # Before 16:00 on any date, today's close is not settled; take prior session
        return previous_market_session(at_market.date(), strictly_before=True)
