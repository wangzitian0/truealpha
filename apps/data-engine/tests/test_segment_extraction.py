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

import re
from decimal import Decimal
from pathlib import Path

import pytest
from data_engine.datahub.standards.filing_extraction import filing_plain_text
from data_engine.datahub.standards.segment_extraction import (
    _UNITS,
    SEGMENT_TOLERANCE,
    _windows,
    as_candidates,
    filing_scale,
    segment_candidates,
    single_segment_statement,
    unitless_windows,
    windows_of,
)
from factors.shared.extraction import Candidate, Partition, PartitionRefusal, select_exhaustive_partition

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


#: The BACKWARD-window case, and it is a different filing on purpose. ADM's total row is
#: itself called "Segment Revenues 79,820 85,099", so the phrase this module matches on lands
#: on the table's LAST line and everything it wants is behind the match. A forward-only window
#: found six headings on this filing and produced zero candidates.
ADM = Path(__file__).resolve().parents[1] / "samples" / "filings" / "ADM_10K_000000708426000011.html"


@pytest.fixture(scope="module")
def adm_text() -> str:
    assert ADM.exists(), f"packaged filing missing: {ADM}"
    return filing_plain_text(ADM.read_bytes())


@pytest.fixture(scope="module")
def adm_recalled(adm_text):
    return segment_candidates(adm_text)


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


def test_a_percentage_restatement_is_never_recalled_as_revenue(recalled) -> None:
    """AVGO restates the same two segments as percentages (58 / 42) under a heading this
    module matches. 58 is not a small revenue — it is a number in a unit the identity has no
    way to compare, and reading it as dollars is how a percentage becomes a part.

    The MECHANISM changed when the window learned to look backwards for a scale: that window
    now resolves to the amounts table's own caption span rather than being set aside as
    unitless. The property is what matters and it is unchanged — those two numbers never
    become parts — so it is asserted on the numbers rather than on the reason.
    """
    values = {c.stated_value for c in recalled}
    assert Decimal("58") not in values and Decimal("42") not in values
    assert Decimal("36858") in values, "while the amounts under the same heading are read"


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


def test_a_scale_stated_nowhere_in_the_filing_is_counted_rather_than_forgotten(adm_text) -> None:
    """ "No segment table matched" and "this document never states its units" have different
    fixes — a heading pattern vs. a filing this module cannot read at all — so the adapter
    reports which one happened rather than collapsing both into "found nothing".

    The bar moved with the filing-wide fallback, and that is the point: a window is only
    uncounted when the FILING declares no scale anywhere, not when one table omits it. ADM
    declares millions 51 times, so its count is zero even though windows of it say nothing.
    """
    assert unitless_windows(adm_text) == 0, "the filing states its scale, so no window is incomparable"
    assert any(not _UNITS.search(w) for _, w in _windows(adm_text)), "though some windows still omit it"


#: A document with a segment table and no unit statement anywhere. Constructed, and the
#: reason it has to be is the finding: NO packaged filing declares its units nowhere. SHOP
#: looked like one and was not — it writes `(in US $ millions)`, which the first version of
#: `_UNITS` could not read because it required `(` to be followed immediately by `in`. A test
#: of mine asserted that as a property of the DOCUMENT when it was a property of the regex,
#: which is the whole failure shape this module keeps being caught by.
_NO_UNITS_ANYWHERE = (
    "Net revenue by segment for the periods presented: "
    "Semiconductor solutions 36,858 Infrastructure software 27,029 Total net revenue 63,887"
)


def test_a_filing_that_declares_no_scale_anywhere_still_refuses() -> None:
    """The fallback is the FILING's own statement, never a default. A document that states
    units nowhere leaves its tables incomparable rather than handing them a guess — and says
    so through the count, which is what separates it from a document with no table at all."""
    assert filing_scale(_NO_UNITS_ANYWHERE) is None
    assert segment_candidates(_NO_UNITS_ANYWHERE) == []
    assert _windows(_NO_UNITS_ANYWHERE), "the heading matched — this is not 'nothing found'"
    assert unitless_windows(_NO_UNITS_ANYWHERE) >= 1, "and the count says the scale is what is missing"


def test_shop_declares_its_scale_and_the_first_pattern_could_not_read_it() -> None:
    """The correction, pinned so it cannot come back. SHOP writes `(in US $ millions)`; the
    pattern that required `(` then `in` scored zero on the whole document and made it look
    like a filing with no units at all."""
    shop = Path(__file__).resolve().parents[1] / "samples" / "filings" / "SHOP_10K_000159480526000007.html"
    text = filing_plain_text(shop.read_bytes())
    assert "in US $ millions" in text, "the phrasing this module used to miss"
    assert filing_scale(text) == MILLIONS
    assert re.search(r"\(\s*in\s+millions\b", text, re.IGNORECASE) is None, (
        "and it is NOT written the way the narrow pattern demanded"
    )


def test_the_window_reaches_backwards_when_the_match_is_the_total_row(adm_recalled) -> None:
    """ADM's segment table is found only by looking behind the match. Before that, this
    filing produced six heading matches and zero candidates — six tables located and thrown
    away, because the `(in millions)` caption sits before the phrase, not after it."""
    names = {c.segment_name for c in adm_recalled}
    assert adm_recalled, "the backward window recovers the rows"
    assert "Crushing" in names and "Vantage Corn Processors" in names
    assert all(c.multiplier == MILLIONS for c in adm_recalled), "scaled by the caption behind them"


