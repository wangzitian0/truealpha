"""#1745 Slice 2: the model call runs through one `infra2_sdk` agent, with one retry layer.

No test reaches the live provider. The transport double stands in for the gateway.
"""

from __future__ import annotations

import http.client
import json
from typing import Any

import pytest
from data_engine.config import settings
from data_engine.quality import model_key_health
from data_engine.sources import gateway, llm, model_decoding, model_policy

KEY = "sk-test-not-a-real-key-0123456789"
TRANSIENT = json.dumps({"error": {"code": "1302", "message": "rate limit"}}).encode()
TERMINAL = json.dumps({"error": {"code": "1113", "message": "no credit"}}).encode()
OK = json.dumps(
    {
        "model": "glm-served",
        "choices": [{"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 20, "completion_tokens": 9, "total_tokens": 29},
    }
).encode()


@pytest.fixture(autouse=True)
def seated(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    monkeypatch.setattr(settings, "llm_api_key", KEY)
    monkeypatch.setattr(settings, "llm_small_model", "glm-test")
    monkeypatch.setattr(model_policy, "pacer_for", lambda environment: None)
    monkeypatch.setattr(gateway, "capacity_environment", lambda: "staging")
    monkeypatch.setattr(model_policy, "jitter", lambda: 0.0)
    waits: list[float] = []
    monkeypatch.setattr(model_policy, "sleep", waits.append)
    return waits


class Script:
    """Answer each request with the next scripted `(status, body)`; keep the last one for later requests."""

    def __init__(self, *answers: tuple[int, bytes]) -> None:
        self.answers = list(answers)
        self.requests: list[tuple[str, dict[str, str], dict[str, Any]]] = []

    def __call__(self, url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
        self.requests.append((url, headers, json.loads(body)))
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


def ask(transport: Script) -> llm.ModelInvocation:
    return llm.invoke(
        None,
        task=model_key_health.PROBE_TASK,
        cik=0,
        accession="",
        user_content="ping",
        caller="test:sdk-agent",
        standard="test",
        parse=lambda text: {"answered": bool(text), "text": text},
        refusal=lambda detail: {"answered": False, "reason": detail},
        persist=False,
        transport=transport,
    )


def test_one_request_carries_the_prompt_the_task_limit_and_the_json_mode() -> None:
    script = Script((200, OK))
    invocation = ask(script)
    ((url, headers, body),) = script.requests
    assert url == f"{settings.llm_base_url.rstrip('/')}/chat/completions"
    assert headers["Authorization"] == f"Bearer {KEY}"
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert body["messages"][0]["content"] == model_key_health.PROBE_TASK.instructions
    assert body["response_format"] == {"type": "json_object"} and body["temperature"] == 0
    assert body["max_tokens"] == model_key_health.PROBE_TASK.max_tokens
    assert invocation.decision == {"answered": True, "text": '{"ok": true}'}
    assert (invocation.prompt_tokens, invocation.completion_tokens, invocation.served_model) == (20, 9, "glm-served")


def test_a_transient_answer_is_retried_by_the_seat_policy_once_per_wait(seated: list[float]) -> None:
    script = Script((429, TRANSIENT), (429, TRANSIENT), (200, OK))
    invocation = ask(script)
    retry = model_policy.retry_spec()
    assert len(script.requests) == 3
    assert seated == [retry.base_backoff_seconds(1), retry.base_backoff_seconds(2)]
    assert invocation.decision["answered"] is True


def test_the_sdk_adds_no_second_retry_layer_when_the_provider_code_is_terminal(seated: list[float]) -> None:
    script = Script((429, TERMINAL))
    with pytest.raises(llm.ModelHTTPError) as raised:
        ask(script)
    assert len(script.requests) == 1 and seated == []
    assert raised.value.status_code == 429 and raised.value.provider_code == "1113"


def test_the_attempts_stop_at_the_seat_maximum(seated: list[float]) -> None:
    script = Script((429, TRANSIENT))
    with pytest.raises(llm.ModelHTTPError):
        ask(script)
    assert len(script.requests) == model_policy.retry_spec().max_attempts
    assert len(seated) == model_policy.retry_spec().max_attempts - 1


def test_a_body_that_is_not_json_raises_the_json_error_after_one_request() -> None:
    script = Script((200, b"<html>gateway</html>"))
    with pytest.raises(json.JSONDecodeError):
        ask(script)
    assert len(script.requests) == 1


def test_an_empty_cut_off_answer_is_kept_as_the_stored_bytes_after_one_request() -> None:
    cut = json.dumps(
        {
            "model": "glm-served",
            "choices": [{"message": {"content": ""}, "finish_reason": "length"}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 300, "total_tokens": 320},
        }
    ).encode()
    script = Script((200, cut))
    invocation = ask(script)
    assert len(script.requests) == 1
    assert invocation.response_sha256 is not None and invocation.completion_tokens == 300


@pytest.mark.parametrize("model", ["glm-5.3-flash", "glm-4.7"])
@pytest.mark.parametrize("version", ["v1", "v2"])
def test_the_bytes_on_the_wire_are_the_stored_request(
    monkeypatch: pytest.MonkeyPatch, model: str, version: str
) -> None:
    monkeypatch.setattr(settings, "llm_small_model", model)
    monkeypatch.setattr(model_policy, "seat_contract", _seat_with(version))
    sent: list[bytes] = []

    def transport(url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
        sent.append(body)
        return 200, OK

    ask_with(transport)
    task = model_key_health.PROBE_TASK
    stored = llm.build_chat_request(task, "ping", model=model, decoding=model_decoding.named(version))
    assert sent == [llm._request_bytes(stored)]
    assert json.loads(sent[0]) == stored
    assert ("thinking" in stored, "clear_thinking" in json.dumps(stored)) == (True, False)


def _seat_with(version: str):  # type: ignore[no-untyped-def]
    seat = model_policy.seat_contract()
    limits = seat.limits_for("staging")
    assert limits is not None
    named = seat.model_copy(
        update={
            "environments": {**seat.environments, "staging": limits.model_copy(update={"decoding_version": version})}
        }
    )
    return lambda: named


def ask_with(transport: Any) -> llm.ModelInvocation:
    return ask(transport)  # type: ignore[arg-type]


def test_importing_the_llm_source_loads_neither_openai_nor_pydantic_ai() -> None:
    """The code server imports `llm` at start; the agent stack (about 50 MiB) loads on the first model call."""
    import subprocess
    import sys

    code = (
        "import sys, data_engine.sources.llm\n"
        "bad = sorted(m for m in sys.modules if m.split('.')[0] in {'openai', 'pydantic_ai', 'httpx2'})\n"
        "print(','.join(bad)); sys.exit(1 if bad else 0)"
    )
    done = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, f"loaded at import: {done.stdout.strip()[:300]}"


def test_a_read_timeout_is_the_error_that_each_caller_handles_for_one_filing() -> None:
    """A provider that does not answer raised a bare `TimeoutError`. The supply-chain lane handles
    `ModelHTTPError` for one filing, so the bare error stopped the lane of the whole universe."""

    def no_answer(url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
        raise TimeoutError("The read operation timed out")

    with pytest.raises(llm.ModelHTTPError) as raised:
        ask(no_answer)  # type: ignore[arg-type]
    assert raised.value.status_code == 0
    assert "TimeoutError" in str(raised.value)
    assert isinstance(raised.value.__cause__, TimeoutError)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (llm.ModelHTTPError(0, "no answer"), True),
        (llm.ModelHTTPError(503, "provider down"), True),
        (llm.ModelHTTPError(400, "refused"), False),
        (llm.ModelCapacityRefused("no call slot"), False),
        (llm.ModelOutputTruncated(2400), False),
    ],
)
def test_only_status_0_and_a_server_error_are_a_missing_answer(error: llm.ModelHTTPError, expected: bool) -> None:
    """#1816: the next run can get an answer after status 0 or 5xx. A refusal or a cut-off answer repeats."""
    assert error.no_answer is expected


@pytest.mark.parametrize("failure", [TimeoutError("timed out"), http.client.IncompleteRead(b"part")])
def test_a_body_cut_short_is_a_missing_answer_like_a_timeout(failure: Exception) -> None:
    """#1816: `http.client.HTTPException` is not an `OSError`. A reply cut short stopped the lane of a
    universe, the symptom that the status 0 mapping removes for a timeout."""

    def broken(url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
        raise failure

    with pytest.raises(llm.ModelHTTPError) as raised:
        ask(broken)  # type: ignore[arg-type]
    assert raised.value.status_code == 0 and raised.value.no_answer
