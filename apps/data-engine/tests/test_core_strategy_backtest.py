"""#758: the nightly backtest decides each quarter-end cutoff from that cutoff's inputs only.

These tests need no database. The decision step runs the real ``evaluate_cutoff`` over the
real strategy definition. The database path is in ``test_backtest_integration.py``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from decimal import Decimal

import pytest
from data_engine.datahub.backtest import (
    ShareBasisError,
    decide,
    price_book,
    require_current_share_basis,
    schedule_cutoffs,
)
from data_engine.datahub.risk_free import MissingRiskFreeRate
from data_engine.datahub.strategy_bridge import load_strategy_definition
from data_engine.datahub.strategy_history import HistoryIssuer
from data_engine.strategy_backtest_gateway import MarketValueVintage
from factors.backtest.corporate_actions import SplitDerivationError, SplitEvent
from factors.composite.strategy_evaluator import IssuerInput
from factors.production_topt import OperatingBranch

Q1 = datetime(2024, 3, 31, 23, 59, 59, tzinfo=UTC)
Q2 = datetime(2024, 6, 30, 23, 59, 59, tzinfo=UTC)
Q3 = datetime(2024, 9, 30, 23, 59, 59, tzinfo=UTC)
CONFIDENCE = Decimal("0.90")
#: The rate of every cutoff in the tests that do not study the rate. The caller measures it (#1255).
RATE = Decimal("0.05")
#: Every input of these issuers became knowable on this day. It is under 214 days before Q3, inside the age limit.
KNOWN = datetime(2024, 3, 1, tzinfo=UTC)


def _issuer(name: str) -> HistoryIssuer:
    return HistoryIssuer(
        issuer_id=f"issuer:t758:{name}",
        instrument_id=f"security:t758:{name}",
        listing_id=f"listing:xnas:t758{name}",
        ticker=f"T758{name.upper()}",
        cik=758_000 + ord(name),
        operating_branch=OperatingBranch.NON_FINANCIAL,
    )


ISSUERS = {issuer.issuer_id: issuer for issuer in (_issuer("a"), _issuer("b"), _issuer("c"))}


def _inputs(name: str, last_close: str) -> IssuerInput:
    """A large-model-native issuer: 9.5M gross profit per employee; P/S is 0.05 at a close of 1."""
    values = {
        "gross_profit": "10000000000",
        "total_assets": "10000000000",
        "headcount": "1000",
        "revenue": "20000000000",
        "shares_outstanding": "1000000000",
        "last_close": last_close,
    }
    return IssuerInput(
        issuer_id=f"issuer:t758:{name}",
        records={key: (Decimal(value), CONFIDENCE) for key, value in values.items()},
        knowable_at=dict.fromkeys(values, KNOWN),
    )


def _rate_of(rate: Decimal):
    return lambda _cutoff: rate


def test_each_cutoff_is_decided_with_the_rate_of_that_cutoff() -> None:
    """#1255: the rate decides the tier, and the tier decides the band.

    The issuer earns 10M of gross profit on 10M of assets per 1,000 employees. So its labor
    efficiency is 10M x (1 - rate) per employee. At a rate of 0.05 that is 9.5M, the large-model-native tier, whose
    P/GP band is 26.67 to 40. At a rate of 0.95 it is 0.5M, the traditional tier, whose band is 7.50
    to 10.00. The close of 200 gives a P/GP of 20: below the first band and above the second. Red when
    `decide` takes one rate for every cutoff, or any rate but the one that `rate_at` names.
    """
    definition = load_strategy_definition()
    inputs = {Q1: [_inputs("a", "200")], Q2: [_inputs("a", "200")]}
    rates = {Q1: Decimal("0.05"), Q2: Decimal("0.95")}

    first, second = decide([Q1, Q2], inputs.__getitem__, ISSUERS, definition, rate_at=rates.__getitem__)

    assert first.weights == {"T758A": Decimal("1")} and first.outcomes == {"selected": 1}
    assert second.weights == {} and second.outcomes == {"rejected_valuation_above_tier_band": 1}
    swapped = {Q1: Decimal("0.95"), Q2: Decimal("0.05")}
    third, fourth = decide([Q1, Q2], inputs.__getitem__, ISSUERS, definition, rate_at=swapped.__getitem__)
    assert (third.weights, fourth.weights) == (second.weights, first.weights)


def test_a_cutoff_without_a_rate_stops_the_decision() -> None:
    """`rate_at` is the caller's measurement. When it raises, no decision is made for a guessed rate."""
    definition = load_strategy_definition()

    def rate_at(_cutoff: datetime) -> Decimal:
        raise MissingRiskFreeRate(Q1, None)

    with pytest.raises(MissingRiskFreeRate):
        decide([Q1], lambda _cutoff: [_inputs("a", "1")], ISSUERS, definition, rate_at=rate_at)


