"""External call gateway, capacity enforcement, landed-key deduplication, and audit ledger (#729, #1521)."""

from __future__ import annotations

import contextvars
import functools
import hashlib
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, deque
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock_time
from decimal import Decimal
from types import MappingProxyType
from typing import Any, NoReturn, TypeVar

from truealpha_contracts.environment_contract import DAY_SECONDS, load_environment_contract

from data_engine.config import settings
from data_engine.datahub.fetch_key import FetchKey
from data_engine.datahub.production_topt.source_registrations import LEDGER_CAPACITIES, CapacityDeclaration
from data_engine.sources import accept_rules
from data_engine.sources.landed_keys import LandedKeys, PostgresLandedKeys

log = logging.getLogger(__name__)
T = TypeVar("T")

_ERROR_LIMIT = 500
PROBE = "probe"
WINDOW_NOT_FINAL = "window_not_final"
MODEL_CALL = "model_call"
ANSWER_REFUSED = "answer_refused"
_REDACTED_KEY_MARKERS = ("apikey", "api_key", "api-key", "token", "secret", "password", "auth", "key")


@dataclass(frozen=True)
class SourceCapacity:
    """Declared rate-window and daily-budget limits for one ledger seat."""

    source: str
    window_seconds: float | None
    calls_per_window: int | None
    daily_budget: int | None = None
    note: str = ""
    account: str | None = None
    pool: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (self.window_seconds is None) != (self.calls_per_window is None):
            raise ValueError(f"capacity for {self.source!r}: window and calls-per-window go together")
        for name in ("window_seconds", "calls_per_window", "daily_budget"):
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise ValueError(f"capacity for {self.source!r} must be positive in every dimension")

    @classmethod
    def declared(cls, source: str, declaration: CapacityDeclaration) -> SourceCapacity:
        pool = (
            ()
            if declaration.account is None
            else load_environment_contract().vendor_accounts.accounts[declaration.account].seats
        )
        return cls(
            source,
            None if declaration.window_seconds is None else float(declaration.window_seconds),
            declaration.calls_per_window,
            declaration.daily_budget,
            declaration.documented,
            declaration.account,
            pool,
        )

    @property
    def enforceable(self) -> bool:
        return self.window_seconds is not None

    @property
    def seats(self) -> tuple[str, ...]:
        return self.pool or (self.source,)

    def in_environment(self, environment: str) -> SourceCapacity | None:
        if self.account is None:
            return self
        share = load_environment_contract().account_share(self.account, environment)
        if not share.admits_a_call:
            return None
        return replace(
            self,
            calls_per_window=None if self.window_seconds is None else share.share_in_window(int(self.window_seconds)),
            daily_budget=None if self.daily_budget is None else share.share_in_window(DAY_SECONDS),
            account=None,
        )


CAPACITIES: Mapping[str, SourceCapacity] = MappingProxyType(
    {seat: SourceCapacity.declared(seat, declaration) for seat, declaration in LEDGER_CAPACITIES.items()}
)


def capacity_window_id(source: str, at: datetime) -> str | None:
    capacity = CAPACITIES.get(source)
    if capacity is None:
        return None
    if capacity.daily_budget is not None:
        return f"{source}:day:{at.astimezone(UTC).date().isoformat()}"
    window = capacity.window_seconds or (60.0 if source == _model_seat() else None)
    if window:
        label = int(window) if float(window).is_integer() else window
        return f"{source}:{label}s:{int(at.timestamp() // window)}"
    return None


_run_key: contextvars.ContextVar[str | None] = contextvars.ContextVar("external_call_run_key", default=None)


@contextmanager
def _bind_var[V](var: contextvars.ContextVar[V], value: V) -> Iterator[None]:
    token = var.set(value)
    try:
        yield
    finally:
        var.reset(token)


@contextmanager
def run_scope(run_key: str) -> Iterator[None]:
    with _bind_var(_run_key, run_key):
        yield


