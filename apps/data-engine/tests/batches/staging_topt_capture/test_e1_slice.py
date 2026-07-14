from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from data_engine.batches.staging_topt_capture.e0_slice import (
    FrozenYahooRequestPlan,
    freeze_yahoo_request_plan,
    load_frozen_e0_corpus,
)
from data_engine.batches.staging_topt_capture.e1_slice import (
    D3E1InteractionError,
    InMemoryRawResponseLedger,
    LandedRawResponse,
    YahooDailyBarNormalizer,
    YahooRawHttpAdapter,
    execute_e1_tiny_interaction,
)

REPOSITORY_ROOT = next(
    parent
    for parent in Path(__file__).resolve().parents
    if (parent / "governance" / "vision-issue-graph.json").is_file()
)
SCOPE_SHA256 = "6" * 64
SCOPE_ID = f"capture-scope:{SCOPE_SHA256}"
ATTEMPTED_AT = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


def _plan() -> FrozenYahooRequestPlan:
    request = load_frozen_e0_corpus(REPOSITORY_ROOT).request
    return freeze_yahoo_request_plan(
        request,
        capture_scope_id=SCOPE_ID,
        capture_scope_sha256=SCOPE_SHA256,
    )


def _execute(handler):
    ledger = InMemoryRawResponseLedger()
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter = YahooRawHttpAdapter(client)
        execution = execute_e1_tiny_interaction(
            _plan(),
            adapter=adapter,
            normalizer=YahooDailyBarNormalizer(),
            raw_ledger=ledger,
            clock=lambda: ATTEMPTED_AT,
        )
    return execution, ledger


def test_tiny_interaction_retains_exact_raw_bytes_and_version_identities() -> None:
    corpus = load_frozen_e0_corpus(REPOSITORY_ROOT)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json; charset=utf-8"},
            content=corpus.raw_body,
        )

    execution, ledger = _execute(handler)
    result = execution.result

    assert result.source_id == "source.yahoo-chart-public"
    assert result.source_version == "1.0.0"
    assert result.adapter_id == "data_engine.d3.yahoo_chart_adapter"
    assert result.adapter_version == "1.0.0"
    assert result.semantic_type_id == "semantic.market-price"
    assert result.semantic_type_version == "1.0.0"
    assert result.source_call_count == 1
    assert result.landed_response_count == 1
    assert result.raw_bytes_retained is True
    assert result.persisted_raw_rows == result.persisted_normalized_rows == 0
    assert result.raw_object_ids == (f"raw-object:{corpus.raw_sha256}",)
    assert execution.landed_raw_responses == ledger.entries
    assert ledger.entries[0].response.body == corpus.raw_body
    assert result.normalized_bar.expected_projection() == corpus.expected_bar.model_dump()
    assert result.interaction_id == f"d3-e1-interaction:{result.content_sha256}"


def test_retryable_response_is_landed_before_a_successful_retry() -> None:
    corpus = load_frozen_e0_corpus(REPOSITORY_ROOT)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, headers={"content-type": "text/plain"}, content=b"temporary")
        return httpx.Response(200, headers={"content-type": "application/json"}, content=corpus.raw_body)

    execution, ledger = _execute(handler)

    assert calls == execution.result.source_call_count == 2
    assert tuple(item.outcome for item in execution.result.attempts) == (
        "retryable-http-status",
        "success",
    )
    assert tuple(item.response.body for item in ledger.entries) == (b"temporary", corpus.raw_body)
    assert execution.result.raw_object_ids == tuple(item.response.raw_object_id for item in ledger.entries)


def test_retryable_status_exhausts_exact_frozen_attempt_budget() -> None:
    calls = 0
    ledger = InMemoryRawResponseLedger()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, headers={"content-type": "text/plain"}, content=f"try-{calls}".encode())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter = YahooRawHttpAdapter(client)
        with pytest.raises(D3E1InteractionError, match="attempt budget exhausted") as captured:
            execute_e1_tiny_interaction(
                _plan(),
                adapter=adapter,
                normalizer=YahooDailyBarNormalizer(),
                raw_ledger=ledger,
                clock=lambda: ATTEMPTED_AT,
            )

    assert calls == adapter.source_call_count == captured.value.source_call_count == 3
    assert len(ledger.entries) == len(captured.value.landed_raw_responses) == 3
    assert tuple(item.outcome for item in captured.value.attempts) == (
        "retryable-http-status",
        "retryable-http-status",
        "retryable-http-status",
    )


