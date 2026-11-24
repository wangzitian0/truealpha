"""The reason registry binds each code to one kind and, for an outcome, one of five classes (#1851)."""

from __future__ import annotations

import re

import pytest
from truealpha_contracts.common import load_data_json
from truealpha_contracts.reasons import ReasonClass, ReasonKind, class_meanings, dispositions, registration

SECTION = load_data_json("registries.v1.json")["reasons"]


def test_the_five_classes_each_have_a_meaning() -> None:
    assert set(class_meanings()) == set(ReasonClass)
    assert all(meaning.strip() for meaning in class_meanings().values())


@pytest.mark.parametrize("code", sorted(SECTION["codes"]))
def test_an_outcome_has_a_class_and_a_pipeline_gap_names_its_issue(code: str) -> None:
    entry = registration(code)
    assert entry is not None and entry.code == code
    if entry.kind is ReasonKind.EVIDENCE:
        assert entry.reason_class is None and entry.tracking_issue is None
    else:
        assert entry.reason_class in ReasonClass
        assert (entry.tracking_issue is not None) is (entry.reason_class is ReasonClass.PIPELINE_GAP)
        assert entry.tracking_issue is None or re.fullmatch(r"#\d+", entry.tracking_issue)


def test_no_listed_prefix_starts_another_so_a_code_matches_one_prefix() -> None:
    prefixes = sorted(SECTION["prefixes"])
    assert [(a, b) for a in prefixes for b in prefixes if a != b and b.startswith(a)] == []
    assert [code for code in SECTION["codes"] if code in SECTION["prefixes"]] == []


@pytest.mark.parametrize(
    ("code", "kind", "reason_class"),
    [
        ("fetch_error:MoomooConnectionError", ReasonKind.OUTCOME, ReasonClass.TRANSIENT),
        ("growth_convention_unsourced:analyst_consensus", ReasonKind.OUTCOME, ReasonClass.WORLD_ABSENCE),
        ("growth_convention:historical_cagr", ReasonKind.EVIDENCE, None),
        ("excluded:missing_gross_profit_fact", ReasonKind.OUTCOME, ReasonClass.NOT_APPLICABLE),
        ("excluded:stale_period", ReasonKind.OUTCOME, ReasonClass.PIPELINE_GAP),
        ("older_than_400d", ReasonKind.OUTCOME, ReasonClass.PIPELINE_GAP),
    ],
)
def test_a_prefixed_code_takes_its_prefix_and_excluded_takes_the_inner_code(
    code: str, kind: ReasonKind, reason_class: ReasonClass | None
) -> None:
    entry = registration(code)
    assert entry is not None and (entry.kind, entry.reason_class) == (kind, reason_class)


@pytest.mark.parametrize("code", ["", "fetch_error:", "excluded:", "excluded:no_such_code", "no_such_code", "growth"])
def test_a_code_the_registry_does_not_list_has_no_entry(code: str) -> None:
    assert registration(code) is None


def test_the_capture_obligations_keep_their_dispositions() -> None:
    assert dispositions()["checksum_mismatch"] == "stop"
    assert dispositions()["rate_limited"] == "retry"
    assert dispositions()["deferred_capacity"] == "trace_only"
    assert len(dispositions()) == 13
