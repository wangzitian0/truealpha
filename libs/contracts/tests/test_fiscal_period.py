"""The property that matters: encode -> parse is the identity on the period."""

from datetime import date, timedelta

import pytest
from truealpha_contracts.fiscal_period import ANNUAL_MINIMUM_DAYS, encode_annual, parse_annual


@pytest.mark.parametrize(
    "end",
    [
        date(2025, 12, 31),  # calendar year
        date(2026, 1, 25),  # NVDA: fiscal year ending in the NEXT calendar year
        date(2022, 1, 2),  # JNJ FY2021 — the shape that broke year-keying
        date(2025, 11, 2),  # AVGO: 52/53-week calendar
        date(2024, 2, 29),  # leap day
    ],
)
def test_encoding_then_parsing_returns_the_same_period(end: date) -> None:
    parsed = parse_annual(encode_annual(end))
    assert parsed is not None, "an encoded annual period must parse back"
    assert parsed.end == end
    assert parsed.days >= ANNUAL_MINIMUM_DAYS


def test_the_filing_year_is_not_the_period_year_and_never_keys_anything() -> None:
    """One 10-K stamps its comparatives with its own tag, so three annual rows share a
    prefix. Keying on it buckets three years together."""
    tag = encode_annual(date(2023, 12, 31), filing_fiscal_year=2025)
    parsed = parse_annual(tag)
    assert parsed is not None
    assert parsed.filing_fiscal_year == 2025
    assert parsed.end.year == 2023


def test_a_period_tagged_annual_but_shorter_than_a_year_does_not_parse_as_annual() -> None:
    """JNJ's real six-month `:FY:` row (#572). The kind is not evidence of duration."""
    assert parse_annual("FY2099:FY:2025-07-01:2025-12-31") is None


def test_a_quarterly_kind_is_not_annual() -> None:
    assert parse_annual("FY2025:Q3:2025-01-01:2025-12-31") is None


@pytest.mark.parametrize("tag", ["", "FY2025", "2025:FY:2025-01-01:2025-12-31", "FY2025:FY:2025-01-01", "junk"])
def test_an_unparseable_tag_is_none_rather_than_an_exception(tag: str) -> None:
    """A caller building a window treats every failure the same way: not comparable."""
    assert parse_annual(tag) is None


def test_the_implied_start_is_a_full_year_before_the_end() -> None:
    end = date(2025, 6, 30)
    parsed = parse_annual(encode_annual(end))
    assert parsed is not None
    assert parsed.start == end.replace(year=end.year - 1) + timedelta(days=1)