def test_transport_errors_are_bounded_and_do_not_invent_raw_bytes() -> None:
    calls = 0
    ledger = InMemoryRawResponseLedger()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("offline", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter = YahooRawHttpAdapter(client)
        with pytest.raises(D3E1InteractionError, match="transport attempt budget exhausted") as captured:
            execute_e1_tiny_interaction(
                _plan(),
                adapter=adapter,
                normalizer=YahooDailyBarNormalizer(),
                raw_ledger=ledger,
                clock=lambda: ATTEMPTED_AT,
            )

    assert calls == adapter.source_call_count == captured.value.source_call_count == 3
    assert not ledger.entries
    assert not captured.value.landed_raw_responses
    assert all(item.outcome == "transport-error" for item in captured.value.attempts)
    assert all(item.raw_object_id is None for item in captured.value.attempts)


def test_non_retryable_http_status_lands_once_and_fails_closed() -> None:
    calls = 0
    ledger = InMemoryRawResponseLedger()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404, headers={"content-type": "text/plain"}, content=b"missing")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter = YahooRawHttpAdapter(client)
        with pytest.raises(D3E1InteractionError, match="non-retryable") as captured:
            execute_e1_tiny_interaction(
                _plan(),
                adapter=adapter,
                normalizer=YahooDailyBarNormalizer(),
                raw_ledger=ledger,
                clock=lambda: ATTEMPTED_AT,
            )

    assert calls == captured.value.source_call_count == 1
    assert ledger.entries[0].response.body == b"missing"
    assert captured.value.attempts[0].outcome == "terminal-http-status"


def test_malformed_success_is_landed_once_and_never_retried() -> None:
    calls = 0
    ledger = InMemoryRawResponseLedger()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, headers={"content-type": "application/json"}, content=b"not-json")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter = YahooRawHttpAdapter(client)
        with pytest.raises(D3E1InteractionError, match="failed normalization") as captured:
            execute_e1_tiny_interaction(
                _plan(),
                adapter=adapter,
                normalizer=YahooDailyBarNormalizer(),
                raw_ledger=ledger,
                clock=lambda: ATTEMPTED_AT,
            )

    assert calls == captured.value.source_call_count == 1
    assert ledger.entries[0].response.body == b"not-json"
    assert captured.value.attempts[0].outcome == "normalization-error"


def test_normalizer_receives_only_an_already_landed_response() -> None:
    corpus = load_frozen_e0_corpus(REPOSITORY_ROOT)
    ledger = InMemoryRawResponseLedger()

    class ObservingNormalizer(YahooDailyBarNormalizer):
        def normalize(self, landed: LandedRawResponse, plan: FrozenYahooRequestPlan):
            assert ledger.contains(landed)
            assert landed.response.body == corpus.raw_body
            return super().normalize(landed, plan)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/json"}, content=corpus.raw_body)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        execution = execute_e1_tiny_interaction(
            _plan(),
            adapter=YahooRawHttpAdapter(client),
            normalizer=ObservingNormalizer(),
            raw_ledger=ledger,
            clock=lambda: ATTEMPTED_AT,
        )

    assert execution.result.attempts[-1].outcome == "success"


def test_plan_drift_is_rejected_before_any_source_call() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    drifted = _plan().model_copy(update={"configuration_sha256": "0" * 64})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter = YahooRawHttpAdapter(client)
        with pytest.raises(ValueError, match="configuration hash drifted"):
            execute_e1_tiny_interaction(
                drifted,
                adapter=adapter,
                normalizer=YahooDailyBarNormalizer(),
                raw_ledger=InMemoryRawResponseLedger(),
                clock=lambda: ATTEMPTED_AT,
            )

    assert calls == adapter.source_call_count == 0


def test_normalizer_rejects_raw_source_identity_drift() -> None:
    corpus = load_frozen_e0_corpus(REPOSITORY_ROOT)
    plan = _plan()
    ledger = InMemoryRawResponseLedger()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/json"}, content=corpus.raw_body)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        response = YahooRawHttpAdapter(client).fetch(
            plan,
            attempt_number=1,
            fetched_at=ATTEMPTED_AT,
        )

    drifted = replace(response, source_version="9.9.9")
    with pytest.raises(ValueError, match="source or call-plan identity drifted"):
        ledger.land(drifted, plan)
    assert not ledger.entries


def test_interaction_requires_a_fresh_adapter_and_raw_ledger() -> None:
    corpus = load_frozen_e0_corpus(REPOSITORY_ROOT)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/json"}, content=corpus.raw_body)

    ledger = InMemoryRawResponseLedger()
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter = YahooRawHttpAdapter(client)
        execute_e1_tiny_interaction(
            _plan(),
            adapter=adapter,
            normalizer=YahooDailyBarNormalizer(),
            raw_ledger=ledger,
            clock=lambda: ATTEMPTED_AT,
        )
        with pytest.raises(ValueError, match="fresh source adapter"):
            execute_e1_tiny_interaction(
                _plan(),
                adapter=adapter,
                normalizer=YahooDailyBarNormalizer(),
                raw_ledger=InMemoryRawResponseLedger(),
                clock=lambda: ATTEMPTED_AT,
            )
