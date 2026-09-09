"""Module 5: the ETF virtual-company consolidation (#36 first slice, #727).

The arithmetic is checked against a hand calculation (#36: "weighted metrics match hand
calculations for at least one real fund report"), the masses against the nesting the
database also enforces, and both refusal paths against the definition's floors — a thinly
covered fund must produce no number at all rather than a confident-looking one.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from factors.base.etf_virtual_company import HoldingLine, consolidate_fund
from factors.registry import FACTOR_REGISTRY
from truealpha_contracts.etf_virtual_company import ETF_CONSOLIDATION_V0, EtfConsolidationDefinition

CUTOFF = datetime(2026, 9, 9, tzinfo=UTC)


def line(name: str, weight: str, *, listing: str | None, gap: str | None, availability: str | None, conf: str | None):
    return HoldingLine(
        holding_name=name,
        weight=Decimal(weight),
        listing_id=listing,
        valuation_gap=None if gap is None else Decimal(gap),
        availability=availability,
        confidence=None if conf is None else Decimal(conf),
    )


#: 60% valued at +0.20, 30% valued at -0.10, 7% resolved-but-unvalued, 3% unresolved.
FUND = [
    line("Valued A", "60", listing="listing:xnas:a", gap="0.20", availability="available", conf="0.90"),
    line("Valued B", "30", listing="listing:xnas:b", gap="-0.10", availability="available", conf="0.70"),
    line("No core row", "7", listing="listing:xnas:c", gap=None, availability="unavailable", conf=None),
    line("Unresolved ISIN", "3", listing=None, gap=None, availability=None, conf=None),
]


def test_registered_as_module_5_base() -> None:
    """init.md §7: "modules 1-6 are base factors ... module 7 is a composite factor".

    Base despite weighting another factor's output, because the distinction is who LOADS:
    this receives provenance-neutral tuples from the runner and never reaches into mart.
    `test_module_identity.py` enforces the same rule across the whole registry.
    """
    spec = FACTOR_REGISTRY["etf_virtual_company"]
    assert (spec.module, spec.kind) == (5, "base")


def test_weighted_gap_matches_the_hand_calculation() -> None:
    result = consolidate_fund(FUND, fund_id="etf:series:S1", as_of=CUTOFF, definition=ETF_CONSOLIDATION_V0)
    # (60 * 0.20 + 30 * -0.10) / 90 = 9 / 90 = 0.10 — the denominator is the VALUED mass,
    # not 100: the number is the average over what could be valued, and the masses say so.
    assert result.weighted_valuation_gap == Decimal("0.10")
    assert result.result.confidence == Decimal("0.70"), "composite confidence is min() of what it consumed"


def test_masses_are_nested_and_account_for_every_filed_line() -> None:
    result = consolidate_fund(FUND, fund_id="etf:series:S1", as_of=CUTOFF, definition=ETF_CONSOLIDATION_V0)
    assert (result.total_weight, result.resolved_weight, result.valued_weight) == (
        Decimal("100"),
        Decimal("97"),
        Decimal("90"),
    )
    assert result.unresolved_weight == Decimal("3"), "the identity gap stays visible"
    assert result.unvalued_weight == Decimal("7"), "the data gap stays visible and separate"
    # The property the mart table also asserts as a CHECK: nothing is lost or double-counted.
    assert result.valued_weight + result.unvalued_weight + result.unresolved_weight == result.total_weight
    assert (result.lines, result.valued_lines) == (4, 2)


def test_partial_coverage_is_flagged_not_hidden() -> None:
    result = consolidate_fund(FUND, fund_id="etf:series:S1", as_of=CUTOFF, definition=ETF_CONSOLIDATION_V0)
    assert set(result.result.flags) == {"partial_valued_mass", "unresolved_holdings"}
    assert result.result.data_availability == "unverified", "a partly-valued fund is not a verified aggregate"


def test_full_coverage_verifies() -> None:
    whole = [line("All of it", "100", listing="listing:xnas:a", gap="0.25", availability="available", conf="0.95")]
    result = consolidate_fund(whole, fund_id="etf:series:S1", as_of=CUTOFF, definition=ETF_CONSOLIDATION_V0)
    assert result.weighted_valuation_gap == Decimal("0.25")
    assert result.result.flags == [] and result.result.data_availability == "verified"


def test_thin_resolution_refuses_rather_than_publishing() -> None:
    """#36: below `minimum_resolved_weight` the aggregate is rejected, not presented."""
    thin = [
        line("Only resolved line", "40", listing="listing:xnas:a", gap="0.50", availability="available", conf="0.9"),
        line("Foreign, unresolved", "60", listing=None, gap=None, availability=None, conf=None),
    ]
    result = consolidate_fund(thin, fund_id="etf:series:S1", as_of=CUTOFF, definition=ETF_CONSOLIDATION_V0)
    assert result.weighted_valuation_gap is None, "40% resolved is below the 50% floor"
    assert result.result.flags == ["resolved_weight_below_minimum"]
    assert result.result.confidence == Decimal(0)
    # The refusal still reports what it saw: the reader learns WHY, not just that.
    assert result.resolved_weight == Decimal("40")


