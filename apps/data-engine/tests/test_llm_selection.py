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
        if text.startswith("select pg_advisory_xact_lock("):
            return _R([(None,)])
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
    # Valid JSON that is not the object asked for — a list, a number — is a refusal too, never
    # a crash (#845: the theme parser died on a list-shaped answer and took the op with it).
    for content in ('[{"value": 305}]', "305", "null"):
        shaped = select_headcount(
            _Conn(),
            cik=1,
            accession="c",
            form="10-K",
            issuer_label="X",
            candidates=AEP,
            caller="t",
            transport=lambda *_, content=content: (
                200,
                json.dumps({"choices": [{"message": {"content": content}}], "usage": {}}).encode(),
            ),
        )
        assert shaped.value is None and "unparseable" in shaped.reason, content


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
        "glm-test",
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


def test_supply_chain_extraction_compacts_large_10k_prompt_and_records_partners(seated):
    from data_engine.sources.llm import identify_supply_chain_partners

    conn = _Conn()
    filing_text = "Item 1. Business. We purchase wafers from TSMC and supplies to McKesson Corporation. " * 400
    assert len(filing_text.encode("utf-8")) > 16_384

    def transport(url, headers, body):
        payload = {
            "model": "glm-5.3-flash",
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "partners": [
                                    {
                                        "partner_name": "Taiwan Semiconductor Manufacturing Company Limited",
                                        "ticker": "TSM",
                                        "relation_type": "supplier",
                                        "revenue_share_pct": None,
                                        "evidence_quote": "We purchase wafers from TSMC.",
                                    },
                                    {
                                        "partner_name": "McKesson Corporation",
                                        "ticker": "MCK",
                                        "relation_type": "customer",
                                        "revenue_share_pct": 14.5,
                                        "evidence_quote": "We supplies to McKesson Corporation.",
                                    },
                                ]
                            }
                        )
                    }
                }
            ],
            "usage": {"prompt_tokens": 8200, "completion_tokens": 110, "total_tokens": 8310},
        }
        return 200, json.dumps(payload).encode("utf-8")

    result = identify_supply_chain_partners(
        conn,
        cik=1045810,
        accession="0001045810-25-000023",
        issuer_label="NVDA",
        filing_text=filing_text,
        transport=transport,
    )
    assert len(result.partners) == 2
    assert result.partners[0].partner_name == "Taiwan Semiconductor Manufacturing Company Limited"
    assert result.partners[0].relation_type == "supplier"
    assert result.partners[0].ticker == "TSM"
    assert result.partners[1].partner_name == "McKesson Corporation"
    assert result.partners[1].relation_type == "customer"
    assert result.partners[1].revenue_share_pct == Decimal("14.5")
    assert result.extractor.startswith("model:glm-5.3-flash:")

    # Stored request JSONB is compacted for >16KB prompts so raw 10-K text does not bloat Postgres
    (row,) = conn.inserts
    stored_req = json.loads(row[17])
    user_msg = stored_req["messages"][1]
    assert "content" not in user_msg
    assert len(user_msg["content_sha256"]) == 64
    assert user_msg["content_bytes"] > 16_384


def test_context_window_exceeded_logs_alert_records_413_refusal_and_raises_model_context_limit_error(
    seated, caplog: pytest.LogCaptureFixture
):
    from data_engine.sources.llm import ModelContextLimitError, identify_supply_chain_partners

    conn = _Conn()
    # > 2.5M chars -> estimated > 1,000,000 tokens
    oversized_10k = "A" * 2_505_000

    def must_not_call_transport(*_args):
        raise AssertionError("pre-flight >1M guard must reject before HTTP call")

    with caplog.at_level("ERROR"), pytest.raises(ModelContextLimitError) as exc_info:
        identify_supply_chain_partners(
            conn,
            cik=1045810,
            accession="0001045810-26-999999",
            issuer_label="NVDA",
            filing_text=oversized_10k,
            transport=must_not_call_transport,
        )

    err = exc_info.value
    assert err.status_code == 413
    assert err.cik == 1045810
    assert err.accession == "0001045810-26-999999"
    assert err.estimated_tokens > 1_000_000
    assert "context_window_exceeded" in caplog.text
    (row,) = conn.inserts
    assert row[12] == 413  # status_code
    assert "context_window_exceeded" in json.loads(row[16])["reason"]


def test_is_context_limit_response_distinguishes_context_overflow_from_unrelated_400_errors():
    from data_engine.sources.llm import _is_context_limit_response

    assert _is_context_limit_response(413, b"{}") is True
    assert (
        _is_context_limit_response(
            400,
            b'{"error":{"code":"context_length_exceeded","message":"This model maximum context length is 1000000"}}',
        )
        is True
    )
    # Unrelated 400 validation errors mentioning max_tokens or digits 1210 in request_id must NOT match
    assert (
        _is_context_limit_response(
            400,
            b'{"error":{"code":"invalid_request_error","param":"max_tokens","request_id":"req_121099"}}',
        )
        is False
    )
