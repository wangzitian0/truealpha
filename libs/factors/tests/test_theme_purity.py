"""Module 6: theme purity (#772, init.md §0 question 6).

The arithmetic is small; what these pin is the choice of DENOMINATOR, because getting it
wrong fails in a direction nobody would suspect from looking at the output.

If the denominator were the classified parts, an issuer whose extraction missed a segment
would score HIGHER purity than one whose extraction was complete — the ranking would reward
the worst data, and every number would still look ordinary. The denominator is therefore the
consolidated total the partition was accepted against.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from factors.base.theme_purity import ThemePurity, ThemeSegment, theme_purity
from factors.registry import FACTOR_REGISTRY

NOW = datetime(2026, 9, 10, tzinfo=UTC)
ISSUER = "issuer:cik:1730168"
THEME = "AI infrastructure"

#: Broadcom FY2025, from the packaged 10-K: the two segments and the consolidated revenue
#: they were accepted against (36,858 + 27,029 = 63,887, residual 0).
AVGO = (
    ThemeSegment("Semiconductor solutions", Decimal("36858"), True),
    ThemeSegment("Infrastructure software", Decimal("27029"), False),
)
AVGO_TOTAL = Decimal("63887")


def purity(segments, *, total=AVGO_TOTAL, residual=Decimal(0), confidence=Decimal("0.9")) -> ThemePurity:
    return theme_purity(
        list(segments),
        entity_id=ISSUER,
        theme=THEME,
        as_of=NOW,
        consolidated_revenue=total,
        partition_residual=residual,
        confidence=confidence,
    )


def test_registered_as_module_6_base() -> None:
    spec = FACTOR_REGISTRY["theme_purity"]
    assert (spec.module, spec.kind) == (6, "base"), "init.md §7 numbers pure-blood screening module 6"


def test_the_share_matches_a_hand_calculation_on_a_real_filing() -> None:
    result = purity(AVGO)
    assert result.theme_share == Decimal("36858") / AVGO_TOTAL
    assert result.result.data_availability == "verified"
    assert result.result.flags == []


def test_the_denominator_is_the_consolidated_total_not_the_classified_parts() -> None:
    """The failure this factor exists to avoid, stated as a test.

    Same in-theme revenue, but the extraction missed a 20,000 segment. Dividing by the parts
    it happens to have would report 36,858/43,887 = 84% — a HIGHER purity for WORSE data.
    Against the consolidated total the share is unchanged, and the residual says coverage is
    incomplete.
    """
    incomplete = (
        ThemeSegment("Semiconductor solutions", Decimal("36858"), True),
        ThemeSegment("Infrastructure software", Decimal("7029"), False),
    )
    result = purity(incomplete, residual=Decimal("20000"))
    parts = Decimal("36858") + Decimal("7029")

    assert result.theme_share == Decimal("36858") / AVGO_TOTAL
    assert result.theme_share != Decimal("36858") / parts, "dividing by the parts rewards a worse extraction"
    assert result.theme_share < Decimal("36858") / parts
    assert "partition_residual" in result.result.flags
    assert result.result.data_availability == "unverified"


def test_a_declined_classification_is_not_a_no() -> None:
    """`None` is unclassified revenue — it lowers confidence in the share. `False` is a
    judgement — it lowers the share itself. A silent classifier must not read as a confident
    'not in the theme'."""
    declined = (
        ThemeSegment("Semiconductor solutions", Decimal("36858"), True),
        ThemeSegment("Infrastructure software", Decimal("27029"), None),
    )
    judged = (
        ThemeSegment("Semiconductor solutions", Decimal("36858"), True),
        ThemeSegment("Infrastructure software", Decimal("27029"), False),
    )
    a, b = purity(declined), purity(judged)

    assert a.theme_share == b.theme_share, "an unclassified segment is not in the numerator either way"
    assert a.unclassified_revenue == Decimal("27029") and b.unclassified_revenue == Decimal(0)
    assert a.unclassified_share > Decimal("0.4"), "and the reader can see how much was never judged"
    assert "unclassified_revenue" in a.result.flags and a.result.data_availability == "unverified"
    assert b.result.data_availability == "verified"


def test_the_three_masses_account_for_the_classified_revenue() -> None:
    mixed = (
        ThemeSegment("A", Decimal("30000"), True),
        ThemeSegment("B", Decimal("20000"), False),
        ThemeSegment("C", Decimal("13887"), None),
    )
    result = purity(mixed)
    assert result.in_theme_revenue + result.out_of_theme_revenue + result.unclassified_revenue == AVGO_TOTAL
    assert result.segments == 3


def test_a_missing_consolidated_revenue_refuses_rather_than_divides() -> None:
    result = theme_purity(list(AVGO), entity_id=ISSUER, theme=THEME, as_of=NOW, consolidated_revenue=None)
    assert result.theme_share is None
    assert result.result.flags == ["no_consolidated_revenue"]
    assert result.result.confidence == Decimal(0)


@pytest.mark.parametrize("total", [Decimal(0), Decimal("-100")])
def test_a_nonpositive_denominator_is_no_share_at_all(total) -> None:
    """Not a small share — an undefined one. Dividing would emit an infinity or a sign flip
    into a ranking."""
    result = purity(AVGO, total=total)
    assert result.theme_share is None
    assert result.result.flags == ["nonpositive_consolidated_revenue"]


def test_no_segments_refuses_and_says_so() -> None:
    result = purity(())
    assert result.theme_share is None and result.result.flags == ["no_segments"]


def test_a_theme_covering_everything_is_exactly_one() -> None:
    whole = (ThemeSegment("Only segment", AVGO_TOTAL, True),)
    result = purity(whole)
    assert result.theme_share == Decimal(1)
    assert result.result.data_availability == "verified"


def test_the_factor_does_not_classify() -> None:
    """Whether a segment is in a theme is a judgement made elsewhere (#772: LLM-assisted
    semantic classification). This factor is deterministic arithmetic over that verdict, so
    the same inputs always give the same number — which is what lets a hand calculation check
    it."""
    first, second = purity(AVGO), purity(AVGO)
    assert first.theme_share == second.theme_share
    assert first.result.flags == second.result.flags


def test_a_share_whose_denominator_is_mostly_unexamined_is_refused() -> None:
    """The governed coverage floor (module 5 states its floors the same way).

    36,858 of 63,887 is 57.7% whether or not the other segment was judged — the denominator
    protects the arithmetic. What it cannot protect is the CLAIM: ranking an issuer whose
    revenue the classifier could read 58% of against one it could read all of compares two
    different measurements. Below the floor the share is withdrawn and the masses stay.
    """
    half_judged = (
        ThemeSegment("Semiconductor solutions", Decimal("36858"), True),
        ThemeSegment("Infrastructure software", Decimal("27029"), None),
    )
    result = theme_purity(
        list(half_judged),
        entity_id=ISSUER,
        theme=THEME,
        as_of=NOW,
        consolidated_revenue=AVGO_TOTAL,
        minimum_classified_share=Decimal("0.80"),
    )
    assert result.theme_share is None, "refused, not a smaller number"
    assert result.result.confidence == Decimal(0)
    assert "below_minimum_classified_share" in result.result.flags
    assert result.in_theme_revenue == Decimal("36858"), "the masses are still on the record"
    assert result.classified_share == Decimal("36858") / AVGO_TOTAL


def test_the_floor_is_the_caller_s_and_defaults_to_none() -> None:
    """A factor that invented a floor would refuse rows its caller never asked it to, and the
    threshold would live somewhere no governed definition records."""
    result = purity(AVGO)
    assert result.theme_share is not None
    assert result.classified_share == Decimal(1), "AVGO is fully judged, so any floor passes"


def test_classified_share_counts_the_residual_against_the_claim() -> None:
    """Not `1 - unclassified_share`: revenue the extraction never recovered was never
    classified either, so a partition that missed 20,000 has that mass working against its
    coverage exactly like a declined segment does."""
    incomplete = (
        ThemeSegment("Semiconductor solutions", Decimal("36858"), True),
        ThemeSegment("Infrastructure software", Decimal("7029"), False),
    )
    result = purity(incomplete, residual=Decimal("20000"))
    assert result.classified_share == Decimal("43887") / AVGO_TOTAL
    assert result.classified_share + result.unclassified_share < Decimal(1), "the residual is the gap"
