"""The external call gateway and its ledger (#729; init.md §1 rule 6 as generalized on
2026-09-04: every call to a data vendor or a model provider goes through one path and
lands in ``staging.api_call_ledger``).

What one ledger row says
------------------------
One row per outbound request, written whether the request succeeded, answered with an
error status, or raised before an answer arrived:

- ``source`` / ``endpoint`` / ``caller`` — who asked which vendor for what;
- ``status_code`` and ``ok`` — the vendor's verdict, ``ok = false`` for any 4xx/5xx or
  exception (a rate-limited request is a failed request, and it still spent quota);
- ``error`` — the vendor's message or the exception, on failure only;
- ``payload_sha256`` / ``byte_length`` — the digest of the exact response body. This is
  the join key to ``raw.fetches.payload_sha256``: a recorded call dereferences to the
  bytes the capture landed, or visibly to nothing (owner requirement 2026-09-04: every
  request traceable to what we stored);
- ``cost`` — 1 call today; tokens for a model provider when #70 gives it a seat;
- ``capacity_window_id`` — the vendor window the call was charged to (``twelvedata:day:
  2026-09-04``), so "used vs declared" is one GROUP BY;
- ``run_key`` — the Dagster run that made the call, bound by the op (`run_scope`).

Why the writer is autocommit and never raises
--------------------------------------------
The row must survive the caller's transaction: a tick that rolls back still made its
vendor calls (the #628 rollbacks re-fetched 408 obligations *because* nothing recorded
the first attempt). And a ledger outage must not turn a successful vendor answer into a
failed capture — the write failure is logged with the full row instead.

Declared capacity is enforced before the call (#729 criterion 4)
----------------------------------------------------------------
Every seat's capacity is declared once, in the source registry
(`source_registrations.LEDGER_CAPACITIES`); ``CAPACITIES`` here is derived from it. A
`CapacityGate` enforces it BEFORE the request: it queues the call until the seat's rate
window has room and refuses it — `BudgetExhausted`, nothing sent, nothing recorded — when
the environment's daily budget is spent. Both checks read the environment's own ledger,
so every process of an environment shares them; the process's own admissions are counted
too, so a ledger that lags a call cannot let the next one through.

Two ways in, one gate:

- a Dagster op binds `capacity_scope()` around its work, and every `record_call` inside
  (so every `http_get`, `http_post` and `urlopen`) is admitted by that gate first — the
  capture ticks, the universe refresh and the confidence report do;
- `SourceGateway.call` admits through its own gate (the standards lane).

Outside both — a reconnaissance script, a unit test — a call is recorded, not gated.
A refusal is announced to the `on_capacity_refusal` listeners before it raises, which is
how a tick counts what its gate refused without any adapter having to report it.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock_time
from decimal import Decimal
from types import MappingProxyType
from typing import Any, NoReturn, TypeVar

from data_engine.config import settings
from data_engine.datahub.production_topt.source_registrations import (
    LEDGER_CAPACITIES,
    CapacityDeclaration,
    environment_share,
)

log = logging.getLogger(__name__)
T = TypeVar("T")

_ERROR_LIMIT = 500
# Matched case-insensitively as substrings of the lower-cased key, so `APIKEY`, `ApiKey`,
# `access_token` and `x-api-key` are all blanked (review on #741).
_REDACTED_KEY_MARKERS = ("apikey", "api_key", "api-key", "token", "secret", "password", "auth", "key")


# --- declared capacity: derived from the source registry, never written here ---------


@dataclass(frozen=True)
class SourceCapacity:
    """The gateway's view of one ledger seat's declared capacity.

    Derived: `CAPACITIES` is computed from `source_registrations.LEDGER_CAPACITIES`, the
    one place a number is written (the 2026-09-16 design audit found this module's own
    table disagreeing with the registry's). Tests build their own to drive a gate with a
    synthetic limit.

    ``None`` in a dimension means nothing is declared for it: a gate does not pace an
    unwindowed seat and does not budget an unbudgeted one. `SourceGateway.call` requires
    both (`enforceable`). ``environment_shares`` splits a shared allowance per environment
    (see `CapacityDeclaration`); `in_environment` resolves it.
    """

    source: str
    window_seconds: float | None
    calls_per_window: int | None
    daily_budget: int | None = None
    note: str = ""
    environment_shares: tuple[tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        if (self.window_seconds is None) != (self.calls_per_window is None):
            raise ValueError(f"capacity for {self.source!r}: window and calls-per-window go together")
        for name in ("window_seconds", "calls_per_window", "daily_budget"):
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise ValueError(f"capacity for {self.source!r} must be positive in every dimension")

    @classmethod
    def declared(cls, source: str, declaration: CapacityDeclaration) -> SourceCapacity:
        return cls(
            source,
            float(declaration.window_seconds),
            declaration.calls_per_window,
            declaration.daily_budget,
            declaration.documented,
            declaration.environment_shares,
        )

    @property
    def enforceable(self) -> bool:
        """Whether `SourceGateway.call` can throttle and budget this source."""
        return self.window_seconds is not None and self.daily_budget is not None

    @property
    def shared(self) -> bool:
        return bool(self.environment_shares)

    def in_environment(self, environment: str) -> SourceCapacity | None:
        """What ``environment`` may spend: the whole seat when it is not shared, the
        environment's share when it is, and None when a shared seat grants it none."""
        if not self.environment_shares:
            return self
        if dict(self.environment_shares).get(environment) is None:
            return None
        return replace(
            self,
            calls_per_window=None
            if self.calls_per_window is None
            else environment_share(self.calls_per_window, self.environment_shares, environment),
            daily_budget=None
            if self.daily_budget is None
            else environment_share(self.daily_budget, self.environment_shares, environment),
            environment_shares=(),
        )


CAPACITIES: Mapping[str, SourceCapacity] = MappingProxyType(
    {seat: SourceCapacity.declared(seat, declaration) for seat, declaration in LEDGER_CAPACITIES.items()}
)


def capacity_window_id(source: str, at: datetime) -> str | None:
    """The window a call at ``at`` is charged to, or None when the vendor declares none.

    Daily budgets win over rate windows: the number that runs out is the one worth
    grouping by. Rate-window buckets are UTC-epoch aligned so two processes agree.
    """
    capacity = CAPACITIES.get(source)
    if capacity is None:
        return None
    if capacity.daily_budget is not None:
        return f"{source}:day:{at.astimezone(UTC).date().isoformat()}"
    if capacity.window_seconds:
        window = capacity.window_seconds
        label = int(window) if float(window).is_integer() else window
        return f"{source}:{label}s:{int(at.timestamp() // window)}"
    return None


# --- run attribution ------------------------------------------------------------------

_run_key: contextvars.ContextVar[str | None] = contextvars.ContextVar("external_call_run_key", default=None)


@contextmanager
def run_scope(run_key: str) -> Iterator[None]:
    """Attribute every call made inside the block to ``run_key`` (the Dagster run id)."""
    token = _run_key.set(run_key)
    try:
        yield
    finally:
        _run_key.reset(token)


def current_run_key() -> str | None:
    return _run_key.get()


# --- the record -----------------------------------------------------------------------


@dataclass
class CallRecord:
    source: str
    endpoint: str
    caller: str
    called_at: datetime
    request_uri: str | None = None
    cost: Decimal = Decimal(1)  # numeric in the ledger: calls today, tokens for a model provider; never a float
    status_code: int | None = None
    payload_sha256: str | None = None
    byte_length: int | None = None
    error: str | None = None
    ok: bool | None = None  # None until the block ends; then derived from status/exception
    duration_ms: int | None = None
    run_key: str | None = None
    capacity_window_id: str | None = None

    def observe(self, *, status_code: int | None = None, body: bytes | str | None = None) -> None:
        """Record what the vendor answered. Call once the response is in hand.

        Text is digested as UTF-8; anything that is not bytes-like leaves the digest
        empty rather than turning a recorded call into a raised one — the ledger
        observes the vendor's answer, it never decides whether the caller may have it.
        """
        if status_code is not None:
            self.status_code = int(status_code)
        if body is None:
            return
        raw = body.encode() if isinstance(body, str) else body
        try:
            digest = hashlib.sha256(raw).hexdigest()
        except TypeError:
            return
        self.payload_sha256 = digest
        self.byte_length = len(raw)

    def fail(self, error: str) -> None:
        """Mark the call failed with a vendor-side reason (a 4xx body, a bad return code)."""
        self.ok = False
        self.error = error[:_ERROR_LIMIT]

    def as_row(self) -> dict[str, Any]:
        """A JSON-safe rendering — the shape the ERROR log carries when the ledger is down."""
        row = asdict(self)
        row["called_at"] = self.called_at.isoformat()
        row["cost"] = str(self.cost)
        return row


def _is_credential_key(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in _REDACTED_KEY_MARKERS)


def redact_uri(uri: str | None) -> str | None:
    """The request URI with credential-bearing query values blanked, for the ledger."""
    if uri is None:
        return None
    parts = urllib.parse.urlsplit(uri)
    if not parts.query:
        return uri
    query = [
        (key, "***" if _is_credential_key(key) else value)
        for key, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


# --- writers --------------------------------------------------------------------------

_pg_conn = None


def _pg():
    """Lazy, module-cached autocommit connection. Autocommit because a ledger row
    must survive even if the caller's transaction rolls back — the call to the vendor
    happened regardless of what the caller then does with the payload."""
    global _pg_conn
    if _pg_conn is None or _pg_conn.closed:
        import psycopg

        _pg_conn = psycopg.connect(settings.database_url, autocommit=True)
    return _pg_conn


def _pg_execute(sql: str, params=()):
    """Execute on the cached connection, reconnecting once on failure. psycopg
    marks a silently dropped connection closed only AFTER an operation fails on
    it, so the first statement after a DB restart/network blip raises — one
    fresh-connection retry covers that without hiding a genuinely down DB
    (the retry's exception propagates)."""
    global _pg_conn
    import psycopg

    try:
        return _pg().execute(sql, params)
    except psycopg.Error:
        try:
            if _pg_conn is not None:
                _pg_conn.close()
        finally:
            _pg_conn = None
        return _pg().execute(sql, params)


_INSERT_SQL = """
insert into staging.api_call_ledger
    (source, endpoint, caller, called_at, ok, status_code, error, duration_ms, request_uri,
     payload_sha256, byte_length, cost, capacity_window_id, run_key)
values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


def _insert_params(record: CallRecord) -> tuple:
    return (
        record.source,
        record.endpoint,
        record.caller,
        record.called_at,
        bool(record.ok),
        record.status_code,
        record.error,
        record.duration_ms,
        record.request_uri,
        record.payload_sha256,
        record.byte_length,
        record.cost,
        record.capacity_window_id,
        record.run_key,
    )


def postgres_writer(record: CallRecord) -> None:
    _pg_execute(_INSERT_SQL, _insert_params(record))


class MemoryLedger(list[CallRecord]):
    """A writer that keeps the rows in memory — the test double (a local probe with no
    warehouse sets `EXTERNAL_CALL_LEDGER=off` instead; `emit` then records nothing).

    It answers the gate's two reads over its own rows, so a gated test is budgeted and
    paced by exactly the calls it recorded."""

    def __call__(self, record: CallRecord) -> None:
        self.append(record)

    def calls_since(self, source: str, since: datetime) -> int:
        return sum(1 for row in self if row.source == source and row.called_at >= since)

    def window(self, source: str, since: datetime) -> tuple[int, datetime | None]:
        times = [row.called_at for row in self if row.source == source and row.called_at > since]
        return len(times), min(times, default=None)


_writer: Callable[[CallRecord], None] = postgres_writer


def set_writer(writer: Callable[[CallRecord], None]) -> Callable[[CallRecord], None]:
    """Swap the sink (tests; local probes without a warehouse). Returns the previous one."""
    global _writer
    previous, _writer = _writer, writer
    return previous


def emit(record: CallRecord, writer: Callable[[CallRecord], None] | None = None) -> None:
    """Write one finished record. Never raises: a ledger outage is logged with the full
    row, not turned into a vendor failure (see the module docstring)."""
    if settings.external_call_ledger == "off":
        return
    try:
        (writer or _writer)(record)
    except Exception:  # noqa: BLE001 - the vendor result must not depend on the ledger
        log.exception("external call ledger write failed; unrecorded call: %s", json.dumps(record.as_row()))


# --- the rule-6 gate (#729 criterion 4): queue on the rate, refuse on the budget ------


class CapacityExceeded(RuntimeError):
    """The rule-6 gate refused a call before it was made: nothing was sent and nothing
    is recorded in the ledger."""

    def __init__(self, source: str, reason: str) -> None:
        super().__init__(f"{source}: {reason}")
        self.source = source
        self.reason = reason


class BudgetExhausted(CapacityExceeded):
    """The seat's daily budget — this environment's share of it — is spent for the UTC day.

    Named so it is never read as "the vendor had nothing" (the August freeze: an exhausted
    shared key looked like a quiet vendor until the governed head stopped advancing). A
    primary adapter classifies the cell `deferred_capacity`, a corroborating origin is lost
    at the `budget` stage, and the tick's corroboration audit counts every refusal.
    ``budget`` is None when the seat is shared and this environment has no share at all.
    """

    def __init__(self, source: str, *, environment: str, budget: int | None, spent: int) -> None:
        if budget is None:
            reason = f"no share of the seat's shared daily budget is declared for {environment}"
        else:
            reason = f"daily budget {budget} spent ({spent} calls today) in {environment}"
        super().__init__(source, reason)
        self.environment = environment
        self.budget = budget
        self.spent = spent


_refusal_listeners: contextvars.ContextVar[tuple[Callable[[CapacityExceeded], None], ...]] = contextvars.ContextVar(
    "capacity_refusal_listeners", default=()
)


@contextmanager
def on_capacity_refusal(listener: Callable[[CapacityExceeded], None]) -> Iterator[None]:
    """Hand every refusal a gate makes inside the block to ``listener`` (the tick's audit)."""
    token = _refusal_listeners.set((*_refusal_listeners.get(), listener))
    try:
        yield
    finally:
        _refusal_listeners.reset(token)


def _refuse(error: CapacityExceeded) -> NoReturn:
    log.warning("rule-6 gate refused a %s call before it was made: %s", error.source, error.reason)
    for listener in _refusal_listeners.get():
        try:
            listener(error)
        except Exception:  # noqa: BLE001 - an audit hook never changes the refusal
            log.exception("capacity refusal listener failed for %s", error.source)
    raise error


_SPENT_SQL = "select count(*) from staging.api_call_ledger where source = %s and called_at >= %s"
_WINDOW_SQL = "select count(*), min(called_at) from staging.api_call_ledger where source = %s and called_at > %s"


def ledger_calls_since(source: str, since: datetime) -> int:
    """Calls this environment's ledger holds for ``source`` at or after ``since``.

    Read from wherever `emit` writes: the warehouse (autocommit, so another process's
    committed calls are visible), a `MemoryLedger` in tests, and nothing when the ledger is
    off — the gate then has only its own admissions to count."""
    if settings.external_call_ledger == "off":
        return 0
    if _writer is postgres_writer:
        row = _pg_execute(_SPENT_SQL, (source, since)).fetchone()
        return int(row[0]) if row else 0
    reader = getattr(_writer, "calls_since", None)
    return int(reader(source, since)) if reader is not None else 0


def ledger_window(source: str, since: datetime) -> tuple[int, datetime | None]:
    """How many calls the ledger holds for ``source`` after ``since``, and the oldest."""
    if settings.external_call_ledger == "off":
        return 0, None
    if _writer is postgres_writer:
        row = _pg_execute(_WINDOW_SQL, (source, since)).fetchone()
        return (int(row[0]), row[1]) if row else (0, None)
    reader = getattr(_writer, "window", None)
    return reader(source, since) if reader is not None else (0, None)


def _utcnow() -> datetime:
    return datetime.now(UTC)


# A ledger-read wait lands exactly on the oldest call's expiry; this keeps the loop from
# re-reading a window whose oldest row is still (by float rounding) inside it.
_EXPIRY_MARGIN_SECONDS = 0.01


@dataclass
class CapacityGate:
    """Admit a call only when it fits the seat's declared capacity (rule 6).

    `admit` queues the call until the rate window has room and refuses it when the
    environment's daily budget is spent. Both counts are read from the environment's
    ledger on every admission — every process of an environment writes there, so a QQQ
    tick and a canary tick running together share one window and one budget — and the
    gate's own admissions are a floor under the budget count (a call in flight is not a row
    yet). The window is also paced locally on the monotonic clock, which is all a gate
    without a window reader (`SourceGateway`) has.

    A ledger that cannot be read refuses the call by raising the database error (fail
    closed): the tick's own transaction is on the same database. Known limit: two
    processes that read a window before either records its call can both be admitted; the
    overshoot is bounded by the environment's concurrent ticks (two at most today, and
    the canary reuses almost every observation).
    """

    capacities: Mapping[str, SourceCapacity] = field(default_factory=lambda: CAPACITIES)
    #: None resolves the deployed environment (APP_ENV) when a shared seat needs it.
    environment: str | None = None
    spent_since: Callable[[str, datetime], int] = ledger_calls_since
    recent_calls: Callable[[str, datetime], tuple[int, datetime | None]] | None = ledger_window
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], datetime] = _utcnow
    #: A call that cannot get a slot in this long is refused rather than queued forever.
    max_wait_seconds: float = 900.0
    _windows: dict[str, deque[float]] = field(default_factory=dict, repr=False)
    _admitted: dict[str, tuple[date, int]] = field(default_factory=dict, repr=False)

    def admit(self, source: str) -> None:
        """Return once a call to ``source`` fits; raise `CapacityExceeded` (nothing sent)
        when it cannot."""
        declared = self.capacities.get(source)
        if declared is None:
            _refuse(CapacityExceeded(source, "no declared capacity — register the source before calling it"))
        capacity = declared.in_environment(self._environment()) if declared.shared else declared
        if capacity is None:
            _refuse(BudgetExhausted(source, environment=self._environment(), budget=None, spent=0))
        if capacity.daily_budget is not None:
            self._check_daily_budget(source, capacity.daily_budget)
        if capacity.window_seconds is not None and capacity.calls_per_window is not None:
            waited = self._queue(source, capacity.window_seconds, capacity.calls_per_window)
            if waited and capacity.daily_budget is not None:
                # Another process may have spent the last of the budget while this call queued.
                self._check_daily_budget(source, capacity.daily_budget)
        today = self.now().astimezone(UTC).date()
        self._admitted[source] = (today, self._admitted_today(source, today) + 1)

    def _environment(self) -> str:
        if self.environment is not None:
            return self.environment
        try:
            return settings.environment_tier.value
        except ValueError:
            # An unknown APP_ENV is named in the refusal and granted no share.
            return settings.app_env

    def _admitted_today(self, source: str, today: date) -> int:
        day, admitted = self._admitted.get(source, (today, 0))
        return admitted if day == today else 0

    def _check_daily_budget(self, source: str, budget: int) -> None:
        today = self.now().astimezone(UTC).date()
        recorded = self.spent_since(source, datetime.combine(today, clock_time.min, tzinfo=UTC))
        spent = max(recorded, self._admitted_today(source, today))
        if spent >= budget:
            _refuse(BudgetExhausted(source, environment=self._environment(), budget=budget, spent=spent))

    def _queue(self, source: str, window: float, calls: int) -> float:
        """Wait until the window has room; returns how long it waited."""
        local = self._windows.setdefault(source, deque())
        waited = 0.0
        while True:
            tick = self.clock()
            while local and tick - local[0] >= window:
                local.popleft()
            wait = window - (tick - local[0]) if len(local) >= calls else 0.0
            if self.recent_calls is not None:
                at = self.now()
                recorded, oldest = self.recent_calls(source, at - timedelta(seconds=window))
                if recorded >= calls and oldest is not None:
                    wait = max(wait, window - (at - oldest).total_seconds() + _EXPIRY_MARGIN_SECONDS)
            if wait <= 0:
                break
            if waited + wait > self.max_wait_seconds:
                _refuse(
                    CapacityExceeded(
                        source,
                        f"rate window ({calls} per {window:g} s) stayed full for {self.max_wait_seconds:g} s",
                    )
                )
            self.sleep(wait)
            waited += wait
        local.append(self.clock())
        return waited


_bound_gate: contextvars.ContextVar[CapacityGate | None] = contextvars.ContextVar("capacity_gate", default=None)


@contextmanager
def capacity_scope(gate: CapacityGate | None = None) -> Iterator[CapacityGate]:
    """Admit every call recorded inside the block through ``gate`` (a fresh one by
    default) before it is made. The Dagster ops that reach vendors bind this next to
    `run_scope`."""
    bound = gate if gate is not None else CapacityGate()
    token = _bound_gate.set(bound)
    try:
        yield bound
    finally:
        _bound_gate.reset(token)


# --- the one path ---------------------------------------------------------------------


def _exact_cost(cost: Decimal | int) -> Decimal:
    """Quota units are exact: an int or a Decimal, never a binary float (review on #741 —
    `Decimal(0.1)` is a 55-digit expansion, and it would land in a numeric column)."""
    if isinstance(cost, bool) or not isinstance(cost, (int, Decimal)):
        raise TypeError(f"cost must be an int or Decimal, not {type(cost).__name__}")
    return Decimal(cost)


_active: contextvars.ContextVar[CallRecord | None] = contextvars.ContextVar("external_call_active", default=None)


@contextmanager
def record_call(
    source: str,
    endpoint: str,
    *,
    caller: str,
    request_uri: str | None = None,
    cost: Decimal | int = 1,
    writer: Callable[[CallRecord], None] | None = None,
    admit: bool = True,
) -> Iterator[CallRecord]:
    """Wrap exactly one outbound request. The block reports the answer through
    ``record.observe(...)``; an exception inside the block is recorded as a failed
    call and re-raised unchanged.

    Inside a `capacity_scope`, the bound gate admits the call BEFORE the block runs: a
    refusal raises `CapacityExceeded` here, the request is never made and no row is
    written. ``admit=False`` is for a caller that has already admitted the call through
    its own gate (`SourceGateway.call`).

    Nested use is one row, not two: when an outer ``record_call`` (a `SourceGateway.call`
    around an adapter that itself goes through `http_get`) is active, the inner block
    receives the OUTER record and enriches it — status, digest, the URI actually asked —
    instead of emitting a second row for the same request (and is not admitted twice).
    """
    outer = _active.get()
    if outer is not None:
        if outer.request_uri is None and request_uri is not None:
            outer.request_uri = redact_uri(request_uri)
        yield outer
        return
    gate = _bound_gate.get()
    if admit and gate is not None:
        gate.admit(source)
    started = time.monotonic()
    called_at = datetime.now(UTC)
    record = CallRecord(
        source=source,
        endpoint=endpoint,
        caller=caller,
        called_at=called_at,
        request_uri=redact_uri(request_uri),
        cost=_exact_cost(cost),
        run_key=_run_key.get(),
        capacity_window_id=capacity_window_id(source, called_at),
    )
    token = _active.set(record)
    try:
        yield record
    except Exception as exc:
        record.ok = False
        if record.error is None:
            record.error = f"{type(exc).__name__}: {exc}"[:_ERROR_LIMIT]
        raise
    finally:
        _active.reset(token)
        record.duration_ms = int((time.monotonic() - started) * 1000)
        if record.ok is None:
            record.ok = record.status_code is None or record.status_code < 400
            if not record.ok and record.error is None:
                record.error = f"HTTP {record.status_code}"
        emit(record, writer)


def _requested_uri(url: str, params: Any) -> str:
    """The URI the request actually asks for, query included — `params=` is how httpx
    callers pass the symbol and window, and a ledger row without them cannot say which
    request it was (Copilot on #741). Credentials are blanked by `record_call`."""
    if not params:
        return url
    items = params.items() if hasattr(params, "items") else params
    query = urllib.parse.urlencode(list(items), doseq=True)
    return f"{url}&{query}" if "?" in url else f"{url}?{query}"


def _observe_http(call: CallRecord, response: Any) -> None:
    """Record an httpx-shaped answer; an error status carries the vendor's own message
    when the body has one (review on #741), not just the code."""
    status = getattr(response, "status_code", None)
    body = getattr(response, "content", None)
    call.observe(status_code=status, body=body)
    if status is not None and int(status) >= 400:
        message = _vendor_message(body) if isinstance(body, bytes) else None
        call.fail(message or f"HTTP {status}")


def http_get(
    client: Any, source: str, endpoint: str, url: str, *, caller: str, cost: Decimal | int = 1, **kwargs: Any
) -> Any:
    """``client.get(url, **kwargs)`` through the ledger (httpx-shaped clients)."""
    with record_call(
        source, endpoint, caller=caller, request_uri=_requested_uri(url, kwargs.get("params")), cost=cost
    ) as call:
        response = client.get(url, **kwargs)
        _observe_http(call, response)
        return response


def http_post(
    client: Any, source: str, endpoint: str, url: str, *, caller: str, cost: Decimal | int = 1, **kwargs: Any
) -> Any:
    """``client.post(url, **kwargs)`` through the ledger (httpx-shaped clients)."""
    with record_call(
        source, endpoint, caller=caller, request_uri=_requested_uri(url, kwargs.get("params")), cost=cost
    ) as call:
        response = client.post(url, **kwargs)
        _observe_http(call, response)
        return response


class SourceHTTPError(Exception):
    """A vendor answered with an error status and the caller asked for that to raise."""

    def __init__(self, status_code: int, body: bytes) -> None:
        super().__init__(f"HTTP {status_code}: {body[:200]!r}")
        self.status_code = status_code
        self.body = body


def _status_of(response: Any) -> int | None:
    status = getattr(response, "status", None)
    if status is None and hasattr(response, "getcode"):
        status = response.getcode()
    return int(status) if status is not None else None


def _vendor_message(body: bytes) -> str | None:
    """The message a JSON error body carries, if any — the vendor's own words."""
    try:
        payload = json.loads(body.decode())
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    for key in ("message", "error", "detail", "Message"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value[:300]
    return None


def urlopen(
    source: str,
    endpoint: str,
    request: str | urllib.request.Request,
    *,
    caller: str,
    timeout: float,
    cost: Decimal | int = 1,
    raise_for_status: bool = False,
) -> tuple[int | None, bytes]:
    """``urllib.request.urlopen`` through the ledger. Returns ``(status, body)``.

    Status-honest (#557): an HTTP error's body IS the vendor's answer, so a 4xx/5xx is
    returned to the caller (recorded as a failed call with the vendor's message) unless
    ``raise_for_status`` is set, in which case ``SourceHTTPError`` carries it.
    """
    uri = request.full_url if isinstance(request, urllib.request.Request) else request
    with record_call(source, endpoint, caller=caller, request_uri=uri, cost=cost) as call:
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed vendor hosts
                body = bytes(response.read())
                status = _status_of(response)
        except urllib.error.HTTPError as error:
            with error:
                body = bytes(error.read())
            status = int(error.code)
            call.observe(status_code=status, body=body)
            call.fail(_vendor_message(body) or f"HTTP {status}")
            if raise_for_status:
                raise SourceHTTPError(status, body) from error
            return status, body
        call.observe(status_code=status, body=body)
        if raise_for_status and status is not None and status >= 400:
            call.fail(_vendor_message(body) or f"HTTP {status}")
            raise SourceHTTPError(status, body)
        return status, body


# --- the explicit gateway (#740): one gate, rows on the caller's connection ------------


@dataclass
class SourceGateway:
    """One path to a vendor or model for callers that must be throttled and budgeted
    explicitly (#740: the standards lane's SEC filings, the model and search seats).

    `call` admits through the gateway's own `CapacityGate` — the declared daily budget
    against the ledger, the rate window paced — runs the callable, and records the
    attempt. Recording goes through `record_call`, so the row has the same shape as every
    adapter's — and when the callable itself goes through `http_get`/`urlopen`, the two
    collapse into ONE row carrying the status, the digest and the URI. Rows are written on
    the caller's connection (the standards tests count them on their fake connection); a
    refused call is `CapacityExceeded` and writes nothing. A seat must declare both a
    window and a daily budget to be called here.
    """

    connection: Any
    caller: str
    capacities: Mapping[str, SourceCapacity] = field(default_factory=lambda: CAPACITIES)
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], datetime] = _utcnow
    environment: str | None = None
    #: The shared rate-window read. Other processes' calls are committed rows the
    #: autocommit ledger connection sees; this gateway's own sit uncommitted in the caller's
    #: transaction and are paced by the gate's local window instead.
    recent_calls: Callable[[str, datetime], tuple[int, datetime | None]] | None = ledger_window
    _gate: CapacityGate = field(init=False, repr=False)

    def __post_init__(self) -> None:
        # The budget is read on the caller's connection, where this gateway's own
        # uncommitted rows are visible alongside every committed one.
        self._gate = CapacityGate(
            capacities=self.capacities,
            environment=self.environment,
            spent_since=self._recorded_since,
            recent_calls=self.recent_calls,
            clock=self.clock,
            sleep=self.sleep,
            now=self.now,
        )

    def capacity(self, source: str) -> SourceCapacity:
        capacity = self.capacities.get(source)
        if capacity is None:
            _refuse(CapacityExceeded(source, "no declared capacity — register the source before calling it"))
        if not capacity.enforceable:
            _refuse(
                CapacityExceeded(
                    source,
                    "declared capacity is not enforceable here — SourceGateway.call needs a rate window "
                    "and a daily budget",
                )
            )
        return capacity

    def call(self, source: str, endpoint: str, fn: Callable[[], T]) -> T:
        self.capacity(source)
        self._gate.admit(source)
        with record_call(source, endpoint, caller=self.caller, writer=self._write, admit=False) as record:
            result = fn()
            if isinstance(result, bytes | str):
                # A helper that returns the body itself (`_get_bytes` in the standards
                # lane): the bytes are what the ledger must dereference to.
                record.observe(body=result)
            else:
                status = getattr(result, "status_code", None)
                body = getattr(result, "content", None)
                if status is not None or isinstance(body, bytes):
                    record.observe(status_code=status, body=body)
            return result

    def _write(self, record: CallRecord) -> None:
        self.connection.execute(_INSERT_SQL, _insert_params(record))

    def _recorded_since(self, source: str, since: datetime) -> int:
        row = self.connection.execute(_SPENT_SQL, (source, since)).fetchone()
        return int(row[0]) if row else 0
