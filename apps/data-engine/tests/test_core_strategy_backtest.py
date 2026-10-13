"""#758: the nightly backtest decides each quarter-end cutoff from that cutoff's inputs only.

These tests need no database. The decision step runs the real ``evaluate_cutoff`` over the
real strategy definition. The database path is in ``test_backtest_integration.py``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from data_engine.datahub.backtest import decide, price_book, schedule_cutoffs
from data_engine.datahub.production_topt.composition import RISK_FREE_RATE
from data_engine.datahub.strategy_bridge import load_strategy_definition
from data_engine.datahub.strategy_history import HistoryIssuer
from factors.backtest.corporate_actions import SplitDerivationError, SplitEvent
from factors.composite.strategy_evaluator import IssuerInput
from factors.production_topt import OperatingBranch

Q1 = datetime(2024, 3, 31, 23, 59, 59, tzinfo=UTC)
Q2 = datetime(2024, 6, 30, 23, 59, 59, tzinfo=UTC)
Q3 = datetime(2024, 9, 30, 23, 59, 59, tzinfo=UTC)
CONFIDENCE = Decimal("0.90")


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
    )


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
    base = decide([Q1, Q2], inputs.__getitem__, ISSUERS, definition, risk_free_rate=RISK_FREE_RATE)
    inputs[Q2] = [_inputs("a", "1000"), _inputs("b", "2"), _inputs("c", "1")]
    after = decide([Q1, Q2], inputs.__getitem__, ISSUERS, definition, risk_free_rate=RISK_FREE_RATE)

    assert base[0].weights == {"T758A": Decimal("0.5"), "T758B": Decimal("0.5")}
    assert after[0] == base[0], "a Q2 input changed the Q1 decision"
    assert after[1].weights == {"T758B": Decimal("0.5"), "T758C": Decimal("0.5")}, "the Q2 change must take effect"


def test_an_issuer_without_lineage_is_excluded_and_never_traded() -> None:
    definition = load_strategy_definition()
    unknown = IssuerInput(issuer_id="issuer:t758:z", records=_inputs("a", "1").records)

    (decision,) = decide(
        [Q1], lambda _cutoff: [unknown, _inputs("b", "2")], ISSUERS, definition, risk_free_rate=RISK_FREE_RATE
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
        risk_free_rate=RISK_FREE_RATE,
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
