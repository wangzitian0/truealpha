"""The risk-free rate of the core strategy, resolved from a measured series (#1255).

The labor-efficiency factor subtracts a capital charge: ``total_assets * risk_free_rate``.
Definition 1.0.0 declares the rate as a per-cutoff parameter of the instrument
``us_treasury_bill_3_month`` (``labor_efficiency.risk_free``). The instrument is a series id of
``mart.macro_series``. This module resolves that parameter for one cutoff from
``mart.macro_series_daily``. ``datahub.treasury_rates`` writes the table from the U.S. Treasury
"Daily Treasury Par Yield Curve Rates".

Four rules decide the answer:

1. A cutoff sees only the observations that were knowable at the cutoff. A first observation is
   knowable at 00:00 UTC of the day after its observation date, so no cutoff sees a rate of its own day.
2. The newest knowable observation answers.
3. When one date holds several stored vintages, the newest vintage that is knowable at the cutoff
   answers. A revised value is knowable from the moment it was fetched, so a cutoff before that
   moment still sees the earlier vintage.
4. An answer that is more than ``MAX_RATE_LAG_DAYS`` old does not count.

A cutoff with no answer raises ``MissingRiskFreeRate``. Its message starts with the reason code of the
definition, ``missing_risk_free_rate_parameter``. No default and no fallback value exists anywhere.
The live tick, the TOPT core engine and the nightly backtest all call ``risk_free_rate_at``, so they
read one series.

The golden replay (``core_strategy_replay``) does not call this module. It keeps the explicit rates
of its corpus, so that its results stay fixed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import psycopg
from truealpha_contracts.strategy import ExclusionReason, RiskFreeInstrument

from data_engine.datahub.production_topt.source_registrations import macro_registration_for

#: The series id of the instrument. The strategy definition declares the same id
#: (``labor_efficiency.risk_free.instrument``). Stored rows carry the text, so a rename of the
#: enum value would orphan the history. ``test_risk_free`` pins the text.
INSTRUMENT = RiskFreeInstrument.US_TREASURY_BILL_3_MONTH.value

#: The most days that the newest observation may trail the cutoff date. It is twice the cadence of
#: the series (``max_age_days`` of the registration). The cadence admits the normal gap of a long
#: weekend. The same length more admits failed runs. A rate that is older than the limit is not a
#: measurement of the cutoff. The `macro_freshness` verdict turns red at the cadence, well before
#: this limit stops a tick.
MAX_RATE_LAG_DAYS = 2 * macro_registration_for(INSTRUMENT).max_age_days

#: The reason code that the strategy definition names for a missing rate parameter.
MISSING_REASON = ExclusionReason.MISSING_RISK_FREE_RATE_PARAMETER.value


@dataclass(frozen=True)
class RiskFreeRate:
    """One resolved rate, with the lineage that proves where it came from."""

    #: The series id of the instrument.
    series_id: str
    #: The annualized rate as a decimal fraction. 3.65 percent is ``Decimal("0.0365")``.
    value: Decimal
    #: The day the Treasury observed the rate.
    observation_date: date
    #: The instant from which the rate counts as known.
    knowable_at: datetime
    #: The source that served the row, as ``mart.macro_series_daily.source`` states it.
    source: str
    #: The ``raw.fetches`` row that holds the bytes which carried the value.
    fetch_id: int

    def lineage(self) -> dict[str, str | int]:
        """The facts that a decision record keeps: series, value, date and source."""
        return {
            "series_id": self.series_id,
            "value": str(self.value),
            "observation_date": self.observation_date.isoformat(),
            "knowable_at": self.knowable_at.isoformat(),
            "source": self.source,
            "fetch_id": self.fetch_id,
        }


class MissingRiskFreeRate(RuntimeError):
    """No usable observation exists for a cutoff. The caller stops, and never guesses."""

    def __init__(self, cutoff_at: datetime, newest: date | None) -> None:
        newest_text = "none stored" if newest is None else newest.isoformat()
        super().__init__(
            f"{MISSING_REASON}: no {INSTRUMENT} observation within {MAX_RATE_LAG_DAYS} days of "
            f"{cutoff_at.isoformat()}; newest knowable observation: {newest_text}"
        )
        self.cutoff_at = cutoff_at
        self.newest = newest


_NEWEST_KNOWABLE_SQL = """
select value, observation_date, knowable_at, source, fetch_id
from mart.macro_series_daily
where series_id = %s and knowable_at <= %s
order by observation_date desc, knowable_at desc, recorded_at desc, id desc
limit 1
"""


def risk_free_rate_at(connection: psycopg.Connection[Any], cutoff_at: datetime) -> RiskFreeRate:
    """The rate that was knowable at ``cutoff_at``. Raise ``MissingRiskFreeRate`` when none counts."""
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