def test_a_change_to_a_later_cutoff_changes_no_earlier_decision() -> None:
    """No lookahead in the decision step.

    At Q2, issuer a becomes expensive and issuer c becomes cheap. The Q1 decision must not see
    it. Red when the decision step reads any cutoff other than its own (for example the latest).
    """
    definition = load_strategy_definition()
    inputs = {
        Q1: [_inputs("a", "1"), _inputs("b", "2"), _inputs("c", "1000")],
        Q2: [_inputs("a", "1"), _inputs("b", "2"), _inputs("c", "1000")],
    }
    base = decide([Q1, Q2], inputs.__getitem__, ISSUERS, definition, rate_at=_rate_of(RATE))
    inputs[Q2] = [_inputs("a", "1000"), _inputs("b", "2"), _inputs("c", "1")]
    after = decide([Q1, Q2], inputs.__getitem__, ISSUERS, definition, rate_at=_rate_of(RATE))

    assert base[0].weights == {"T758A": Decimal("0.5"), "T758B": Decimal("0.5")}
    assert after[0] == base[0], "a Q2 input changed the Q1 decision"
    assert after[1].weights == {"T758B": Decimal("0.5"), "T758C": Decimal("0.5")}, "the Q2 change must take effect"


def test_an_issuer_without_lineage_is_excluded_and_never_traded() -> None:
    definition = load_strategy_definition()
    known = _inputs("a", "1")
    unknown = IssuerInput(issuer_id="issuer:t758:z", records=known.records, knowable_at=known.knowable_at)

    (decision,) = decide(
        [Q1], lambda _cutoff: [unknown, _inputs("b", "2")], ISSUERS, definition, rate_at=_rate_of(RATE)
    )

    assert decision.weights == {"T758B": Decimal("1")}
    assert decision.outcomes["missing_issuer_class"] == 1


