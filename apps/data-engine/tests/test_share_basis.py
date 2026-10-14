"""The live share-basis rule (#1262): a filed count on the share basis of a captured close.

`share_basis.live_on_close_basis` takes the splits from the two stored daily series, classes the
count only when its class matters, and fails closed with a named reason. These tests build the
series in memory and run the real split derivation. No database.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from data_engine.datahub.market_prices import is_xnys_trading_day
from data_engine.datahub.price_series import DailySeries
from data_engine.datahub.production_topt.concept_mapping import DEFAULT_RULESET
from data_engine.datahub.share_basis import (
    CLOSE_JUMP_BEYOND_SERIES,
    LISTINGS_DISAGREE,
    NO_SPLIT_ADJUSTED_SERIES,
    NO_UNADJUSTED_SERIES,
    SERIES_DO_NOT_COVER,
    SHARE_BASIS_AMBIGUOUS,
    SHARE_COUNT_UNDATED,
    SPLIT_DERIVATION_FAILED,
    LiveShareCount,
    OnCloseBasis,
    SplitHistory,
    Unavailable,
    live_on_close_basis,
    on_close_basis,
    split_history,
)
from factors.backtest.corporate_actions import SplitEvent

SHARES = Decimal("10000000")
CLOSE = Decimal("40")
#: The count's own date (a cover date) and its filing date. A 10:1 split falls between them.
OWN_DATE, FILED = date(2026, 2, 20), date(2026, 3, 5)
EX_DATE = date(2026, 3, 2)
SESSION = date(2026, 3, 31)
#: The XNYS session before SESSION: series that end here trail the close by one session.
PREVIOUS_SESSION = date(2026, 3, 30)
ACCESSION = "0000123456-26-000007"
COUNT_KNOWABLE = datetime(2026, 3, 5, tzinfo=UTC)
CLOSE_KNOWABLE = datetime(2026, 3, 31, tzinfo=UTC)
#: The outcome of a count the series value: on the filed basis, as no split is known.
VALUED = OnCloseBasis(SHARES, COUNT_KNOWABLE, 1)


def _sessions(first: date, last: date) -> list[date]:
    return [
        first + timedelta(days=n)
        for n in range((last - first).days + 1)
        if is_xnys_trading_day(first + timedelta(days=n))
    ]


def _series(
    symbol: str,
    unadjusted: Callable[[date], Decimal],
    split_adjusted: Callable[[date], Decimal],
    *,
    first: date = date(2025, 12, 1),
    last: date = SESSION,
) -> tuple[DailySeries, DailySeries]:
    """Both stored series of one symbol, as `price_series.load_daily_series` returns them."""
    days = _sessions(first, last)
    return (
        {symbol: {day: (unadjusted(day), unadjusted(day)) for day in days}},
        {symbol: {day: (split_adjusted(day), split_adjusted(day)) for day in days}},
    )


def _history(symbol: str = "NVDA", *, ex_date: date | None = EX_DATE, **span: date) -> SplitHistory:
    """The history of one symbol whose two series show a 10:1 split on `ex_date` (None: no split)."""
    unadjusted, split_adjusted = _series(
        symbol,
        lambda day: CLOSE * 10 if ex_date is not None and day < ex_date else CLOSE,
        lambda _day: CLOSE,
        **span,
    )
    history = split_history(symbol, unadjusted, split_adjusted)
    assert isinstance(history, SplitHistory), history
    return history


def _flat_history(level: Decimal, *, last: date) -> SplitHistory:
    """The history of NVDA's two series at one close, with no split, from 2025-12-01 to `last`."""
    unadjusted, split_adjusted = _series("NVDA", lambda _day: level, lambda _day: level, last=last)
    history = split_history("NVDA", unadjusted, split_adjusted)
    assert isinstance(history, SplitHistory), history
    return history


def _count(entry: object | None = None, *, close: Decimal = CLOSE) -> LiveShareCount:
    return LiveShareCount(
        shares=SHARES,
        knowable_at=COUNT_KNOWABLE,
        entry={"accession": ACCESSION, "filed": FILED.isoformat(), "period_end": OWN_DATE.isoformat()}
        if entry is None
        else entry,
        close_session=SESSION,
        close_knowable_at=CLOSE_KNOWABLE,
        close=close,
    )


def _document(taxonomy: str, concept: str) -> dict[str, Any]:
    """A stored company-facts document whose one entry carries the count under `concept`."""
    entry = {"accn": ACCESSION, "end": OWN_DATE.isoformat(), "val": int(SHARES), "filed": FILED.isoformat()}
    return {"facts": {taxonomy: {concept: {"units": {"shares": [entry]}}}}}


