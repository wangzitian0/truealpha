"""Segment-revenue recall against a real filing (#772, q6).

Driven by the packaged AVGO 10-K rather than a constructed string, because every design
decision in `segment_extraction` came from what that document actually contains — a prior-year
column, a change column, a percentage restatement under the same heading, and a per-segment
income statement 140k characters later.

The property under test is not "recall is accurate". It is that **recall plus the accounting
identity** yields the right table and refuses everything else, so that a recall pass which
grabs too much fails LOUDLY. A segment set that is quietly wrong moves every share and
inverts q6's ranking with every number still looking like a number.

The filing is PACKAGED, and its absence is a failure rather than a skip. The first version of
this file pointed at `apps/data-engine/data/samples/` — a gitignored directory — so every
assertion below silently skipped in CI while the file read as proof that recall worked
(review on #805). A test that cannot run is not a weaker test; it is a green check standing
where a check was supposed to be.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from data_engine.datahub.standards.filing_extraction import filing_plain_text
from data_engine.datahub.standards.segment_extraction import (
    SEGMENT_TOLERANCE,
    as_candidates,
    segment_candidates,
    unitless_windows,
    windows_of,
)
from factors.shared.extraction import Partition, PartitionRefusal, select_exhaustive_partition

FILING = Path(__file__).resolve().parents[1] / "samples" / "filings" / "AVGO_10K_000173016825000121.html"
#: Broadcom's FY2025 consolidated net revenue as the CAPTURE PLANE holds it — absolute, the
#: shape `consolidated_revenue()` returns (prod, 2026-09-10: 63887000000). The filing prints
#: 63,887 and declares "(In millions)"; reconciling those two is what recall's unit scaling
#: does, and comparing them unscaled is a refusal six orders of magnitude wide.
CONSOLIDATED = Decimal("63887000000")
MILLIONS = Decimal("1000000")


@pytest.fixture(scope="module")
def text() -> str:
    assert FILING.exists(), (
        f"packaged filing missing: {FILING}. It is committed under samples/filings/ so this "
        "suite runs in CI; a skip here would hide every assertion in this file."
    )
    return filing_plain_text(FILING.read_bytes())


@pytest.fixture(scope="module")
def recalled(text):
    return segment_candidates(text)


def _verdicts(recalled):
    candidates = as_candidates(recalled)
    return {
        window: select_exhaustive_partition(
            candidates,
            total=CONSOLIDATED,
            tolerance=SEGMENT_TOLERANCE * recalled[indices[0]].multiplier,
            indices=indices,
        )
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
    named = {recalled[i].segment_name: recalled[i].stated_value for i in partition.candidate_indices}
    assert named == {
        "Semiconductor solutions": Decimal("36858"),
        "Infrastructure software": Decimal("27029"),
    }
    assert partition.residual == Decimal("0"), "36,858 + 27,029 = 63,887 exactly"
    assert window is not None


def test_the_parts_are_scaled_into_the_oracle_s_units(recalled) -> None:
    """The defect this pins is arithmetic, not recall. The oracle is absolute
    (63,887,000,000); the table prints millions. Comparing them as printed makes every real
    segment table SHORT by a factor of a million — an extraction that fails on every issuer
    while each individual number is exactly what the filing says."""
    semis = next(c for c in recalled if c.segment_name == "Semiconductor solutions")
    assert semis.stated_value == Decimal("36858"), "the row is recorded as the filing prints it"
    assert semis.multiplier == MILLIONS, 'read from the table\'s own "(In millions)" caption'
    assert semis.value == Decimal("36858000000"), "and compared on the oracle's scale"
    assert semis.value / CONSOLIDATED < 1


def test_a_table_that_states_no_scale_is_not_recalled_as_revenue(recalled, text) -> None:
    """AVGO restates the same two segments as percentages (58 / 42) under a heading this
    module matches. 58 is not a small revenue — it is a number in a unit the identity has no
    way to compare, and reading it as dollars is how a percentage becomes a part.

    This is the ONE thing excluded before the identity sees it, and it is not a judgement
    about which table is right: the exclusion is that the document never stated a scale.
    """
    assert unitless_windows(text) >= 1, "the percentage restatement is found and set aside"
    assert Decimal("58") not in {c.stated_value for c in recalled}
    assert Decimal("42") not in {c.stated_value for c in recalled}


def test_a_skipped_window_is_counted_rather_than_forgotten(text, recalled) -> None:
    """ "No segment table matched" and "the table states no scale" have different fixes — a
    heading pattern vs. a caption pattern — so the adapter reports which one happened rather
    than collapsing both into "found nothing".

    Both paths must be live on this filing, or the distinction is untested: at least one
    window was set aside for stating no scale, AND at least one was read.
    """
    assert unitless_windows(text) >= 1
    assert recalled, "the scaled windows still produced candidates"


def test_every_other_window_is_refused_rather_than_filtered(recalled) -> None:
    """Recall still returns the per-segment income statement — it is not clever, on purpose.
    Its cost, R&D and operating-income rows sum to far more than the issuer earned, and it is
    refused by the identity, which is the half that cannot be fooled by a regex that matches
    too much."""
    refused = [v for v in _verdicts(recalled).values() if isinstance(v, PartitionRefusal)]
    assert refused, "the other tables must be present and refused, not silently dropped"
    assert all(v in (PartitionRefusal.SHORT, PartitionRefusal.OVER) for v in refused)


def test_the_prior_year_column_is_never_read(recalled) -> None:
    """30,096 and 21,478 are FY2024. Reading a second column would produce a set summing to
    neither year — the identity would refuse and the extraction would fail for a reason no
    reader could see from the numbers."""
    values = {c.stated_value for c in recalled}
    assert Decimal("30096") not in values and Decimal("21478") not in values


def test_the_total_row_is_never_a_candidate(recalled) -> None:
    """It is the identity's own oracle. As a part it would double the sum."""
    assert Decimal("63887") not in {c.stated_value for c in recalled}
    assert not any(c.segment_name.lower().startswith("total") for c in recalled)


def test_header_date_fragments_are_not_segments(recalled) -> None:
    """`November 2, 2025` matches the row pattern as a label plus a number."""
    assert not any("November" in c.segment_name for c in recalled)


def test_every_candidate_carries_the_text_it_was_read_from(recalled) -> None:
    """init.md §9: a landed fact points at the span that stated it, so it stays re-readable."""
    for candidate in recalled:
        assert candidate.sentence.strip(), f"{candidate.segment_name} has no evidence span"
        assert candidate.segment_name in candidate.sentence
        assert str(int(candidate.stated_value)) in candidate.sentence.replace(",", ""), (
            "the span shows the number as printed, not the scaled one"
        )


def test_a_segment_is_never_recalled_twice_with_the_same_value(recalled) -> None:
    """The heading matches both a lead-in sentence and the table caption, so the same rows are
    swept more than once. One segment stated twice double-counts into every share."""
    pairs = [(c.segment_name, c.stated_value) for c in recalled]
    assert len(pairs) == len(set(pairs))