@contextmanager
def script_run(name: str) -> Iterator[str]:
    run_key = f"script:{name}:{int(_utcnow().timestamp())}"
    with run_scope(run_key):
        yield run_key


@dataclass
class CallRecord:
    source: str
    endpoint: str
    caller: str
    called_at: datetime
    request_uri: str | None = None
    cost: Decimal = Decimal(1)
    status_code: int | None = None
    payload_sha256: str | None = None
    byte_length: int | None = None
    error: str | None = None
    ok: bool | None = None
    duration_ms: int | None = None
    run_key: str | None = None
    capacity_window_id: str | None = None
    attempt: int | None = None
    provider_code: str | None = None
    request_params: dict[str, Any] | None = None
    fetch_key: str | None = None
    completion_tokens: int | None = None
    reasoning_tokens: int | None = None
    repeat_reason: str | None = None

    def observe(self, *, status_code: int | None = None, body: bytes | str | None = None) -> None:
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
        if self.fetch_key is None or self.repeat_reason is not None:
            return
        if self.status_code is not None and self.status_code >= 400:
            return
        if not accept_rules.accepts(FetchKey.from_ledger_key(self.fetch_key), raw):
            self.repeat_reason = ANSWER_REFUSED

    def fail(self, error: str) -> None:
        self.ok = False
        self.error = error[:_ERROR_LIMIT]

    def as_row(self) -> dict[str, Any]:
        row = asdict(self)
        row["called_at"] = self.called_at.isoformat()
        row["cost"] = str(self.cost)
        return row


def _is_credential_key(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in _REDACTED_KEY_MARKERS)


def redact_uri(uri: str | None) -> str | None:
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


_pg_conn = None


def _pg():
    global _pg_conn
    if _pg_conn is None or _pg_conn.closed:
        import psycopg

        _pg_conn = psycopg.connect(settings.database_url, autocommit=True)
    return _pg_conn


def _pg_reset() -> None:
    global _pg_conn
    try:
        if _pg_conn is not None:
            _pg_conn.close()
    finally:
        _pg_conn = None


def _pg_execute(sql: str, params=()):
    import psycopg

    try:
        return _pg().execute(sql, params)
    except psycopg.errors.UndefinedColumn:
        raise
    except psycopg.Error:
        _pg_reset()
        return _pg().execute(sql, params)


_BASE_COLUMNS = (
    "source",
    "endpoint",
    "caller",
    "called_at",
    "ok",
    "status_code",
    "error",
    "duration_ms",
    "request_uri",
    "payload_sha256",
    "byte_length",
    "cost",
    "capacity_window_id",
    "run_key",
)
_ATTEMPT_COLUMNS = ("attempt", "provider_code", "request_params")
_TOKEN_COLUMNS = ("completion_tokens", "reasoning_tokens")
_KEY_COLUMNS = ("fetch_key",)
_REASON_COLUMNS = ("repeat_reason",)
_OPTIONAL_GROUPS = (_ATTEMPT_COLUMNS, _TOKEN_COLUMNS, _KEY_COLUMNS, _REASON_COLUMNS)


@functools.cache
def _statement(columns: tuple[str, ...]) -> str:
    placeholders = ["%s::jsonb" if column == "request_params" else "%s" for column in columns]
    return f"insert into staging.api_call_ledger ({', '.join(columns)}) values ({', '.join(placeholders)})"


_INSERT_SQL = _statement(_BASE_COLUMNS)
_INSERT_ATTEMPT_SQL = _statement(_BASE_COLUMNS + _ATTEMPT_COLUMNS)
_INSERT_KEYED_SQL = _statement(_BASE_COLUMNS + _KEY_COLUMNS)


def _value(record: CallRecord, column: str) -> Any:
    if column == "ok":
        return bool(record.ok)
    if column == "request_params":
        return None if record.request_params is None else json.dumps(record.request_params, sort_keys=True)
    return getattr(record, column)


def _insert_params(record: CallRecord) -> tuple:
    return tuple(_value(record, column) for column in _BASE_COLUMNS)


