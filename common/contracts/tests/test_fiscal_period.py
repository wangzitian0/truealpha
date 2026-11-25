from datetime import date, timedelta

import pytest
from truealpha_contracts.fiscal_period import ANNUAL_MINIMUM_DAYS, encode_annual, parse_annual

pytestmark = pytest.mark.contract


@pytest.mark.parametrize(
    "end", [date(2025, 12, 31), date(2026, 1, 25), date(2022, 1, 2), date(2025, 11, 2), date(2024, 2, 29)]
)
def test_encoding_then_parsing_returns_the_same_period(end: date) -> None:
    parsed = parse_annual(encode_annual(end))
    assert parsed is not None and parsed.end == end and parsed.days >= ANNUAL_MINIMUM_DAYS


def test_filing_year_short_and_unparseable_periods() -> None:
    parsed = parse_annual(encode_annual(date(2023, 12, 31), filing_fiscal_year=2025))
    assert parsed is not None and parsed.filing_fiscal_year == 2025 and parsed.end.year == 2023
    for tag in (
        "FY2099:FY:2025-07-01:2025-12-31",
        "FY2025:Q3:2025-01-01:2025-12-31",
        "",
        "FY2025",
        "2025:FY:2025-01-01:2025-12-31",
        "FY2025:FY:2025-01-01",
        "junk",
    ):
        assert parse_annual(tag) is None
    end = date(2025, 6, 30)
    full = parse_annual(encode_annual(end))
    assert full is not None and full.start == end.replace(year=end.year - 1) + timedelta(days=1)