def _no_read() -> Any:
    raise AssertionError("the stored documents and the ruleset are read only when the class of the count matters")


def test_on_close_basis_applies_the_splits_after_the_basis_date_through_the_session() -> None:
    splits = (SplitEvent("NVDA", EX_DATE, 10, 1), SplitEvent("NVDA", date(2026, 4, 6), 2, 1))

    adjusted = on_close_basis(
        SHARES,
        COUNT_KNOWABLE,
        basis_date=OWN_DATE,
        close_session=SESSION,
        close_knowable_at=CLOSE_KNOWABLE,
        splits=splits,
    )
    unchanged = on_close_basis(
        SHARES,
        COUNT_KNOWABLE,
        basis_date=EX_DATE,
        close_session=SESSION,
        close_knowable_at=CLOSE_KNOWABLE,
        splits=splits,
    )

    assert adjusted == OnCloseBasis(SHARES * 10, CLOSE_KNOWABLE, 10), "the split after the session is not applied"
    assert unchanged == OnCloseBasis(SHARES, COUNT_KNOWABLE, 1), "a count dated on the ex-date already holds it"


_CLASSED = {
    # A cover count is on the basis of its own date, before the split: it is multiplied.
    "cover": ([_document("dei", "EntityCommonStockSharesOutstanding")], OnCloseBasis(SHARES * 10, CLOSE_KNOWABLE, 10)),
    # A statement count is restated for the split before its filing: it is not multiplied again.
    "statement": ([_document("us-gaap", "CommonStockSharesOutstanding")], OnCloseBasis(SHARES, COUNT_KNOWABLE, 1)),
}


@pytest.mark.parametrize("kind", sorted(_CLASSED))
def test_a_split_between_the_count_and_its_filing_is_resolved_by_the_stored_document(kind: str) -> None:
    documents, expected = _CLASSED[kind]

    outcome = live_on_close_basis(_count(), [_history()], documents=lambda: documents, ruleset=lambda: DEFAULT_RULESET)

    assert outcome == expected


def test_a_count_no_stored_document_classes_is_unavailable_when_a_split_lies_before_its_filing() -> None:
    """The projector falls back to the filing date here. The live path does not guess a class."""
    outcome = live_on_close_basis(_count(), [_history()], documents=lambda: [], ruleset=lambda: DEFAULT_RULESET)

    assert isinstance(outcome, Unavailable) and outcome.reason == SHARE_BASIS_AMBIGUOUS, outcome


def test_the_stored_documents_are_not_read_when_the_split_follows_the_filing() -> None:
    outcome = live_on_close_basis(_count(), [_history(ex_date=date(2026, 3, 16))], documents=_no_read, ruleset=_no_read)

    assert outcome == OnCloseBasis(SHARES * 10, CLOSE_KNOWABLE, 10)


@pytest.mark.parametrize(
    ("span", "uncovered"),
    [
        ({"last": date(2026, 3, 27)}, "the series end two sessions before the close's session"),
        ({"first": date(2026, 2, 23)}, "the series start after the count's own date"),
    ],
)
def test_series_that_do_not_span_the_basis_date_to_the_close_make_the_issuer_unavailable(
    span: dict[str, date], uncovered: str
) -> None:
    outcome = live_on_close_basis(_count(), [_history(ex_date=None, **span)], documents=_no_read, ruleset=_no_read)

    assert isinstance(outcome, Unavailable) and outcome.reason == SERIES_DO_NOT_COVER, uncovered


def test_series_one_session_behind_the_close_value_the_issuer_when_the_close_holds_still() -> None:
    """In winter the market-data refresh can run before the vendor lists the day's bar (#1262)."""
    outcome = live_on_close_basis(
        _count(), [_flat_history(CLOSE, last=PREVIOUS_SESSION)], documents=_no_read, ruleset=_no_read
    )

    assert outcome == VALUED


def test_a_close_at_one_tenth_of_the_last_stored_close_is_a_jump_beyond_the_series() -> None:
    """A 10-for-1 split on the session the series do not hold yet: the issuer is excluded that night."""
    outcome = live_on_close_basis(
        _count(), [_flat_history(CLOSE * 10, last=PREVIOUS_SESSION)], documents=_no_read, ruleset=_no_read
    )

    assert isinstance(outcome, Unavailable) and outcome.reason == CLOSE_JUMP_BEYOND_SERIES, outcome
    assert "NVDA" in outcome.detail and "400" in outcome.detail and "0.1000" in outcome.detail, outcome.detail


