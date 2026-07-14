from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from data_engine.batches.staging_topt_capture.e0_slice import (
    ADAPTER_ID,
    ADAPTER_VERSION,
    ASSIGNED_CONFIDENCE,
    CHART_URL,
    CORPUS_PATH,
    EXPECTED_DENOMINATOR,
    EXPECTED_PARENT,
    MAXIMUM_ATTEMPTS,
    SOURCE_ID,
    SOURCE_VERSION,
    ExpectedYahooBar,
    FrozenYahooRequestPlan,
    YahooChartAdapter,
    YahooChartRequest,
    YahooDailyBar,
    freeze_yahoo_request_plan,
    load_frozen_e0_corpus,
    parse_yahoo_chart_response,
)
from pydantic import ValidationError

REPOSITORY_ROOT = next(
    parent
    for parent in Path(__file__).resolve().parents
    if (parent / "governance" / "vision-issue-graph.json").is_file()
)
SCOPE_SHA256 = "5" * 64
SCOPE_ID = f"capture-scope:{SCOPE_SHA256}"


def _payload() -> dict[str, Any]:
    corpus = load_frozen_e0_corpus(REPOSITORY_ROOT)
    return json.loads(corpus.raw_body)


def _body(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), allow_nan=False).encode()


def _result(payload: dict[str, Any]) -> dict[str, Any]:
    return payload["chart"]["result"][0]


def _plan() -> FrozenYahooRequestPlan:
    request = load_frozen_e0_corpus(REPOSITORY_ROOT).request
    return freeze_yahoo_request_plan(
        request,
        capture_scope_id=SCOPE_ID,
        capture_scope_sha256=SCOPE_SHA256,
    )


def test_frozen_corpus_binds_parent_denominator_request_and_raw_bytes() -> None:
    corpus = load_frozen_e0_corpus(REPOSITORY_ROOT)

    assert corpus.corpus_sha256 == "0261ec9f9b3763c3182a6daabf2e5e4652d351f0342a491f8279cc818d7af9e9"
    assert corpus.accepted_parent == EXPECTED_PARENT
    assert corpus.denominator == EXPECTED_DENOMINATOR
    assert corpus.request.source_id == SOURCE_ID
    assert corpus.request.source_version == SOURCE_VERSION
    assert corpus.request.adapter_id == ADAPTER_ID
    assert corpus.request.adapter_version == ADAPTER_VERSION
    assert corpus.request.maximum_attempts == MAXIMUM_ATTEMPTS
    assert corpus.raw_sha256 == "a6bf655955317bd4940ab646d9cc93fb23562c70ad469d62d97ebe01da0efbdc"


def test_parser_preserves_decimal_tokens_identity_and_unadjusted_policy() -> None:
    corpus = load_frozen_e0_corpus(REPOSITORY_ROOT)
    bar = parse_yahoo_chart_response(corpus.raw_body, corpus.request)

    assert bar.expected_projection() == corpus.expected_bar.model_dump()
    assert bar.open == Decimal("166.97000122070312")
    assert bar.high == Decimal("174.6199951171875")
    assert bar.low == Decimal("166.9600067138672")
    assert bar.close == Decimal("174.39999389648438")
    assert bar.adjusted_close == Decimal("174.39999389648438")
    assert bar.confidence == ASSIGNED_CONFIDENCE
    assert bar.price_basis == "unadjusted"
    assert bar.adjusted_close_use == "reconciliation-only"
    assert bar.adjusted_close_factor_visible is False
    assert bar.raw_response_sha256 == corpus.raw_sha256
    assert not any(isinstance(value, float) for value in bar.model_dump().values())


def test_mocked_http_consumes_one_frozen_plan_without_runtime_activation() -> None:
    corpus = load_frozen_e0_corpus(REPOSITORY_ROOT)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, headers={"content-type": "application/json; charset=utf-8"}, content=corpus.raw_body)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        bar = YahooChartAdapter(client).fetch_once(_plan())

    assert bar.expected_projection() == corpus.expected_bar.model_dump()
    assert len(seen) == 1
    assert str(seen[0].url).startswith(CHART_URL.format(symbol="NVDA"))
    assert seen[0].url.params["interval"] == "1d"
    assert seen[0].url.params["period1"] == "1774915200"
    assert seen[0].url.params["period2"] == "1775001600"
    assert seen[0].headers["user-agent"] == "TrueAlpha D3 bounded research capture"


