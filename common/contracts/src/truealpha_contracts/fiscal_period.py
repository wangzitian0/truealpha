"""Fiscal-period tag encoder and parser (`FY<filing_fy>:<kind>:<start>:<end>`, init.md Section 6)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

__all__ = ["ANNUAL_MINIMUM_DAYS", "FiscalPeriod", "encode_annual", "format_pattern", "parse_annual"]

ANNUAL_MINIMUM_DAYS = 350
_TAG = re.compile(
    r"^FY(?P<filing_fy>\d{4}):(?P<kind>[A-Z0-9]+):(?P<start>\d{4}-\d{2}-\d{2}):(?P<end>\d{4}-\d{2}-\d{2})$"
)


@dataclass(frozen=True)
class FiscalPeriod:
    filing_fiscal_year: int
    kind: str
    start: date
    end: date

    @property
    def days(self) -> int:
        return (self.end - self.start).days

    @property
    def is_annual(self) -> bool:
        return self.kind == "FY" and self.days >= ANNUAL_MINIMUM_DAYS


def encode_annual(end: date, *, filing_fiscal_year: int | None = None) -> str:
    try:
        prior = end.replace(year=end.year - 1)
    except ValueError:
        prior = end.replace(year=end.year - 1, day=28)
    start = prior + timedelta(days=1)
    fy = filing_fiscal_year if filing_fiscal_year is not None else end.year
    return f"FY{fy}:FY:{start.isoformat()}:{end.isoformat()}"


def parse_annual(tag: str) -> FiscalPeriod | None:
    matched = _TAG.match(tag)
    if matched is None:
        return None
    period = FiscalPeriod(
        filing_fiscal_year=int(matched["filing_fy"]),
        kind=matched["kind"],
        start=date.fromisoformat(matched["start"]),
        end=date.fromisoformat(matched["end"]),
    )
    return period if period.is_annual else None


def format_pattern() -> str:
    return _TAG.pattern
