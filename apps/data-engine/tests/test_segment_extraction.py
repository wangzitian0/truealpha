"""Segment-revenue recall against a real filing (#772, q6).

Driven by the packaged AVGO 10-K rather than a constructed string, because every design
decision in `segment_extraction` came from what that document actually contains — a prior-year
column, a change column, a percentage restatement under the same heading, and a per-segment
income statement 140k characters later.

The property under test is not "recall is accurate". It is that **recall plus the accounting
identity** yields the right table and refuses everything else, so that a recall pass which
grabs too much fails LOUDLY. A segment set that is quietly wrong moves every share and
inverts q6's ranking with every number still looking like a number.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from data_engine.datahub.standards.filing_extraction import filing_plain_text
from data_engine.datahub.standards.segment_extraction import (
    as_candidates,
    segment_candidates,
    windows_of,
)
from factors.shared.extraction import Partition, PartitionRefusal, select_exhaustive_partition

FILING = Path(__file__).resolve().parents[1] / "data" / "samples" / "AVGO_10K_000173016825000121.html"
#: Broadcom's FY2025 consolidated net revenue, in millions, as the same filing states it.
#: In production this comes from the run's own core row, never a constant — the adapter reads
#: it there. Here it stands in for that number so recall can be tested without a database.
CONSOLIDATED = Decimal("63887")
TOL = Decimal("1")


@pytest.fixture(scope="module")
def text() -> str:
    if not FILING.exists():
        pytest.skip(f"packaged filing missing: {FILING}")
    return filing_plain_text(FILING.read_bytes())


@pytest.fixture(scope="module")
def recalled(text):
    return segment_candidates(text)


def _verdicts(recalled):
    candidates = as_candidates(recalled)
    return {
        window: select_exhaustive_partition(candidates, total=CONSOLIDATED, tolerance=TOL, indices=indices)
        for window, indices in windows_of(recalled).items()
    }


def test_exactly_one_window_is_accepted(recalled) -> None:
    """The filing states its segments once in dollars. Two accepted windows would mean two
    different answers to one question, and the adapter would have to choose — which is the
    judgement this whole design exists to avoid."""
    accepted = [v for v in _verdicts(recalled).values() if isinstance(v, Partition)]
    assert len(accepted) == 1, f"expected one accepted table, got {len(accepted)}"


def test_the_accepted_partition_is_the_real_segment_table(recalled) -> None:
    verdicts = _verdicts(recalled)
    window, partition = next((w, v) for w, v in verdicts.items() if isinstance(v, Partition))
    named = {recalled[i].segment_name: recalled[i].value for i in partition.candidate_indices}
    assert named == {
        "Semiconductor solutions": Decimal("36858"),
        "Infrastructure software": Decimal("27029"),
    }
    assert partition.residual == Decimal("0"), "36,858 + 27,029 = 63,887 exactly"
    assert window is not None


def test_every_other_window_is_refused_rather_than_filtered(recalled) -> None:
    """Recall still returns the percentage table and the per-segment income statement — it is
    not clever, on purpose. They are refused by the identity, which is the half that cannot be
    fooled by a regex that matches too much."""
    refused = [v for v in _verdicts(recalled).values() if isinstance(v, PartitionRefusal)]
    assert refused, "the other tables must be present and refused, not silently dropped"
    assert all(v in (PartitionRefusal.SHORT, PartitionRefusal.OVER) for v in refused)


def test_the_percentage_restatement_is_a_separate_window(recalled) -> None:
    """The trap that made the first version fail: the amounts table and the percentage table
    share a heading and sit adjacent, so a fixed-width window swallowed both and their four
    parts summed to neither. Cutting at the table's own total row separates them."""
    percentages = [c for c in recalled if c.value in (Decimal("58"), Decimal("42"))]
    assert percentages, "the percentage rows are still recalled"
    amounts = [c for c in recalled if c.value in (Decimal("36858"), Decimal("27029"))]
    assert amounts
    assert {c.window_start for c in percentages}.isdisjoint({c.window_start for c in amounts}), (
        "the two tables must land in different windows"
    )


def test_the_prior_year_column_is_never_read(recalled) -> None:
    """30,096 and 21,478 are FY2024. Reading a second column would produce a set summing to
    neither year — the identity would refuse and the extraction would fail for a reason no
    reader could see from the numbers."""
    values = {c.value for c in recalled}
    assert Decimal("30096") not in values and Decimal("21478") not in values


def test_the_total_row_is_never_a_candidate(recalled) -> None:
    """It is the identity's own oracle. As a part it would double the sum."""
    assert Decimal("63887") not in {c.value for c in recalled}
    assert not any(c.segment_name.lower().startswith("total") for c in recalled)


def test_header_date_fragments_are_not_segments(recalled) -> None:
    """`November 2, 2025` matches the row pattern as a label plus a number."""
    assert not any("November" in c.segment_name for c in recalled)


def test_every_candidate_carries_the_text_it_was_read_from(recalled) -> None:
    """init.md §9: a landed fact points at the span that stated it, so it stays re-readable."""
    for candidate in recalled:
        assert candidate.sentence.strip(), f"{candidate.segment_name} has no evidence span"
        assert candidate.segment_name in candidate.sentence


def test_a_segment_is_never_recalled_twice_with_the_same_value(recalled) -> None:
    """The heading matches both a lead-in sentence and the table caption, so the same rows are
    swept more than once. One segment stated twice double-counts into every share."""
    pairs = [(c.segment_name, c.value) for c in recalled]
    assert len(pairs) == len(set(pairs))
