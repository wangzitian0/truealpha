"""#758: split events come from the ratio of the unadjusted and split-adjusted closes.

The shapes follow NVDA's 10-for-1 split, effective 2024-06-10: the unadjusted close fell from
1208.88 to 121.79 overnight, and the split-adjusted series divides every earlier close by 10.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from factors.backtest.corporate_actions import SeriesBar, SplitDerivationError, SplitEvent, derive_split_events

EX_DATE = date(2024, 6, 10)
#: Unadjusted (open, close) of NVDA around its split.
NVDA = {
    date(2024, 6, 5): (Decimal("1186.50"), Decimal("1224.40")),
    date(2024, 6, 6): (Decimal("1240.48"), Decimal("1209.98")),
    date(2024, 6, 7): (Decimal("1197.70"), Decimal("1208.88")),
    date(2024, 6, 10): (Decimal("120.37"), Decimal("121.79")),
    date(2024, 6, 11): (Decimal("121.77"), Decimal("120.91")),
    date(2024, 6, 12): (Decimal("123.06"), Decimal("125.20")),
}


def _bars(unadjusted: dict[date, tuple[Decimal, Decimal]], adjusted_close) -> list[SeriesBar]:
    return [
        SeriesBar(day, open_, close, adjusted_close(day, close)) for day, (open_, close) in sorted(unadjusted.items())
    ]


def _split_adjusted(day: date, close: Decimal) -> Decimal:
    return close / 10 if day < EX_DATE else close


def test_a_10_for_1_split_is_one_event_on_its_ex_date() -> None:
    events = derive_split_events("NVDA", _bars(NVDA, _split_adjusted))

    assert events == (SplitEvent("NVDA", EX_DATE, 10, 1),)


def test_a_3_for_2_split_has_the_factor_3_over_2() -> None:
    prices = {
        date(2024, 2, 22): (Decimal("150"), Decimal("150")),
        date(2024, 2, 23): (Decimal("151"), Decimal("152")),
        date(2024, 2, 26): (Decimal("101.5"), Decimal("102")),
    }

    events = derive_split_events(
        "SPLT", _bars(prices, lambda day, close: close / Decimal("1.5") if day < date(2024, 2, 26) else close)
    )

    assert events == (SplitEvent("SPLT", date(2024, 2, 26), 3, 2),)


def test_rounding_noise_in_the_ratio_is_no_split() -> None:
    start = date(2024, 1, 2)
    prices = {start + timedelta(days=index): (Decimal(100), Decimal(100)) for index in range(20)}

    def rounded(day: date, close: Decimal) -> Decimal:
        return close * (Decimal("1.0001") if day.day % 2 else Decimal("0.9999"))

    assert derive_split_events("FLAT", _bars(prices, rounded)) == ()


def test_a_step_the_unadjusted_series_does_not_show_stops_the_run() -> None:
    """Two vintages of the split-adjusted series: one fetched before the split for old sessions.

    The ratio steps from 1 to 10 on 2024-06-06 without any drop in the unadjusted price. A
    derivation that trusted the ratio alone would apply a 1-for-10 reverse split to held shares.
    """

    def stale_then_fresh(day: date, close: Decimal) -> Decimal:
        if day < date(2024, 6, 6):
            return close
        return close / 10 if day < EX_DATE else close

    with pytest.raises(SplitDerivationError, match="does not drop by that factor"):
        derive_split_events("NVDA", _bars(NVDA, stale_then_fresh))


def test_a_series_also_adjusted_for_dividends_stops_the_run() -> None:
    start = date(2024, 1, 2)
    prices = {start + timedelta(days=index): (Decimal(100), Decimal(100)) for index in range(40)}

    def drifting(day: date, close: Decimal) -> Decimal:
        return close * (1 - Decimal("0.0002") * (start + timedelta(days=39) - day).days)

    with pytest.raises(SplitDerivationError, match="without a split factor"):
        derive_split_events("DRIFT", _bars(prices, drifting))


def test_a_latest_ratio_other_than_1_stops_the_run() -> None:
    with pytest.raises(SplitDerivationError, match="not on one share basis"):
        derive_split_events("NVDA", _bars(NVDA, lambda day, close: close / 10))


def test_an_unadjusted_session_after_the_split_adjusted_series_stops_the_run() -> None:
    """A split after the last split-adjusted bar would leave no step in the ratio."""
    bars = _bars(NVDA, _split_adjusted)
    trimmed = [
        bar if bar.session < EX_DATE else SeriesBar(bar.session, bar.unadjusted_open, bar.unadjusted_close, None)
        for bar in bars
    ]

    with pytest.raises(SplitDerivationError, match="cannot be derived"):
        derive_split_events("NVDA", trimmed)


def test_the_split_factor_counts_splits_after_the_filing_through_the_close() -> None:
    """#1257: a count filed on `after` meets a close of `through`; splits in between multiply it."""
    from fractions import Fraction

    from factors.backtest.corporate_actions import split_factor_between

    events = (SplitEvent("X", date(2024, 2, 26), 3, 1), SplitEvent("X", date(2024, 6, 10), 10, 1))

    assert split_factor_between(events, after=date(2024, 5, 29), through=date(2024, 6, 28)) == 10
    assert split_factor_between(events, after=date(2024, 1, 31), through=date(2024, 6, 28)) == 30
    assert split_factor_between(events, after=date(2024, 6, 10), through=date(2024, 6, 28)) == 1, (
        "a split on the filing date is reflected in the filing"
    )
    assert split_factor_between(events, after=date(2024, 5, 29), through=date(2024, 6, 10)) == 10, (
        "the close of the ex-date is on the new basis"
    )
    assert split_factor_between(events, after=date(2024, 5, 29), through=date(2024, 6, 7)) == 1
    assert split_factor_between(
        (SplitEvent("Y", date(2024, 3, 1), 1, 10),), after=date(2024, 1, 1), through=date(2024, 3, 1)
    ) == Fraction(1, 10)
