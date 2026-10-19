"""The fiscal-period tag: one encoder, one parser, one format (init.md Section 6).

The tag is `<filing fiscal year>:<kind>:<start ISO date>:<end ISO date>`, e.g.
`FY2025:FY:2025-01-01:2025-12-31`.

**Why this module exists rather than an f-string on one side and a regex on the other.**
That is exactly what it replaced: `strategy_bridge` built the tag and `factors.base.peg`
matched it with `re.compile(r":FY:(\\d{4}-\\d{2}-\\d{2}):(\\d{4}-\\d{2}-\\d{2})$")`, with
nothing shared between them. A format known only to a producer and a regex does not fail
loudly when the two drift — the parser simply matches nothing, the series comes back empty,
and the factor reports `missing_net_income`, which is indistinguishable from an issuer that
never filed. Silence that looks like a legitimate refusal is this repository's most
expensive failure shape.

**The leading fiscal year is the FILING's, not the period's.** A 10-K stamps its
comparatives with its own tag, so one filing yields three annual rows all prefixed
`FY2025`. Never key on it. `parse_annual` returns the period's own start and end, and
callers key on the END DATE.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

__all__ = ["ANNUAL_MINIMUM_DAYS", "FiscalPeriod", "encode_annual", "format_pattern", "parse_annual"]

# An annual period: shorter spans are quarterly facts that must not be compared with
# annual ones. 350 days absorbs 52/53-week fiscal calendars. JNJ published a `:FY:` row
# spanning six months with the real year absent (#572), which is why a `:FY:` kind is not
# by itself evidence of an annual period.
ANNUAL_MINIMUM_DAYS = 350

_TAG = re.compile(
    r"^FY(?P<filing_fy>\d{4}):(?P<kind>[A-Z0-9]+):(?P<start>\d{4}-\d{2}-\d{2}):(?P<end>\d{4}-\d{2}-\d{2})$"
)


@dataclass(frozen=True)
class FiscalPeriod:
    """A parsed tag. `filing_fiscal_year` is retained for provenance-free auditing and is
    deliberately NOT usable as a period key — see the module docstring."""

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
    """The tag for an annual period ending `end`.

    The start is `end` less one year plus a day, which is the convention the capture side
    has always used: only the period end is known at the point a series is projected, and
    an annual period's start is implied by it. `parse_annual` re-checks the duration rather
    than trusting the kind, so an implied start can never smuggle a short period through.
    """
    try:
        prior = end.replace(year=end.year - 1)
    except ValueError:
        # 29 February has no counterpart in a common year. `strategy_bridge` shipped this
        # same expression unguarded, so a fiscal year ending on a leap day would have
        # raised inside the capture tick rather than producing a period.
        prior = end.replace(year=end.year - 1, day=28)
    start = prior + timedelta(days=1)
    return f"FY{filing_fiscal_year if filing_fiscal_year is not None else end.year}:FY:{start.isoformat()}:{end.isoformat()}"


def parse_annual(tag: str) -> FiscalPeriod | None:
    """The period a tag describes, or None when it is not an annual period.

    None covers three distinct cases on purpose — unparseable, non-annual kind, and tagged
    annual but shorter than a year — because every one of them means the same thing to a
    caller building a multi-year window: this observation is not comparable across years.
    """
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
    """The regex source, exposed so a gate can assert nobody re-implements it."""
    return _TAG.pattern