@pytest.mark.parametrize(
    ("last_stored", "live", "expected"),
    [
        (Decimal("60"), Decimal("40"), CLOSE_JUMP_BEYOND_SERIES),  # x0.667: a 3-for-2 split, exactly
        (Decimal("59.99"), Decimal("40"), VALUED),  # just inside the lower limit
        (Decimal("40"), Decimal("60"), CLOSE_JUMP_BEYOND_SERIES),  # x1.5: the upper limit, exactly
        (Decimal("40"), Decimal("59.99"), VALUED),  # just inside the upper limit
    ],
)
def test_a_close_on_the_jump_limit_is_excluded_and_a_close_just_inside_is_valued(
    last_stored: Decimal, live: Decimal, expected: object
) -> None:
    outcome = live_on_close_basis(
        _count(close=live), [_flat_history(last_stored, last=PREVIOUS_SESSION)], documents=_no_read, ruleset=_no_read
    )

    assert (outcome.reason if isinstance(outcome, Unavailable) else outcome) == expected, outcome


def test_a_listing_without_stored_bars_takes_the_splits_of_the_issuers_other_listing() -> None:
    """Alphabet: the market-data lane stores GOOGL, not GOOG. The close can be GOOG's."""
    outcome = live_on_close_basis(
        _count(), [None, _history("GOOGL", ex_date=date(2026, 3, 16))], documents=_no_read, ruleset=_no_read
    )

    assert outcome == OnCloseBasis(SHARES * 10, CLOSE_KNOWABLE, 10)


def test_two_listings_that_show_different_splits_make_the_issuer_unavailable() -> None:
    outcome = live_on_close_basis(
        _count(),
        [_history("GOOGL", ex_date=date(2026, 3, 16)), _history("GOOG", ex_date=None)],
        documents=_no_read,
        ruleset=_no_read,
    )

    assert isinstance(outcome, Unavailable) and outcome.reason == LISTINGS_DISAGREE, outcome


def test_an_issuer_with_no_stored_bars_on_any_listing_is_unavailable() -> None:
    outcome = live_on_close_basis(_count(), [None, None], documents=_no_read, ruleset=_no_read)

    assert isinstance(outcome, Unavailable) and outcome.reason == NO_SPLIT_ADJUSTED_SERIES, outcome


@pytest.mark.parametrize(
    "entry",
    [
        {},
        {"filed": FILED.isoformat()},
        {"period_end": OWN_DATE.isoformat()},
        {"filed": "2026-13-45", "period_end": OWN_DATE.isoformat()},
    ],
)
def test_a_count_without_its_own_date_or_its_filing_date_is_unavailable(entry: dict[str, str]) -> None:
    outcome = live_on_close_basis(_count(entry), [_history(ex_date=None)], documents=_no_read, ruleset=_no_read)

    assert isinstance(outcome, Unavailable) and outcome.reason == SHARE_COUNT_UNDATED, outcome


def test_a_gap_of_one_listing_makes_the_issuer_unavailable_with_its_reason() -> None:
    gap = Unavailable(NO_SPLIT_ADJUSTED_SERIES, "GOOG: unadjusted bars without split-adjusted bars")

    outcome = live_on_close_basis(_count(), [gap, _history("GOOGL")], documents=_no_read, ruleset=_no_read)

    assert outcome == gap


def test_split_history_names_each_series_it_cannot_prove() -> None:
    unadjusted, split_adjusted = _series("NVDA", lambda _day: CLOSE, lambda _day: CLOSE)
    # Two vintages of the split-adjusted series: a tenfold step the unadjusted series does not show.
    _raw, stale_then_fresh = _series("NVDA", lambda _day: CLOSE, lambda day: CLOSE / 10 if day < EX_DATE else CLOSE)

    no_split_adjusted = split_history("NVDA", unadjusted, {})
    no_unadjusted = split_history("NVDA", {}, split_adjusted)
    failed = split_history("NVDA", unadjusted, stale_then_fresh)

    assert split_history("GOOG", unadjusted, split_adjusted) is None, "no bar of a symbol adds nothing"
    assert isinstance(no_split_adjusted, Unavailable) and no_split_adjusted.reason == NO_SPLIT_ADJUSTED_SERIES
    assert isinstance(no_unadjusted, Unavailable) and no_unadjusted.reason == NO_UNADJUSTED_SERIES
    assert isinstance(failed, Unavailable) and failed.reason == SPLIT_DERIVATION_FAILED and "NVDA" in failed.detail
