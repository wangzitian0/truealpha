# ruff: noqa: I001, E401, E402, E701, E702, E731, F401, F811
# fmt: off
from __future__ import annotations; import contextvars; import functools; import hashlib; import json; import logging; import time; import urllib.error; import urllib.parse; import urllib.request; from collections import Counter, deque; from collections.abc import Callable, Iterator, Mapping; from contextlib import contextmanager; from dataclasses import asdict, dataclass, field, replace; from datetime import UTC, date, datetime, timedelta; from datetime import time as clock_time; from decimal import Decimal; from types import MappingProxyType; from typing import Any, NoReturn, TypeVar; from truealpha_contracts.environment_contract import DAY_SECONDS, load_environment_contract; from data_engine.config import settings; from data_engine.datahub.fetch_key import FetchKey; from data_engine.datahub.production_topt.source_registrations import LEDGER_CAPACITIES, CapacityDeclaration; from data_engine.sources import accept_rules; from data_engine.sources.landed_keys import LandedKeys, PostgresLandedKeys; log = logging.getLogger(__name__); T = TypeVar("T"); _ERROR_LIMIT = 500; PROBE, WINDOW_NOT_FINAL, MODEL_CALL, ANSWER_REFUSED = "probe", "window_not_final", "model_call", "answer_refused"; _REDACTED_KEY_MARKERS = ("apikey", "api_key", "api-key", "token", "secret", "password", "auth", "key")
@dataclass(frozen=True)
class SourceCapacity:
    source: str; window_seconds: float | None; calls_per_window: int | None; daily_budget: int | None = None; note: str = ""; account: str | None = None; pool: tuple[str, ...] = ()
    def __post_init__(self) -> None:
        if (self.window_seconds is None) != (self.calls_per_window is None): raise ValueError(f"capacity for {self.source!r}: window and calls-per-window go together")
        for name in ("window_seconds", "calls_per_window", "daily_budget"):
            if (val := getattr(self, name)) is not None and val <= 0: raise ValueError(f"capacity for {self.source!r} must be positive in every dimension")
    @classmethod
    def declared(cls, source: str, d: CapacityDeclaration) -> SourceCapacity: pool = () if d.account is None else load_environment_contract().vendor_accounts.accounts[d.account].seats; return cls( source, None if d.window_seconds is None else float(d.window_seconds), d.calls_per_window, d.daily_budget, d.documented, d.account, pool, )
    @property
    def enforceable(self) -> bool: return self.window_seconds is not None
    @property
    def seats(self) -> tuple[str, ...]: return self.pool or (self.source,)
    def in_environment(self, environment: str) -> SourceCapacity | None:
        if self.account is None: return self
        if not (share := load_environment_contract().account_share(self.account, environment)).admits_a_call: return None
        return replace( self, calls_per_window=None if self.window_seconds is None else share.share_in_window(int(self.window_seconds)), daily_budget=None if self.daily_budget is None else share.share_in_window(DAY_SECONDS), account=None, )
CAPACITIES: Mapping[str, SourceCapacity] = MappingProxyType( {seat: SourceCapacity.declared(seat, declaration) for seat, declaration in LEDGER_CAPACITIES.items()} )
def capacity_window_id(source: str, at: datetime) -> str | None:
    if (capacity := CAPACITIES.get(source)) is None: return None
    if capacity.daily_budget is not None: return f"{source}:day:{at.astimezone(UTC).date().isoformat()}"
    if window := capacity.window_seconds or (60.0 if source == _model_seat() else None): label = int(window) if float(window).is_integer() else window; return f"{source}:{label}s:{int(at.timestamp() // window)}"
    return None
_run_key: contextvars.ContextVar[str | None] = contextvars.ContextVar("external_call_run_key", default=None)
@contextmanager
def run_scope(run_key: str) -> Iterator[None]:
    token = _run_key.set(run_key)
    try: yield
    finally: _run_key.reset(token)
@contextmanager
def script_run(name: str) -> Iterator[str]:
    run_key = f"script:{name}:{int(_utcnow().timestamp())}"
    with run_scope(run_key): yield run_key
