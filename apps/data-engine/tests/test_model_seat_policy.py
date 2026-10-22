"""#1444: one retry policy and one pacer for every model call.

Every test drives the real `llm.invoke` entry point, or the deployed key-probe job, with a fake
transport. No test reaches the live provider. The limits come from the environment contract;
no test writes its own copy of a limit. Time is simulated: a fake sleep advances a fake monotonic
clock and moves the recorded ledger rows into the past, so a 60 s wait costs no real time.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from math import ceil
from pathlib import Path
from typing import Any

import pytest
from data_engine.config import settings
from data_engine.lanes import quality
from data_engine.quality import model_key_health, nightly_verdicts
from data_engine.sources import gateway, llm, model_policy
from truealpha_contracts.environment_contract import load_environment_contract

SEAT = load_environment_contract().model_seat
RETRY = SEAT.retry
FAKE_KEY = "sk-test-not-a-real-key-0123456789"
TICK = "2026-09-16T00:15:00+00:00"

OK_BODY = json.dumps(
    {
        "model": "glm-served",
        "choices": [{"message": {"content": '{"ok": true}'}}],
        "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
    }
).encode()


def rate_limited(code: str | None = "1302", message: str = "rate limit") -> bytes:
    """The body the provider sent on 2026-10-21 for a limit hit (code 1302, message in Chinese)."""
    error: dict[str, str] = {"message": message}
    if code is not None:
        error["code"] = code
    return json.dumps({"error": error}).encode()


class World:
    """Simulated time. `sleep` advances the monotonic clock and ages every ledger row."""

    def __init__(self, ledger: gateway.MemoryLedger) -> None:
        self.now = 1_000.0
        self.sleeps: list[float] = []
        self.events: list[str] = []
        self._ledger = ledger
        self._lock = threading.Lock()

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self.sleeps.append(seconds)
            self.events.append("sleep")
            self.now += seconds
            for row in self._ledger:
                row.called_at -= timedelta(seconds=seconds)


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, call_ledger: gateway.MemoryLedger) -> World:
    simulated = World(call_ledger)
    monkeypatch.setattr(model_policy, "clock", simulated.clock)
    monkeypatch.setattr(model_policy, "sleep", simulated.sleep)
    monkeypatch.setattr(model_policy, "jitter", lambda: 0.0)
    monkeypatch.setattr(model_policy, "_pacers", {})
    monkeypatch.setattr(settings, "llm_api_key", FAKE_KEY)
    return simulated


def in_environment(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.setattr(gateway, "capacity_environment", lambda: name)


class Transport:
    """A scripted provider. Each entry is a `(status, body)` answer; the last one repeats."""

    def __init__(self, answers: list[tuple[int, bytes]], world: World | None = None, hold: float = 0.0) -> None:
        self.answers = list(answers)
        self.asked = 0
        self.in_flight = 0
        self.max_in_flight = 0
        self._world = world
        self._hold = hold
        self._lock = threading.Lock()

    def __call__(self, url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
        with self._lock:
            self.asked += 1
            answer = self.answers[min(self.asked, len(self.answers)) - 1]
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        if self._world is not None:
            self._world.events.append("call")
        if self._hold:
            time.sleep(self._hold)
        with self._lock:
            self.in_flight -= 1
        return answer


def ask(transport: Transport, *, caller: str = "test:model-seat", user_content: str = "ping") -> llm.ModelInvocation:
    return llm.invoke(
        None,
        task=model_key_health.PROBE_TASK,
        cik=0,
        accession="",
        user_content=user_content,
        caller=caller,
        standard="test",
        parse=lambda content: {"answered": bool(content)},
        refusal=lambda detail: {"ok": False},
        persist=False,
        transport=transport,
    )


# --- 1. retry: the answer of 2026-10-21 -----------------------------------------------------


def test_code_1302_twice_then_success_is_three_ledger_rows_and_a_success(world, call_ledger, monkeypatch) -> None:
    in_environment(monkeypatch, "staging")
    transport = Transport([(429, rate_limited()), (429, rate_limited()), (200, OK_BODY)], world)
    invocation = ask(transport)
    assert invocation.decision == {"answered": True}
    assert transport.asked == 3
    assert [(row.attempt, row.status_code, row.ok, row.provider_code) for row in call_ledger] == [
        (1, 429, False, "1302"),
        (2, 429, False, "1302"),
        (3, 200, True, None),
    ]
    assert {row.source for row in call_ledger} == {llm.SOURCE}
    assert world.sleeps == [RETRY.base_backoff_seconds(1), RETRY.base_backoff_seconds(2)]


def test_the_random_extra_only_lengthens_the_wait(world, monkeypatch) -> None:
    in_environment(monkeypatch, "staging")
    monkeypatch.setattr(model_policy, "jitter", lambda: 1.0)
    ask(Transport([(429, rate_limited()), (200, OK_BODY)], world))
    base = RETRY.base_backoff_seconds(1)
    assert world.sleeps == [base * (100 + RETRY.jitter_percent) / 100]
    assert world.sleeps[0] > base


def test_a_call_that_never_succeeds_waits_at_least_ninety_seconds_and_then_stops(
    world, call_ledger, monkeypatch
) -> None:
    in_environment(monkeypatch, "staging")
    transport = Transport([(429, rate_limited())], world)
    with pytest.raises(llm.ModelHTTPError) as raised:
        ask(transport)
    assert raised.value.status_code == 429 and raised.value.provider_code == "1302"
    assert transport.asked == RETRY.max_attempts == len(call_ledger)
    assert sum(world.sleeps) >= RETRY.min_total_wait_seconds
    assert sum(world.sleeps) <= RETRY.deadline_seconds


def test_the_deadline_stops_a_call_before_its_attempts_are_used(world, call_ledger, monkeypatch) -> None:
    """A stuck call must not hold a worker for ever: the next wait may not pass the deadline."""
    in_environment(monkeypatch, "staging")
    first, second = RETRY.base_backoff_seconds(1), RETRY.base_backoff_seconds(2)
    short = RETRY.model_copy(update={"deadline_seconds": first + second + 1})
    monkeypatch.setattr(model_policy, "retry_spec", lambda: short)
    transport = Transport([(429, rate_limited())], world)
    with pytest.raises(llm.ModelHTTPError):
        ask(transport)
    # Two waits fit inside the deadline. The third wait would end after it.
    assert transport.asked == 3 == len(call_ledger)
    assert world.sleeps == [first, second]


def test_the_wait_for_the_rate_window_counts_against_the_deadline(world, call_ledger, monkeypatch) -> None:
    """The window of a full minute must not hold a call past its deadline."""
    in_environment(monkeypatch, "staging")
    per_minute = SEAT.limits_for("staging").calls_per_minute  # type: ignore[union-attr]
    waits = [RETRY.base_backoff_seconds(n) for n in range(1, per_minute + 1)]
    # The attempts after the first `per_minute` ones would wait for the window, about 60 s less
    # the age of the oldest attempt. One second is left of the deadline when that wait begins.
    short = RETRY.model_copy(update={"deadline_seconds": sum(waits) + 1})
    monkeypatch.setattr(model_policy, "retry_spec", lambda: short)
    transport = Transport([(429, rate_limited())], world)
    with pytest.raises(llm.ModelHTTPError) as raised:
        ask(transport)
    assert raised.value.provider_code == "1302", "the last answer of the provider stands, not a gate error"
    assert transport.asked == per_minute == len(call_ledger)
    assert world.now - 1_000.0 <= short.deadline_seconds


class SpyPacer:
    """The real pacer plus a count of admissions and of slots held right now."""

    def __init__(self, pacer: model_policy.ModelPacer) -> None:
        self._pacer = pacer
        self.admissions = 0
        self.held = 0

    def admitted(self, remaining_seconds):
        spy = self

        class _Slot:
            def __enter__(self) -> None:
                self._inner = spy._pacer.admitted(remaining_seconds)
                self._inner.__enter__()
                spy.admissions += 1
                spy.held += 1

            def __exit__(self, *exc: object) -> bool | None:
                spy.held -= 1
                return self._inner.__exit__(*exc)

        return _Slot()


def test_every_attempt_is_admitted_and_no_slot_is_held_while_the_call_waits(world, call_ledger, monkeypatch) -> None:
    in_environment(monkeypatch, "staging")
    real = model_policy.pacer_for("staging")
    assert real is not None
    spy = SpyPacer(real)
    monkeypatch.setattr(model_policy, "pacer_for", lambda environment: spy)
    held_during_waits: list[int] = []
    real_sleep = world.sleep

    def sleep(seconds: float) -> None:
        held_during_waits.append(spy.held)
        real_sleep(seconds)

    monkeypatch.setattr(model_policy, "sleep", sleep)
    ask(Transport([(429, rate_limited()), (429, rate_limited()), (200, OK_BODY)], world))
    assert spy.admissions == len(call_ledger) == 3
    assert held_during_waits and set(held_during_waits) == {0}, "a waiting call holds no concurrency slot"


def test_the_task_timeout_reaches_the_network_call_of_its_own_thread(world, monkeypatch) -> None:
    """Two calls wait for the one concurrency slot of staging while a third holds it. Each socket
    call then carries the timeout of its own task. A shared module global gives both the value
    that was set last."""
    import dataclasses
    import time as real_time

    in_environment(monkeypatch, "staging")  # one concurrent call
    seen: dict[str, float] = {}
    holder_in_flight, release_holder = threading.Event(), threading.Event()

    def urlopen(request: urllib.request.Request, timeout: float) -> _Answer:
        name = threading.current_thread().name
        seen[name] = timeout
        if name == "holder":
            holder_in_flight.set()
            release_holder.wait(timeout=30)
        return _Answer(OK_BODY)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    def call(timeout_seconds: int) -> None:
        task = dataclasses.replace(model_key_health.PROBE_TASK, timeout_seconds=timeout_seconds)
        llm.invoke(
            None,
            task=task,
            cik=0,
            accession="",
            user_content="ping",
            caller="t",
            standard="t",
            parse=lambda content: {},
            refusal=lambda detail: {},
            persist=False,
        )

    holder = threading.Thread(target=call, args=(33,), name="holder")
    holder.start()
    assert holder_in_flight.wait(timeout=10)
    waiters = [
        threading.Thread(target=call, args=(11,), name="short"),
        threading.Thread(target=call, args=(22,), name="long"),
    ]
    for waiter in waiters:
        waiter.start()
        real_time.sleep(0.3)  # let it set its timeout and queue for the slot
    release_holder.set()
    for thread in (holder, *waiters):
        thread.join(timeout=30)
    assert seen == {"holder": 33, "short": 11, "long": 22}


@pytest.mark.parametrize("transport_raises", [False, True], ids=["answers", "raises"])
def test_the_task_timeout_does_not_outlive_its_call(world, monkeypatch, transport_raises) -> None:
    """The variable holds the timeout of the call that is running on this thread, and nothing after it."""
    import dataclasses

    in_environment(monkeypatch, "dev")
    seen_inside: list[int] = []

    def transport(url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
        seen_inside.append(llm._attempt_timeout.get())
        if transport_raises:
            raise TimeoutError("timed out")
        return 200, OK_BODY

    task = dataclasses.replace(model_key_health.PROBE_TASK, timeout_seconds=33)
    call = {
        "task": task,
        "cik": 0,
        "accession": "",
        "user_content": "ping",
        "caller": "t",
        "standard": "t",
        "parse": lambda content: {},
        "refusal": lambda detail: {},
        "persist": False,
        "transport": transport,
    }
    if transport_raises:
        with pytest.raises(TimeoutError):
            llm.invoke(None, **call)
    else:
        llm.invoke(None, **call)
    assert seen_inside == [33]
    assert llm._attempt_timeout.get() == llm.ModelTask.timeout_seconds, "the timeout of the call was reset"


def test_the_default_transport_keeps_the_three_argument_form_that_callers_patch() -> None:
    import inspect

    assert list(inspect.signature(llm._gateway_transport).parameters) == ["url", "headers", "body"]


def test_a_call_that_finds_no_slot_before_its_deadline_is_refused_and_sends_nothing(
    world, call_ledger, monkeypatch
) -> None:
    """The wait for a concurrency slot counts against the deadline too. Real time, one second."""
    in_environment(monkeypatch, "staging")  # one concurrent call
    monkeypatch.setattr(model_policy, "retry_spec", lambda: RETRY.model_copy(update={"deadline_seconds": 1}))
    holding, release = threading.Event(), threading.Event()

    def blocked(url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
        holding.set()
        release.wait(timeout=30)
        return 200, OK_BODY

    first = threading.Thread(target=ask, args=(blocked,))
    first.start()
    assert holding.wait(timeout=10)
    outcome: list[BaseException] = []

    def second_call() -> None:
        try:
            ask(Transport([(200, OK_BODY)]))
        except BaseException as error:  # noqa: BLE001 - the test reads the type
            outcome.append(error)

    second = threading.Thread(target=second_call)
    second.start()
    second.join(timeout=15)
    release.set()
    first.join(timeout=15)
    assert not second.is_alive(), "a call waits for a slot until its deadline, not for ever"
    (refused,) = outcome
    assert isinstance(refused, llm.ModelCapacityRefused) and refused.status_code == 429
    assert [row.attempt for row in call_ledger] == [1], "only the call that held the slot made a request"


def test_the_pacer_reads_the_time_left_again_after_each_wait(monkeypatch) -> None:
    """The slot wait, the lock wait and the window wait each get what is left, not the first value."""
    pacer = model_policy.ModelPacer("staging", SEAT.limits_for("staging"))  # type: ignore[arg-type]
    timeouts: list[float] = []

    class Spy:
        def __init__(self, inner) -> None:
            self._inner = inner

        def acquire(self, timeout: float) -> bool:
            timeouts.append(timeout)
            return self._inner.acquire()

        def release(self) -> None:
            self._inner.release()

    pacer._slots = Spy(pacer._slots)  # type: ignore[assignment]
    pacer._admission = Spy(pacer._admission)  # type: ignore[assignment]
    monkeypatch.setattr(pacer._gate, "admit", lambda seat: timeouts.append(pacer._gate.max_wait_seconds))
    left = iter([9.0, 6.0, 3.0])
    with pacer.admitted(lambda: next(left)):
        pass
    assert timeouts == [9.0, 6.0, 3.0]


def test_the_wait_for_the_admission_lock_stops_at_the_deadline() -> None:
    pacer = model_policy.ModelPacer("production", SEAT.limits_for("production"))  # type: ignore[arg-type]
    outcome: list[BaseException] = []

    def admit_once() -> None:
        try:
            with pacer.admitted(lambda: 0.2):
                pass
        except BaseException as error:  # noqa: BLE001 - the test reads the type
            outcome.append(error)

    pacer._admission.acquire()  # another thread sits in the rate gate
    try:
        waiter = threading.Thread(target=admit_once)
        waiter.start()
        waiter.join(timeout=10)
        assert not waiter.is_alive(), "the wait for the lock ends at the deadline"
    finally:
        pacer._admission.release()
        waiter.join(timeout=10)
    (refused,) = outcome
    assert isinstance(refused, gateway.CapacityExceeded) and "admission lock" in refused.reason
    assert pacer._slots.acquire(blocking=False), "the slot was given back"


def test_the_probe_names_a_refusal_by_the_pacer_and_not_a_provider_answer() -> None:
    health = model_key_health.classify(llm.ModelCapacityRefused("no concurrency slot before the deadline of the call"))
    assert (health.ok, health.outcome, health.status_code) == (False, "provider-error", None)
    assert "HTTP" not in health.summary and "slot or rate window" in health.summary


@pytest.mark.parametrize(
    ("status", "body", "attempts"),
    [
        (429, rate_limited("1302"), RETRY.max_attempts),
        (429, rate_limited(None), RETRY.max_attempts),
        (429, b"<html>too many requests</html>", RETRY.max_attempts),
        (503, rate_limited("1305"), RETRY.max_attempts),
        (429, rate_limited("1113"), 1),
        (429, rate_limited("1310"), 1),
        (500, rate_limited(None), 1),
        (401, rate_limited("1000"), 1),
        (400, rate_limited("1301"), 1),
    ],
    ids=[
        "429-code-1302",
        "429-without-code",
        "429-without-json",
        "503-transient-code",
        "429-no-funds-is-terminal",
        "429-quota-used-up-is-terminal",
        "500-is-not-retried",
        "401-is-not-retried",
        "400-is-not-retried",
    ],
)
def test_only_a_transient_answer_is_retried(world, call_ledger, monkeypatch, status, body, attempts) -> None:
    in_environment(monkeypatch, "staging")
    transport = Transport([(status, body)], world)
    with pytest.raises(llm.ModelHTTPError):
        ask(transport)
    assert transport.asked == attempts == len(call_ledger)


def test_a_transport_error_is_one_failed_row_and_is_raised_unchanged(world, call_ledger, monkeypatch) -> None:
    in_environment(monkeypatch, "staging")

    def broken(url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
        raise TimeoutError("timed out")

    with pytest.raises(TimeoutError):
        llm.invoke(
            None,
            task=model_key_health.PROBE_TASK,
            cik=0,
            accession="",
            user_content="ping",
            caller="test",
            standard="test",
            parse=lambda content: {},
            refusal=lambda detail: {},
            persist=False,
            transport=broken,
        )
    ((row),) = call_ledger
    assert row.ok is False and row.attempt == 1 and "TimeoutError" in (row.error or "")


# --- 2. pacing: the contract's limits, per environment ---------------------------------------


@pytest.mark.parametrize("environment", ["production", "staging"])
def test_five_concurrent_calls_never_exceed_the_contract_limit(world, monkeypatch, environment) -> None:
    in_environment(monkeypatch, environment)
    limit = SEAT.limits_for(environment).max_concurrent_calls  # type: ignore[union-attr]
    # The fake provider holds each call for 50 ms of real time so that the threads overlap.
    # Waits for the per-minute window run in simulated time.
    transport = Transport([(200, OK_BODY)], hold=0.05)
    threads = [threading.Thread(target=ask, args=(transport,)) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert transport.asked == 5
    assert transport.max_in_flight == limit, "the limiter lets exactly the contract limit through, no more, no less"


def test_the_per_minute_limit_makes_the_next_attempt_wait_for_the_window(world, call_ledger, monkeypatch) -> None:
    in_environment(monkeypatch, "staging")
    per_minute = SEAT.limits_for("staging").calls_per_minute  # type: ignore[union-attr]
    transport = Transport([(200, OK_BODY)], world)
    for _ in range(per_minute + 1):
        ask(transport)
    assert world.events == ["call"] * per_minute + ["sleep", "call"]
    (waited,) = world.sleeps
    assert 59 <= waited <= 61, "the wait runs to the end of the 60 s window of the oldest attempt"


def test_the_retry_of_a_rate_limited_attempt_counts_in_the_per_minute_limit(world, call_ledger, monkeypatch) -> None:
    """Each attempt is one request to the account, so a retry spends the same limit as a first try."""
    in_environment(monkeypatch, "staging")
    per_minute = SEAT.limits_for("staging").calls_per_minute  # type: ignore[union-attr]
    ask(Transport([(429, rate_limited())] * (per_minute - 1) + [(200, OK_BODY)], world))
    assert len(call_ledger) == per_minute
    waits_so_far = len(world.sleeps)
    elapsed = sum(world.sleeps)
    ask(Transport([(200, OK_BODY)], world))
    # The window holds `per_minute` attempts. The next call waits until the oldest one is 60 s old.
    (waited,) = world.sleeps[waits_so_far:]
    assert waited == pytest.approx(60 - elapsed, abs=0.5)


def test_an_environment_the_contract_does_not_list_is_not_paced(world, monkeypatch) -> None:
    in_environment(monkeypatch, "dev")
    transport = Transport([(200, OK_BODY)], world)
    for _ in range(25):
        ask(transport)
    assert world.sleeps == []


# --- 3. the ledger row: parameters without secrets --------------------------------------------


def test_each_attempt_records_the_request_parameters_without_the_key(world, call_ledger, monkeypatch) -> None:
    in_environment(monkeypatch, "staging")
    ask(Transport([(429, rate_limited()), (200, OK_BODY)], world))
    assert len(call_ledger) == 2
    expected = {
        "model": settings.llm_model,
        "thinking": "disabled",
        "max_tokens": model_key_health.PROBE_TASK.max_tokens,
        "temperature": 0,
        "response_format": "json_object",
    }
    assert [row.request_params for row in call_ledger] == [expected, expected]
    for row in call_ledger:
        serialized = json.dumps(row.as_row())
        assert FAKE_KEY not in serialized and "Bearer" not in serialized


def _columns(sql: str) -> list[str]:
    inside = sql[sql.index("(") + 1 : sql.index(")")]
    return [column.strip() for column in inside.split(",")]


def test_the_ledger_insert_maps_each_value_to_its_own_column(world, call_ledger, monkeypatch) -> None:
    in_environment(monkeypatch, "staging")
    ask(Transport([(429, rate_limited("1302")), (200, OK_BODY)], world))
    rows = []
    for record in call_ledger:
        columns, params = _columns(gateway._insert_sql(record)), gateway._insert_params(record)
        assert len(columns) == len(params), "one value for each column"
        rows.append(dict(zip(columns, params, strict=True)))
    assert [(row["attempt"], row["provider_code"], row["status_code"]) for row in rows] == [
        (1, "1302", 429),
        (2, None, 200),
    ]
    for record, row in zip(call_ledger, rows, strict=True):
        for column in gateway._BASE_COLUMNS:
            expected = bool(record.ok) if column == "ok" else getattr(record, column)
            assert row[column] == expected, column
    assert [json.loads(row["request_params"])["max_tokens"] for row in rows] == [
        model_key_health.PROBE_TASK.max_tokens
    ] * 2


def test_an_unknown_column_is_a_statement_error_and_is_not_asked_again(monkeypatch) -> None:
    """A fresh connection meets the same schema, so the statement runs once."""
    import psycopg

    class Connection:
        closed = False
        executions = 0

        def execute(self, sql: str, params: tuple = ()) -> None:
            Connection.executions += 1
            raise psycopg.errors.UndefinedColumn("no such column")

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(gateway, "_pg", lambda: Connection())
    with pytest.raises(psycopg.errors.UndefinedColumn):
        gateway._pg_execute("select 1")
    assert Connection.executions == 1


def test_a_row_of_another_source_does_not_depend_on_the_attempt_columns() -> None:
    record = gateway.CallRecord(source="sec", endpoint="submissions", caller="t", called_at=datetime.now(UTC))
    assert gateway._insert_sql(record) == gateway._INSERT_SQL
    assert len(gateway._insert_params(record)) == len(_columns(gateway._INSERT_SQL))


def test_a_model_row_survives_a_schema_that_lacks_the_attempt_columns(monkeypatch) -> None:
    """The llm-service applies the migration at its boot; the data-engine may start before it."""
    import psycopg

    executed: list[str] = []

    def execute(sql: str, params: tuple = ()) -> None:
        executed.append(sql)
        if sql == gateway._INSERT_ATTEMPT_SQL:
            raise psycopg.errors.UndefinedColumn('column "attempt" does not exist')

    monkeypatch.setattr(gateway, "_pg_execute", execute)
    record = gateway.CallRecord(
        source=llm.SOURCE, endpoint="x", caller="t", called_at=datetime.now(UTC), attempt=1, provider_code="1302"
    )
    gateway.postgres_writer(record)
    assert executed == [gateway._INSERT_ATTEMPT_SQL, gateway._INSERT_SQL]


# --- 4. the probe: the same policy through its deployed job -----------------------------------


class _Answer:
    def __init__(self, body: bytes) -> None:
        self._body = body
        self.status = 200

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _Answer:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False


def _network(monkeypatch: pytest.MonkeyPatch, answers: list[Any]) -> list[urllib.request.Request]:
    asked: list[urllib.request.Request] = []

    def urlopen(request: urllib.request.Request, timeout: float) -> _Answer:
        asked.append(request)
        answer = answers[min(len(asked), len(answers)) - 1]
        if isinstance(answer, int):
            raise urllib.error.HTTPError(request.full_url, answer, "error", {}, io.BytesIO(rate_limited()))
        return _Answer(answer)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return asked


@pytest.fixture
def verdicts(monkeypatch) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    monkeypatch.setattr(nightly_verdicts, "record", lambda name, **kwargs: rows.append({"check": name, **kwargs}))
    return rows


def test_the_probe_job_survives_429_429_success_through_the_one_policy(
    world, call_ledger, verdicts, monkeypatch
) -> None:
    in_environment(monkeypatch, "production")
    asked = _network(monkeypatch, [429, 429, OK_BODY])
    result = quality.model_key_health_job.execute_in_process(tags={nightly_verdicts.TICK_TAG: TICK})
    assert result.success
    assert [(row["check"], row["ok"]) for row in verdicts] == [("model_key_health", True)]
    assert len(asked) == 3
    assert [row.status_code for row in call_ledger] == [429, 429, 200]
    assert {row.caller for row in call_ledger} == {model_key_health.CALLER}
    assert {row.run_key for row in call_ledger} == {f"dagster:{result.run_id}"}
    assert world.sleeps == [RETRY.base_backoff_seconds(1), RETRY.base_backoff_seconds(2)]


def test_a_probe_that_is_rate_limited_for_ever_is_red_and_names_the_provider_code(
    world, call_ledger, verdicts, monkeypatch
) -> None:
    in_environment(monkeypatch, "production")
    _network(monkeypatch, [429])
    result = quality.model_key_health_job.execute_in_process(
        tags={nightly_verdicts.TICK_TAG: TICK}, raise_on_error=False
    )
    assert not result.success
    ((row),) = verdicts
    assert row["ok"] is False and "HTTP 429" in row["summary"] and "1302" in row["summary"]
    assert FAKE_KEY not in row["summary"]
    assert len(call_ledger) == RETRY.max_attempts
    assert sum(world.sleeps) >= RETRY.min_total_wait_seconds


# --- 5. the probe minute comes from the contract ------------------------------------------------


def test_the_probe_minutes_of_the_two_live_environments_differ_and_come_from_the_contract() -> None:
    production = quality.model_key_health_cron("production")
    staging = quality.model_key_health_cron("staging")
    assert production == SEAT.probe_cron("production") and staging == SEAT.probe_cron("staging")
    assert production.split()[0] != staging.split()[0]
    assert production.split()[1:] == staging.split()[1:] == [str(SEAT.probe.hour_utc), "*", "*", "*"]


@pytest.mark.parametrize("environment", sorted(SEAT.environments))
def test_the_schedule_of_each_live_environment_runs_in_its_own_slot(environment) -> None:
    """Built for the environment, not read from the CI environment (which the contract does not list)."""
    schedule = quality.build_model_key_health_schedule(environment)
    assert schedule.cron_schedule == SEAT.probe_cron(environment)
    assert schedule.name == "model_key_health_schedule"
    others = {quality.build_model_key_health_schedule(name).cron_schedule for name in SEAT.environments}
    assert len(others) == len(SEAT.environments), "no two live environments share a probe minute"


@pytest.mark.parametrize("environment", sorted(SEAT.environments))
def test_a_process_of_each_environment_deploys_the_schedule_of_its_own_slot(environment) -> None:
    """The real wiring: import the lane in a fresh process whose APP_ENV names the environment.
    A CI process (APP_ENV=ci) is not listed in the contract and cannot tell the slots apart."""
    code = (
        "from data_engine.lanes import quality; "
        "print(quality.defs.get_schedule_def('model_key_health_schedule').cron_schedule)"
    )
    env = {**os.environ, "APP_ENV": environment}
    done = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
    assert done.stdout.strip().splitlines()[-1] == SEAT.probe_cron(environment)


def test_every_probe_slot_ends_before_the_release_fetch_proof_starts() -> None:
    proof_minute, proof_hour, *_ = quality.RELEASE_FETCH_PROOF_CRON.split()
    longest = RETRY.deadline_seconds + model_key_health.PROBE_TASK.timeout_seconds + SEAT.probe.launch_margin_seconds
    for environment in SEAT.environments:
        minute, hour, *_ = quality.model_key_health_cron(environment).split()
        assert hour == proof_hour and int(minute) + ceil(longest / 60) <= int(proof_minute), environment


def test_the_probe_deadline_fits_inside_the_run_time_bound_of_its_job() -> None:
    bound = load_environment_contract().max_runtime_seconds(quality.MODEL_KEY_HEALTH_JOB_NAME)
    assert bound is not None
    assert (
        RETRY.deadline_seconds + model_key_health.PROBE_TASK.timeout_seconds + SEAT.probe.launch_margin_seconds < bound
    )


# --- 6. the key name comes from settings --------------------------------------------------------


def test_the_missing_key_message_names_the_variable_that_settings_reads(monkeypatch) -> None:
    manifest = json.loads((Path(__file__).resolve().parents[1] / "required-env.generated.json").read_text())
    (entry,) = [item for item in manifest["fields"] if item["field"] == "llm_api_key"]
    assert settings.llm_api_key_env == entry["env"]
    monkeypatch.setattr(settings, "llm_api_key", "")
    monkeypatch.setattr(type(settings), "llm_api_key_env", property(lambda self: "LLM_API_KEY_PRODUCTION"))
    with pytest.raises(llm.ModelNotConfigured, match="LLM_API_KEY_PRODUCTION"):
        ask(Transport([(200, OK_BODY)]))
