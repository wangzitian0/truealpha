"""Coverage for the external-oracle layer (#429).

Note what these tests can and cannot do. They prove the oracle's selection rule is the
independent one and that its SQL is valid — both are properties of THIS code, so a
fixture is the right instrument. They deliberately do NOT assert any captured value is
correct: that assertion only has meaning against a real database and a live vendor, which
is `scripts/assert_data_invariants.py`, not pytest. A test that seeded its own warehouse
and then checked the warehouse would be the exact self-referential loop this layer exists
to break.
"""

from __future__ import annotations

import os
from datetime import date
from decimal import Decimal

import psycopg
import pytest
from data_engine.config import settings
from data_engine.quality.invariants import INVARIANTS, check
from data_engine.quality.vendor_oracle import Drift, gross_profit, latest_across_variants, period_reporting

_CUTOFF = date(2026, 7, 27)


def _annual(end: str, start: str, val: object, filed: str) -> dict:
    return {"end": end, "start": start, "val": val, "filed": filed}


def _facts(**concepts: list[dict]) -> dict:
    return {"facts": {"us-gaap": {name: {"units": {"USD": rows}} for name, rows in concepts.items()}}}


# -- the rule that separates the oracle from the code it audits -------------------------


def test_latest_period_wins_across_variants_not_declaration_order() -> None:
    """The AAPL case, reduced: a legacy tag stops in 2018, the modern tag continues.

    The adapter returns the first variant carrying any value, so declaration order decides
    and the abandoned tag wins forever. The oracle must follow the issuer instead.
    """
    facts = _facts(
        Revenues=[_annual("2018-09-29", "2017-10-01", 265595000000, "2018-11-05")],
        RevenueFromContractWithCustomerExcludingAssessedTax=[
            _annual("2025-09-27", "2024-09-29", 416161000000, "2025-10-31")
        ],
    )
    found = latest_across_variants(
        facts, ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax"), "USD", _CUTOFF
    )
    assert found is not None
    assert found.value == Decimal("416161000000")
    assert found.period_end == date(2025, 9, 27)


def test_declaration_order_does_not_change_the_answer() -> None:
    """Reordering the variant tuple must not move the result — otherwise the oracle has
    the same order-dependence it exists to detect."""
    facts = _facts(
        Revenues=[_annual("2018-09-29", "2017-10-01", 265595000000, "2018-11-05")],
        SalesRevenueNet=[_annual("2025-09-27", "2024-09-29", 416161000000, "2025-10-31")],
    )
    forward = latest_across_variants(facts, ("Revenues", "SalesRevenueNet"), "USD", _CUTOFF)
    reverse = latest_across_variants(facts, ("SalesRevenueNet", "Revenues"), "USD", _CUTOFF)
    assert forward is not None and reverse is not None
    assert forward.value == reverse.value == Decimal("416161000000")


def test_a_stale_reported_gross_profit_loses_to_a_current_derivation() -> None:
    """The AMZN case: `GrossProfit` last tagged for FY2009, FY2025 computable from
    revenue and cost tags that are both present."""
    facts = _facts(
        GrossProfit=[_annual("2009-12-31", "2009-01-01", 5531000000, "2010-01-29")],
        RevenueFromContractWithCustomerExcludingAssessedTax=[
            _annual("2025-12-31", "2025-01-01", 716924000000, "2026-02-06")
        ],
        CostOfGoodsAndServicesSold=[_annual("2025-12-31", "2025-01-01", 356414000000, "2026-02-06")],
    )
    found = gross_profit(facts, _CUTOFF)
    assert found is not None
    assert found.period_end == date(2025, 12, 31)
    assert found.value == Decimal("360510000000")


def test_a_current_reported_gross_profit_beats_an_older_derivation() -> None:
    """The rule is 'later period wins', not 'derivation always wins'."""
    facts = _facts(
        GrossProfit=[_annual("2025-12-31", "2025-01-01", 195201000000, "2026-02-01")],
        Revenues=[_annual("2024-12-31", "2024-01-01", 900, "2025-02-01")],
        CostOfRevenue=[_annual("2024-12-31", "2024-01-01", 400, "2025-02-01")],
    )
    found = gross_profit(facts, _CUTOFF)
    assert found is not None
    assert found.value == Decimal("195201000000")


def test_facts_filed_after_the_cutoff_are_not_knowable() -> None:
    facts = _facts(
        Revenues=[
            _annual("2024-12-31", "2024-01-01", 100, "2025-02-01"),
            _annual("2025-12-31", "2025-01-01", 200, "2026-08-01"),  # filed after cutoff
        ]
    )
    found = latest_across_variants(facts, ("Revenues",), "USD", _CUTOFF)
    assert found is not None
    assert found.value == Decimal("100")


def test_quarterly_spans_never_win_over_annual_ones() -> None:
    facts = _facts(
        Revenues=[
            _annual("2025-12-31", "2025-01-01", 900, "2026-02-01"),
            _annual("2026-03-31", "2026-01-01", 250, "2026-04-20"),  # a quarter
        ]
    )
    found = latest_across_variants(facts, ("Revenues",), "USD", _CUTOFF)
    assert found is not None
    assert found.value == Decimal("900")


def test_an_issuer_that_reports_nothing_yields_nothing() -> None:
    assert latest_across_variants(_facts(), ("Revenues",), "USD", _CUTOFF) is None
    assert gross_profit(_facts(), _CUTOFF) is None


# -- the invariant SQL is valid and runs -----------------------------------------------


@pytest.fixture
def connection():
    try:
        active = psycopg.connect(settings.database_url, connect_timeout=3, autocommit=False)
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs the required integration coverage")
    try:
        active.execute("select 1")
        yield active
    finally:
        active.rollback()
        active.close()


def test_every_invariant_executes_and_returns_a_count(connection) -> None:
    """Validity only. An empty CI database satisfies every invariant vacuously, which is
    precisely why passing here is not evidence about production data."""
    results = check(connection)
    assert len(results) == len(INVARIANTS)
    assert all(item.violations >= 0 for item in results)


def test_each_invariant_declares_why_it_needs_no_domain_knowledge() -> None:
    for invariant in INVARIANTS:
        assert invariant.self_evident_because, f"{invariant.id} must say why it is self-evident"
        assert invariant.statement


def test_staleness_is_measured_from_the_period_mart_actually_used() -> None:
    """A stale-tag defect and a restatement look identical in a diff; only the period mart's
    value came from separates them, and the payload does not record it — so it is recovered
    by matching the value back to a vendor period."""
    facts = _facts(
        Revenues=[_annual("2018-09-29", "2017-10-01", 265595000000, "2018-11-05")],
        RevenueFromContractWithCustomerExcludingAssessedTax=[
            _annual("2025-09-27", "2024-09-29", 416161000000, "2025-10-31")
        ],
    )
    concepts = ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax")
    drift = Drift(
        ticker="AAPL",
        field="revenue",
        mart_value=Decimal("265595000000"),
        vendor=latest_across_variants(facts, concepts, "USD", _CUTOFF),
        cutoff=_CUTOFF,
        mart_period_end=period_reporting(facts, concepts, Decimal("265595000000"), _CUTOFF),
    )
    assert not drift.agrees
    assert drift.mart_period_end == date(2018, 9, 29)
    assert drift.staleness_years == 7


def test_a_value_matching_no_vendor_period_claims_no_staleness() -> None:
    facts = _facts(Revenues=[_annual("2025-12-31", "2025-01-01", 900, "2026-02-01")])
    assert period_reporting(facts, ("Revenues",), Decimal("12345"), _CUTOFF) is None