@dataclass
class CallRecord:
    source: str; endpoint: str; caller: str; called_at: datetime; request_uri: str | None = None; cost: Decimal = Decimal(1); status_code: int | None = None; payload_sha256: str | None = None; byte_length: int | None = None; error: str | None = None; ok: bool | None = None; duration_ms: int | None = None; run_key: str | None = None; capacity_window_id: str | None = None; attempt: int | None = None; provider_code: str | None = None; request_params: dict[str, Any] | None = None; fetch_key: str | None = None; completion_tokens: int | None = None; reasoning_tokens: int | None = None; repeat_reason: str | None = None
    def observe(self, *, status_code: int | None = None, body: bytes | str | None = None) -> None:
        if status_code is not None: self.status_code = int(status_code)
        if body is None: return
        raw = body.encode() if isinstance(body, str) else body
        try: self.payload_sha256 = hashlib.sha256(raw).hexdigest()
        except TypeError: return
        self.byte_length = len(raw)
        if ( self.fetch_key is None or self.repeat_reason is not None or (self.status_code is not None and self.status_code >= 400) ): return
        if not accept_rules.accepts(FetchKey.from_ledger_key(self.fetch_key), raw): self.repeat_reason = ANSWER_REFUSED
    def fail(self, error: str) -> None: self.ok, self.error = False, error[:_ERROR_LIMIT]
    def as_row(self) -> dict[str, Any]: return {**asdict(self), "called_at": self.called_at.isoformat(), "cost": str(self.cost)}
def _is_credential_key(key: str) -> bool: lowered = key.lower(); return any(marker in lowered for marker in _REDACTED_KEY_MARKERS)
def redact_uri(uri: str | None) -> str | None:
    if uri is None or not (parts := urllib.parse.urlsplit(uri)).query: return uri
    query = [ (k, "***" if _is_credential_key(k) else v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True) ]; return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))
_pg_conn = None
def _pg():
    global _pg_conn
    if _pg_conn is None or _pg_conn.closed: import psycopg; _pg_conn = psycopg.connect(settings.database_url, autocommit=True)
    return _pg_conn
def _pg_reset() -> None:
    global _pg_conn
    try:
        if _pg_conn is not None: _pg_conn.close()
    finally: _pg_conn = None
def _pg_execute(sql: str, params=()):
    import psycopg
    try: return _pg().execute(sql, params)
    except psycopg.errors.UndefinedColumn: raise
    except psycopg.Error: _pg_reset(); return _pg().execute(sql, params)
_BASE_COLUMNS = ( "source", "endpoint", "caller", "called_at", "ok", "status_code", "error", "duration_ms", "request_uri", "payload_sha256", "byte_length", "cost", "capacity_window_id", "run_key", ); _ATTEMPT_COLUMNS = ("attempt", "provider_code", "request_params"); _TOKEN_COLUMNS = ("completion_tokens", "reasoning_tokens"); _KEY_COLUMNS = ("fetch_key",); _REASON_COLUMNS = ("repeat_reason",); _OPTIONAL_GROUPS = (_ATTEMPT_COLUMNS, _TOKEN_COLUMNS, _KEY_COLUMNS, _REASON_COLUMNS)
@functools.cache
def _statement(columns: tuple[str, ...]) -> str: placeholders = ["%s::jsonb" if col == "request_params" else "%s" for col in columns]; return f"insert into staging.api_call_ledger ({', '.join(columns)}) values ({', '.join(placeholders)})"
_INSERT_SQL = _statement(_BASE_COLUMNS); _INSERT_ATTEMPT_SQL = _statement(_BASE_COLUMNS + _ATTEMPT_COLUMNS); _INSERT_KEYED_SQL = _statement(_BASE_COLUMNS + _KEY_COLUMNS)
def _value(record: CallRecord, column: str) -> Any:
    if column == "ok": return bool(record.ok)
    if column == "request_params": return None if record.request_params is None else json.dumps(record.request_params, sort_keys=True)
    return getattr(record, column)
def _insert_params(record: CallRecord) -> tuple: return tuple(_value(record, column) for column in _BASE_COLUMNS)
def _insert_ladder(record: CallRecord) -> list[tuple[str, tuple]]: groups = [g for g in _OPTIONAL_GROUPS if any(getattr(record, col) is not None for col in g)]; return [ ( _statement(cols := _BASE_COLUMNS + tuple(c for g in groups[:kept] for c in g)), tuple(_value(record, c) for c in cols), ) for kept in range(len(groups), -1, -1) ]
def _insert_statement(record: CallRecord) -> tuple[str, tuple]: return _insert_ladder(record)[0]
def postgres_writer(record: CallRecord) -> None:
    import psycopg; *richer, (base_sql, base_params) = _insert_ladder(record)
    for sql, params in richer:
        try: _pg_execute(sql, params); return
        except psycopg.errors.UndefinedColumn: log.warning("staging.api_call_ledger lacks a column of this row (a migration has not run); trying fewer")
    _pg_execute(base_sql, base_params)
