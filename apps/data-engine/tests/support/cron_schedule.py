"""5-field UTC cron schedule reader for schedule-window tests, backed by infra2_sdk.ci."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from infra2_sdk.ci import _field_values

HORIZON_DAYS = 5 * 366


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
        minutes, hours, days, months, weekdays = (
            frozenset(_field_values(part, field))
            for part, field in zip(parts, ("minute", "hour", "dom", "month", "dow"), strict=True)
        )
        return cls(
            expression=expression,
            minutes=minutes,
            hours=hours,
            days=days,
            months=months,
            weekdays=frozenset(day % 7 for day in weekdays),
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
