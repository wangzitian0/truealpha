"""Dimensional arithmetic for Measure values."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from truealpha_contracts.measure import (
    FiscalPeriod,
    Measure,
    MeasureUnit,
    Money,
    PerUnit,
    Units,
)
from truealpha_contracts.metrics import INPUT_KEY_ALIASES, METRICS, UnitFamily

UNIT_MISMATCH_REASON = "unit_mismatch"
DEFAULT_REPORTING_CURRENCY = "USD"


class UnitMismatchError(ValueError):
    """Incompatible measure units."""

    def __init__(self, message: str = UNIT_MISMATCH_REASON) -> None:
        super().__init__(message)
        self.reason = UNIT_MISMATCH_REASON


def default_unit_for_metric(
    metric: str,
    unit_family: UnitFamily,
    subject: str,
    *,
    currency: str = DEFAULT_REPORTING_CURRENCY,
) -> MeasureUnit:
    if unit_family is UnitFamily.PER_SHARE:
        return PerUnit(currency=currency, instrument_id=subject)
    if unit_family is UnitFamily.CURRENCY:
        return Money(currency=currency)
    return Units(instrument_id=subject)


def measure_for_input_key(
    input_key: str,
    value: Decimal,
    *,
    subject: str,
    currency: str = DEFAULT_REPORTING_CURRENCY,
    period: FiscalPeriod | None = None,
    knowable_at: datetime | None = None,
) -> Measure:
    metric = INPUT_KEY_ALIASES.get(input_key, input_key)
    spec = METRICS.get(metric)
    unit_family = spec.unit_family if spec is not None else UnitFamily.COUNT
    unit = default_unit_for_metric(metric, unit_family, subject, currency=currency)
    return Measure(
        value=value,
        unit=unit,
        period=period,
        knowable_at=knowable_at,
        subject=subject,
    )


def times(left: Measure, right: Measure) -> Measure:
    """Multiply PerUnit and Units into Money."""
    if isinstance(left.unit, PerUnit) and isinstance(right.unit, Units):
        per_unit, units = left.unit, right.unit
    elif isinstance(left.unit, Units) and isinstance(right.unit, PerUnit):
        per_unit, units = right.unit, left.unit
    else:
        raise UnitMismatchError(UNIT_MISMATCH_REASON)

    if per_unit.instrument_id != units.instrument_id:
        raise UnitMismatchError(UNIT_MISMATCH_REASON)

    known = (
        max(left.knowable_at, right.knowable_at)
        if left.knowable_at is not None and right.knowable_at is not None
        else (left.knowable_at if left.knowable_at is not None else right.knowable_at)
    )
    return Measure(
        value=left.value * right.value,
        unit=Money(currency=per_unit.currency),
        period=left.period if left.period is not None else right.period,
        knowable_at=known,
        subject=left.subject or right.subject,
    )


__all__ = [
    "FiscalPeriod",
    "Measure",
    "MeasureUnit",
    "Money",
    "PerUnit",
    "UNIT_MISMATCH_REASON",
    "UnitMismatchError",
    "Units",
    "default_unit_for_metric",
    "measure_for_input_key",
    "times",
]
