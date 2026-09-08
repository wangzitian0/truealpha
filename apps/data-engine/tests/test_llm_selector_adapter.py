"""`llm.as_selector` / `llm.as_selection`: the model provider expressed as the shared
extraction primitive's `Selector` protocol (#769, #70 scope 2).

`test_llm_selection.py` covers `select_headcount` itself (ledger, replay, persistence)
in full; these tests only cover the adapter — that it delegates to `select_headcount`
unchanged and translates the result into `factors.shared.extraction.Selection`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from data_engine.config import settings
from data_engine.sources import gateway
from data_engine.sources.llm import as_selection, as_selector
from factors.shared.extraction import Candidate, Selector

AEP = [
    Candidate(17581, "As of December 31, 2025, the subsidiaries of AEP had a total of 17,581 employees."),
    Candidate(6994, "As of December 31, 2025, AEPSC had 6,994 employees."),
]


class _Conn:
    def __init__(self):
        self.inserts: list[tuple] = []

    def execute(self, sql, params=()):
        text = " ".join(sql.split())
        if text.startswith("select invocation_id, decision"):
            return _R([])
        if text.startswith("insert into staging.model_invocations"):
            self.inserts.append(params)
            return _R([])
        raise AssertionError(text[:60])


class _R:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


def _answer(value, index, reason="x", served="glm-served"):
    body = {
        "model": served,
        "choices": [{"message": {"content": json.dumps({"value": value, "candidate_index": index, "reason": reason})}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    return json.dumps(body).encode()


@pytest.fixture
def seated(monkeypatch):
    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    monkeypatch.setattr(settings, "llm_model", "glm-test")
    ledger = gateway.MemoryLedger()
    previous = gateway.set_writer(ledger)
    yield ledger
    gateway.set_writer(previous)


def test_the_bound_selector_conforms_to_the_protocol(seated):
    selector = as_selector(
        _Conn(),
        cik=4904,
        accession="a",
        form="10-K",
        issuer_label="AEP",
        caller="t",
        transport=lambda *_: (200, _answer(17581, 0)),
        now=lambda: datetime(2026, 9, 7, 12, tzinfo=UTC),
    )
    assert isinstance(selector, Selector)


def test_a_chosen_candidate_becomes_a_shared_primitive_selection(seated):
    selector = as_selector(
        _Conn(),
        cik=4904,
        accession="a",
        form="10-K",
        issuer_label="AEP",
        caller="t",
        transport=lambda *_: (200, _answer(17581, 0, "subsidiaries-wide total")),
        now=lambda: datetime(2026, 9, 7, 12, tzinfo=UTC),
    )
    selection = selector(AEP)
    assert selection is not None
    assert selection.value == 17581 and selection.candidate_index == 0
    assert selection.extractor.startswith("model:glm-served:")
    assert selection.invocation_id is not None
    assert selection.reason == "subsidiaries-wide total"


def test_a_declined_selection_becomes_none_not_a_guess(seated):
    selector = as_selector(
        _Conn(),
        cik=1,
        accession="a",
        form="10-K",
        issuer_label="X",
        caller="t",
        transport=lambda *_: (200, _answer(None, None, "no total stated")),
    )
    assert selector(AEP) is None


def test_a_non_integral_candidate_value_raises_rather_than_silently_truncating(seated):
    """Copilot review on #782: `Candidate.value` is `int | float` on the shared primitive,
    but this module's own `Candidate` is `int`-only (headcount). A bare `int(42.7)` would
    have thrown away `.7` with no complaint; this must raise instead."""
    selector = as_selector(
        _Conn(),
        cik=1,
        accession="a",
        form="10-K",
        issuer_label="X",
        caller="t",
        transport=lambda *_: (200, _answer(17581, 0)),
    )
    with pytest.raises(ValueError, match="non-integral"):
        selector([Candidate(17581.7, "As of December 31, 2025, we had 17,581.7 employees on average.")])


def test_a_whole_number_float_candidate_value_is_accepted(seated):
    selector = as_selector(
        _Conn(),
        cik=1,
        accession="a",
        form="10-K",
        issuer_label="X",
        caller="t",
        transport=lambda *_: (200, _answer(17581, 0)),
    )
    selection = selector([Candidate(17581.0, "whole-number float")])
    assert selection is not None and selection.value == 17581


def test_as_selection_declines_on_a_refusal_without_calling_anything():
    from data_engine.sources.llm import ModelSelection

    refusal = ModelSelection(
        value=None,
        candidate_index=None,
        reason="model answered null",
        model="glm-test",
        provider="zhipu-glm-coding-plan",
        prompt_sha256="a" * 64,
        request_sha256="b" * 64,
        response_sha256="c" * 64,
        prompt_tokens=1,
        completion_tokens=1,
        invocation_id="model-invocation:" + "d" * 64,
        replayed=False,
    )
    assert as_selection(refusal) is None
