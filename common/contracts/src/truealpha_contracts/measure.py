"""Typed measurement contracts for monetary, share-unit, and per-unit values."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from truealpha_contracts.fiscal_period import FiscalPeriod as AnnualFiscalPeriod

FiscalPeriod = str | date | tuple[date, date] | AnnualFiscalPeriod


@dataclass(frozen=True)
class Money:
    """Monetary unit with an ISO 4217 currency code."""

    currency: str

    def __post_init__(self) -> None:
        if not isinstance(self.currency, str) or not self.currency.strip():
            raise ValueError("Money.currency must be a non-empty string")


@dataclass(frozen=True)
class Units:
    """Count of units for a specific instrument or share basis."""

    instrument_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.instrument_id, str) or not self.instrument_id.strip():
            raise ValueError("Units.instrument_id must be a non-empty string")


@dataclass(frozen=True)
class PerUnit:
    """Monetary amount per unit of a specific instrument or share basis."""

    currency: str
    instrument_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.currency, str) or not self.currency.strip():
            raise ValueError("PerUnit.currency must be a non-empty string")
        if not isinstance(self.instrument_id, str) or not self.instrument_id.strip():
            raise ValueError("PerUnit.instrument_id must be a non-empty string")


MeasureUnit = Money | Units | PerUnit


@dataclass(frozen=True)
class Measure:
    """Measured quantity bound to its dimensional unit, period, knowable timestamp, and subject."""

    value: Decimal
    unit: MeasureUnit
    period: FiscalPeriod | None = None
    knowable_at: datetime | None = None
    subject: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.value, Decimal):
            object.__setattr__(self, "value", Decimal(str(self.value)))
        if not isinstance(self.unit, (Money, Units, PerUnit)):
            raise TypeError("Measure.unit must be Money, Units, or PerUnit")


__all__ = [
    "FiscalPeriod",
    "Measure",
    "MeasureUnit",
    "Money",
    "PerUnit",
    "Units",
]