class MemoryLedger(list[CallRecord]):
    def __call__(self, record: CallRecord) -> None: self.append(record)
    def calls_since(self, source: str, since: datetime) -> int: return sum(1 for row in self if row.source == source and row.called_at >= since)
    def window(self, source: str, since: datetime) -> tuple[int, datetime | None]: times = [row.called_at for row in self if row.source == source and row.called_at > since]; return len(times), min(times, default=None)
    def daily_calls(self, source: str, since: datetime, until: datetime) -> dict[tuple[str, date], int]: return dict( Counter( (row.caller, row.called_at.astimezone(UTC).date()) for row in self if row.source == source and since <= row.called_at < until ) )
_writer: Callable[[CallRecord], None] = postgres_writer
def set_writer(writer: Callable[[CallRecord], None]) -> Callable[[CallRecord], None]: global _writer; previous, _writer = _writer, writer; return previous
def emit(record: CallRecord) -> None:
    if settings.external_call_ledger == "off": return
    try: _writer(record)
    except Exception: log.exception("external call ledger write failed; unrecorded call: %s", json.dumps(record.as_row()))
class CapacityExceeded(RuntimeError):
    def __init__(self, source: str, reason: str) -> None: super().__init__(f"{source}: {reason}"); self.source, self.reason = source, reason
class BudgetExhausted(CapacityExceeded):
    def __init__(self, source: str, *, environment: str, budget: int, spent: int) -> None: super().__init__(source, f"daily budget {budget} spent ({spent} calls today) in {environment}"); self.environment, self.budget, self.spent = environment, budget, spent
SHARE_BELOW_ONE_CALL = "share_below_one_call"
class ShareBelowOneCall(CapacityExceeded):
    def __init__(self, source: str, *, environment: str, account: str) -> None: super().__init__( source, f"{SHARE_BELOW_ONE_CALL}: the {account} account gives {environment} less than one call in a limit window", ); self.environment, self.account = environment, account
_refusal_listeners: contextvars.ContextVar[tuple[Callable[[CapacityExceeded], None], ...]] = contextvars.ContextVar( "capacity_refusal_listeners", default=() )
@contextmanager
def on_capacity_refusal(listener: Callable[[CapacityExceeded], None]) -> Iterator[None]:
    token = _refusal_listeners.set((*_refusal_listeners.get(), listener))
    try: yield
    finally: _refusal_listeners.reset(token)
def _refuse(error: CapacityExceeded) -> NoReturn:
    log.warning("rule-6 gate refused a %s call before it was made: %s", error.source, error.reason)
    for listener in _refusal_listeners.get():
        try: listener(error)
        except Exception: log.exception("capacity refusal listener failed for %s", error.source)
    raise error
_SPENT_SQL = "select count(*) from staging.api_call_ledger where source = %s and called_at >= %s"; _WINDOW_SQL = "select count(*), min(called_at) from staging.api_call_ledger where source = %s and called_at > %s"; _DAILY_SQL = "select caller, (called_at at time zone'UTC')::date, count(*) from staging.api_call_ledger where source = %s and called_at >= %s and called_at < %s group by 1, 2"
def ledger_calls_since(source: str, since: datetime) -> int:
    if settings.external_call_ledger == "off": return 0
    if _writer is postgres_writer: return int(row[0]) if (row := _pg_execute(_SPENT_SQL, (source, since)).fetchone()) else 0
    return int(reader(source, since)) if (reader := getattr(_writer, "calls_since", None)) is not None else 0
def ledger_window(source: str, since: datetime) -> tuple[int, datetime | None]:
    if settings.external_call_ledger == "off": return 0, None
    if _writer is postgres_writer: return (int(row[0]), row[1]) if (row := _pg_execute(_WINDOW_SQL, (source, since)).fetchone()) else (0, None)
    return reader(source, since) if (reader := getattr(_writer, "window", None)) is not None else (0, None)
