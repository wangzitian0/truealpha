"""Dimensional arithmetic over typed Measure values."""

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
    """Raised when two measures cannot be multiplied because their units do not cancel."""

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
    """Derive the canonical MeasureUnit for a registered metric and subject."""
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
    """Wrap a scalar input value in its canonical Measure for the given strategy input key."""
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


def _combine_knowable_at(left: datetime | None, right: datetime | None) -> datetime | None:
    if left is not None and right is not None:
        return max(left, right)
    return left if left is not None else right


def _combine_period(left: FiscalPeriod | None, right: FiscalPeriod | None) -> FiscalPeriod | None:
    return left if left is not None else right


def times(left: Measure, right: Measure) -> Measure:
    """Multiply PerUnit(currency, instrument_id) and Units(instrument_id) into Money(currency).

    Raises UnitMismatchError when the unit pair is not (PerUnit, Units) or when
    instrument_id differs between the two operands.
    """
    if isinstance(left.unit, PerUnit) and isinstance(right.unit, Units):
        per_unit = left.unit
        units = right.unit
    elif isinstance(left.unit, Units) and isinstance(right.unit, PerUnit):
        per_unit = right.unit
        units = left.unit
    else:
        raise UnitMismatchError(UNIT_MISMATCH_REASON)

    if per_unit.instrument_id != units.instrument_id:
        raise UnitMismatchError(UNIT_MISMATCH_REASON)

    return Measure(
        value=left.value * right.value,
        unit=Money(currency=per_unit.currency),
        period=_combine_period(left.period, right.period),
        knowable_at=_combine_knowable_at(left.knowable_at, right.knowable_at),
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