def test_http_is_not_attempted_without_a_valid_frozen_plan() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter = YahooChartAdapter(client)
        with pytest.raises(TypeError, match="frozen Yahoo request plan"):
            adapter.fetch_once(None)  # type: ignore[arg-type]

    assert calls == 0


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value["chart"].update(error={"code": "Bad Request"}), "contains an error"),
        (lambda value: value["chart"].update(result=[]), "exactly one result"),
        (
            lambda value: value["chart"].update(result=[_result(value), dict(_result(value))]),
            "exactly one result",
        ),
        (lambda value: _result(value)["meta"].update(symbol="AAPL"), "metadata mismatch: symbol"),
        (lambda value: _result(value)["meta"].update(currency="EUR"), "metadata mismatch: currency"),
        (lambda value: _result(value)["meta"].update(instrumentType="ETF"), "metadata mismatch: instrumentType"),
        (
            lambda value: _result(value)["meta"].update(exchangeTimezoneName="UTC"),
            "metadata mismatch: exchangeTimezoneName",
        ),
        (
            lambda value: _result(value).update(timestamp=[_result(value)["timestamp"][0] + 86400]),
            "expected trading session",
        ),
        (
            lambda value: _result(value)["indicators"]["quote"][0].update(open=[]),
            "series lengths differ",
        ),
        (
            lambda value: _result(value)["indicators"]["quote"][0]["open"].__setitem__(0, None),
            "open must be a JSON number",
        ),
        (
            lambda value: _result(value)["indicators"]["adjclose"][0]["adjclose"].__setitem__(0, None),
            "adjusted_close must be a JSON number",
        ),
        (
            lambda value: _result(value)["indicators"]["quote"][0]["volume"].__setitem__(0, 2.5),
            "volume must be an integral",
        ),
        (
            lambda value: _result(value)["indicators"]["quote"][0]["volume"].__setitem__(0, -1),
            "volume must be nonnegative",
        ),
        (
            lambda value: _result(value)["indicators"]["quote"][0]["open"].__setitem__(0, 0),
            "greater than 0",
        ),
        (
            lambda value: _result(value)["indicators"]["quote"][0]["high"].__setitem__(0, 100),
            "high is below",
        ),
    ],
)
def test_parser_fails_closed_on_malformed_yahoo_payloads(mutation, message) -> None:
    payload = _payload()
    mutation(payload)
    with pytest.raises((ValueError, ValidationError), match=message):
        parse_yahoo_chart_response(_body(payload), load_frozen_e0_corpus(REPOSITORY_ROOT).request)


@pytest.mark.parametrize(
    "body",
    [
        b"not-json",
        b"[]",
        b'{"chart":{"result":null,"result":[]}}',
        b'{"chart":{"result":[],"error":NaN}}',
    ],
)
def test_parser_rejects_malformed_root_duplicate_keys_and_nonfinite_json(body: bytes) -> None:
    with pytest.raises(ValueError):
        parse_yahoo_chart_response(body, load_frozen_e0_corpus(REPOSITORY_ROOT).request)


def test_request_rejects_wide_window_wrong_date_and_attempt_budget() -> None:
    request = load_frozen_e0_corpus(REPOSITORY_ROOT).request
    payload = request.model_dump(mode="json")

    with pytest.raises(ValidationError, match="exactly one day"):
        YahooChartRequest.model_validate(
            {**payload, "query_end_exclusive": (request.query_end_exclusive + timedelta(days=1)).isoformat()}
        )
    with pytest.raises(ValidationError, match="expected trading date"):
        YahooChartRequest.model_validate(
            {**payload, "expected_trading_date": (request.expected_trading_date + timedelta(days=1)).isoformat()}
        )
    with pytest.raises(ValidationError):
        YahooChartRequest.model_validate({**payload, "maximum_attempts": MAXIMUM_ATTEMPTS + 1})
    with pytest.raises(ValidationError):
        YahooChartRequest.model_validate({**payload, "maximum_attempts": 1.0})


def test_models_reject_binary_float_prices_confidence_and_volume() -> None:
    expected = load_frozen_e0_corpus(REPOSITORY_ROOT).expected_bar.model_dump(mode="json")
    for mutation in ({"open": 166.97}, {"confidence": 0.8}, {"volume": 226181300.0}):
        with pytest.raises(ValidationError):
            ExpectedYahooBar.model_validate({**expected, **mutation})

    bar = parse_yahoo_chart_response(
        load_frozen_e0_corpus(REPOSITORY_ROOT).raw_body,
        load_frozen_e0_corpus(REPOSITORY_ROOT).request,
    )
    with pytest.raises(ValidationError):
        YahooDailyBar.model_validate({**bar.model_dump(mode="json"), "close": 174.4})


def test_call_plan_is_content_addressed_and_detects_copy_drift() -> None:
    plan = _plan()
    plan.verify()
    assert plan.call_plan_id == f"d3-yahoo-call-plan:{plan.configuration_sha256}"
    assert plan.url == CHART_URL.format(symbol="NVDA")

    drifted = plan.model_copy(update={"params": (*plan.params, ("includePrePost", "true"))})
    with pytest.raises(ValueError, match="request drifted"):
        drifted.verify()


def test_http_status_and_content_type_fail_before_parsing() -> None:
    corpus = load_frozen_e0_corpus(REPOSITORY_ROOT)

    def status_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, request=request, headers={"content-type": "application/json"})

    with httpx.Client(transport=httpx.MockTransport(status_handler)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            YahooChartAdapter(client).fetch_once(_plan())

    def type_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, headers={"content-type": "text/html"}, content=corpus.raw_body)

    with httpx.Client(transport=httpx.MockTransport(type_handler)) as client:
        with pytest.raises(ValueError, match="content type"):
            YahooChartAdapter(client).fetch_once(_plan())


def test_corpus_path_is_canonical_and_batch_private() -> None:
    assert CORPUS_PATH.as_posix() == "apps/data-engine/tests/fixtures/staging_topt_capture/corpus.v1.json"
