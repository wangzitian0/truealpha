"""The model as a gated source (#70 scope 2, #735 B1): chooses among enumerated candidates,
never invents, records every invocation, replays instead of re-asking."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from data_engine.config import settings
from data_engine.sources import gateway
from data_engine.sources.llm import PROMPT_SHA256, Candidate, ModelNotConfigured, build_request, select_headcount

AEP = [
    Candidate(17581, "As of December 31, 2025, the subsidiaries of AEP had a total of 17,581 employees."),
    Candidate(6994, "As of December 31, 2025, AEPSC had 6,994 employees."),
]


class _Conn:
    def __init__(self, replay_row=None):
        self.replay_row = replay_row
        self.inserts: list[tuple] = []

    def execute(self, sql, params=()):
        text = " ".join(sql.split())
        if text.startswith("select invocation_id, decision"):
            assert "status_code < 400" in text and "request_sha256 = %s" in text
            return _R([self.replay_row] if self.replay_row else [])
        if text.startswith("insert into staging.model_invocations"):
            self.inserts.append(params)
            return _R([])
        raise AssertionError(text[:60])


class _R:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


def _answer(value, index, reason="x", total=305, served="glm-served"):
    body = {
        "model": served,
        "choices": [{"message": {"content": json.dumps({"value": value, "candidate_index": index, "reason": reason})}}],
        "usage": {"prompt_tokens": 244, "completion_tokens": 61, "total_tokens": total},
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


def test_no_provider_seated_is_an_explicit_refusal(monkeypatch):
    monkeypatch.setattr(settings, "llm_api_key", "")
    with pytest.raises(ModelNotConfigured):
        select_headcount(None, cik=4904, accession="a", form="10-K", issuer_label="AEP", candidates=AEP, caller="t")


def test_the_request_binds_decoding_settings_and_the_instructions_are_content_addressed():
    request = build_request("AEP", "10-K", AEP, model="glm-test")
    assert request["temperature"] == 0 and request["thinking"] == {"type": "disabled"}
    assert request["response_format"] == {"type": "json_object"}
    assert "17,581" in request["messages"][1]["content"] and "[1] 6,994" in request["messages"][1]["content"]
    assert len(PROMPT_SHA256) == 64


def test_a_candidate_choice_is_recorded_with_its_token_cost_and_persisted(seated):
    calls = []

    def transport(url, headers, body):
        calls.append((url, headers["Authorization"][:7], json.loads(body)["model"]))
        return 200, _answer(17581, 0, "subsidiaries-wide total")

    conn = _Conn()
    selection = select_headcount(
        conn,
        cik=4904,
        accession="000000490426000013",
        form="10-K",
        issuer_label="AEP",
        candidates=AEP,
        caller="t",
        transport=transport,
        now=lambda: datetime(2026, 9, 7, 12, tzinfo=UTC),
    )

    assert selection.value == 17581 and selection.candidate_index == 0 and not selection.replayed
    # The extractor names the model the provider says it answered with, not the requested
    # alias: on 2026-09-07 every prod ask for glm-4.7 came back `"model": "glm-5.3-flash"`.
    assert selection.served_model == "glm-served" and selection.model == "glm-test"
    assert selection.extractor.startswith("model:glm-served:")
    assert calls == [("https://open.bigmodel.cn/api/coding/paas/v4/chat/completions", "Bearer ", "glm-test")]
    (record,) = seated
    assert record.source == "filing-extraction-model" and record.cost == Decimal(305) and record.ok
    (row,) = conn.inserts
    assert row[0] == selection.invocation_id and row[5] == 4904 and row[6] == "000000490426000013"
    assert json.loads(row[16])["value"] == 17581 and row[15] == Decimal(305)
    assert row[2] == "glm-test" and row[21] == "glm-served"


def test_a_value_that_is_not_a_candidate_is_a_refusal_not_a_fact(seated):
    selection = select_headcount(
        _Conn(),
        cik=1,
        accession="a",
        form="10-K",
        issuer_label="X",
        candidates=AEP,
        caller="t",
        transport=lambda *_: (200, _answer(20000, 0, "made up")),
    )
    assert selection.value is None and "not a candidate" in selection.reason


def test_null_and_garbage_answers_decline_rather_than_guess(seated):
    null = select_headcount(
        _Conn(),
        cik=1,
        accession="a",
        form="10-K",
        issuer_label="X",
        candidates=AEP,
        caller="t",
        transport=lambda *_: (200, _answer(None, None, "no total stated")),
    )
    garbage = select_headcount(
        _Conn(),
        cik=1,
        accession="b",
        form="10-K",
        issuer_label="X",
        candidates=AEP,
        caller="t",
        transport=lambda *_: (200, json.dumps({"choices": [{"message": {"content": "sure!"}}], "usage": {}}).encode()),
    )
    assert null.value is None and null.reason == "no total stated"
    assert garbage.value is None and "unparseable" in garbage.reason


def test_a_prior_invocation_is_replayed_without_calling_the_provider(seated):
    row = (
        "model-invocation:" + "a" * 64,
        {"value": 17581, "candidate_index": 0, "reason": "stored"},
        "b" * 64,
        244,
        61,
        "c" * 64,
        "zhipu-glm-coding-plan",
        "glm-served-earlier",
    )
    conn = _Conn(replay_row=row)

    def transport(*_):
        raise AssertionError("the provider must not be called on replay")

    selection = select_headcount(
        conn,
        cik=4904,
        accession="acc",
        form="10-K",
        issuer_label="AEP",
        candidates=AEP,
        caller="t",
        transport=transport,
    )
    assert selection.replayed and selection.value == 17581 and selection.invocation_id.endswith("a" * 64)
    assert selection.served_model == "glm-served-earlier" and selection.extractor.startswith(
        "model:glm-served-earlier:"
    )
    assert conn.inserts == [] and list(seated) == []


def test_probe_mode_neither_replays_nor_records(seated):
    conn = _Conn(
        replay_row=(
            "model-invocation:" + "a" * 64,
            {"value": 6994, "candidate_index": 1, "reason": "stored"},
            None,
            1,
            1,
            "c" * 64,
            "p",
        )
    )
    selection = select_headcount(
        conn,
        cik=4904,
        accession="acc",
        form="10-K",
        issuer_label="AEP",
        candidates=AEP,
        caller="t",
        persist=False,
        transport=lambda *_: (200, _answer(17581, 0)),
    )
    assert selection.value == 17581 and not selection.replayed and conn.inserts == []


def test_a_vendor_error_is_a_failed_ledger_row_a_recorded_refusal_and_raises(seated):
    conn = _Conn()
    with pytest.raises(RuntimeError, match="HTTP 429"):
        select_headcount(
            conn,
            cik=1,
            accession="a",
            form="10-K",
            issuer_label="X",
            candidates=AEP,
            caller="t",
            transport=lambda *_: (429, b'{"error":{"code":"1113"}}'),
        )
    (record,) = seated
    assert record.ok is False and record.status_code == 429
    (row,) = conn.inserts  # the error is an invocation that happened
    assert row[12] == 429 and json.loads(row[16])["value"] is None and "HTTP 429" in json.loads(row[16])["reason"]


def test_replay_is_keyed_on_the_exact_request_so_a_changed_candidate_set_is_asked_afresh(seated):
    """Review on #754: same filing, different candidates (or schema/decoding) = a new ask."""
    asked = []
    conn = _Conn()  # no stored row matches any request digest

    def transport(url, headers, body):
        asked.append(json.loads(body)["messages"][1]["content"])
        return 200, _answer(17581, 0)

    select_headcount(
        conn,
        cik=4904,
        accession="acc",
        form="10-K",
        issuer_label="AEP",
        candidates=AEP,
        caller="t",
        transport=transport,
    )
    select_headcount(
        conn,
        cik=4904,
        accession="acc",
        form="10-K",
        issuer_label="AEP",
        candidates=AEP[:1],
        caller="t",
        transport=transport,
    )
    assert len(asked) == 2 and asked[0] != asked[1]
    assert conn.inserts[0][10] != conn.inserts[1][10]  # request_sha256 differs