class _Rows:
    def __init__(self, rows: list[tuple[datetime]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple[datetime]]:
        return self._rows


class _Connection:
    def __init__(self, cutoffs: list[datetime]) -> None:
        self._cutoffs = cutoffs

    def execute(self, _sql: str, _params: object = None) -> _Rows:
        return _Rows([(cutoff,) for cutoff in self._cutoffs])


def test_schedule_cutoffs_keep_the_quarter_ends_and_drop_monthly_and_live_cutoffs() -> None:
    schedule = load_strategy_definition().schedule
    stored = [Q2, datetime(2024, 4, 1, tzinfo=UTC), Q1, datetime(2024, 5, 7, 22, 15, tzinfo=UTC), Q3]

    assert schedule_cutoffs(_Connection(stored), schedule) == [Q1, Q2, Q3]  # type: ignore[arg-type]


def test_a_missing_quarter_between_the_first_and_the_latest_stops_the_run() -> None:
    schedule = load_strategy_definition().schedule

    # The message holds at both call sites, before and after canonicalization (review on #1266).
    with pytest.raises(
        ValueError, match=r"quarter-end cutoffs are not contiguous: \['2024-06-30T23:59:59\+00:00'\] are missing"
    ):
        schedule_cutoffs(_Connection([Q1, Q3]), schedule)  # type: ignore[arg-type]


def test_the_price_book_takes_split_events_from_the_two_series() -> None:
    before, ex_date = date(2024, 6, 7), date(2024, 6, 10)
    unadjusted = {
        "NVDA": {before: (Decimal("1197.70"), Decimal("1208.88")), ex_date: (Decimal("120.37"), Decimal("121.79"))}
    }
    split_adjusted = {
        "NVDA": {before: (Decimal("119.770"), Decimal("120.888")), ex_date: (Decimal("120.37"), Decimal("121.79"))}
    }

    book = price_book(unadjusted, split_adjusted)

    assert book.splits == (SplitEvent("NVDA", ex_date, 10, 1),)
    assert book.bars["NVDA"][ex_date].open == Decimal("120.37"), "the book prices trades on unadjusted bars"


def test_the_price_book_refuses_a_split_it_cannot_confirm() -> None:
    day, next_day = date(2024, 6, 7), date(2024, 6, 10)
    unadjusted = {"FLAT": {day: (Decimal("100"), Decimal("100")), next_day: (Decimal("100"), Decimal("100"))}}
    split_adjusted = {"FLAT": {day: (Decimal("10"), Decimal("10")), next_day: (Decimal("100"), Decimal("100"))}}

    with pytest.raises(SplitDerivationError):
        price_book(unadjusted, split_adjusted)


def test_strategy_weights_sum_to_exactly_one_when_six_are_selected() -> None:
    """Audit finding 1 on the strategy side: six weights of 0.166667 sum to 1.000002, and the engine
    refuses a target above 1. The decision step gives the remainder to the last symbol instead."""
    from factors.backtest.engine import TargetPortfolio

    definition = load_strategy_definition()
    wider = definition.model_copy(update={"selection": definition.selection.model_copy(update={"selection_count": 6})})
    names = ["a", "b", "c", "d", "e", "f"]
    issuers = {issuer.issuer_id: issuer for issuer in (_issuer(name) for name in names)}

    (decision,) = decide(
        [Q1],
        lambda _cutoff: [_inputs(name, str(index + 1)) for index, name in enumerate(names)],
        issuers,
        wider,
        rate_at=_rate_of(RATE),
    )

    assert len(decision.weights) == 6
    assert sum(decision.weights.values()) == 1
    assert sorted(decision.weights.values())[-1] == Decimal("0.166667")
    TargetPortfolio(Q1, decision.weights)


def test_unadjusted_bars_without_a_split_adjusted_series_stop_split_derivation() -> None:
    """#1257: without the second series no split can be seen, so no share count or return can rely on it."""
    from data_engine.datahub.price_series import split_events_of

    unadjusted = {"ONE": {date(2024, 6, 7): (Decimal("100"), Decimal("100"))}}

    with pytest.raises(SplitDerivationError, match="without split-adjusted bars"):
        split_events_of("ONE", unadjusted, {})
    assert split_events_of("NONE", {}, {}) == (), "a symbol with no bar has no split"


def test_the_price_book_names_every_symbol_without_derivable_splits() -> None:
    """Review round 3, item 3c: two symbols fail split derivation in one book. The error names both;
    a book that stops at the first symbol hides the second until the next run."""
    day, next_day = date(2024, 6, 7), date(2024, 6, 10)
    flat = {day: (Decimal("100"), Decimal("100")), next_day: (Decimal("100"), Decimal("100"))}
    unadjusted = {"ALFA": flat, "BRAV": flat, "GOOD": flat}
    split_adjusted = {
        "ALFA": {day: (Decimal("10"), Decimal("10")), next_day: (Decimal("100"), Decimal("100"))},
        "GOOD": flat,
    }

    with pytest.raises(SplitDerivationError) as raised:
        price_book(unadjusted, split_adjusted)

    message = str(raised.value)
    assert "ALFA" in message and "BRAV" in message
    assert "GOOD" not in message


# -- the share-basis check (#1270) -------------------------------------------------------------

NVDA = "issuer:t1270:nvda"
NFLX = "issuer:t1270:nflx"
NVDA_SPLIT = SplitEvent("NVDA", date(2024, 6, 10), 10, 1)
Q4_2025 = datetime(2025, 12, 31, 23, 59, 59, tzinfo=UTC)


def _vintage(shares_known: date, close_known: date, issuer_id: str = NVDA) -> MarketValueVintage:
    """A filing is knowable at the end of its filed day, as the projector stamps it."""
    return MarketValueVintage(
        issuer_id,
        datetime.combine(shares_known, time.max, tzinfo=UTC),
        datetime.combine(close_known, time.max, tzinfo=UTC),
    )


def test_a_share_count_known_before_a_split_that_its_close_follows_stops_the_run() -> None:
    """Staging, 2024-06-30: NVDA held 2,460,000,000 shares (filed 2024-05-29) beside a post-split close.
    Red when the check does not raise, or when its message omits the symbol, the cutoff, the split
    or the job that corrects the row."""
    vintages = {Q2: [_vintage(date(2024, 5, 29), date(2024, 6, 28))]}

    with pytest.raises(ShareBasisError) as raised:
        require_current_share_basis(vintages, {NVDA: "NVDA"}, [NVDA_SPLIT])

    message = str(raised.value)
    assert "NVDA at 2024-06-30T23:59:59+00:00" in message
    assert "10/1 split on 2024-06-10" in message
    assert "share count known 2024-05-29, close known 2024-06-28" in message
    assert "strategy_history_projection_pipeline" in message


@pytest.mark.parametrize(
    ("shares_known", "close_known", "stale"),
    [
        (date(2024, 5, 29), date(2024, 6, 28), True),  # the count is older than the split, the close follows it
        (date(2024, 6, 9), date(2024, 6, 10), True),  # the close is on the ex-date: the split is in
        (date(2024, 6, 10), date(2024, 6, 28), False),  # a count known on the ex-date is taken as split-adjusted
        (date(2024, 6, 11), date(2024, 6, 28), False),  # the filing follows the split
        (date(2024, 6, 28), date(2024, 6, 28), False),  # the corrected row: knowable at the close
        (date(2024, 5, 29), date(2024, 6, 7), False),  # the close precedes the split: one share basis
    ],
)
def test_a_split_after_the_count_and_on_or_before_the_close_makes_the_count_stale(
    shares_known: date, close_known: date, stale: bool
) -> None:
    vintages = {Q2: [_vintage(shares_known, close_known)]}

    if stale:
        with pytest.raises(ShareBasisError):
            require_current_share_basis(vintages, {NVDA: "NVDA"}, [NVDA_SPLIT])
    else:
        require_current_share_basis(vintages, {NVDA: "NVDA"}, [NVDA_SPLIT])


def test_only_the_splits_of_the_issuers_own_symbol_count() -> None:
    """NVDA's split is no reason to stop a run on an issuer that trades as AMZN."""
    vintages = {Q2: [_vintage(date(2024, 5, 29), date(2024, 6, 28))]}

    require_current_share_basis(vintages, {NVDA: "AMZN"}, [NVDA_SPLIT])


def test_a_row_under_an_id_that_is_not_a_canonical_issuer_is_ignored() -> None:
    """The projector writes canonical ids only, so it never corrects a legacy row. Reading one would
    stop every later run for good."""
    vintages = {Q2: [_vintage(date(2024, 5, 29), date(2024, 6, 28), "issuer:lei:legacy")]}

    require_current_share_basis(vintages, {NVDA: "NVDA"}, [NVDA_SPLIT])


def test_one_error_names_every_failing_symbol_and_cutoff_and_every_split_between() -> None:
    """Review of `split_events_by_symbol`: an error that stops at the first symbol hides the others
    until the next run. Here NVDA fails at two cutoffs (the second with two splits) and NFLX at one."""
    splits = [NVDA_SPLIT, SplitEvent("NVDA", date(2024, 8, 1), 3, 2), SplitEvent("NFLX", date(2025, 11, 17), 10, 1)]
    vintages = {
        Q2: [_vintage(date(2024, 5, 29), date(2024, 6, 28))],
        Q3: [_vintage(date(2024, 5, 29), date(2024, 9, 27))],
        Q4_2025: [
            _vintage(date(2025, 10, 30), date(2025, 12, 30), NFLX),
            _vintage(date(2025, 12, 30), date(2025, 12, 30)),
        ],
    }

    with pytest.raises(ShareBasisError) as raised:
        require_current_share_basis(vintages, {NVDA: "NVDA", NFLX: "NFLX"}, splits)

    message = str(raised.value)
    assert "(3 found)" in message
    assert (
        "NVDA at 2024-06-30T23:59:59+00:00 (share count known 2024-05-29, close known 2024-06-28, 10/1 split" in message
    )
    assert "NVDA at 2024-09-30T23:59:59+00:00" in message
    assert "10/1 split on 2024-06-10, 3/2 split on 2024-08-01)" in message
    assert "NFLX at 2025-12-31T23:59:59+00:00" in message
    assert "NVDA at 2025-12-31" not in message, "the corrected NVDA row at the last cutoff passes"


# -- #1265: a selected symbol without an open -------------------------------------------------


def _selected(cutoff: datetime, *symbols: str):
    from data_engine.datahub.backtest import CutoffDecision

    return CutoffDecision(cutoff, {symbol: Decimal("0.5") for symbol in symbols}, symbols, len(symbols), {})


def test_two_cutoffs_of_one_unfilled_selection_give_one_line_with_both_dates() -> None:
    """GOOG is selected at Q1 and Q2 and has no open on either fill session. HELD has no open at Q2,
    but no cutoff selects it: a held symbol stays held, so no weight of it stayed cash."""
    from data_engine.datahub.backtest import LIMITATIONS, run_limitations, selected_without_open
    from factors.backtest.engine import Fill, Simulation

    decisions = [_selected(Q1, "GOOG", "AMZN"), _selected(Q2, "GOOG", "AMZN"), _selected(Q3, "AMZN", "MSFT")]
    fills = (
        Fill(Q1, date(2024, 4, 1), ("GOOG",)),
        Fill(Q2, date(2024, 7, 1), ("GOOG", "HELD")),
        Fill(Q3, date(2024, 10, 1), ()),
    )

    unfilled = selected_without_open(decisions, Simulation((), (), fills, ()))

    assert unfilled == {"GOOG": (date(2024, 3, 31), date(2024, 6, 30))}
    assert run_limitations((), unfilled)[len(LIMITATIONS) :] == (
        "The strategy selected GOOG at 2024-03-31, 2024-06-30, but no open existed on the fill session, "
        "so the weight it could not buy stayed cash.",
    )


def test_a_run_whose_selections_all_open_adds_nothing_to_the_limitations() -> None:
    from data_engine.datahub.backtest import LIMITATIONS, run_limitations, selected_without_open
    from factors.backtest.engine import Fill, Simulation

    fills = (Fill(Q1, date(2024, 4, 1), ()), Fill(Q2, None))

    unfilled = selected_without_open([_selected(Q1, "GOOG"), _selected(Q2, "GOOG")], Simulation((), (), fills, ()))

    assert unfilled == {}
    assert run_limitations((), unfilled) == LIMITATIONS


def test_the_benchmark_line_is_unchanged_and_the_strategy_lines_follow_it() -> None:
    from data_engine.datahub.backtest import LIMITATIONS, run_limitations

    limitations = run_limitations(("T758E", "T758F"), {"MSFT": (date(2024, 9, 30),), "GOOG": (date(2024, 3, 31),)})

    assert limitations[len(LIMITATIONS) :] == (
        "These benchmark symbols had no open after the first fill, so their weight stayed cash for the whole run: "
        "T758E, T758F.",
        "The strategy selected GOOG at 2024-03-31, but no open existed on the fill session, "
        "so the weight it could not buy stayed cash.",
        "The strategy selected MSFT at 2024-09-30, but no open existed on the fill session, "
        "so the weight it could not buy stayed cash.",
    )
