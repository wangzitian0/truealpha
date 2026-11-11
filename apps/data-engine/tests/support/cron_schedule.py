"""5-field UTC cron schedule reader for schedule-window tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

HORIZON_DAYS = 5 * 366
_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
_WEEKDAYS = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}


def _parse_token(token: str, names: dict[str, int] | None, low: int, high: int) -> int:
    lowered = token.lower()
    value = names[lowered] if names and lowered in names else int(token)
    if value == 7 and high == 6:
        return 0
    if not low <= value <= high:
        raise ValueError(f"cron value {token!r} outside {low}..{high}")
    return value


def _parse_field(spec: str, low: int, high: int, names: dict[str, int] | None = None) -> frozenset[int]:
    values: set[int] = set()
    for part in spec.split(","):
        base, _, step_text = part.partition("/")
        step = int(step_text) if step_text else 1
        if base == "*":
            start, end = low, high
        elif "-" in base:
            left, right = base.split("-", 1)
            start = _parse_token(left, names, low, high)
            end = _parse_token(right, names, low, high)
        else:
            start = _parse_token(base, names, low, high)
            end = high if step_text else start
        values.update(range(start, end + 1, step))
    return frozenset(values)


@dataclass(frozen=True)
class Cron:
    expression: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    days_restricted: bool
    weekdays_restricted: bool

    @classmethod
    def parse(cls, expression: str) -> Cron:
        parts = expression.split()
        if len(parts) != 5:
            raise ValueError(f"cron {expression!r} has {len(parts)} fields, not 5")
        return cls(
            expression=expression,
            minutes=_parse_field(parts[0], 0, 59),
            hours=_parse_field(parts[1], 0, 23),
            days=_parse_field(parts[2], 1, 31),
            months=_parse_field(parts[3], 1, 12, _MONTHS),
            weekdays=_parse_field(parts[4], 0, 6, _WEEKDAYS),
            days_restricted=parts[2] != "*",
            weekdays_restricted=parts[4] != "*",
        )

    def fires_on(self, day: date) -> bool:
        if day.month not in self.months:
            return False
        in_days, in_weekdays = day.day in self.days, day.isoweekday() % 7 in self.weekdays
        return (
            (in_days or in_weekdays)
            if (self.days_restricted and self.weekdays_restricted)
            else (in_days and in_weekdays)
        )

    def _times(self, day: date) -> list[datetime]:
        if not self.fires_on(day):
            return []
        return [
            datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)
            for hour in sorted(self.hours)
            for minute in sorted(self.minutes)
        ]

    def fires_between(self, start: datetime, end: datetime) -> list[datetime]:
        fires: list[datetime] = []
        day = start.date()
        while day <= end.date():
            fires.extend(moment for moment in self._times(day) if start <= moment <= end)
            day += timedelta(days=1)
        return fires

    def next_fire(self, after: datetime) -> datetime | None:
        day = after.date()
        for _ in range(HORIZON_DAYS):
            later = [moment for moment in self._times(day) if moment > after]
            if later:
                return later[0]
            day += timedelta(days=1)
        return None
