"""#1443 acceptance 1: replay the real production TOPT ticks of 2026-10-07 to 2026-10-13.

What happened (`fixtures/mu_2026_10_topt_ticks.json`, read from production with a read-only
session): Micron's served revenue was FY2025 (37.378B, period end 2025-08-28) until SEC's 10-K of
2026-10-09, while the second financial-fact origin reported FY2026 (133.188B, period end
2026-09-03) from 2026-09-30. Policy v2 accepted the price-to-sales of 31.30 on 10-08, then refused
the correction to 8.74 on every tick from 10-09 to 10-12 because it compared each tick with the
previous accepted run. The head stayed on 10-08 for four nights.

The replay runs policy v4 over the same inputs with the tick's head semantics: a run that no run
rule refuses becomes the previous accepted run of the next tick. The ticks the gate refused
materialized nothing, so for them the fixture keeps Micron's inputs only, and its price-to-sales
is derived as `compute_topt_core` derives it: market value over revenue.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from pathlib import Path
from typing import Any

import pytest
from factors.composite import plausibility_policy as policy
from factors.composite.plausibility_policy import IndependentReference, Row, Violation

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "mu_2026_10_topt_ticks.json"
MU = "b38753a7-a326-5868-8b26-b9e7ded7620b"
FY2026 = date(2026, 9, 3)


def _decimal(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def _date(value: str | None) -> date | None:
    return None if value is None else date.fromisoformat(value)


def _derived_ps(item: dict[str, Any]) -> Decimal:
    """Market value over revenue in the 34-digit context `compute_topt_core` computes in."""
    fact = item["financial_fact"]
    with localcontext(Context(prec=34, rounding=ROUND_HALF_EVEN)):
        market_value = Decimal(item["market_price"]["close"]) * Decimal(fact["shares_outstanding"])
        return market_value / Decimal(fact["revenue"])


def _row(item: dict[str, Any]) -> Row:
    fact, published, second = item["financial_fact"], item["published"], item["second_origin"]
    references: tuple[IndependentReference, ...] = ()
    if second is not None and second["currency"] == fact["currency"]:
        references = (
            IndependentReference(
                reference_id=second["observation_id"],
                figures={
                    date.fromisoformat(end): {name: _decimal(value) for name, value in values.items()}
                    for end, values in second["by_period_end"].items()
                },
            ),
        )
    return Row(
        listing_id=item["listing_id"],
        operating_branch=item["operating_branch"],
        # A refused tick stored no row; Micron's inputs on those ticks are complete (revenue,
        # shares and a close), the same inputs that made it available on 10-08 and 10-13.
        availability=published["availability"] if published else "available",
        operating_efficiency=_decimal(published["operating_efficiency"]) if published else None,
        current_ps=_decimal(published["current_ps"]) if published else _derived_ps(item),
        last_close=Decimal(item["market_price"]["close"]),
        revenue_period_end=_date(fact["revenue_period_end"]),
        revenue=_decimal(fact["revenue"]),
        gross_profit=_decimal(fact["gross_profit"]),
        operating_period_end=_date(fact["operating_period_end"]),
        gross_profit_is_revenue_proxy=fact["gross_profit_is_revenue_proxy"],
        references=references,
    )


@dataclass(frozen=True)
class Tick:
    cutoff: str
    materialized: bool
    rows: list[Row]
    refusals: list[Violation]
    reviews: list[Violation]


def _replay() -> list[Tick]:
    document = json.loads(FIXTURE.read_text())
    previous: list[Row] = []
    replayed: list[Tick] = []
    for tick in document["ticks"]:
        rows = [_row(item) for item in tick["rows"]]
        refusals = policy.evaluate(rows)
        replayed.append(Tick(tick["cutoff"][:10], tick["materialized"], rows, refusals, policy.reviews(rows, previous)))
        if not any(violation.rule in policy.RUN_RULES for violation in refusals):
            previous = rows
    return replayed


@pytest.fixture(scope="module")
def ticks() -> dict[str, Tick]:
    return {tick.cutoff: tick for tick in _replay()}


def _mu(tick: Tick) -> Row:
    (row,) = [row for row in tick.rows if row.listing_id == MU]
    return row


def test_the_fixture_is_the_real_sequence(ticks: dict[str, Tick]) -> None:
    """Preconditions, so the verdicts below cannot pass on a fixture that lost its shape."""
    assert list(ticks) == [
        "2026-10-07",
        "2026-10-08",
        "2026-10-09",
        "2026-10-10",
        "2026-10-11",
        "2026-10-12",
        "2026-10-13",
    ]
    assert [tick.materialized for tick in ticks.values()] == [True, True, False, False, False, False, True]
    assert [len(tick.rows) for tick in ticks.values()] == [20, 20, 1, 1, 1, 1, 20]
    # The reference judges 17 of 20 rows on every materialized tick. It reports no revenue or gross
    # profit for the bank and the insurer, and XOM serves no revenue (its row is unavailable).
    for tick in ticks.values():
        judged = policy.judged_listings(tick.rows)
        unjudged = sorted(
            (row.operating_branch, row.revenue is None) for row in tick.rows if row.listing_id not in judged
        )
        expected = [("financial", False), ("insurance", False), ("non_financial", True)] if tick.materialized else []
        assert (len(judged), unjudged) == ((17, expected) if tick.materialized else (1, [])), tick.cutoff
    # the derivation used for the refused ticks reproduces the stored price-to-sales exactly
    document = json.loads(FIXTURE.read_text())
    for tick in document["ticks"]:
        for item in tick["rows"]:
            if item["listing_id"] == MU and item["published"]:
                assert _derived_ps(item) == Decimal(item["published"]["current_ps"])
    assert _mu(ticks["2026-10-08"]).current_ps == Decimal("31.29837341569479372893145700679544")
    assert _mu(ticks["2026-10-09"]).current_ps == Decimal("8.741286641048743130011712766915938")


def test_the_wrong_value_is_refused_where_the_reference_contradicts_it(ticks: dict[str, Tick]) -> None:
    """The served FY2025 revenue is superseded by the reference's FY2026 figure: Micron is refused
    on 10-07 and on 10-08, where the 31.30 appeared, and no other issuer is refused."""
    for cutoff in ("2026-10-07", "2026-10-08"):
        tick = ticks[cutoff]
        assert {(v.rule, v.listing_id) for v in tick.refusals} == {(policy.RULE_INDEPENDENT_REFERENCE, MU)}, cutoff
        revenue = next(v for v in tick.refusals if v.detail.startswith("revenue"))
        assert "revenue 37,378,000,000 is for the period ending 2025-08-28" in revenue.detail
        assert "reports 133,188,000,000 for the later period ending 2026-09-03 (3.56x)" in revenue.detail
        # one issuer of twenty: the run itself is not refused, so the other nineteen publish
        assert not any(v.rule in policy.RUN_RULES for v in tick.refusals)


def test_the_correction_is_accepted_and_the_head_advances_every_night(ticks: dict[str, Tick]) -> None:
    """From 10-09 the served revenue is FY2026 and agrees with the reference: nothing is refused on
    any later tick, so the head advances every night instead of staying on 10-08 for four."""
    for cutoff in ("2026-10-09", "2026-10-10", "2026-10-11", "2026-10-12", "2026-10-13"):
        tick = ticks[cutoff]
        assert _mu(tick).revenue_period_end == FY2026 and _mu(tick).revenue == Decimal("133188000000")
        assert tick.refusals == [], cutoff
    # the 31.30 → 8.74 step on 10-09 is a new fiscal year: not even a review flag
    assert ticks["2026-10-09"].reviews == []
    assert _mu(ticks["2026-10-13"]).current_ps == Decimal("8.097285727289845932065951887557438")


def test_no_refusal_in_the_replay_reads_the_previous_run(ticks: dict[str, Tick]) -> None:
    """#1443 acceptance 2 on the real sequence: every refusal comes from an issuer or run rule."""
    for tick in ticks.values():
        for violation in tick.refusals:
            assert violation.rule in policy.RULES and violation.rule not in policy.REVIEWS
            assert "versus the previous run" not in violation.detail
