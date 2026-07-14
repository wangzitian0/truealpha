"""Batch-private D3 E1 raw interaction boundary and bounded retry evidence."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol

import httpx
from data_engine.batches.staging_topt_capture.e0_slice import (
    FrozenYahooRequestPlan,
    YahooDailyBar,
    parse_yahoo_chart_response,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator
from truealpha_contracts.common import canonical_sha256

_RETRYABLE_STATUS_CODES = frozenset({408, 425, 429})


def _sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _aware_utc(value: datetime, *, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True)
class RawSourceResponse:
    """Exact source bytes and identities returned by one HTTP attempt."""

    source_id: str
    source_version: str
    adapter_id: str
    adapter_version: str
    call_plan_id: str
    configuration_sha256: str
    attempt_number: int
    status_code: int
    content_type: str
    fetched_at: datetime
    body: bytes

    def __post_init__(self) -> None:
        if self.attempt_number < 1:
            raise ValueError("raw response attempt number must be positive")
        if not 100 <= self.status_code <= 599:
            raise ValueError("raw response status code is invalid")
        if not isinstance(self.body, bytes):
            raise TypeError("raw response body must remain exact bytes")
        object.__setattr__(self, "fetched_at", _aware_utc(self.fetched_at, label="fetched_at"))

    @property
    def sha256(self) -> str:
        return _sha256(self.body)

    @property
    def raw_object_id(self) -> str:
        return f"raw-object:{self.sha256}"

    def verify_plan(self, plan: FrozenYahooRequestPlan) -> None:
        plan.verify()
        expected = (
            plan.source.source_id,
            plan.source.source_version,
            plan.source.adapter_id,
            plan.source.adapter_version,
            plan.call_plan_id,
            plan.configuration_sha256,
        )
        actual = (
            self.source_id,
            self.source_version,
            self.adapter_id,
            self.adapter_version,
            self.call_plan_id,
            self.configuration_sha256,
        )
        if actual != expected:
            raise ValueError("raw response source or call-plan identity drifted")
        if self.attempt_number > plan.request.maximum_attempts:
            raise ValueError("raw response exceeds the frozen attempt budget")


@dataclass(frozen=True)
class LandedRawResponse:
    """Token proving that an exact raw response entered the append-only E1 ledger."""

    landing_id: str
    landing_number: int
    response: RawSourceResponse


class InMemoryRawResponseLedger:
    """E1-only byte ledger; durable object/database persistence starts at E2."""

    def __init__(self) -> None:
        self._entries: list[LandedRawResponse] = []

    @property
    def entries(self) -> tuple[LandedRawResponse, ...]:
        return tuple(self._entries)

    def land(self, response: RawSourceResponse, plan: FrozenYahooRequestPlan) -> LandedRawResponse:
        response.verify_plan(plan)
        if any(entry.response.attempt_number == response.attempt_number for entry in self._entries):
            raise ValueError("a raw response is already landed for this attempt")
        landing_number = len(self._entries) + 1
        payload = {
            "call_plan_id": response.call_plan_id,
            "attempt_number": response.attempt_number,
            "landing_number": landing_number,
            "status_code": response.status_code,
            "fetched_at": response.fetched_at.isoformat(),
            "raw_object_id": response.raw_object_id,
        }
        landed = LandedRawResponse(
            landing_id=f"d3-raw-landing:{canonical_sha256(payload)}",
            landing_number=landing_number,
            response=response,
        )
        self._entries.append(landed)
        return landed

    def contains(self, landed: LandedRawResponse) -> bool:
        return any(entry is landed for entry in self._entries)


class RawSourceAdapter(Protocol):
    @property
    def source_call_count(self) -> int: ...

    def fetch(
        self,
        plan: FrozenYahooRequestPlan,
        *,
        attempt_number: int,
        fetched_at: datetime,
    ) -> RawSourceResponse: ...


class RawResponseNormalizer(Protocol):
    def normalize(
        self,
        landed: LandedRawResponse,
        plan: FrozenYahooRequestPlan,
    ) -> YahooDailyBar: ...


class YahooRawHttpAdapter:
    """HTTP-only adapter that never parses or persists a response."""

    def __init__(self, client: httpx.Client) -> None:
        self._client = client
        self._source_call_count = 0

    @property
    def source_call_count(self) -> int:
        return self._source_call_count

    def fetch(
        self,
        plan: FrozenYahooRequestPlan,
        *,
        attempt_number: int,
        fetched_at: datetime,
    ) -> RawSourceResponse:
        if not isinstance(plan, FrozenYahooRequestPlan):
            raise TypeError("a frozen Yahoo request plan is required before HTTP")
        plan.verify()
        if not 1 <= attempt_number <= plan.request.maximum_attempts:
            raise ValueError("attempt number exceeds the frozen plan")
        self._source_call_count += 1
        response = self._client.get(
            plan.url,
            params=plan.params,
            headers={"User-Agent": plan.source.user_agent, "Accept": "application/json"},
            timeout=plan.source.timeout_seconds,
        )
        return RawSourceResponse(
            source_id=plan.source.source_id,
            source_version=plan.source.source_version,
            adapter_id=plan.source.adapter_id,
            adapter_version=plan.source.adapter_version,
            call_plan_id=plan.call_plan_id,
            configuration_sha256=plan.configuration_sha256,
            attempt_number=attempt_number,
            status_code=response.status_code,
            content_type=response.headers.get("content-type", ""),
            fetched_at=fetched_at,
            body=response.content,
        )


class YahooDailyBarNormalizer:
    """Normalize only a response that has already been landed as raw bytes."""

    def normalize(self, landed: LandedRawResponse, plan: FrozenYahooRequestPlan) -> YahooDailyBar:
        if not isinstance(landed, LandedRawResponse):
            raise TypeError("a landed raw response is required before normalization")
        response = landed.response
        response.verify_plan(plan)
        if response.status_code != 200:
            raise ValueError("Yahoo normalization requires HTTP 200")
        content_type = response.content_type.split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise ValueError("Yahoo response content type is not application/json")
        return parse_yahoo_chart_response(response.body, plan.request)


class SourceCallAttempt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    attempt_number: int = Field(ge=1, le=3, strict=True)
    attempted_at: datetime
    outcome: Literal[
        "success",
        "retryable-http-status",
        "transport-error",
        "terminal-http-status",
        "normalization-error",
    ]
    status_code: int | None = Field(default=None, ge=100, le=599, strict=True)
    landing_id: str | None = Field(default=None, pattern=r"^d3-raw-landing:[0-9a-f]{64}$")
    raw_object_id: str | None = Field(default=None, pattern=r"^raw-object:[0-9a-f]{64}$")
    raw_byte_length: int | None = Field(default=None, ge=0, strict=True)
    error_class: Literal["httpx.TransportError"] | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> SourceCallAttempt:
        object.__setattr__(self, "attempted_at", _aware_utc(self.attempted_at, label="attempted_at"))
        has_raw = self.raw_object_id is not None
        if has_raw != (self.landing_id is not None and self.raw_byte_length is not None):
            raise ValueError("raw attempt evidence must be complete")
        if self.outcome == "transport-error":
            if has_raw or self.status_code is not None or self.error_class != "httpx.TransportError":
                raise ValueError("transport errors cannot claim an HTTP response")
        elif not has_raw or self.status_code is None or self.error_class is not None:
            raise ValueError("HTTP outcomes require landed raw evidence")
        return self


class D3E1TinyResult(BaseModel):
    """Content-addressed result for one bounded source interaction."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    interaction_id: str = Field(default="", pattern=r"^(?:|d3-e1-interaction:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    capture_scope_id: str = Field(pattern=r"^capture-scope:[0-9a-f]{64}$")
    capture_scope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    call_plan_id: str = Field(pattern=r"^d3-yahoo-call-plan:[0-9a-f]{64}$")
    configuration_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_id: Literal["source.yahoo-chart-public"] = "source.yahoo-chart-public"
    source_version: Literal["1.0.0"] = "1.0.0"
    adapter_id: Literal["data_engine.d3.yahoo_chart_adapter"] = "data_engine.d3.yahoo_chart_adapter"
    adapter_version: Literal["1.0.0"] = "1.0.0"
    semantic_type_id: Literal["semantic.market-price"] = "semantic.market-price"
    semantic_type_version: Literal["1.0.0"] = "1.0.0"
    maximum_attempts: int = Field(ge=1, le=3, strict=True)
    source_call_count: int = Field(ge=1, le=3, strict=True)
    landed_response_count: int = Field(ge=1, le=3, strict=True)
    landing_ids: tuple[str, ...]
    raw_object_ids: tuple[str, ...]
    raw_bytes_retained: Literal[True] = True
    normalized_bar: YahooDailyBar
    attempts: tuple[SourceCallAttempt, ...] = Field(min_length=1, max_length=3)
    persisted_raw_rows: Literal[0] = 0
    persisted_normalized_rows: Literal[0] = 0
    stable_handoff: Literal[False] = False

    @model_validator(mode="after")
    def identify(self) -> D3E1TinyResult:
        if self.source_call_count != len(self.attempts):
            raise ValueError("source call count does not match attempt evidence")
        if tuple(item.attempt_number for item in self.attempts) != tuple(range(1, self.source_call_count + 1)):
            raise ValueError("source attempts are not contiguous")
        if self.attempts[-1].outcome != "success":
            raise ValueError("accepted E1 interaction must end in success")
        landed = tuple(item for item in self.attempts if item.raw_object_id is not None)
        if self.landed_response_count != len(landed):
            raise ValueError("landed response count does not match attempt evidence")
        if self.landing_ids != tuple(item.landing_id for item in landed):
            raise ValueError("landing identities do not match attempt evidence")
        if self.raw_object_ids != tuple(item.raw_object_id for item in landed):
            raise ValueError("raw object identities do not match attempt evidence")
        if self.normalized_bar.raw_response_sha256 != self.raw_object_ids[-1].removeprefix("raw-object:"):
            raise ValueError("normalized bar does not reference the final raw response")
        payload = self.model_dump(mode="json", exclude={"interaction_id", "content_sha256"})
        expected_sha256 = canonical_sha256(payload)
        expected_id = f"d3-e1-interaction:{expected_sha256}"
        if self.content_sha256 and self.content_sha256 != expected_sha256:
            raise ValueError("E1 interaction content hash drifted")
        if self.interaction_id and self.interaction_id != expected_id:
            raise ValueError("E1 interaction identity drifted")
        object.__setattr__(self, "content_sha256", expected_sha256)
        object.__setattr__(self, "interaction_id", expected_id)
        return self


@dataclass(frozen=True)
class D3E1TinyExecution:
    """Accepted result bundled with the exact in-memory raw response bytes."""

    result: D3E1TinyResult
    landed_raw_responses: tuple[LandedRawResponse, ...]

    def __post_init__(self) -> None:
        if self.result.landing_ids != tuple(item.landing_id for item in self.landed_raw_responses):
            raise ValueError("E1 execution lost a raw landing")
        if self.result.raw_object_ids != tuple(item.response.raw_object_id for item in self.landed_raw_responses):
            raise ValueError("E1 execution raw bytes do not match the accepted result")


class D3E1InteractionError(RuntimeError):
    """Fail-closed interaction error with bounded, non-secret attempt evidence."""

    def __init__(
        self,
        message: str,
        *,
        attempts: tuple[SourceCallAttempt, ...],
        source_call_count: int,
        landed_raw_responses: tuple[LandedRawResponse, ...],
    ) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.source_call_count = source_call_count
        self.landed_raw_responses = landed_raw_responses


def _attempt_with_raw(
    response: RawSourceResponse,
    landed: LandedRawResponse,
    *,
    outcome: Literal[
        "success",
        "retryable-http-status",
        "terminal-http-status",
        "normalization-error",
    ],
) -> SourceCallAttempt:
    return SourceCallAttempt(
        attempt_number=response.attempt_number,
        attempted_at=response.fetched_at,
        outcome=outcome,
        status_code=response.status_code,
        landing_id=landed.landing_id,
        raw_object_id=response.raw_object_id,
        raw_byte_length=len(response.body),
    )


def _raise_interaction_error(
    message: str,
    *,
    attempts: list[SourceCallAttempt],
    adapter: RawSourceAdapter,
    ledger: InMemoryRawResponseLedger,
) -> None:
    raise D3E1InteractionError(
        message,
        attempts=tuple(attempts),
        source_call_count=adapter.source_call_count,
        landed_raw_responses=ledger.entries,
    )


def execute_e1_tiny_interaction(
    plan: FrozenYahooRequestPlan,
    *,
    adapter: RawSourceAdapter,
    normalizer: RawResponseNormalizer,
    raw_ledger: InMemoryRawResponseLedger,
    clock: Callable[[], datetime],
) -> D3E1TinyExecution:
    """Execute one frozen call plan without durable persistence or activation."""

    if not isinstance(plan, FrozenYahooRequestPlan):
        raise TypeError("a frozen Yahoo request plan is required before interaction")
    plan.verify()
    if adapter.source_call_count != 0:
        raise ValueError("E1 interaction requires a fresh source adapter")
    if raw_ledger.entries:
        raise ValueError("E1 interaction requires a fresh raw ledger")

    attempts: list[SourceCallAttempt] = []
    for attempt_number in range(1, plan.request.maximum_attempts + 1):
        attempted_at = _aware_utc(clock(), label="clock result")
        try:
            response = adapter.fetch(
                plan,
                attempt_number=attempt_number,
                fetched_at=attempted_at,
            )
        except httpx.TransportError:
            attempts.append(
                SourceCallAttempt(
                    attempt_number=attempt_number,
                    attempted_at=attempted_at,
                    outcome="transport-error",
                    error_class="httpx.TransportError",
                )
            )
            if attempt_number == plan.request.maximum_attempts:
                _raise_interaction_error(
                    "Yahoo transport attempt budget exhausted",
                    attempts=attempts,
                    adapter=adapter,
                    ledger=raw_ledger,
                )
            continue

        landed = raw_ledger.land(response, plan)
        retryable = response.status_code in _RETRYABLE_STATUS_CODES or response.status_code >= 500
        if retryable:
            attempts.append(_attempt_with_raw(response, landed, outcome="retryable-http-status"))
            if attempt_number == plan.request.maximum_attempts:
                _raise_interaction_error(
                    "Yahoo HTTP attempt budget exhausted",
                    attempts=attempts,
                    adapter=adapter,
                    ledger=raw_ledger,
                )
            continue
        if response.status_code != 200:
            attempts.append(_attempt_with_raw(response, landed, outcome="terminal-http-status"))
            _raise_interaction_error(
                "Yahoo returned a non-retryable HTTP status",
                attempts=attempts,
                adapter=adapter,
                ledger=raw_ledger,
            )
        try:
            bar = normalizer.normalize(landed, plan)
        except (TypeError, ValueError) as error:
            attempts.append(_attempt_with_raw(response, landed, outcome="normalization-error"))
            try:
                _raise_interaction_error(
                    "Yahoo response failed normalization",
                    attempts=attempts,
                    adapter=adapter,
                    ledger=raw_ledger,
                )
            except D3E1InteractionError as interaction_error:
                raise interaction_error from error

        attempts.append(_attempt_with_raw(response, landed, outcome="success"))
        result = D3E1TinyResult(
            capture_scope_id=plan.capture_scope_id,
            capture_scope_sha256=plan.capture_scope_sha256,
            call_plan_id=plan.call_plan_id,
            configuration_sha256=plan.configuration_sha256,
            maximum_attempts=plan.request.maximum_attempts,
            source_call_count=adapter.source_call_count,
            landed_response_count=len(raw_ledger.entries),
            landing_ids=tuple(item.landing_id for item in raw_ledger.entries),
            raw_object_ids=tuple(item.response.raw_object_id for item in raw_ledger.entries),
            normalized_bar=bar,
            attempts=tuple(attempts),
        )
        return D3E1TinyExecution(result=result, landed_raw_responses=raw_ledger.entries)

    raise RuntimeError("unreachable E1 interaction state")


__all__ = [
    "D3E1InteractionError",
    "D3E1TinyExecution",
    "D3E1TinyResult",
    "InMemoryRawResponseLedger",
    "LandedRawResponse",
    "RawResponseNormalizer",
    "RawSourceAdapter",
    "RawSourceResponse",
    "SourceCallAttempt",
    "YahooDailyBarNormalizer",
    "YahooRawHttpAdapter",
    "execute_e1_tiny_interaction",
]
