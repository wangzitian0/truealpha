"""The set-valued selection rule (#772, q6's prerequisite primitive).

`select_single_candidate` answers "which ONE of these is the value" — right for a metric
stated once in several places, like a headcount. A DECOMPOSED metric needs the other shape:
segment revenue's parts ARE the answer, and the question is whether they account for the
whole.

The failure this exists to prevent is specific and quiet. A missed segment silently RAISES
every remaining segment's share, so "the purest name under a theme" — init.md §0 question 6
— inverts while every number on the page still looks like a number. So the rule refuses on
the accounting identity rather than on how plausible the parts look, and the refusals are
typed: short, over, nothing found and nothing to check against are four different problems.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from factors.shared.extraction import (
    RULE_EXHAUSTIVE_PARTITION,
    Candidate,
    Partition,
    Partitioner,
    PartitionRefusal,
    select_exhaustive_partition,
)

TOL = Decimal("0.5")


def parts(*values: str) -> list[Candidate]:
    return [Candidate(float(v), f"segment stating {v}") for v in values]


def test_parts_that_account_for_the_whole_are_the_answer() -> None:
    result = select_exhaustive_partition(parts("60", "30", "10"), total=Decimal("100"), tolerance=TOL)
    assert isinstance(result, Partition)
    assert result.candidate_indices == (0, 1, 2)
    assert result.parts_sum == Decimal("100") and result.residual == Decimal("0")
    assert result.extractor == RULE_EXHAUSTIVE_PARTITION
    assert result.invocation_id is None, "the deterministic rule invokes no model"


def test_a_missed_part_is_refused_rather_than_ranked() -> None:
    """The quiet failure: 60+30 over a true 100 would make the first segment 67% instead of
    60% and the ranking wrong, with nothing on the page looking unusual."""
    result = select_exhaustive_partition(parts("60", "30"), total=Decimal("100"), tolerance=TOL)
    assert result is PartitionRefusal.SHORT


def test_double_counting_is_refused_and_named_separately() -> None:
    """Intersegment revenue is a real line in these tables. Summing it with the external
    lines exceeds the consolidated total, and that is a different defect from a miss — it
    needs an exclusion, not a better recall pass."""
    result = select_exhaustive_partition(parts("60", "30", "20"), total=Decimal("100"), tolerance=TOL)
    assert result is PartitionRefusal.OVER


def test_the_two_directions_are_distinguishable() -> None:
    """A caller that collapses the refusals into one cannot say which happened, and the
    fixes differ. Pinned as a property rather than left to the two cases above."""
    assert PartitionRefusal.SHORT is not PartitionRefusal.OVER
    short = select_exhaustive_partition(parts("99"), total=Decimal("100"), tolerance=TOL)
    over = select_exhaustive_partition(parts("101"), total=Decimal("100"), tolerance=TOL)
    assert {short, over} == {PartitionRefusal.SHORT, PartitionRefusal.OVER}


def test_rounding_inside_the_tolerance_is_accepted_and_the_residual_kept() -> None:
    """Filings round. The residual is not discarded: it is the honest size of the doubt a
    consumer computing shares should be able to see."""
    result = select_exhaustive_partition(parts("60.2", "29.9", "10.2"), total=Decimal("100"), tolerance=TOL)
    assert isinstance(result, Partition)
    assert result.residual == Decimal("-0.3")
    assert abs(result.residual) <= TOL


def test_the_tolerance_boundary_is_inclusive_on_both_sides() -> None:
    exact_short = select_exhaustive_partition(parts("99.5"), total=Decimal("100"), tolerance=TOL)
    exact_over = select_exhaustive_partition(parts("100.5"), total=Decimal("100"), tolerance=TOL)
    assert isinstance(exact_short, Partition), "a residual EQUAL to the tolerance is within it"
    assert isinstance(exact_over, Partition)
    assert select_exhaustive_partition(parts("99.49"), total=Decimal("100"), tolerance=TOL) is PartitionRefusal.SHORT


def test_no_candidates_and_no_total_are_different_refusals() -> None:
    """ "Recall found nothing" and "there is nothing to check against" have different owners:
    one is the adapter's recall pass, the other is a missing consolidated revenue on the wide
    row."""
    assert select_exhaustive_partition([], total=Decimal("100"), tolerance=TOL) is PartitionRefusal.NO_CANDIDATES
    assert select_exhaustive_partition(parts("100"), total=None, tolerance=TOL) is PartitionRefusal.NO_TOTAL


def test_a_proposed_subset_is_still_judged_by_the_identity() -> None:
    """A `Partitioner` proposes; the accounting identity decides. A proposal that drops a
    real part is refused exactly as a rule-built set would be — the model does not get to
    settle whether its own answer is complete."""
    candidates = parts("60", "30", "10")
    good = select_exhaustive_partition(candidates, total=Decimal("100"), tolerance=TOL, indices=[0, 1, 2])
    assert isinstance(good, Partition)

    dropped = select_exhaustive_partition(candidates, total=Decimal("100"), tolerance=TOL, indices=[0, 1])
    assert dropped is PartitionRefusal.SHORT

    assert select_exhaustive_partition(candidates, total=Decimal("100"), tolerance=TOL, indices=[]) is (
        PartitionRefusal.NO_CANDIDATES
    )


def test_a_subset_that_happens_to_balance_is_accepted_with_its_own_indices() -> None:
    """The identity is about the parts CHOSEN, not about every candidate recall produced —
    an intersegment-elimination line correctly excluded must not make the set look short."""
    candidates = parts("60", "40", "25")  # the 25 is an intersegment line to exclude
    result = select_exhaustive_partition(candidates, total=Decimal("100"), tolerance=TOL, indices=[0, 1])
    assert isinstance(result, Partition)
    assert result.candidate_indices == (0, 1)
    assert result.parts_sum == Decimal("100")


def test_float_candidates_do_not_leak_binary_error_into_the_acceptance() -> None:
    """`Candidate.value` is int|float by contract; the ACCEPTANCE is monetary. 0.1+0.2 must
    not put this over a zero tolerance."""
    result = select_exhaustive_partition(parts("0.1", "0.2"), total=Decimal("0.3"), tolerance=Decimal("0"))
    assert isinstance(result, Partition), "the sum must be taken in Decimal, not float"
    assert result.parts_sum == Decimal("0.3") and result.residual == Decimal("0")


def test_the_partitioner_protocol_is_structural() -> None:
    """Same contract as `Selector`: implemented outside this library, so the shape is
    required without importing a concrete implementation."""

    class _Proposer:
        def __call__(self, candidates, *, total):  # noqa: ARG002
            return (0,)

    assert isinstance(_Proposer(), Partitioner)


@pytest.mark.parametrize("bad", [PartitionRefusal.SHORT, PartitionRefusal.OVER, PartitionRefusal.NO_CANDIDATES])
def test_a_refusal_is_never_mistaken_for_a_partition(bad) -> None:
    """Callers branch on the type. A refusal that duck-typed as a `Partition` would be
    consumed as an answer."""
    assert not isinstance(bad, Partition)
