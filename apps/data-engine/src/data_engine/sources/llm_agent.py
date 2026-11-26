"""The SDK agent path of the model call (#1745 Slice 2).

Kept apart from `llm.py` so that importing `llm` does not load `openai` and `pydantic_ai` (about 50 MiB):
the Dagster code server imports `llm` at start and makes no model call.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import nullcontext
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx
import httpx2
from infra2_sdk.llm import DecodingSpec, LlmSettings, create_llm_agent, llm_call_context
from openai import AsyncOpenAI
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import ModelHTTPError as SdkModelHTTPError
from pydantic_ai.exceptions import RetryModelRequest
from pydantic_ai.models.zai import ZaiModel
from pydantic_ai.providers.zai import ZaiProvider
from pydantic_ai.usage import UsageLimits

from data_engine.datahub.fetch_key import FetchKey
from data_engine.sources import gateway, llm, model_policy
from data_engine.sources.gateway import record_call
from data_engine.sources.llm import (
    ENDPOINT,
    FINISH_REASON_CUT_OFF,
    SOURCE,
    ModelCapacityRefused,
    ModelTask,
    Transport,
    _cut_off,
    _request_bytes,
    _request_params,
    _token_count,
)

logger = logging.getLogger(__name__)

_WIRE_HEADERS = frozenset({"authorization", "content-type", "accept"})
_JSON_HEADERS = {"content-type": "application/json"}


@dataclass
class _Captured:
    """What the gateway answered on the latest HTTP attempt of one model call."""

    status: int = 0
    body: bytes = b""
    provider_code: str | None = None
    parsed: dict[str, Any] | None = None
    error: BaseException | None = None


class _GatewayWire(httpx2.AsyncBaseTransport):
    """Send each SDK request through the gateway: pacer, `api_call_ledger` row, and raw answer bytes."""

    def __init__(
        self,
        *,
        send: Transport,
        budget: model_policy.RetryBudget,
        pacer: model_policy.ModelPacer | None,
        identity: FetchKey,
        caller: str,
        request_params: dict[str, Any],
        answer_required: bool,
        captured: _Captured,
        wire_body: bytes,
    ) -> None:
        self._send = send
        self._wire_body = wire_body
        self._budget = budget
        self._pacer = pacer
        self._identity = identity
        self._caller = caller
        self._request_params = request_params
        self._answer_required = answer_required
        self._captured = captured

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        captured = self._captured
        attempt = self._budget.begin_attempt()
        req_url = str(request.url)
        headers = {name.title(): value for name, value in request.headers.items() if name.lower() in _WIRE_HEADERS}
        await request.aread()  # the SDK body is dropped: the stored request is what goes on the wire
        wire_body = self._wire_body
        try:
            with (
                self._pacer.admitted(self._budget.remaining_seconds) if self._pacer is not None else nullcontext(),
                record_call(
                    SOURCE,
                    ENDPOINT,
                    identity=self._identity,
                    caller=self._caller,
                    request_uri=req_url,
                    cost=0,
                    admit=False,
                ) as call,
            ):
                call.attempt = attempt
                call.request_params = self._request_params
                status, raw_body = self._send(req_url, headers, wire_body)
                call.observe(status_code=status, body=raw_body)
                captured.status, captured.body, captured.provider_code, captured.parsed = status, raw_body, None, None
                if status >= 400:
                    provider_code = model_policy.provider_code(raw_body)
                    captured.provider_code = provider_code
                    call.provider_code = provider_code
                    call.fail(llm.redacted_http_error(status, provider_code))
                    return httpx2.Response(status, content=raw_body, headers=_JSON_HEADERS)
                parsed = json.loads(raw_body.decode())
                captured.parsed = parsed
                usage = parsed.get("usage") or {}
                call.cost = Decimal(int(usage.get("total_tokens") or 0))
                call.completion_tokens = _token_count(usage.get("completion_tokens"))
                call.reasoning_tokens = _token_count(
                    (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
                )
                if self._answer_required and _cut_off(parsed):
                    call.fail(f"finish_reason={FINISH_REASON_CUT_OFF}: the answer was cut at max_tokens")
                return httpx2.Response(status, content=raw_body, headers=_JSON_HEADERS)
        except BaseException as exc:
            captured.error = exc
            raise


class _SeatRetry(AbstractCapability[Any]):
    """The only retry layer: wait and retry when the seat contract calls the HTTP answer transient."""

    def __init__(self, budget: model_policy.RetryBudget, captured: _Captured) -> None:
        super().__init__()
        self._budget = budget
        self._captured = captured

    async def on_model_request_error(self, ctx: Any, *, request_context: Any, error: Exception) -> Any:
        status = self._captured.status
        if not isinstance(error, SdkModelHTTPError) or status < 400:
            raise error
        wait = self._budget.wait_before_next(status, self._captured.provider_code)
        if wait is None:
            raise error
        logger.warning(
            "model call answered HTTP %s (provider code %s) on attempt %s; waiting %.1f s",
            status,
            self._captured.provider_code,
            self._budget.attempts,
            wait,
        )
        model_policy.sleep(wait)
        raise RetryModelRequest()


def _call_site(task: ModelTask) -> str:
    return "data-engine." + re.sub(r"[^a-z0-9_.-]+", "-", task.prompt_version.split(":")[0].lower())[:48]


def _close(http_client: httpx2.AsyncClient) -> None:
    """Close the client without letting a close error replace the call result or error."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        try:
            asyncio.run(http_client.aclose())
        except Exception:
            logger.warning("closing the model HTTP client failed", exc_info=True)


