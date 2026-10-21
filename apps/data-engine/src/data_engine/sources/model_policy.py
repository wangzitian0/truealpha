"""One retry policy and one pacer for every model call (#1444).

All environments call one coding-plan account, and other users of that account share its rate
limit. On 2026-10-21 the production key probe got HTTP 429 with provider code 1302 three times in
seven seconds, while staging probed the same account in the same minute. The limits live in the
``model_seat`` section of ``environment_contract.v1.json``. This module reads them and holds no
number of its own.

- ``RetryBudget`` decides, after each answer, whether the call waits and tries again. A transient
  answer is HTTP 429 without a terminal provider code, or any answer with a transient provider code.
  The waits grow by a factor, carry a random extra that only lengthens them, and stop at the
  deadline of the call.
- ``ModelPacer`` admits one attempt at a time under the limits of one environment: at most
  ``max_concurrent_calls`` attempts at once, at most ``calls_per_minute`` attempts in any 60 s
  window. The rate is the ``CapacityGate`` of the source gateway, so it reads the ledger of the
  environment and counts the attempts of every process. The concurrency limit holds inside one
  process. Every model caller of the data engine asks one question at a time, so this limit has
  never been the binding one.
"""

from __future__ import annotations

import json
import random
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from truealpha_contracts.environment_contract import (
    ModelRetrySpec,
    ModelSeatContract,
    ModelSeatEnvironmentLimits,
    load_environment_contract,
)

from data_engine.sources import gateway

# Indirections for tests: a whole retry schedule then runs without real waiting.
clock = time.monotonic
sleep = time.sleep
jitter = random.random

_RATE_WINDOW_SECONDS = 60.0
_PROVIDER_CODE_LIMIT = 16


def seat_contract() -> ModelSeatContract:
    return load_environment_contract().model_seat


def retry_spec() -> ModelRetrySpec:
    return seat_contract().retry


def provider_code(body: bytes) -> str | None:
    """The code in the error body of the provider (``{"error": {"code": "1302", ...}}``), if any."""
    try:
        payload = json.loads(body.decode())
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    code = error.get("code") if isinstance(error, dict) else payload.get("code")
    if code is None or isinstance(code, bool) or not isinstance(code, str | int):
        return None
    return str(code).strip()[:_PROVIDER_CODE_LIMIT] or None


@dataclass(frozen=True)
class RetryPolicy:
    spec: ModelRetrySpec

    def is_transient(self, status: int, code: str | None) -> bool:
        """Whether the answer is worth another attempt. A terminal code is never retried."""
        if code is not None and code in self.spec.terminal_provider_codes:
            return False
        if code is not None and code in self.spec.transient_provider_codes:
            return True
        return status in self.spec.transient_http_statuses

    def wait_seconds(self, retry: int, unit: float) -> float:
        """The wait before retry ``retry`` (1-based). ``unit`` in [0, 1) scales the random extra."""
        return self.spec.base_backoff_seconds(retry) * (100 + self.spec.jitter_percent * unit) / 100


@dataclass
class RetryBudget:
    """The attempts and the time left to one logical model call."""

    spec: ModelRetrySpec = field(default_factory=lambda: retry_spec())
    attempts: int = 0
    started_at: float = field(default_factory=lambda: clock())

    def begin_attempt(self) -> int:
        self.attempts += 1
        return self.attempts

    def wait_before_next(self, status: int, code: str | None) -> float | None:
        """Seconds to wait before the next attempt, or None when the call must stop now.

        The call stops when the answer is not transient, when every attempt is used, or when the
        next wait would end after the deadline.
        """
        policy = RetryPolicy(self.spec)
        if not policy.is_transient(status, code) or self.attempts >= self.spec.max_attempts:
            return None
        wait = policy.wait_seconds(self.attempts, jitter())
        if clock() - self.started_at + wait > self.spec.deadline_seconds:
            return None
        return wait


class ModelPacer:
    """Admit the attempts of one environment under its concurrency and per-minute limits."""

    def __init__(self, environment: str, limits: ModelSeatEnvironmentLimits) -> None:
        self.seat = seat_contract().seat
        self._slots = threading.BoundedSemaphore(limits.max_concurrent_calls)
        self._admission = threading.Lock()
        self._gate = gateway.CapacityGate(
            capacities={self.seat: gateway.SourceCapacity(self.seat, _RATE_WINDOW_SECONDS, limits.calls_per_minute)},
            environment=environment,
            clock=lambda: clock(),
            sleep=lambda seconds: sleep(seconds),
        )

    @contextmanager
    def admitted(self) -> Iterator[None]:
        """Hold one concurrency slot for the attempt. Wait first for the slot, then for the window.

        The rate wait comes second so that the window counts the moment the attempt starts."""
        with self._slots:
            with self._admission:
                self._gate.admit(self.seat)
            yield


_pacers: dict[str, ModelPacer] = {}
_pacers_lock = threading.Lock()


def pacer_for(environment: str) -> ModelPacer | None:
    """The pacer of ``environment``, or None when the contract lists no limits for it (dev, ci)."""
    limits = seat_contract().limits_for(environment)
    if limits is None:
        return None
    with _pacers_lock:
        pacer = _pacers.get(environment)
        if pacer is None:
            pacer = _pacers[environment] = ModelPacer(environment, limits)
    return pacer