def ledger_daily_calls(source: str, since: datetime, until: datetime) -> dict[tuple[str, date], int]:
    if settings.external_call_ledger == "off": return {}
    if _writer is postgres_writer: return {(str(c), day): int(n) for c, day, n in _pg_execute(_DAILY_SQL, (source, since, until)).fetchall()}
    return dict(reader(source, since, until)) if (reader := getattr(_writer, "daily_calls", None)) is not None else {}
def capacity_environment() -> str:
    try: return settings.environment_tier.value
    except ValueError: return settings.app_env
def _utcnow() -> datetime: return datetime.now(UTC)
_EXPIRY_MARGIN_SECONDS = 0.01
@dataclass
class CapacityGate:
    capacities: Mapping[str, SourceCapacity] = field(default_factory=lambda: CAPACITIES); environment: str | None = None; spent_since: Callable[[str, datetime], int] = ledger_calls_since; recent_calls: Callable[[str, datetime], tuple[int, datetime | None]] | None = ledger_window; clock: Callable[[], float] = time.monotonic; sleep: Callable[[float], None] = time.sleep; now: Callable[[], datetime] = _utcnow; max_wait_seconds: float = 900.0; _windows: dict[str, deque[float]] = field(default_factory=dict, repr=False); _admitted: dict[str, tuple[date, int]] = field(default_factory=dict, repr=False)
    def admit(self, source: str) -> None:
        if (declared := self.capacities.get(source)) is None: _refuse(CapacityExceeded(source, "no declared capacity — register the source before calling it"))
        if (capacity := declared.in_environment(self._environment())) is None: _refuse(ShareBelowOneCall(source, environment=self._environment(), account=str(declared.account)))
        seats = capacity.seats
        if capacity.daily_budget is not None: self._check_daily_budget(source, seats, capacity.daily_budget)
        if capacity.window_seconds is not None and capacity.calls_per_window is not None:
            if ( self._queue(source, seats, capacity.window_seconds, capacity.calls_per_window) and capacity.daily_budget is not None ): self._check_daily_budget(source, seats, capacity.daily_budget)
        today = self.now().astimezone(UTC).date(); self._admitted[seats[0]] = (today, self._admitted_today(seats[0], today) + 1)
    def _environment(self) -> str: return self.environment if self.environment is not None else capacity_environment()
    def _admitted_today(self, pool: str, today: date) -> int: day, admitted = self._admitted.get(pool, (today, 0)); return admitted if day == today else 0
    def _check_daily_budget(self, source: str, seats: tuple[str, ...], budget: int) -> None:
        today = self.now().astimezone(UTC).date(); since = datetime.combine(today, clock_time.min, tzinfo=UTC); spent = max(sum(self.spent_since(seat, since) for seat in seats), self._admitted_today(seats[0], today))
        if spent >= budget: _refuse(BudgetExhausted(source, environment=self._environment(), budget=budget, spent=spent))
    def _queue(self, source: str, seats: tuple[str, ...], window: float, calls: int) -> float:
        local = self._windows.setdefault(seats[0], deque()); waited = 0.0
        while True:
            tick = self.clock()
            while local and tick - local[0] >= window: local.popleft()
            wait = window - (tick - local[0]) if len(local) >= calls else 0.0
            if self.recent_calls is not None:
                at = self.now(); windows = [self.recent_calls(seat, at - timedelta(seconds=window)) for seat in seats]; oldest = min((first for _, first in windows if first is not None), default=None)
                if sum(c for c, _ in windows) >= calls and oldest is not None: wait = max(wait, window - (at - oldest).total_seconds() + _EXPIRY_MARGIN_SECONDS)
            if wait <= 0: break
            if waited + wait > self.max_wait_seconds: _refuse( CapacityExceeded( source, f"rate window ({calls} per {window:g} s) stayed full for {self.max_wait_seconds:g} s" ) )
            self.sleep(wait); waited += wait
        local.append(self.clock()); return waited
_bound_gate: contextvars.ContextVar[CapacityGate | None] = contextvars.ContextVar("capacity_gate", default=None)
@contextmanager
def capacity_scope(gate: CapacityGate | None = None) -> Iterator[CapacityGate]:
    bound = gate if gate is not None else CapacityGate(); token = _bound_gate.set(bound)
    try: yield bound
    finally: _bound_gate.reset(token)
