"""Test support: the cover of an inline XBRL annual filing (#1519, step 1a).

The cover of a 10-K or 20-F tags each registered security with a title (`dei:Security12bTitle`) and a
ticker (`dei:TradingSymbol`) in one context. `cover_html` writes that shape. The reader of
`data_engine.datahub.standards.listing_unit_ratio` pairs title and ticker by context id.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from data_engine.datahub.standards.listing_unit_ratio import PATTERN_PLAIN_SHARE, ListingUnitRatio


def plain_share_ratio(_cik: int, _cutoff: date, _ticker: str | None) -> ListingUnitRatio:
    """A reader for tests of non-ratio behavior: one ordinary share per listing unit."""
    return ListingUnitRatio(Decimal(1), PATTERN_PLAIN_SHARE, title="Common stock")


def cover_html(*lines: tuple[str | None, str]) -> bytes:
    """An inline XBRL cover. Each line is (ticker or None, title); one line is one context."""
    parts = ["<html><body><ix:header><ix:resources>"]
    for index in range(1, len(lines) + 1):
        parts.append(
            f'<xbrli:context id="c-{index}"><xbrli:entity><xbrli:identifier scheme="x">1</xbrli:identifier>'
            f"</xbrli:entity><xbrli:period><xbrli:startDate>2025-01-01</xbrli:startDate>"
            f"<xbrli:endDate>2025-12-31</xbrli:endDate></xbrli:period></xbrli:context>"
        )
    parts.append("</ix:resources></ix:header><p>Securities registered pursuant to Section 12(b) of the Act:</p>")
    for index, (symbol, title) in enumerate(lines, start=1):
        parts.append(
            f'<ix:nonNumeric contextRef="c-{index}" name="dei:Security12bTitle" id="t-{index}">{title}</ix:nonNumeric>'
        )
        if symbol is not None:
            parts.append(
                f'<ix:nonNumeric contextRef="c-{index}" name="dei:TradingSymbol" id="s-{index}">{symbol}</ix:nonNumeric>'
            )
    parts.append("</body></html>")
    return "".join(parts).encode()
