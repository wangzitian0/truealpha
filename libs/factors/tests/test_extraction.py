"""The shared structured-extraction primitive (#769, #70): candidate -> selection.

`select_single_candidate` is exactly the rule that used to live only inside
`data_engine.datahub.standards.filing_extraction.select_total` — these tests exercise it
directly, independent of anything filing-shaped, so a future non-SEC adapter (segment
revenue, #769's acceptance criterion 2) can trust it without re-deriving the cases here.
"""

from __future__ import annotations

from factors.shared.extraction import (
    RULE_SINGLE_CANDIDATE,
    Candidate,
    Extraction,
    Selection,
    Selector,
    select_single_candidate,
)


def test_no_candidates_selects_nothing() -> None:
    assert select_single_candidate([]) is None


def test_one_candidate_resolves_by_the_rule() -> None:
    selection = select_single_candidate([Candidate(42000, "we had 42,000 employees")])
    assert selection is not None
    assert selection.value == 42000
    assert selection.candidate_index == 0
    assert selection.extractor == RULE_SINGLE_CANDIDATE == "rule:single-candidate:v1"
    assert selection.invocation_id is None


def test_several_candidates_agreeing_on_one_value_still_resolve() -> None:
    """Two sentences stating the same figure (e.g. a cover page and a body restatement)
    are one distinct value — nothing to choose — and the FIRST candidate wins, matching
    `select_total`'s pre-#769 behaviour (`totals[0]`)."""
    candidates = [
        Candidate(33000, "As of November 2, 2025, we had approximately 33,000 employees.", "November 2, 2025"),
        Candidate(33000, "we had 33,000 employees worldwide", None),
    ]
    selection = select_single_candidate(candidates)
    assert selection is not None
    assert selection.candidate_index == 0
    assert selection.value == 33000


def test_two_distinct_values_defer_rather_than_guess() -> None:
    candidates = [Candidate(50000, "total"), Candidate(12000, "R&D segment")]
    assert select_single_candidate(candidates) is None


def test_selection_and_candidate_are_frozen() -> None:
    candidate = Candidate(1, "s")
    selection = Selection(1, 0, RULE_SINGLE_CANDIDATE, "reason")
    for obj, field, value in ((candidate, "value", 2), (selection, "value", 2)):
        try:
            setattr(obj, field, value)
        except Exception:  # noqa: BLE001 - any exception proves immutability; the type varies by dataclass
            continue
        raise AssertionError(f"{obj!r}.{field} was mutable")


def test_extraction_bundles_candidates_and_a_selection() -> None:
    candidates = (Candidate(1, "s"),)
    selection = select_single_candidate(candidates)
    extraction = Extraction(candidates=candidates, selection=selection)
    assert extraction.candidates == candidates
    assert extraction.selection is not None and extraction.selection.value == 1


def _fake_model_selector(candidates: list[Candidate]) -> Selection | None:
    """Stands in for a model behind the source gateway (data_engine.sources.llm) —
    the library never imports a concrete one; it only requires this shape (`Selector`)."""
    if not candidates:
        return None
    chosen = max(range(len(candidates)), key=lambda i: candidates[i].value)
    return Selection(
        value=candidates[chosen].value,
        candidate_index=chosen,
        extractor="model:fake-model:deadbeefcafe1",
        reason="fake: chose the larger figure",
        invocation_id="model-invocation:" + "0" * 64,
    )


def test_a_model_backed_callable_satisfies_the_selector_protocol() -> None:
    assert isinstance(_fake_model_selector, Selector)
    selection = _fake_model_selector([Candidate(50000, "total"), Candidate(12000, "R&D segment")])
    assert selection is not None
    assert selection.value == 50000 and selection.invocation_id is not None


def test_a_declining_selector_returns_none_like_the_rule_does() -> None:
    assert _fake_model_selector([]) is None