def _insert_ladder(record: CallRecord) -> list[tuple[str, tuple]]:
    groups = [group for group in _OPTIONAL_GROUPS if any(getattr(record, column) is not None for column in group)]
    ladder: list[tuple[str, tuple]] = []
    for kept in range(len(groups), -1, -1):
        columns: tuple[str, ...] = _BASE_COLUMNS + tuple(column for group in groups[:kept] for column in group)
        ladder.append((_statement(columns), tuple(_value(record, column) for column in columns)))
    return ladder


def _insert_statement(record: CallRecord) -> tuple[str, tuple]:
    return _insert_ladder(record)[0]


def postgres_writer(record: CallRecord) -> None:
    import psycopg

    *richer, (base_sql, base_params) = _insert_ladder(record)
    for sql, params in richer:
        try:
            _pg_execute(sql, params)
            return
        except psycopg.errors.UndefinedColumn:
            log.warning("staging.api_call_ledger lacks a column of this row (a migration has not run); trying fewer")
    _pg_execute(base_sql, base_params)


class MemoryLedger(list[CallRecord]):
    """In-memory test double implementing both writer and ledger window/budget queries."""

    def __call__(self, record: CallRecord) -> None:
        self.append(record)

    def calls_since(self, source: str, since: datetime) -> int:
        return sum(1 for row in self if row.source == source and row.called_at >= since)

    def window(self, source: str, since: datetime) -> tuple[int, datetime | None]:
        times = [row.called_at for row in self if row.source == source and row.called_at > since]
        return len(times), min(times, default=None)

    def daily_calls(self, source: str, since: datetime, until: datetime) -> dict[tuple[str, date], int]:
        return dict(
            Counter(
                (row.caller, row.called_at.astimezone(UTC).date())
                for row in self
                if row.source == source and since <= row.called_at < until
            )
        )


_writer: Callable[[CallRecord], None] = postgres_writer


def set_writer(writer: Callable[[CallRecord], None]) -> Callable[[CallRecord], None]:
    global _writer
    previous, _writer = _writer, writer
    return previous


def emit(record: CallRecord) -> None:
    if settings.external_call_ledger == "off":
        return
    try:
        _writer(record)
    except Exception:  # noqa: BLE001
        log.exception("external call ledger write failed; unrecorded call: %s", json.dumps(record.as_row()))


class CapacityExceeded(RuntimeError):
    """Raised when the rule-6 capacity gate refuses an outbound call before dispatch."""

    def __init__(self, source: str, reason: str) -> None:
        super().__init__(f"{source}: {reason}")
        self.source = source
        self.reason = reason


class BudgetExhausted(CapacityExceeded):
    """Raised when the environment's daily seat budget is spent for the UTC day."""

    def __init__(self, source: str, *, environment: str, budget: int, spent: int) -> None:
        super().__init__(source, f"daily budget {budget} spent ({spent} calls today) in {environment}")
        self.environment = environment
        self.budget = budget
        self.spent = spent


SHARE_BELOW_ONE_CALL = "share_below_one_call"


class ShareBelowOneCall(CapacityExceeded):
    """Raised when the environment's share of a shared vendor account is below one call per window."""

    def __init__(self, source: str, *, environment: str, account: str) -> None:
        super().__init__(
            source,
            f"{SHARE_BELOW_ONE_CALL}: the {account} account gives {environment} less than one call in a limit window",
        )
        self.environment = environment
        self.account = account


_refusal_listeners: contextvars.ContextVar[tuple[Callable[[CapacityExceeded], None], ...]] = contextvars.ContextVar(
    "capacity_refusal_listeners", default=()
)


@contextmanager
def on_capacity_refusal(listener: Callable[[CapacityExceeded], None]) -> Iterator[None]:
    with _bind_var(_refusal_listeners, (*_refusal_listeners.get(), listener)):
        yield