def _exact_cost(cost: Decimal | int) -> Decimal:
    if isinstance(cost, bool) or not isinstance(cost, (int, Decimal)): raise TypeError(f"cost must be an int or Decimal, not {type(cost).__name__}")
    return Decimal(cost)
_active: contextvars.ContextVar[CallRecord | None] = contextvars.ContextVar("external_call_active", default=None); _window_final: contextvars.ContextVar[bool] = contextvars.ContextVar("external_call_window_final", default=True); _probe: contextvars.ContextVar[bool] = contextvars.ContextVar("external_call_probe", default=False)
@functools.cache
def _model_seat() -> str: return load_environment_contract().model_seat.seat
def _creation_reason(source: str) -> str | None:
    if _probe.get(): return PROBE
    if not _window_final.get(): return WINDOW_NOT_FINAL
    return MODEL_CALL if source == _model_seat() else None
@contextmanager
def record_call( source: str, endpoint: str, *, identity: FetchKey, caller: str, request_uri: str | None = None, cost: Decimal | int = 1, admit: bool = True, ) -> Iterator[CallRecord]:
    if (outer := _active.get()) is not None:
        if outer.request_uri is None and request_uri is not None: outer.request_uri = redact_uri(request_uri)
        yield outer; return
    if admit and (gate := _bound_gate.get()) is not None: gate.admit(source)
    started, called_at = time.monotonic(), datetime.now(UTC); record = CallRecord( source=source, endpoint=endpoint, caller=caller, called_at=called_at, request_uri=redact_uri(request_uri), cost=_exact_cost(cost), run_key=_run_key.get(), capacity_window_id=capacity_window_id(source, called_at), fetch_key=identity.ledger_key, repeat_reason=_creation_reason(source), ); token = _active.set(record)
    try: yield record
    except Exception as exc:
        record.ok = False
        if record.error is None: record.error = f"{type(exc).__name__}: {exc}"[:_ERROR_LIMIT]
        raise
    finally:
        _active.reset(token); record.duration_ms = int((time.monotonic() - started) * 1000)
        if record.ok is None:
            record.ok = record.status_code is None or record.status_code < 400
            if not record.ok and record.error is None: record.error = f"HTTP {record.status_code}"
        emit(record)
def _requested_uri(url: str, params: Any) -> str:
    if not params: return url
    query = urllib.parse.urlencode(list(params.items() if hasattr(params, "items") else params), doseq=True); return f"{url}&{query}" if "?" in url else f"{url}?{query}"
def _observe_http(call: CallRecord, response: Any) -> None:
    status, body = getattr(response, "status_code", None), getattr(response, "content", None); call.observe(status_code=status, body=body)
    if status is not None and int(status) >= 400: call.fail((_vendor_message(body) if isinstance(body, bytes) else None) or f"HTTP {status}")
def http_get( client: Any, source: str, endpoint: str, url: str, *, identity: FetchKey, caller: str, cost: Decimal | int = 1, **kwargs: Any, ) -> Any:
    with record_call( source, endpoint, identity=identity, caller=caller, request_uri=_requested_uri(url, kwargs.get("params")), cost=cost, ) as call: response = client.get(url, **kwargs); _observe_http(call, response); return response
def http_post( client: Any, source: str, endpoint: str, url: str, *, identity: FetchKey, caller: str, cost: Decimal | int = 1, **kwargs: Any, ) -> Any:
    with record_call( source, endpoint, identity=identity, caller=caller, request_uri=_requested_uri(url, kwargs.get("params")), cost=cost, ) as call: response = client.post(url, **kwargs); _observe_http(call, response); return response
class SourceHTTPError(Exception):
    def __init__(self, status_code: int, body: bytes) -> None: super().__init__(f"HTTP {status_code}: {body[:200]!r}"); self.status_code, self.body = status_code, body
def _status_of(response: Any) -> int | None:
    status = getattr(response, "status", None)
    if status is None and hasattr(response, "getcode"): status = response.getcode()
    return int(status) if status is not None else None
def _vendor_message(body: bytes) -> str | None:
    try: payload = json.loads(body.decode())
    except (ValueError, UnicodeDecodeError): return None
    if not isinstance(payload, dict): return None
    return next( (v[:300] for k in ("message", "error", "detail", "Message") if isinstance(v := payload.get(k), str) and v), None )