def test_adm_still_refuses_because_its_table_is_nested(adm_recalled) -> None:
    """Recovering the rows is not the same as answering. ADM reports sub-segments under
    sub-totals ("Total Ag Services and Oilseeds"), and this module's flat model cuts the
    window at the FIRST total — so the parts it recovers are a subset and no window balances.

    A refusal is the right outcome for a table this module cannot represent, and it is the
    identity that produces it rather than a guess. Pinned so that a future nested-table
    change has to state what it did to this case.
    """
    consolidated = Decimal("85099") * MILLIONS
    candidates = as_candidates(adm_recalled)
    accepted = [
        select_exhaustive_partition(
            candidates,
            total=consolidated,
            tolerance=SEGMENT_TOLERANCE * adm_recalled[indices[0]].multiplier,
            indices=indices,
        )
        for indices in windows_of(adm_recalled).values()
    ]
    assert not any(isinstance(v, Partition) for v in accepted), "no window balances, so none is landed"


def test_a_single_segment_issuer_is_a_determinate_answer_not_a_miss() -> None:
    """The failure this fixes is the one that would have hurt q6 most: a pure-play IS the
    purest name under its theme, and every single-segment issuer was being refused. The
    ranking would have systematically excluded exactly the companies it exists to find.

    Measured against the packaged corpus — the statement is detected on the four issuers that
    make it, and on none of the ones that report segments.
    """
    root = Path(__file__).resolve().parents[1] / "samples" / "filings"
    single = {"DDOG", "DUOL", "PLUG", "SHOP"}
    for path in sorted(root.glob("*.html")):
        if "8K" in path.name:
            continue
        ticker = path.name.split("_")[0]
        statement = single_segment_statement(filing_plain_text(path.read_bytes()))
        if ticker in single:
            assert statement is not None, f"{ticker} states one segment and must be detected"
            assert "segment" in statement.lower()
        else:
            assert statement is None, f"{ticker} reports segments; a false positive would replace its table"


def test_a_table_that_states_no_scale_inherits_the_filing_scale(adm_recalled, adm_text) -> None:
    """A filing declares its units once at the top of the financial statements and every table
    below inherits them. ADM says millions 51 times and thousands twice; two of its segment
    windows state nothing of their own and are not scaleless — they are using the filing's.

    Measured because the alternative failed in production: ADP's segment table states no scale
    within 900 characters in EITHER direction, so the backward window recovered nothing and
    six tables stayed discarded.
    """
    assert filing_scale(adm_text) == MILLIONS
    inherited = [c for c in adm_recalled if c.scale_source == "filing"]
    assert inherited, "some window took the filing's scale"
    assert all(c.multiplier == MILLIONS for c in inherited)
    stated = [c for c in adm_recalled if c.scale_source == "table"]
    assert stated, "and some still state their own — the two are distinguishable on the row"


def test_the_dominant_declaration_wins_over_a_stray_one(adm_text) -> None:
    """ADM declares thousands twice, in tables that are not its segment tables. A rule that
    took the FIRST declaration, or any declaration, would scale a segment table by 1,000 and
    it would refuse."""
    from collections import Counter

    from data_engine.datahub.standards.segment_extraction import _UNITS

    counts = Counter(m.group(1).lower() for m in _UNITS.finditer(adm_text))
    assert counts["thousands"] > 0 and counts["millions"] > counts["thousands"]
    assert filing_scale(adm_text) == MILLIONS


def test_inheriting_a_wrong_scale_refuses_rather_than_publishes(recalled) -> None:
    """The whole argument for a fallback instead of a refusal.

    A classification the model gets wrong produces a plausible number that nothing catches. A
    SCALE that is wrong by 1,000 makes the parts miss the consolidated total by 1,000, and the
    identity refuses the set. That asymmetry is why this may be guessed at all, so it is
    asserted rather than argued.
    """
    candidates = as_candidates(recalled)
    accepted = [
        (window, indices)
        for window, indices in windows_of(recalled).items()
        if isinstance(
            select_exhaustive_partition(
                candidates,
                total=CONSOLIDATED,
                tolerance=SEGMENT_TOLERANCE * recalled[indices[0]].multiplier,
                indices=indices,
            ),
            Partition,
        )
    ]
    assert len(accepted) == 1, "AVGO's real table balances"
    _window, indices = accepted[0]

    over = [Candidate(float(recalled[i].value) * 1000, recalled[i].sentence) for i in indices]
    under = [Candidate(float(recalled[i].value) / 1000, recalled[i].sentence) for i in indices]
    for wrong, expected in ((over, PartitionRefusal.OVER), (under, PartitionRefusal.SHORT)):
        verdict = select_exhaustive_partition(
            wrong, total=CONSOLIDATED, tolerance=SEGMENT_TOLERANCE * MILLIONS, indices=list(range(len(wrong)))
        )
        assert not isinstance(verdict, Partition), "a scale off by 1,000 cannot be accepted"
        assert verdict is expected, "and the refusal even says which way it was wrong"


def test_avgo_is_unchanged_by_the_fallback(recalled) -> None:
    """The fallback must not reach a filing that states its own scale. AVGO declares millions
    in the caption of the table this module accepts, so nothing about it inherits."""
    assert all(c.scale_source == "table" for c in recalled)
    assert Decimal("58") not in {c.stated_value for c in recalled}