def _refuse(error: CapacityExceeded) -> NoReturn:
    log.warning("rule-6 gate refused a %s call before it was made: %s", error.source, error.reason)
    for listener in _refusal_listeners.get():
        try:
            listener(error)
        except Exception:  # noqa: BLE001
            log.exception("capacity refusal listener failed for %s", error.source)
    raise error


_SPENT_SQL = "select count(*) from staging.api_call_ledger where source = %s and called_at >= %s"
_WINDOW_SQL = "select count(*), min(called_at) from staging.api_call_ledger where source = %s and called_at > %s"
_DAILY_SQL = (
    "select caller, (called_at at time zone 'UTC')::date, count(*) from staging.api_call_ledger "
    "where source = %s and called_at >= %s and called_at < %s group by 1, 2"
)


def _ledger_read(default: Any, sql: str, params: tuple[Any, ...], method: str, *args: Any, many: bool = False) -> Any:
    if settings.external_call_ledger == "off":
        return default
    if _writer is postgres_writer:
        cursor = _pg_execute(sql, params)
        return cursor.fetchall() if many else cursor.fetchone()
    reader = getattr(_writer, method, None)
    return reader(*args) if reader is not None else default


def ledger_calls_since(source: str, since: datetime) -> int:
    res = _ledger_read(0, _SPENT_SQL, (source, since), "calls_since", source, since)
    return int(res[0]) if isinstance(res, tuple) else int(res or 0)


def ledger_window(source: str, since: datetime) -> tuple[int, datetime | None]:
    res = _ledger_read((0, None), _WINDOW_SQL, (source, since), "window", source, since)
    return (int(res[0]), res[1]) if res else (0, None)


def ledger_daily_calls(source: str, since: datetime, until: datetime) -> dict[tuple[str, date], int]:
    res = _ledger_read({}, _DAILY_SQL, (source, since, until), "daily_calls", source, since, until, many=True)
    if isinstance(res, list):
        return {(str(caller), day): int(calls) for caller, day, calls in res}
    return dict(res)


def capacity_environment() -> str:
    try:
        return settings.environment_tier.value
    except ValueError:
        return settings.app_env


def _utcnow() -> datetime:
    return datetime.now(UTC)


_EXPIRY_MARGIN_SECONDS = 0.01


@dataclass
class CapacityGate:
    """Enforce declared per-window rate pacing and daily call budgets across seats."""

    capacities: Mapping[str, SourceCapacity] = field(default_factory=lambda: CAPACITIES)
    environment: str | None = None
    spent_since: Callable[[str, datetime], int] = ledger_calls_since
    recent_calls: Callable[[str, datetime], tuple[int, datetime | None]] | None = ledger_window
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], datetime] = _utcnow
    max_wait_seconds: float = 900.0
    _windows: dict[str, deque[float]] = field(default_factory=dict, repr=False)
    _admitted: dict[str, tuple[date, int]] = field(default_factory=dict, repr=False)

    def admit(self, source: str) -> None:
        declared = self.capacities.get(source)
        if declared is None:
            _refuse(CapacityExceeded(source, "no declared capacity — register the source before calling it"))
        capacity = declared.in_environment(self._environment())
        if capacity is None:
            _refuse(ShareBelowOneCall(source, environment=self._environment(), account=str(declared.account)))
        seats = capacity.seats
        if capacity.daily_budget is not None:
            self._check_daily_budget(source, seats, capacity.daily_budget)
        if capacity.window_seconds is not None and capacity.calls_per_window is not None:
            waited = self._queue(source, seats, capacity.window_seconds, capacity.calls_per_window)
            if waited and capacity.daily_budget is not None:
                self._check_daily_budget(source, seats, capacity.daily_budget)
        today = self.now().astimezone(UTC).date()
        self._admitted[seats[0]] = (today, self._admitted_today(seats[0], today) + 1)

    def _environment(self) -> str:
        return self.environment if self.environment is not None else capacity_environment()

    def _admitted_today(self, pool: str, today: date) -> int:
        day, admitted = self._admitted.get(pool, (today, 0))
        return admitted if day == today else 0

    def _check_daily_budget(self, source: str, seats: tuple[str, ...], budget: int) -> None:
        today = self.now().astimezone(UTC).date()
        since = datetime.combine(today, clock_time.min, tzinfo=UTC)
        recorded = sum(self.spent_since(seat, since) for seat in seats)
        spent = max(recorded, self._admitted_today(seats[0], today))
        if spent >= budget:
            _refuse(BudgetExhausted(source, environment=self._environment(), budget=budget, spent=spent))

    def _queue(self, source: str, seats: tuple[str, ...], window: float, calls: int) -> float:
        local = self._windows.setdefault(seats[0], deque())
        waited = 0.0
        while True:
            tick = self.clock()
            while local and tick - local[0] >= window:
                local.popleft()
            wait = window - (tick - local[0]) if len(local) >= calls else 0.0
            if self.recent_calls is not None:
                at = self.now()
                since = at - timedelta(seconds=window)
                windows = [self.recent_calls(seat, since) for seat in seats]
                recorded = sum(count for count, _ in windows)
                oldest = min((first for _, first in windows if first is not None), default=None)
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
    bound = gate if gate is not None else CapacityGate()
    with _bind_var(_bound_gate, bound):
        yield bound


