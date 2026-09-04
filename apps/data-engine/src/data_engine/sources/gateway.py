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
failed capture — the write failure is logged with the full row instead. Enforcement of
declared capacity (refuse or defer *before* the call, #729 criterion 4) is not this
module's job yet; ``CAPACITIES`` is the interim declaration until #72's per-source
registration object carries it.
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
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, TypeVar

from data_engine.config import settings

log = logging.getLogger(__name__)
T = TypeVar("T")

_ERROR_LIMIT = 500
# Matched case-insensitively as substrings of the lower-cased key, so `APIKEY`, `ApiKey`,
# `access_token` and `x-api-key` are all blanked (review on #741).
_REDACTED_KEY_MARKERS = ("apikey", "api_key", "api-key", "token", "secret", "password", "auth", "key")


# --- declared capacity (interim seat until #72's registration object) ------------------


@dataclass(frozen=True)
class SourceCapacity:
    """What the vendor documents, and what the gateway enforces for a source that is
    called through `SourceGateway.call` (#740: SEC, the model and search seats).

    ``None`` means the vendor publishes no such limit — legal for a record-only source
    (yahoo, the index operator); a source that is *called through the gateway* must
    declare every dimension, or `SourceGateway.capacity` refuses it.

    Sources: Twelve Data pricing page (Basic: 8 credits/min, 800/day); SEC developer
    FAQ (10 requests/s — 8 declared, leaving headroom for the daily tick on the same
    host and User-Agent); OpenFIGI API docs (keyed: 25 requests / 6 s, 100 jobs each);
    moomoo API docs (60 requests / 30 s per quote endpoint; `moomoo_ledger` paces 8 / 30 s).
    """

    source: str
    window_seconds: float | None
    calls_per_window: int | None
    daily_budget: int | None = None
    note: str = ""

    def __post_init__(self) -> None:
        if (self.window_seconds is None) != (self.calls_per_window is None):
            raise ValueError(f"capacity for {self.source!r}: window and calls-per-window go together")
        for name in ("window_seconds", "calls_per_window", "daily_budget"):
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise ValueError(f"capacity for {self.source!r} must be positive in every dimension")

    @property
    def enforceable(self) -> bool:
        """Whether `SourceGateway.call` can throttle and budget this source."""
        return self.window_seconds is not None and self.daily_budget is not None


CAPACITIES: Mapping[str, SourceCapacity] = {
    "sec": SourceCapacity("sec", 1.0, 8, 5000, "SEC fair-access guidance; 8 of 10/s declared"),
    "nport": SourceCapacity("nport", 1.0, 8, 5000, "SEC fair-access guidance (same hosts as sec)"),
    "twelvedata": SourceCapacity("twelvedata", 60.0, 8, 800, "Basic plan; ONE key is shared by staging and production"),
    "openfigi": SourceCapacity("openfigi", 6.0, 25, None, "keyed tier; 100 jobs per request"),
    "moomoo": SourceCapacity("moomoo", 30.0, 60, None, "per quote endpoint; moomoo_ledger paces 8 / 30 s"),
    "yahoo": SourceCapacity("yahoo", None, None, None, "undocumented; paced by the per-symbol loop"),
    "nasdaq-index": SourceCapacity("nasdaq-index", None, None, None, "undocumented; weekly refresh"),
    # The model and search seats are provider-agnostic placeholders sized for a QQQ
    # backfill; a provider's real limits replace them in the PR that provisions it (#70, #732).
    "filing-extraction-model": SourceCapacity("filing-extraction-model", 60.0, 30, 2000, "placeholder seat (#70)"),
    "search": SourceCapacity("search", 60.0, 20, 500, "placeholder seat (#732)"),
}


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
    warehouse sets `EXTERNAL_CALL_LEDGER=off` instead; `emit` then records nothing)."""

    def __call__(self, record: CallRecord) -> None:
        self.append(record)


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
) -> Iterator[CallRecord]:
    """Wrap exactly one outbound request. The block reports the answer through
    ``record.observe(...)``; an exception inside the block is recorded as a failed
    call and re-raised unchanged.

    Nested use is one row, not two: when an outer ``record_call`` (a `SourceGateway.call`
    around an adapter that itself goes through `http_get`) is active, the inner block
    receives the OUTER record and enriches it — status, digest, the URI actually asked —
    instead of emitting a second row for the same request.
    """
    outer = _active.get()
    if outer is not None:
        if outer.request_uri is None and request_uri is not None:
            outer.request_uri = redact_uri(request_uri)
        yield outer
        return
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


# --- the enforcing gateway (#740): throttle, budget, refuse — and one row per call -------


class CapacityExceeded(RuntimeError):
    def __init__(self, source: str, reason: str) -> None:
        super().__init__(f"{source}: {reason}")
        self.source = source
        self.reason = reason


@dataclass
class SourceGateway:
    """One path to a vendor or model for callers that must be throttled and budgeted
    (#740: the standards lane's SEC filings, the model and search seats).

    `call` checks the declared daily budget against the ledger, paces the rate window,
    runs the callable, and records the attempt. Recording goes through `record_call`, so
    the row has the same shape as every adapter's — and when the callable itself goes
    through `http_get`/`urlopen`, the two collapse into ONE row carrying the status,
    the digest and the URI. Rows are written on the caller's connection (the standards
    tests count them on their fake connection); a refused call is `CapacityExceeded`
    and writes nothing.
    """

    connection: Any
    caller: str
    capacities: Mapping[str, SourceCapacity] = field(default_factory=lambda: CAPACITIES)
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    _windows: dict[str, deque[float]] = field(default_factory=dict)
    # (utc day, calls) per source: a gateway that lives across midnight re-reads the
    # ledger for the new day instead of carrying yesterday's count into it.
    _spent_today: dict[str, tuple[date, int]] = field(default_factory=dict)

    def capacity(self, source: str) -> SourceCapacity:
        capacity = self.capacities.get(source)
        if capacity is None or not capacity.enforceable:
            raise CapacityExceeded(source, "no declared capacity — register the source before calling it")
        return capacity

    def call(self, source: str, endpoint: str, fn: Callable[[], T]) -> T:
        capacity = self.capacity(source)
        self._check_daily_budget(source, capacity)
        self._throttle(source, capacity)
        day, spent = self._spent_today.get(source, (self.now().date(), 0))
        self._spent_today[source] = (day, spent + 1)
        with record_call(source, endpoint, caller=self.caller, writer=self._write) as record:
            result = fn()
            status = getattr(result, "status_code", None)
            body = getattr(result, "content", None)
            if status is not None or isinstance(body, bytes):
                record.observe(status_code=status, body=body)
            return result

    def _write(self, record: CallRecord) -> None:
        self.connection.execute(_INSERT_SQL, _insert_params(record))

    def _check_daily_budget(self, source: str, capacity: SourceCapacity) -> None:
        now = self.now()
        today = now.date()
        cached = self._spent_today.get(source)
        if cached is None or cached[0] != today:
            day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            row = self.connection.execute(
                "select count(*) from staging.api_call_ledger where source = %s and called_at >= %s",
                (source, day_start),
            ).fetchone()
            cached = (today, int(row[0]) if row else 0)
            self._spent_today[source] = cached
        assert capacity.daily_budget is not None  # enforceable, checked by `capacity()`
        if cached[1] >= capacity.daily_budget:
            raise CapacityExceeded(source, f"daily budget {capacity.daily_budget} spent ({cached[1]} calls today)")

    def _throttle(self, source: str, capacity: SourceCapacity) -> None:
        assert capacity.window_seconds is not None and capacity.calls_per_window is not None
        window = self._windows.setdefault(source, deque())
        now = self.clock()
        while window and now - window[0] >= capacity.window_seconds:
            window.popleft()
        if len(window) >= capacity.calls_per_window:
            wait = capacity.window_seconds - (now - window[0])
            if wait > 0:
                self.sleep(wait)
            now = self.clock()
            while window and now - window[0] >= capacity.window_seconds:
                window.popleft()
        window.append(now)