def _httpx_send(timeout_seconds: int) -> Transport:
    def _send(url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
        with httpx.Client(timeout=float(timeout_seconds)) as http_client:
            response = http_client.post(url, content=body, headers=headers)
        return int(response.status_code), bytes(response.content)

    return _send


def call_provider(
    llm_cfg: LlmSettings,
    user_content: str,
    request_body: dict[str, Any],
    decoding: DecodingSpec,
    task: ModelTask,
    caller: str,
    identity: FetchKey,
    transport: Transport | None,
) -> tuple[int, bytes, dict[str, Any], dict[str, Any], str | None]:
    """Run one model call through the SDK agent.

    The SDK drives retry, telemetry and guards. The gateway sends `_request_bytes(request_body)`, the stored
    request, so the wire bytes equal what `request_sha256` hashes. The answer bytes are the source of truth.
    """
    budget = model_policy.RetryBudget()
    captured = _Captured()
    wire = _GatewayWire(
        send=transport or _httpx_send(task.timeout_seconds),
        budget=budget,
        pacer=model_policy.pacer_for(gateway.capacity_environment()),
        identity=identity,
        caller=caller,
        request_params=_request_params(request_body, decoding),
        answer_required=task.answer_required,
        captured=captured,
        wire_body=_request_bytes(request_body),
    )
    http_client = httpx2.AsyncClient(transport=wire)
    client = AsyncOpenAI(
        api_key=llm_cfg.api_key,
        base_url=llm_cfg.base_url,
        max_retries=0,
        http_client=http_client,
    )
    call_site = _call_site(task)
    agent_decoding = DecodingSpec(
        decoding_version=decoding.decoding_version,
        thinking=decoding.thinking,
        temperature=decoding.temperature,
        max_tokens=task.max_tokens,
        timeout=float(task.timeout_seconds),
    )
    agent = create_llm_agent(
        llm_cfg,
        model=ZaiModel(str(request_body["model"]), provider=ZaiProvider(openai_client=client)),
        call_site=call_site,
        decoding=agent_decoding,
        instructions=task.instructions,
        usage_limits=UsageLimits(request_limit=1),
        max_attempts=1,
        retries=0,
        capabilities=[_SeatRetry(budget, captured)],
    )
    with llm_call_context(
        call_site=call_site,
        decoding_version=decoding.decoding_version,
        domain_run_id=gateway.current_run_key(),
        prompt_sha256=task.prompt_sha256,
    ):
        try:
            agent.run_sync(user_content)
        except Exception as failure:
            return _after_failed_run(captured, failure)
        finally:
            _close(http_client)
    return _answered(captured)


def _answered(captured: _Captured) -> tuple[int, bytes, dict[str, Any], dict[str, Any], str | None]:
    if captured.status >= 400:
        return captured.status, captured.body, {}, {}, captured.provider_code
    payload = dict(captured.parsed or {})
    return captured.status, captured.body, payload, payload.get("usage") or {}, None


def _after_failed_run(
    captured: _Captured, failure: Exception
) -> tuple[int, bytes, dict[str, Any], dict[str, Any], str | None]:
    """Map an agent failure back to the gateway answer, as the pre-SDK path did."""
    error = captured.error
    if isinstance(error, gateway.CapacityExceeded):
        if captured.status == 0:
            raise ModelCapacityRefused(error.reason, transient=error.transient) from error
        return _answered(captured)
    if isinstance(error, OSError | httpx.HTTPError):
        # A read timeout, a refused connection or a body cut short. Each caller handles ModelHTTPError for
        # one filing; the bare error stopped the lane of the whole universe.
        raise llm.ModelHTTPError(0, f"{SOURCE}: no answer: {type(error).__name__}: {error}") from error
    if error is not None:
        raise error
    if captured.status >= 400 or captured.parsed is not None:
        # The agent judged the answer (empty text after a cut-off, a usage limit); the stored bytes decide.
        return _answered(captured)
    raise failure