def test_thin_valuation_refuses_even_when_resolution_is_good() -> None:
    thin = [
        line("Valued sliver", "10", listing="listing:xnas:a", gap="0.50", availability="available", conf="0.9"),
        line("Resolved, no row", "80", listing="listing:xnas:b", gap=None, availability="unavailable", conf=None),
        line("Unresolved", "10", listing=None, gap=None, availability=None, conf=None),
    ]
    result = consolidate_fund(thin, fund_id="etf:series:S1", as_of=CUTOFF, definition=ETF_CONSOLIDATION_V0)
    assert result.weighted_valuation_gap is None, "10% valued is below the 20% floor"
    assert result.result.flags == ["valued_weight_below_minimum"]


def test_definition_is_content_addressed() -> None:
    """Two rows are comparable only under the same definition (#59's versioning rule)."""
    looser = EtfConsolidationDefinition(
        factor_version="v0", minimum_resolved_weight=Decimal("10"), minimum_valued_weight=Decimal("5")
    )
    assert looser.content_sha256 != ETF_CONSOLIDATION_V0.content_sha256
    # ...and a looser definition publishes where v0 refuses, which is exactly why the row
    # records which definition produced it.
    thin = [
        line("Valued sliver", "10", listing="listing:xnas:a", gap="0.50", availability="available", conf="0.9"),
        line("Resolved, no row", "85", listing="listing:xnas:b", gap=None, availability="unavailable", conf=None),
    ]
    assert consolidate_fund(thin, fund_id="f", as_of=CUTOFF, definition=ETF_CONSOLIDATION_V0).result.value is None
    assert consolidate_fund(thin, fund_id="f", as_of=CUTOFF, definition=looser).result.value == Decimal("0.50")


def test_a_line_with_no_weight_is_not_counted_as_zero() -> None:
    """A filing line whose pctVal did not parse must not silently enter the denominator."""
    with_null = [*FUND, HoldingLine("No pctVal", None, "listing:xnas:d", Decimal("9.0"), "available", Decimal("0.9"))]
    result = consolidate_fund(with_null, fund_id="f", as_of=CUTOFF, definition=ETF_CONSOLIDATION_V0)
    assert result.total_weight == Decimal("100"), "an unparsed weight adds nothing to any mass"
    assert result.weighted_valuation_gap == Decimal("0.10"), "and cannot move the aggregate"


@pytest.mark.parametrize(
    "empty", [[], [line("Unresolved", "100", listing=None, gap=None, availability=None, conf=None)]]
)
def test_a_fund_with_nothing_to_value_refuses(empty) -> None:
    result = consolidate_fund(empty, fund_id="f", as_of=CUTOFF, definition=ETF_CONSOLIDATION_V0)
    assert result.weighted_valuation_gap is None
    assert result.result.flags == ["resolved_weight_below_minimum"]
