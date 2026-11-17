"""PIT risk-free rate resolver from `mart.macro_series_daily` (#1255)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import psycopg
from truealpha_contracts.strategy import ExclusionReason, RiskFreeInstrument

from data_engine.datahub.production_topt.source_registrations import macro_registration_for

INSTRUMENT = RiskFreeInstrument.US_TREASURY_BILL_3_MONTH.value
MAX_RATE_LAG_DAYS = 2 * macro_registration_for(INSTRUMENT).max_age_days
MISSING_REASON = ExclusionReason.MISSING_RISK_FREE_RATE_PARAMETER.value


@dataclass(frozen=True)
class RiskFreeRate:
    series_id: str
    value: Decimal
    observation_date: date
    knowable_at: datetime
    source: str
    fetch_id: int

    def lineage(self) -> dict[str, str | int]:
        return {
            "series_id": self.series_id,
            "value": str(self.value),
            "observation_date": self.observation_date.isoformat(),
            "knowable_at": self.knowable_at.isoformat(),
            "source": self.source,
            "fetch_id": self.fetch_id,
        }


class MissingRiskFreeRate(RuntimeError):
    def __init__(self, cutoff_at: datetime, newest: date | None) -> None:
        newest_text = "none stored" if newest is None else newest.isoformat()
        super().__init__(
            f"{MISSING_REASON}: no {INSTRUMENT} observation within {MAX_RATE_LAG_DAYS} days of "
            f"{cutoff_at.isoformat()}; newest knowable observation: {newest_text}"
        )
        self.cutoff_at = cutoff_at
        self.newest = newest


_NEWEST_KNOWABLE_SQL = (
    "select value, observation_date, knowable_at, source, fetch_id from mart.macro_series_daily "
    "where series_id = %s and knowable_at <= %s "
    "order by observation_date desc, knowable_at desc, recorded_at desc, id desc limit 1"
)


def risk_free_rate_at(connection: psycopg.Connection[Any], cutoff_at: datetime) -> RiskFreeRate:
    """Return the newest knowable rate at `cutoff_at`."""
    if cutoff_at.tzinfo is None or cutoff_at.utcoffset() is None:
        raise ValueError("cutoff_at must carry a time zone")
    row = connection.execute(_NEWEST_KNOWABLE_SQL, (INSTRUMENT, cutoff_at)).fetchone()
    if row is None:
        raise MissingRiskFreeRate(cutoff_at, None)
    value, observation_date, knowable_at, source, fetch_id = row
    if (cutoff_at.astimezone(UTC).date() - observation_date).days > MAX_RATE_LAG_DAYS:
        raise MissingRiskFreeRate(cutoff_at, observation_date)
    return RiskFreeRate(
        series_id=INSTRUMENT,
        value=value,
        observation_date=observation_date,
        knowable_at=knowable_at,
        source=source,
        fetch_id=fetch_id,
    )


__all__ = [
    "INSTRUMENT",
    "MAX_RATE_LAG_DAYS",
    "MISSING_REASON",
    "MissingRiskFreeRate",
    "RiskFreeRate",
    "risk_free_rate_at",
]
