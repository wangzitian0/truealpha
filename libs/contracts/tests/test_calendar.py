from datetime import date, datetime
from zoneinfo import ZoneInfo

from truealpha_contracts.calendar import (
    XNYS_HOLIDAYS,
    count_market_sessions_between,
    is_market_session,
    previous_market_session,
    settled_session_for_cutoff,
)

NY = ZoneInfo("America/New_York")


def test_weekends_are_not_sessions():
    assert not is_market_session(date(2026, 11, 21))  # Saturday
    assert not is_market_session(date(2026, 11, 22))  # Sunday


def test_thanksgiving_and_holidays_are_not_sessions():
    # 2026 Thanksgiving
    assert date(2026, 11, 26) in XNYS_HOLIDAYS
    assert not is_market_session(date(2026, 11, 26))

    # 2026 Christmas
    assert date(2026, 12, 25) in XNYS_HOLIDAYS
    assert not is_market_session(date(2026, 12, 25))


def test_black_friday_is_a_trading_session():
    # Black Friday (half-day session)
    assert is_market_session(date(2026, 11, 27))


def test_previous_market_session_on_holiday():
    # Thursday Thanksgiving -> Wednesday
    assert previous_market_session(date(2026, 11, 26), strictly_before=False) == date(2026, 11, 25)
    assert previous_market_session(date(2026, 11, 26), strictly_before=True) == date(2026, 11, 25)

    # Friday after Thanksgiving (active session)
    assert previous_market_session(date(2026, 11, 27), strictly_before=False) == date(2026, 11, 27)
    assert previous_market_session(date(2026, 11, 27), strictly_before=True) == date(2026, 11, 25)


def test_count_market_sessions_does_not_penalize_holiday():
    # Between Wednesday 2026-11-25 and Friday 2026-11-27:
    # Only Friday counts (Thursday Thanksgiving is skipped).
    assert count_market_sessions_between(date(2026, 11, 25), date(2026, 11, 27)) == 1

    # Over a normal weekend: Friday to Monday = 1 session
    assert count_market_sessions_between(date(2026, 11, 20), date(2026, 11, 23)) == 1


def test_settled_session_for_cutoff_on_holiday():
    # Thanksgiving 2026-11-26 21:00 ET -> settles to 2026-11-25 (issue #863 requirement)
    thanksgiving_evening = datetime(2026, 11, 26, 21, 0, tzinfo=NY)
    assert settled_session_for_cutoff(thanksgiving_evening) == date(2026, 11, 25)

    # Black Friday 2026-11-27 17:00 ET -> settles to 2026-11-27 (market closed earlier at 13:00)
    friday_evening = datetime(2026, 11, 27, 17, 0, tzinfo=NY)
    assert settled_session_for_cutoff(friday_evening) == date(2026, 11, 27)

    # Regular Monday morning 09:00 ET (before open) -> settles to Friday
    monday_morning = datetime(2026, 11, 30, 9, 0, tzinfo=NY)
    assert settled_session_for_cutoff(monday_morning) == date(2026, 11, 27)