def urlopen( source: str, endpoint: str, request: str | urllib.request.Request, *, identity: FetchKey, caller: str, timeout: float, cost: Decimal | int = 1, raise_for_status: bool = False, ) -> tuple[int | None, bytes]:
    uri = request.full_url if isinstance(request, urllib.request.Request) else request
    with record_call(source, endpoint, identity=identity, caller=caller, request_uri=uri, cost=cost) as call:
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response: body, status = bytes(response.read()), _status_of(response)
        except urllib.error.HTTPError as error:
            with error: body = bytes(error.read())
            status = int(error.code); call.observe(status_code=status, body=body); call.fail(_vendor_message(body) or f"HTTP {status}")
            if raise_for_status: raise SourceHTTPError(status, body) from error
            return status, body
        call.observe(status_code=status, body=body)
        if raise_for_status and status is not None and status >= 400: call.fail(_vendor_message(body) or f"HTTP {status}"); raise SourceHTTPError(status, body)
        return status, body
@contextmanager
def probe_scope(active: bool = True) -> Iterator[None]:
    token = _probe.set(active)
    try: yield
    finally: _probe.reset(token)
_landed_keys: LandedKeys = PostgresLandedKeys(_pg, _pg_reset)
def utc_day() -> str: return _utcnow().date().isoformat()
def landed(key: FetchKey) -> bytes | None:
    if settings.external_call_ledger == "off" or _probe.get(): return None
    try: return _landed_keys.lookup(key)
    except Exception: log.exception("landed-key lookup failed for %s; the key will be fetched", key.ledger_key); return None
def land( key: FetchKey, body: bytes, *, content_type: str = "application/json", fetched_at: datetime | None = None ) -> None:
    if settings.external_call_ledger == "off" or _probe.get(): return
    try: _landed_keys.land( key, body, content_type=content_type, fetched_at=fetched_at or _utcnow(), run_key=_run_key.get() )
    except Exception: log.exception("landing %s failed; the next call for this key will fetch again", key.ledger_key)
def fetch_once( key: FetchKey, fetch: Callable[[FetchKey], bytes], *, content_type: str = "application/json", keep: bool = True ) -> bytes:
    if _probe.get(): return fetch(key)
    if not keep:
        token = _window_final.set(False)
        try: return fetch(key)
        finally: _window_final.reset(token)
    if (hit := landed(key)) is not None: return hit
    started, body = _utcnow(), fetch(key)
    if accept_rules.accepts(key, body): land(key, body, content_type=content_type, fetched_at=started)
    return body
@dataclass
class SourceGateway:
    connection: Any; caller: str; capacities: Mapping[str, SourceCapacity] = field(default_factory=lambda: CAPACITIES); clock: Callable[[], float] = time.monotonic; sleep: Callable[[float], None] = time.sleep; now: Callable[[], datetime] = _utcnow; environment: str | None = None; recent_calls: Callable[[str, datetime], tuple[int, datetime | None]] | None = ledger_window; _gate: CapacityGate = field(init=False, repr=False)
    def __post_init__(self) -> None: self._gate = CapacityGate( self.capacities, self.environment, self._recorded_since, self.recent_calls, self.clock, self.sleep, self.now )
    def capacity(self, source: str) -> SourceCapacity:
        if (capacity := self.capacities.get(source)) is None: _refuse(CapacityExceeded(source, "no declared capacity — register the source before calling it"))
        if not capacity.enforceable: _refuse( CapacityExceeded( source, "declared capacity is not enforceable here — SourceGateway.call needs a rate window" ) )
        return capacity
    def call(self, source: str, endpoint: str, fn: Callable[[], T], *, identity: FetchKey) -> T:
        self.capacity(source); self._gate.admit(source)
        with record_call(source, endpoint, identity=identity, caller=self.caller, admit=False) as record:
            result = fn()
            if isinstance(result, bytes | str): record.observe(body=result)
            else:
                status, body = getattr(result, "status_code", None), getattr(result, "content", None)
                if status is not None or isinstance(body, bytes): record.observe(status_code=status, body=body)
            return result
    def _recorded_since(self, source: str, since: datetime) -> int: return int(row[0]) if (row := self.connection.execute(_SPENT_SQL, (source, since)).fetchone()) else 0