def _exact_cost(cost: Decimal | int) -> Decimal:
    if isinstance(cost, bool) or not isinstance(cost, (int, Decimal)):
        raise TypeError(f"cost must be an int or Decimal, not {type(cost).__name__}")
    return Decimal(cost)


_active: contextvars.ContextVar[CallRecord | None] = contextvars.ContextVar("external_call_active", default=None)
_window_final: contextvars.ContextVar[bool] = contextvars.ContextVar("external_call_window_final", default=True)
_probe: contextvars.ContextVar[bool] = contextvars.ContextVar("external_call_probe", default=False)


@functools.cache
def _model_seat() -> str:
    return load_environment_contract().model_seat.seat


def _creation_reason(source: str) -> str | None:
    if _probe.get():
        return PROBE
    if not _window_final.get():
        return WINDOW_NOT_FINAL
    if source == _model_seat():
        return MODEL_CALL
    return None


@contextmanager
def record_call(
    source: str,
    endpoint: str,
    *,
    identity: FetchKey,
    caller: str,
    request_uri: str | None = None,
    cost: Decimal | int = 1,
    admit: bool = True,
) -> Iterator[CallRecord]:
    """Admit and record one outbound vendor or model request (collapsing nested calls into the outer record)."""
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
        fetch_key=identity.ledger_key,
        repeat_reason=_creation_reason(source),
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
        emit(record)


def _requested_uri(url: str, params: Any) -> str:
    if not params:
        return url
    items = params.items() if hasattr(params, "items") else params
    query = urllib.parse.urlencode(list(items), doseq=True)
    return f"{url}&{query}" if "?" in url else f"{url}?{query}"


def _observe_http(call: CallRecord, response: Any) -> None:
    status = getattr(response, "status_code", None)
    body = getattr(response, "content", None)
    call.observe(status_code=status, body=body)
    if status is not None and int(status) >= 400:
        message = _vendor_message(body) if isinstance(body, bytes) else None
        call.fail(message or f"HTTP {status}")


def _http_request(
    method: str,
    client: Any,
    source: str,
    endpoint: str,
    url: str,
    *,
    identity: FetchKey,
    caller: str,
    cost: Decimal | int = 1,
    **kwargs: Any,
) -> Any:
    with record_call(
        source,
        endpoint,
        identity=identity,
        caller=caller,
        request_uri=_requested_uri(url, kwargs.get("params")),
        cost=cost,
    ) as call:
        response = getattr(client, method)(url, **kwargs)
        _observe_http(call, response)
        return response


def http_get(
    client: Any,
    source: str,
    endpoint: str,
    url: str,
    *,
    identity: FetchKey,
    caller: str,
    cost: Decimal | int = 1,
    **kwargs: Any,
) -> Any:
    return _http_request("get", client, source, endpoint, url, identity=identity, caller=caller, cost=cost, **kwargs)


