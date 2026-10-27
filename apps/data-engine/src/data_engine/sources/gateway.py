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
- ``run_key`` — the run that made the call: ``dagster:<run_id>``, bound for the whole op by `external_op`, or
  ``script:<name>:<epoch seconds>``, bound by `script_run`. A call outside every run is recorded with a NULL
  ``run_key`` and never refused (owner decision D5); the dashboard counts it (#1521).
- ``fetch_key`` — the key of the resource that the call fetched: the ``identity`` of the call (#1521);
- ``repeat_reason`` — why a second row of that key is no duplicate fetch: ``probe``, ``window_not_final``,
  ``answer_refused`` or ``model_call``. The gateway derives it from the call itself; no caller passes one.

Why the writer is autocommit and never raises
--------------------------------------------
The row must survive the caller's transaction: a tick that rolls back still made its
vendor calls (the #628 rollbacks re-fetched 408 obligations *because* nothing recorded
the first attempt). And a ledger outage must not turn a successful vendor answer into a
failed capture — the write failure is logged with the full row instead.

Declared capacity is enforced before the call (#729 criterion 4)
----------------------------------------------------------------
Every seat's capacity is read once, in the source registry
(`source_registrations.LEDGER_CAPACITIES`), from the vendor account row of the environment contract
(#1520); ``CAPACITIES`` here is derived from it. A `CapacityGate` enforces the share of its environment
BEFORE the request: it queues the call until the seat's rate window has room and refuses it —
`BudgetExhausted`, nothing sent, nothing recorded — when the environment's daily budget is spent. It refuses
the call with `ShareBelowOneCall` when the share of the environment is less than one call in a window. Both
checks read the environment's own ledger, so every process of an environment shares them. The seats of one
account (`sec`, `nport`) count together. The process's own admissions are counted too, so a ledger that
lags a call cannot let the next one through.

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

#: Why a second row with the same fetch key is not a duplicate fetch (#1521). The gateway derives it from
#: the attributes of the call; no caller passes one. A row carries at most one, in this order.
PROBE = "probe"  # a forced run asks the vendor again on purpose (`probe_scope`)
WINDOW_NOT_FINAL = "window_not_final"  # the request is not final, so the next caller asks again (`keep=False`)
MODEL_CALL = "model_call"  # a model attempt: the same request can be asked again (a retry, a later replay)
ANSWER_REFUSED = "answer_refused"  # the registered rule of the key refused the answer (`accept_rules`)
# Matched case-insensitively as substrings of the lower-cased key, so `APIKEY`, `ApiKey`,
# `access_token` and `x-api-key` are all blanked (review on #741).
_REDACTED_KEY_MARKERS = ("apikey", "api_key", "api-key", "token", "secret", "password", "auth", "key")


# --- declared capacity: derived from the source registry, never written here ---------


@dataclass(frozen=True)
class SourceCapacity:
    """The gateway's view of one ledger seat's declared capacity.

    Derived: `CAPACITIES` is computed from `source_registrations.LEDGER_CAPACITIES`, which reads the vendor
    account row of the environment contract. Tests build their own to drive a gate with a synthetic limit.

    ``None`` in a dimension means nothing is declared for it: a gate does not pace an
    unwindowed seat and does not budget an unbudgeted one. `SourceGateway.call` requires a
    rate window (`enforceable`). ``account`` names the vendor account that several environments call: the
    figures are then the whole account figures and `in_environment` applies the share of one environment
    (`EnvironmentContract.account_share`). ``pool`` lists the seats of that account, which the gate counts
    together.
    """

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
            float(declaration.window_seconds),
            declaration.calls_per_window,
            declaration.daily_budget,
            declaration.documented,
            declaration.account,
            pool,
        )

    @property
    def enforceable(self) -> bool:
        """Whether `SourceGateway.call` can throttle this source."""
        return self.window_seconds is not None

    @property
    def seats(self) -> tuple[str, ...]:
        """The seats that spend this capacity together: the seats of the account, or this seat alone."""
        return self.pool or (self.source,)

    def in_environment(self, environment: str) -> SourceCapacity | None:
        """What ``environment`` may spend: the whole seat when it has no account, the share of the
        environment (`account_share`) when it has one, and None when the share is less than one call."""
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


@contextmanager
def script_run(name: str) -> Iterator[str]:
    """Attribute every call that a script makes to one run, `script:<name>:<UTC epoch seconds>` (#1521 step 4).

    A script has no Dagster run. Its `main` runs inside this block, so its rows carry a run key like an op's
    rows do. Yields the key."""
    run_key = f"script:{name}:{int(_utcnow().timestamp())}"
    with run_scope(run_key):
        yield run_key


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
    #: The 1-based attempt of one logical model call that retries (#1444); None for other sources.
    attempt: int | None = None
    #: The code in the error body of the provider, for example "1302" (#1444).
    provider_code: str | None = None
    #: The request parameters without any secret: model, thinking type, max_tokens (#1444).
    request_params: dict[str, Any] | None = None
    #: The canonical fetch key this call fetched (`FetchKey.ledger_key`), named by the `identity` of the call.
    fetch_key: str | None = None
    #: What the response says it cost in output tokens; reasoning tokens only when it reports them.
    completion_tokens: int | None = None
    reasoning_tokens: int | None = None
    #: Why a repeat of `fetch_key` is not a duplicate fetch: `PROBE`, `WINDOW_NOT_FINAL`, `MODEL_CALL` or
    #: `ANSWER_REFUSED`. Derived by the gateway; None for an ordinary call (#1521 step 3).
    repeat_reason: str | None = None

    def observe(self, *, status_code: int | None = None, body: bytes | str | None = None) -> None:
        """Record what the vendor answered. Call once the response is in hand.

        Text is digested as UTF-8; anything that is not bytes-like leaves the digest
        empty rather than turning a recorded call into a raised one — the ledger
        observes the vendor's answer, it never decides whether the caller may have it.
        An answer with a success status that the registered rule of the key refuses gets the reason
        `answer_refused`; the row keeps its key.
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
        if self.fetch_key is None or self.repeat_reason is not None:
            return
        if self.status_code is not None and self.status_code >= 400:
            return
        if not accept_rules.accepts(FetchKey.from_ledger_key(self.fetch_key), raw):
            self.repeat_reason = ANSWER_REFUSED

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


def _pg_reset() -> None:
    """Drop the cached connection, so the next `_pg()` opens a fresh one."""
    global _pg_conn
    try:
        if _pg_conn is not None:
            _pg_conn.close()
    finally:
        _pg_conn = None


def _pg_execute(sql: str, params=()):
    """Execute on the cached connection, reconnecting once on failure. psycopg
    marks a silently dropped connection closed only AFTER an operation fails on
    it, so the first statement after a DB restart/network blip raises — one
    fresh-connection retry covers that without hiding a genuinely down DB
    (the retry's exception propagates)."""
    import psycopg

    try:
        return _pg().execute(sql, params)
    except psycopg.errors.UndefinedColumn:
        raise  # a statement error: a fresh connection would meet the same schema
    except psycopg.Error:
        _pg_reset()
        return _pg().execute(sql, params)


#: The columns of every ledger row. Three groups of columns exist only after a migration, and a record
#: that sets none of them is written with `_BASE_COLUMNS` alone. So a code server that starts before the
#: migration replay never fails an ordinary audit row (rows of the other sources never depend on a migration).
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
#: A model attempt (#1444): `20261021T0900_datahub_ledger_model_attempt_columns.sql`.
_ATTEMPT_COLUMNS = ("attempt", "provider_code", "request_params")
#: The output tokens of a model attempt (#1444): `20261021T1100_datahub_ledger_model_token_columns.sql`.
_TOKEN_COLUMNS = ("completion_tokens", "reasoning_tokens")
#: A keyed fetch (#1411 K7): `20261021T1200_datahub_landed_fetch_keys.sql`.
_KEY_COLUMNS = ("fetch_key",)
#: The reason that a repeat of the key is no duplicate (#1521 step 3): `20261027T0543_datahub_ledger_repeat_reason.sql`.
_REASON_COLUMNS = ("repeat_reason",)
#: Each group is written by a record that sets one of its columns.
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
    """The values of `_BASE_COLUMNS`, in their order."""
    return tuple(_value(record, column) for column in _BASE_COLUMNS)


def _insert_ladder(record: CallRecord) -> list[tuple[str, tuple]]:
    """The INSERTs to try for ``record``, the most complete first, each with its parameters.

    The first holds the base columns and every group that the record sets. The next ones drop the
    groups from the last: the migrations of the groups apply in the order of `_OPTIONAL_GROUPS`, so a
    schema that lacks a group lacks the groups after it too. The last one is the base row."""
    groups = [group for group in _OPTIONAL_GROUPS if any(getattr(record, column) is not None for column in group)]
    ladder: list[tuple[str, tuple]] = []
    for kept in range(len(groups), -1, -1):
        columns: tuple[str, ...] = _BASE_COLUMNS + tuple(column for group in groups[:kept] for column in group)
        ladder.append((_statement(columns), tuple(_value(record, column) for column in columns)))
    return ladder


def _insert_statement(record: CallRecord) -> tuple[str, tuple]:
    """The most complete INSERT for ``record`` and its parameters."""
    return _insert_ladder(record)[0]


def postgres_writer(record: CallRecord) -> None:
    """Write one row. The data-engine and the llm-service deploy together, and only the llm-service
    applies the migrations at boot. A row that meets the schema before a migration of its groups is
    written with the groups that exist and loses the others for that window."""
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
    """Swap the sink (tests; local probes without a warehouse). Returns the previous one."""
    global _writer
    previous, _writer = _writer, writer
    return previous


def emit(record: CallRecord) -> None:
    """Write one finished record. Never raises: a ledger outage is logged with the full
    row, not turned into a vendor failure (see the module docstring)."""
    if settings.external_call_ledger == "off":
        return
    try:
        _writer(record)
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
    """

    def __init__(self, source: str, *, environment: str, budget: int, spent: int) -> None:
        super().__init__(source, f"daily budget {budget} spent ({spent} calls today) in {environment}")
        self.environment = environment
        self.budget = budget
        self.spent = spent


#: The reason of a refusal when the share of an environment is less than one call in a window (#1520).
SHARE_BELOW_ONE_CALL = "share_below_one_call"


class ShareBelowOneCall(CapacityExceeded):
    """The share of this environment in the vendor account is less than one call in a limit window.

    The contract gives the environment percent 0, or a floor of 0 (amount x percent // 100). The contract
    loads and the image boots: only the call is refused, nothing is sent and nothing is recorded. An
    environment that the percent table does not name (local, CI) meets this refusal at an account that it
    may not call: Twelve Data, OpenFIGI and moomoo. A local run of `universe_refresh` with a bound gate is refused
    at the OpenFIGI step. This is intended (working choice U1 of #1520): a local run never spends a shared key.
    """

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


# Budgets and windows count requests (rows), never `cost`: see `CapacityDeclaration.daily_budget`.
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


_DAILY_SQL = (
    "select caller, (called_at at time zone 'UTC')::date, count(*) from staging.api_call_ledger "
    "where source = %s and called_at >= %s and called_at < %s group by 1, 2"
)


def ledger_daily_calls(source: str, since: datetime, until: datetime) -> dict[tuple[str, date], int]:
    """Calls per (caller, UTC day) that the ledger holds for ``source`` in ``[since, until)``.

    The rows the gate counts, read from the same place (`ledger_calls_since`). A lane that
    budgets its own share of a seat against the other callers reads them here (#1277)."""
    if settings.external_call_ledger == "off":
        return {}
    if _writer is postgres_writer:
        rows = _pg_execute(_DAILY_SQL, (source, since, until)).fetchall()
        return {(str(caller), day): int(calls) for caller, day, calls in rows}
    reader = getattr(_writer, "daily_calls", None)
    return dict(reader(source, since, until)) if reader is not None else {}


def capacity_environment() -> str:
    """The environment whose share of a shared seat this process spends.

    The resolved tier. An unknown APP_ENV stays as written: a gate names it in the refusal and
    grants it no share. `CapacityGate` and every budget that reads a share use this one answer."""
    try:
        return settings.environment_tier.value
    except ValueError:
        return settings.app_env


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
        capacity = declared.in_environment(self._environment())
        if capacity is None:
            _refuse(ShareBelowOneCall(source, environment=self._environment(), account=str(declared.account)))
        seats = capacity.seats
        if capacity.daily_budget is not None:
            self._check_daily_budget(source, seats, capacity.daily_budget)
        if capacity.window_seconds is not None and capacity.calls_per_window is not None:
            waited = self._queue(source, seats, capacity.window_seconds, capacity.calls_per_window)
            if waited and capacity.daily_budget is not None:
                # Another process may have spent the last of the budget while this call queued.
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
        """Wait until the window has room for the seats that spend it together; returns how long it waited."""
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
#: False while `fetch_once(keep=False)` makes a request that is not final: its row carries `window_not_final`.
_window_final: contextvars.ContextVar[bool] = contextvars.ContextVar("external_call_window_final", default=True)
#: True inside a probe: a forced run that asks every vendor again and uses no index (`probe_scope`).
_probe: contextvars.ContextVar[bool] = contextvars.ContextVar("external_call_probe", default=False)


@functools.cache
def _model_seat() -> str:
    """The ledger seat of the model provider, named once by the environment contract."""
    return load_environment_contract().model_seat.seat


def _creation_reason(source: str) -> str | None:
    """The repeat reason that the state at the start of a call decides: probe, unfinished window, model."""
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
    """Wrap exactly one outbound request. The block reports the answer through
    ``record.observe(...)``; an exception inside the block is recorded as a failed
    call and re-raised unchanged.

    ``identity`` is the fetch key of the resource that the request asks for (#1521): the row names
    it. A call that names no resource does not type-check and raises `TypeError` at the call. The row
    also carries the reason that a repeat of its key is no duplicate, derived from the call itself:
    a probe, a window that is not final, a model attempt, or (in `observe`) a refused answer.

    Inside a `capacity_scope`, the bound gate admits the call BEFORE the block runs: a
    refusal raises `CapacityExceeded` here, the request is never made and no row is
    written. ``admit=False`` is for a caller that has already admitted the call through
    its own gate (`SourceGateway.call`).

    Nested use is one row, not two: when an outer ``record_call`` (a `SourceGateway.call`
    around an adapter that itself goes through `http_get`) is active, the inner block
    receives the OUTER record and enriches it — status, digest, the URI actually asked —
    instead of emitting a second row for the same request (and is not admitted twice). The outer
    block's ``identity`` names the row, so both pass the same key.
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
    """``client.get(url, **kwargs)`` through the ledger (httpx-shaped clients)."""
    with record_call(
        source,
        endpoint,
        identity=identity,
        caller=caller,
        request_uri=_requested_uri(url, kwargs.get("params")),
        cost=cost,
    ) as call:
        response = client.get(url, **kwargs)
        _observe_http(call, response)
        return response


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
    """``client.post(url, **kwargs)`` through the ledger (httpx-shaped clients)."""
    with record_call(
        source,
        endpoint,
        identity=identity,
        caller=caller,
        request_uri=_requested_uri(url, kwargs.get("params")),
        cost=cost,
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
    identity: FetchKey,
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
    with record_call(source, endpoint, identity=identity, caller=caller, request_uri=uri, cost=cost) as call:
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


# --- landed keys (#1411 K3): each key is fetched once -----------------------------------


@contextmanager
def probe_scope(active: bool = True) -> Iterator[None]:
    """Make every keyed fetch in the block a probe when ``active``.

    A forced tick (`force_fetch`, #874) exists to prove that the deployed release reaches its vendors
    (`quality.release_fetch_proof`). A probe therefore asks the vendor again and neither reads nor writes
    the index of landed keys. Its rows carry their key and the reason `probe`, so the verdict skips them."""
    token = _probe.set(active)
    try:
        yield
    finally:
        _probe.reset(token)


_landed_keys: LandedKeys = PostgresLandedKeys(_pg, _pg_reset)


def utc_day() -> str:
    """The current UTC day, ISO form: the ``as_of`` of a vendor document that has no version of its own."""
    return _utcnow().date().isoformat()


def landed(key: FetchKey) -> bytes | None:
    """The bytes that landed under ``key``, or None when the key has not landed.

    The index outlives the process: a key that landed in an earlier tick, an earlier run or an
    earlier container is served here with no external call. A lookup that fails reads as a miss
    and is logged, because dedup never decides whether a caller may fetch. With the ledger off
    (a local probe with no warehouse) nothing is landed and nothing is found."""
    if settings.external_call_ledger == "off" or _probe.get():
        return None
    try:
        return _landed_keys.lookup(key)
    except Exception:  # noqa: BLE001 - an index outage costs one call, which the ledger records
        log.exception("landed-key lookup failed for %s; the key will be fetched", key.ledger_key)
        return None


def land(
    key: FetchKey, body: bytes, *, content_type: str = "application/json", fetched_at: datetime | None = None
) -> None:
    """Record ``body`` as the landing of ``key``, on the connection that survives a rollback."""
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
    except Exception:  # noqa: BLE001 - the vendor answered; a failed landing costs a later refetch, not this call
        log.exception("landing %s failed; the next call for this key will fetch again", key.ledger_key)


def fetch_once(
    key: FetchKey,
    fetch: Callable[[FetchKey], bytes],
    *,
    content_type: str = "application/json",
    keep: bool = True,
) -> bytes:
    """The bytes of ``key``: from the index when it landed, else from ``fetch(key)`` once.

    ``fetch`` makes exactly ONE external request through the gateway (`http_get`, `urlopen`,
    `SourceGateway.call`) and names ``key`` as its identity. The ledger row of that request carries
    the key, and the nightly verdict `fetch_keys_once` counts rows per key. A ``fetch`` that raises
    lands nothing, so a failed or refused call is tried again by the next caller.

    The registered rule of the key (`accept_rules`) judges the answer. An answer it refuses (an error body
    with status 200, a window whose session has not settled) goes to the caller and stays out of the index;
    its row keeps the key and carries `answer_refused`. ``keep=False`` is a request that is not final at
    all: nothing is read from or written to the index, and its row carries `window_not_final`. Inside a
    `probe_scope` every request is like that, with the reason `probe`."""
    if _probe.get():
        return fetch(key)
    if not keep:
        token = _window_final.set(False)
        try:
            return fetch(key)
        finally:
            _window_final.reset(token)
    hit = landed(key)
    if hit is not None:
        return hit
    started = _utcnow()
    body = fetch(key)
    if accept_rules.accepts(key, body):
        land(key, body, content_type=content_type, fetched_at=started)
    return body


# --- the explicit gateway (#740): one gate, rows from the one writer ----------------------


@dataclass
class SourceGateway:
    """One path to a vendor or model for callers that must be throttled and budgeted
    explicitly (#740: the standards lane's SEC filings, the model and search seats).

    `call` admits through the gateway's own `CapacityGate` — the daily budget against the ledger
    where the account has one, the rate window paced — runs the callable, and records the
    attempt. Recording goes through `record_call`, so the row has the same shape as every
    adapter's — and when the callable itself goes through `http_get`/`urlopen`, the two
    collapse into ONE row carrying the status, the digest and the URI. The row goes through
    `emit` like every other row (autocommit, so a rollback of the caller's transaction keeps
    it); a refused call is `CapacityExceeded` and writes nothing. A seat must declare a
    rate window to be called here.
    """

    connection: Any
    caller: str
    capacities: Mapping[str, SourceCapacity] = field(default_factory=lambda: CAPACITIES)
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], datetime] = _utcnow
    environment: str | None = None
    #: The shared rate-window read: every row, this gateway's own included, is committed by the
    #: autocommit ledger connection, and the same connection reads it back.
    recent_calls: Callable[[str, datetime], tuple[int, datetime | None]] | None = ledger_window
    _gate: CapacityGate = field(init=False, repr=False)

    def __post_init__(self) -> None:
        # The budget is read on the caller's connection; every row is committed on the ledger
        # connection, so it is visible there.
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
                # A helper that returns the body itself (`_get_bytes` in the standards
                # lane): the bytes are what the ledger must dereference to.
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