def http_post(
    client: Any,
    source: str,
    endpoint: str,
    url: str,
    *,
    identity: FetchKey,
    caller: str,
    cost: Decimal | int = 1,
    **kwargs: Any,
) -> Any:
    return _http_request("post", client, source, endpoint, url, identity=identity, caller=caller, cost=cost, **kwargs)


class SourceHTTPError(Exception):
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
    identity: FetchKey,
    caller: str,
    timeout: float,
    cost: Decimal | int = 1,
    raise_for_status: bool = False,
) -> tuple[int | None, bytes]:
    uri = request.full_url if isinstance(request, urllib.request.Request) else request
    with record_call(source, endpoint, identity=identity, caller=caller, request_uri=uri, cost=cost) as call:
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
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


@contextmanager
def probe_scope(active: bool = True) -> Iterator[None]:
    with _bind_var(_probe, active):
        yield


_landed_keys: LandedKeys = PostgresLandedKeys(_pg, _pg_reset)


def utc_day() -> str:
    return _utcnow().date().isoformat()


def landed(key: FetchKey) -> bytes | None:
    if settings.external_call_ledger == "off" or _probe.get():
        return None
    try:
        return _landed_keys.lookup(key)
    except Exception:  # noqa: BLE001
        log.exception("landed-key lookup failed for %s; the key will be fetched", key.ledger_key)
        return None


def land(
    key: FetchKey, body: bytes, *, content_type: str = "application/json", fetched_at: datetime | None = None
) -> None:
    if settings.external_call_ledger == "off" or _probe.get():
        return
    try:
        _landed_keys.land(
            key,
            body,
            content_type=content_type,
            fetched_at=fetched_at or _utcnow(),
            run_key=_run_key.get(),
        )
    except Exception:  # noqa: BLE001
        log.exception("landing %s failed; the next call for this key will fetch again", key.ledger_key)


def fetch_once(
    key: FetchKey,
    fetch: Callable[[FetchKey], bytes],
    *,
    content_type: str = "application/json",
    keep: bool = True,
) -> bytes:
    """Return landed bytes for `key`, or execute `fetch(key)` once and land accepted answers."""
    if _probe.get():
        return fetch(key)
    if not keep:
        with _bind_var(_window_final, False):
            return fetch(key)
    hit = landed(key)
    if hit is not None:
        return hit
    started = _utcnow()
    body = fetch(key)
    if accept_rules.accepts(key, body):
        land(key, body, content_type=content_type, fetched_at=started)
    return body


@dataclass
class SourceGateway:
    """Explicit rate-window and budget-enforced vendor gateway for standards and model callers."""

    connection: Any
    caller: str
    capacities: Mapping[str, SourceCapacity] = field(default_factory=lambda: CAPACITIES)
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], datetime] = _utcnow
    environment: str | None = None
    recent_calls: Callable[[str, datetime], tuple[int, datetime | None]] | None = ledger_window
    _gate: CapacityGate = field(init=False, repr=False)

    def __post_init__(self) -> None:
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
                    "declared capacity is not enforceable here — SourceGateway.call needs a rate window",
                )
            )
        return capacity

    def call(self, source: str, endpoint: str, fn: Callable[[], T], *, identity: FetchKey) -> T:
        self.capacity(source)
        self._gate.admit(source)
        with record_call(source, endpoint, identity=identity, caller=self.caller, admit=False) as record:
            result = fn()
            if isinstance(result, bytes | str):
                record.observe(body=result)
            else:
                status = getattr(result, "status_code", None)
                body = getattr(result, "content", None)
                if status is not None or isinstance(body, bytes):
                    record.observe(status_code=status, body=body)
            return result

    def _recorded_since(self, source: str, since: datetime) -> int:
        row = self.connection.execute(_SPENT_SQL, (source, since)).fetchone()
        return int(row[0]) if row else 0
