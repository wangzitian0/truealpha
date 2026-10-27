"""Single source of truth for environment scope, sentinels, known answers, and vendor accounts (#1416, #1520).

Loads ``environment_contract.v1.json`` and ``sentinel_known_answers.v1.json`` from
``truealpha_contracts.data``. Both Python lanes and the generated TypeScript module
(``apps/app-web/src/contracts/environmentContract.generated.ts``) derive their rules
from this contract so no environment schedule or vendor share is hand-mirrored.

Every vendor account that more than one environment calls has one row in ``vendor_accounts`` (#1520).
The row holds each limit of the vendor with its evidence, and one percent per environment.
``account_share`` is the one function that turns a limit and a percent into a number of calls.
The share is ``amount * percent // 100``, an integer floor, in the window of the limit.
No other file keeps a vendor figure, a percent or a demand estimate.

Every universe has one record in ``universes`` (#1522): its key, id prefix, lane name, membership, roles,
price series flag, report sample and tick cron per environment. No other file keeps its own copy or list of
a universe key, an id prefix, a lane name, a member list or a tick time.
Each environment holds a list of universes it runs. A universe is ``scheduled`` or ``stopped``.
A stopped universe names the precondition that unlocks it. No other file says that a universe is
stopped (owner decision of 2026-10-21).

The contract also holds the measured run times of every deployed Dagster job. The data-engine
run launcher derives each run's maximum runtime from them (#1442). It holds the memory capacity
of the Dagster code server, and the data-engine run queue limit derives from it (#1467).
It holds the retry policy, the per-environment pacing and the probe slots of the model seat (#1444).
"""

from __future__ import annotations

import functools
import math
from collections.abc import Mapping
from decimal import Decimal
from importlib import resources
from typing import Literal, Self, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from truealpha_contracts.common import canonical_sha256

ENVIRONMENT_CONTRACT_RESOURCE = "environment_contract.v1.json"
SENTINEL_KNOWN_ANSWERS_RESOURCE = "sentinel_known_answers.v1.json"

EnvironmentKey = Literal["preview", "staging", "production"]
#: The other names that a process or a stored row gives an environment: alias -> environment.
ENVIRONMENT_ALIASES: Mapping[str, str] = {"prod": "production"}
UniverseStatus = Literal["scheduled", "stopped"]
#: ``strategy``: the tick seeds the strategy inputs and runs the frozen strategy. ``fund_consolidation``: the
#: tick values the fund whose holdings are the members. ``sentinel``: a deploy check, no served universe.
UniverseRole = Literal["strategy", "fund_consolidation", "sentinel"]
MembershipKind = Literal["frozen_corpus", "index_api", "nport", "static"]


class UnknownUniverse(LookupError):
    """A key, id or lane name that no record of the environment contract holds."""


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class SentinelMember(_StrictFrozenModel):
    ticker: str = Field(min_length=1)
    issuer_id: str = Field(pattern=r"^issuer:cik:\d{10}$")
    listing_id: str = Field(pattern=r"^listing:x(?:nas|nys):[a-z0-9.]+$")
    trait: str = Field(min_length=1)
    defect_refs: tuple[str, ...] = Field(min_length=1)


class UniverseMembership(_StrictFrozenModel):
    """Where the members of a universe come from. A field that the kind does not use must stay empty."""

    kind: MembershipKind
    #: ``index_api``: the endpoint of the index operator.
    url: str | None = Field(default=None, min_length=1)
    #: The one exchange that lists every member, or None for a multi-exchange universe (#1411 K6).
    mic: str | None = Field(default=None, min_length=1)
    #: The fund whose N-PORT holdings give or check the members.
    nport_ticker: str | None = Field(default=None, min_length=1)
    #: ``static``: the tickers of a hand-picked list.
    members: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _the_kind_fixes_the_fields(self) -> Self:
        used = {
            "frozen_corpus": set(),
            "index_api": {"url", "mic", "nport_ticker"},
            "nport": {"nport_ticker", "mic"},
            "static": {"members", "mic"},
        }[self.kind]
        required = {"index_api": "url", "nport": "nport_ticker", "static": "members"}.get(self.kind)
        stray = sorted(
            name for name in ("url", "mic", "nport_ticker", "members") if getattr(self, name) and name not in used
        )
        if stray or (required is not None and not getattr(self, required)):
            raise ValueError(
                f"a {self.kind} membership needs {required or 'no field'} and holds {stray or 'nothing else'}"
            )
        return self


class UniverseRecord(_StrictFrozenModel):
    """The one record of a universe (#1522). Every reader of the universe reads this record.

    The key of the record is its key in ``EnvironmentContract.universes``. ``lane_name`` is the name that
    the lanes, the run tags and the stored verdicts give the universe: it stays as stored. ``id_prefix``
    starts every governed universe id of the universe. ``cron`` holds the tick time of each environment
    that ticks the universe.
    """

    region: str = Field(pattern=r"^[a-z]{2}$")
    id_prefix: str = Field(pattern=r"^universe:[a-z0-9]+-[a-z]{2}-$")
    lane_name: str = Field(min_length=1)
    label: str = Field(min_length=1)
    membership: UniverseMembership
    roles: tuple[UniverseRole, ...]
    price_series: bool
    report_sample: tuple[str, ...]
    cron: Mapping[EnvironmentKey, str] = Field(min_length=1)

    def cron_for(self, environment: str) -> str | None:
        """The tick cron of ``environment``, or None when the record does not time it there."""
        return next((cron for name, cron in self.cron.items() if name == environment), None)

    @property
    def is_sentinel(self) -> bool:
        """A sentinel is a deploy check. It has no served head and no status in an environment list."""
        return "sentinel" in self.roles

    @property
    def runs_strategy(self) -> bool:
        return "strategy" in self.roles

    @property
    def consolidates_funds(self) -> bool:
        return "fund_consolidation" in self.roles


class EnvironmentTickContract(_StrictFrozenModel):
    """The size of one tick. The tick time is in the universe record.

    A universe tick runs when its universe is ``scheduled``. A sentinel tick (the canary) is no
    universe and runs whenever the record holds a cron for the environment.
    """

    estimated_listings: int = Field(ge=0)


class UnlockEndToEnd(_StrictFrozenModel):
    """The universe that must run end to end in ``environment`` before a stopped universe starts."""

    environment: EnvironmentKey
    universe: str = Field(min_length=1)


class UnlockVerdict(_StrictFrozenModel):
    """The nightly verdict that must be green for ``consecutive_green_nights`` nights in a row."""

    name: str = Field(pattern=r"^[a-z0-9_]+$")
    environment: EnvironmentKey
    consecutive_green_nights: int = Field(ge=1)


class UniverseUnlock(_StrictFrozenModel):
    """The machine-readable precondition of a stopped universe. Both parts must hold."""

    end_to_end: UnlockEndToEnd
    verdict: UnlockVerdict


class EnvironmentUniverse(_StrictFrozenModel):
    """One universe of an environment and its status. A stopped universe names its unlock."""

    key: str = Field(min_length=1)
    status: UniverseStatus
    unlock: UniverseUnlock | None = None

    @model_validator(mode="after")
    def _only_a_stopped_universe_has_an_unlock(self) -> Self:
        if (self.status == "stopped") != (self.unlock is not None):
            raise ValueError(
                f"{self.key}: a {self.status} universe {'needs' if self.status == 'stopped' else 'must not have'} an unlock"
            )
        return self


class EnvironmentSpec(_StrictFrozenModel):
    environment: EnvironmentKey
    trigger: str = Field(min_length=1)
    live_vendor_calls: bool
    universes: tuple[EnvironmentUniverse, ...]
    ticks: Mapping[str, EnvironmentTickContract]

    @model_validator(mode="after")
    def _a_universe_is_listed_once(self) -> Self:
        keys = [universe.key for universe in self.universes]
        if len(set(keys)) != len(keys):
            raise ValueError(f"{self.environment}: a universe is listed twice: {keys}")
        return self


#: The window of a request limit that the gate reads as a daily budget. The gate counts from 00:00 UTC.
DAY_SECONDS = 86_400
_ENVIRONMENT_KEYS: tuple[str, ...] = get_args(EnvironmentKey)
#: Who holds the credential of the account. An environment that the percent table does not name (local, CI)
#: gets the whole figure of a ``host_ip`` account (another host has another address) and no call to any other.
AccountHolder = Literal["api_key", "host_ip", "account"]
#: What the vendor counts a limit per. ``not_stated``: the vendor page does not say. The contract then reads
#: one counter for all endpoints, processes and environments, at the smallest published figure.
CountsPer = Literal["api_key", "host_ip", "account", "endpoint", "connection", "not_stated"]
LimitUnit = Literal["request", "distinct_stock"]
#: ``published``: a vendor page states the figure. ``measured``: we measured it. ``none_published``: the vendor
#: states no figure. ``not_known``: no figure is known. ``self_imposed``: our own pace, never divided.
EvidenceKind = Literal["published", "measured", "none_published", "not_known", "self_imposed"]
_ISO_DATE = r"^\d{4}-\d{2}-\d{2}$"


class UnknownVendorAccount(LookupError):
    """An account or seat name that no row of ``vendor_accounts`` holds."""


def _check_percents(owner: str, percent: Mapping[EnvironmentKey, int]) -> None:
    if set(percent) != set(_ENVIRONMENT_KEYS):
        raise ValueError(f"{owner}: the percent table names {sorted(percent)}, not {sorted(_ENVIRONMENT_KEYS)}")
    if any(not 0 <= value <= 100 for value in percent.values()):
        raise ValueError(f"{owner}: a percent is an integer from 0 to 100: {dict(percent)}")
    if sum(percent.values()) > 100:
        raise ValueError(f"{owner}: the percents sum past 100: {dict(percent)}")


class LimitEvidence(_StrictFrozenModel):
    """Where the figure of a limit comes from. The kind fixes which fields hold a value and which stay empty."""

    kind: EvidenceKind
    source: tuple[str, ...] = ()
    checked_at: str | None = Field(default=None, pattern=_ISO_DATE)
    method: str | None = Field(default=None, min_length=1)
    reason: str | None = Field(default=None, min_length=1)

    @field_validator("source", mode="before")
    @classmethod
    def _one_source_may_be_a_bare_string(cls, value: object) -> object:
        """A JSON list is a tuple here (strict mode takes no list). A bare string is one source."""
        if isinstance(value, str):
            return (value,)
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def _the_kind_fixes_the_fields(self) -> Self:
        needs = {
            "published": {"source", "checked_at"},
            "measured": {"method", "checked_at"},
            "none_published": {"reason"},
            "not_known": {"reason"},
            "self_imposed": {"reason", "checked_at"},
        }[self.kind]
        optional = {"measured": {"source"}, "none_published": {"source", "checked_at"}}.get(self.kind, set())
        held = {name for name in ("source", "checked_at", "method", "reason") if getattr(self, name)}
        if needs - held or held - needs - optional:
            raise ValueError(
                f"{self.kind} evidence needs {sorted(needs)}: missing {sorted(needs - held)}, "
                f"stray {sorted(held - needs - optional)}"
            )
        if any(not url.startswith("https://") for url in self.source):
            raise ValueError(f"{self.kind} evidence names a source that is no https URL: {self.source}")
        return self


class VendorLimit(_StrictFrozenModel):
    """One limit of a vendor account. A limit with no amount is shown and never invented."""

    id: str = Field(pattern=r"^[a-z0-9_]+$")
    unit: LimitUnit
    amount: int | None = Field(default=None, gt=0)
    window_seconds: int | None = Field(default=None, gt=0)
    counts_per: CountsPer
    applies_to: str | None = Field(default=None, min_length=1)
    evidence: LimitEvidence

    @model_validator(mode="after")
    def _the_evidence_fixes_the_amount(self) -> Self:
        has_figure = self.evidence.kind in ("published", "measured", "self_imposed")
        if has_figure != (self.amount is not None):
            raise ValueError(
                f"{self.id}: {self.evidence.kind} evidence "
                f"{'needs an amount' if has_figure else 'carries no amount'}, got {self.amount}"
            )
        if self.amount is not None and self.window_seconds is None:
            raise ValueError(f"{self.id}: a limit with an amount needs a window")
        return self

    @property
    def divided(self) -> bool:
        """Whether the environments split the amount. Our own pace (``self_imposed``) is whole for each."""
        return self.amount is not None and self.evidence.kind != "self_imposed"


class VendorAccount(_StrictFrozenModel):
    """The one row of a vendor account that several environments call (#1520).

    ``seats`` are the ledger seats that draw from the account: the gate counts the calls of all of them
    against each limit. ``percent`` replaces the default percent of the table, and ``percent_reason`` says why.
    ``outside_consumers``: other users spend the same account, so no percent describes it.
    """

    holder: AccountHolder
    seats: tuple[str, ...] = Field(min_length=1)
    outside_consumers: bool
    percent: Mapping[EnvironmentKey, int] | None = None
    percent_reason: str | None = Field(default=None, min_length=1)
    note: str | None = Field(default=None, min_length=1)
    limits: tuple[VendorLimit, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _the_row_is_consistent(self) -> Self:
        if len(set(self.seats)) != len(self.seats):
            raise ValueError(f"a seat is listed twice: {self.seats}")
        ids = [limit.id for limit in self.limits]
        if len(set(ids)) != len(ids):
            raise ValueError(f"a limit id is listed twice: {ids}")
        windows = [
            limit.window_seconds for limit in self.limits if limit.unit == "request" and limit.amount is not None
        ]
        if len(set(windows)) != len(windows):
            raise ValueError(f"two request limits share one window: {windows}")
        if (self.percent is None) != (self.percent_reason is None):
            raise ValueError("a percent override and its percent_reason go together")
        if self.percent is not None:
            if self.outside_consumers:
                raise ValueError("an account with outside consumers has no percent")
            _check_percents("percent override", self.percent)
        return self


class VendorAccounts(_StrictFrozenModel):
    """Every vendor account, and the default percent of each environment (owner decision D4, 2026-10-26)."""

    description: str = Field(min_length=1)
    default_percent: Mapping[EnvironmentKey, int]
    accounts: Mapping[str, VendorAccount] = Field(min_length=1)

    @model_validator(mode="after")
    def _the_table_is_consistent(self) -> Self:
        _check_percents("default_percent", self.default_percent)
        seats = [seat for row in self.accounts.values() for seat in row.seats]
        if len(set(seats)) != len(seats):
            raise ValueError(f"a seat belongs to two accounts: {sorted(seats)}")
        return self

    def account_of_seat(self, seat: str) -> str:
        """The name of the one account that the ledger seat ``seat`` draws from."""
        for name, row in self.accounts.items():
            if seat in row.seats:
                return name
        raise UnknownVendorAccount(f"{seat!r} is the seat of no vendor account: {sorted(self.accounts)}")

    def percent_of(self, account: str, environment: str) -> int | None:
        """The percent of ``environment`` in ``account``, or None when the table does not name the environment."""
        row = self.accounts[account]
        table = row.percent if row.percent is not None else self.default_percent
        return next((value for key, value in table.items() if key == environment), None)


class LimitShare(_StrictFrozenModel):
    """What one environment may spend of one limit. ``share`` is None when the limit has no amount."""

    limit_id: str
    unit: LimitUnit
    window_seconds: int | None
    amount: int | None
    share: int | None
    divided: bool


class AccountShare(_StrictFrozenModel):
    """What one environment may spend of one vendor account, limit by limit (`account_share`)."""

    account: str
    environment: str
    percent: int
    limits: tuple[LimitShare, ...]

    def share_in_window(self, window_seconds: int) -> int | None:
        """The calls that the environment may make in a window of ``window_seconds``, or None when no limit has one."""
        return next(
            (
                limit.share
                for limit in self.limits
                if limit.unit == "request" and limit.window_seconds == window_seconds and limit.share is not None
            ),
            None,
        )

    @property
    def admits_a_call(self) -> bool:
        """False when the share of any request limit is below one call: the environment may not call the account.

        True when no request limit has an amount: there is no share to refuse."""
        return all(limit.share >= 1 for limit in self.limits if limit.unit == "request" and limit.share is not None)


class MeasuredRunTimes(_StrictFrozenModel):
    """The successful runs of one job in one environment over the measurement window."""

    successes: int = Field(ge=1)
    longest_minutes: float = Field(gt=0)


class LiveTickFit(_StrictFrozenModel):
    """The minutes of one live tick as a linear function of its listing count (#1418)."""

    intercept_minutes: float = Field(gt=0)
    minutes_per_listing: float = Field(gt=0)
    source: str = Field(min_length=1)


class JobRunTimeBasis(_StrictFrozenModel):
    """The basis of the maximum runtime of one job. Each job declares exactly one basis.

    ``measured``: the successful runs per environment. ``estimate_tick``: the job never ran, and
    the live-tick fit at the listing count of this tick gives its run time. ``unmeasured``: the
    reason why no run time exists; the instance-wide limit of ``dagster.yaml`` bounds the job.
    """

    measured: Mapping[EnvironmentKey, MeasuredRunTimes] | None = Field(default=None, min_length=1)
    estimate_tick: str | None = Field(default=None, min_length=1)
    unmeasured: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _declare_exactly_one_basis(self) -> Self:
        declared = [name for name in ("measured", "estimate_tick", "unmeasured") if getattr(self, name) is not None]
        if len(declared) != 1:
            raise ValueError(f"a job declares exactly one run-time basis, got {declared or 'none'}")
        return self


class RunTimeBounds(_StrictFrozenModel):
    """The measured run times of every deployed Dagster job and the rule that bounds them (#1442)."""

    description: str = Field(min_length=1)
    margin: float = Field(gt=1)
    floor_minutes: float = Field(gt=0)
    measured_at: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    window_days: int = Field(ge=1)
    source: str = Field(min_length=1)
    live_tick_fit: LiveTickFit
    jobs: Mapping[str, JobRunTimeBasis]


#: The owner rule of #1418: planned demand stays at or below 70% of capacity.
UTILIZATION_CEILING = 0.7


class MeasuredProcessMemory(_StrictFrozenModel):
    """What the code server container held, read once (#1467).

    ``grpc_server_hwm_mib`` is the ``VmHWM`` of the first process. ``fresh_import_hwm_mib`` is the
    ``VmHWM`` of a fresh interpreter that imports ``data_engine.dagster_defs``: no run is smaller.
    ``working_run_hwm_mib`` is the largest ``VmHWM`` of any process except the code server that the
    sensor sampled: the size of a working run, read directly. The ``memory.peak`` of the container
    is no field here: it counts page cache and splits into no size per run."""

    at: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    environment: EnvironmentKey
    method: str = Field(min_length=1)
    grpc_server_hwm_mib: float = Field(gt=0)
    fresh_import_hwm_mib: float = Field(gt=0)
    working_run_hwm_mib: float = Field(gt=0)
    source: str = Field(min_length=1)

    @model_validator(mode="after")
    def _a_working_run_is_not_smaller_than_a_fresh_import(self) -> Self:
        if self.working_run_hwm_mib < self.fresh_import_hwm_mib:
            raise ValueError(
                f"a working run, {self.working_run_hwm_mib} MiB, is below a fresh import,"
                f" {self.fresh_import_hwm_mib} MiB: one of the two readings is wrong"
            )
        return self


class CodeServerCapacity(_StrictFrozenModel):
    """The memory of the Dagster code server container and the run limit that follows from it (#1467).

    The gRPC server and every run process share one cgroup. The processes may hold the share
    ``UTILIZATION_CEILING`` of the ceiling (``usable_mib``). The server holds ``server_mib``: its
    measured ``VmHWM`` plus ``server_margin``. The queue admits ``concurrent_runs`` runs; at least 2,
    because a run that waits for other runs holds a slot while it waits. Each run executes in
    ``processes_per_run`` processes (1 when its job uses the in-process executor). The memory left
    inside ``usable_mib``, divided over the slots, is the budget of one run (``run_mib``). The budget
    is a decision, not a measurement. It must cover the measured working run, and the nightly verdict
    compares every run process with it.
    """

    description: str = Field(min_length=1)
    ceiling_mib: int = Field(ge=1)
    ceiling_source: str = Field(min_length=1)
    server_margin: float = Field(gt=0)
    concurrent_runs: int = Field(ge=1)
    server_processes: int = Field(ge=1)
    processes_per_run: int = Field(ge=1)
    measured: MeasuredProcessMemory

    @model_validator(mode="after")
    def _budget_covers_the_measurements(self) -> Self:
        if self.concurrent_runs < 2:
            raise ValueError("the queue needs 2 slots: a waiting run and the run it waits for")
        if self.run_mib < self.measured.working_run_hwm_mib:
            raise ValueError(
                f"the budget of one run, {self.run_mib} MiB, is below the measured working run,"
                f" {self.measured.working_run_hwm_mib} MiB: the slots do not fit inside the usable share of the ceiling"
            )
        return self

    @property
    def usable_mib(self) -> int:
        """The memory that the processes may hold: ``UTILIZATION_CEILING`` of the ceiling, rounded down."""
        return math.floor(Decimal(self.ceiling_mib) * Decimal(str(UTILIZATION_CEILING)))

    @property
    def server_mib(self) -> int:
        """The memory of the gRPC server: its measured ``VmHWM`` plus ``server_margin``, rounded up."""
        grown = Decimal(str(self.measured.grpc_server_hwm_mib)) * (1 + Decimal(str(self.server_margin)))
        return math.ceil(grown)

    @property
    def run_mib(self) -> int:
        """The budget of one run process: what is left beside the server, divided over the slots."""
        free = self.usable_mib - self.server_processes * self.server_mib
        return max(free // (self.concurrent_runs * self.processes_per_run), 0)

    @property
    def max_concurrent_runs(self) -> int:
        """The queue limit of ``dagster.yaml``."""
        return self.concurrent_runs

    @property
    def max_waiting_runs(self) -> int:
        """The most runs that may wait for other runs at one time. One slot stays free for the run waited for."""
        return self.concurrent_runs - 1


class ModelRetrySpec(_StrictFrozenModel):
    """The one retry policy of every model call (#1444).

    A transient answer is HTTP 429 without a terminal provider code, or any answer with a transient
    provider code. The wait before retry ``n`` (1-based) is ``min(initial x factor^(n-1), max)``
    seconds. A random extra of at most ``jitter_percent`` is added to it and never taken away, so the
    waits of a full run always sum to at least ``min_total_wait_seconds``.
    """

    transient_http_statuses: tuple[int, ...] = Field(min_length=1)
    transient_provider_codes: tuple[str, ...]
    terminal_provider_codes: tuple[str, ...]
    max_attempts: int = Field(ge=2)
    initial_backoff_seconds: int = Field(ge=1)
    backoff_factor: int = Field(ge=1)
    max_backoff_seconds: int = Field(ge=1)
    jitter_percent: int = Field(ge=0, le=100)
    min_total_wait_seconds: int = Field(ge=1)
    deadline_seconds: int = Field(ge=1)

    def base_backoff_seconds(self, retry: int) -> int:
        """The wait before retry ``retry`` (1-based) without the random extra."""
        if retry < 1:
            raise ValueError(f"retry is 1-based, got {retry}")
        return min(self.initial_backoff_seconds * self.backoff_factor ** (retry - 1), self.max_backoff_seconds)

    @property
    def base_total_wait_seconds(self) -> int:
        """The waits of a call that uses every attempt, without the random extra."""
        return sum(self.base_backoff_seconds(retry) for retry in range(1, self.max_attempts))

    @model_validator(mode="after")
    def _the_policy_outlasts_one_rate_window(self) -> Self:
        overlap = set(self.transient_provider_codes) & set(self.terminal_provider_codes)
        if overlap:
            raise ValueError(f"a provider code cannot be transient and terminal: {sorted(overlap)}")
        if self.base_total_wait_seconds < self.min_total_wait_seconds:
            raise ValueError(
                f"the waits sum to {self.base_total_wait_seconds} s, below min_total_wait_seconds "
                f"{self.min_total_wait_seconds}"
            )
        if self.deadline_seconds < self.min_total_wait_seconds:
            raise ValueError(
                f"deadline_seconds {self.deadline_seconds} is below min_total_wait_seconds {self.min_total_wait_seconds}"
            )
        return self


class ModelProbeSlots(_StrictFrozenModel):
    """The UTC hour of the nightly key probe and the width of one environment's slot (#1444)."""

    hour_utc: int = Field(ge=0, le=23)
    slot_minutes: int = Field(ge=1)
    attempt_timeout_seconds: int = Field(ge=1)
    launch_margin_seconds: int = Field(ge=0)


#: The decoding version of the model request that an environment uses unless the contract names
#: another one (#1444). The versions themselves are defined in `data_engine.sources.model_decoding`.
DEFAULT_DECODING_VERSION = "v1"


class ModelSeatEnvironmentLimits(_StrictFrozenModel):
    """What one environment may spend of the shared model account (#1444)."""

    max_concurrent_calls: int = Field(ge=1)
    calls_per_minute: int = Field(ge=1)
    probe_offset_minutes: int = Field(ge=0, le=59)
    #: The decoding version of the model request in this environment.
    decoding_version: str = Field(default=DEFAULT_DECODING_VERSION, pattern=r"^v[0-9]+$")


class ModelSeatContract(_StrictFrozenModel):
    """Retry, pacing and probe slots of the model seat. All environments share one account (#1444)."""

    description: str = Field(min_length=1)
    seat: str = Field(min_length=1)
    shared_credential: str = Field(min_length=1)
    measured_at: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    source: str = Field(min_length=1)
    retry: ModelRetrySpec
    probe: ModelProbeSlots
    environments: Mapping[str, ModelSeatEnvironmentLimits] = Field(min_length=1)

    @model_validator(mode="after")
    def _probe_slots_hold_the_whole_retry_deadline(self) -> Self:
        longest_probe_seconds = self.launch_margin_and_deadline_seconds + self.probe.attempt_timeout_seconds
        slot_floor = math.ceil(longest_probe_seconds / 60)
        if self.probe.slot_minutes < slot_floor:
            raise ValueError(
                f"slot_minutes {self.probe.slot_minutes} is below the longest probe of {slot_floor} minutes "
                "(launch margin, retry deadline and one attempt)"
            )
        offsets = sorted(limits.probe_offset_minutes for limits in self.environments.values())
        for earlier, later in zip(offsets, offsets[1:], strict=False):
            if later - earlier < self.probe.slot_minutes:
                raise ValueError(f"probe offsets {offsets} are closer than slot_minutes {self.probe.slot_minutes}")
        return self

    @property
    def launch_margin_and_deadline_seconds(self) -> int:
        return self.probe.launch_margin_seconds + self.retry.deadline_seconds

    def limits_for(self, environment: str) -> ModelSeatEnvironmentLimits | None:
        """The limits of ``environment``, or None when the environment is not listed (dev, ci)."""
        return self.environments.get(environment)

    def decoding_version_for(self, environment: str) -> str:
        """The decoding version of ``environment``; an environment that is not listed uses the default."""
        limits = self.limits_for(environment)
        return DEFAULT_DECODING_VERSION if limits is None else limits.decoding_version

    def probe_offset_minutes(self, environment: str) -> int:
        limits = self.limits_for(environment)
        return 0 if limits is None else limits.probe_offset_minutes

    def probe_cron(self, environment: str) -> str:
        """The daily cron of the key probe of ``environment``, derived from the slot table."""
        return f"{self.probe_offset_minutes(environment)} {self.probe.hour_utc} * * *"


class EnvironmentContract(_StrictFrozenModel):
    schema_version: Literal[1]
    contract_version: str = Field(min_length=1)
    description: str = Field(min_length=1)
    sentinels: tuple[SentinelMember, ...]
    universes: Mapping[str, UniverseRecord] = Field(min_length=1)
    environments: Mapping[str, EnvironmentSpec]
    vendor_accounts: VendorAccounts
    run_time_bounds: RunTimeBounds
    code_server_capacity: CodeServerCapacity
    model_seat: ModelSeatContract

    @model_validator(mode="after")
    def _the_records_agree_with_the_environments(self) -> Self:
        for field in ("id_prefix", "lane_name"):
            values = [getattr(record, field) for record in self.universes.values()]
            if len(set(values)) != len(values):
                raise ValueError(f"two universe records share a {field}: {sorted(values)}")
        for env_name, spec in self.environments.items():
            ticking = {key for key, record in self.universes.items() if env_name in record.cron}
            if set(spec.ticks) != ticking:
                raise ValueError(
                    f"{env_name}: the ticks {sorted(spec.ticks)} and the universe records with a cron {sorted(ticking)} differ"
                )
            listed = {entry.key for entry in spec.universes}
            named = listed | {entry.unlock.end_to_end.universe for entry in spec.universes if entry.unlock is not None}
            unknown = sorted(named - set(self.universes))
            if unknown:
                raise ValueError(f"{env_name}: the environment names universes with no record: {unknown}")
            served_ticks = {key for key in spec.ticks if not self.universes[key].is_sentinel}
            if listed != served_ticks:
                raise ValueError(
                    f"{env_name}: the universes {sorted(listed)} and the universe ticks {sorted(served_ticks)} differ"
                )
        return self

    @model_validator(mode="after")
    def _validate_sentinels_and_environments(self) -> Self:
        if len(self.sentinels) != 10:
            raise ValueError(f"sentinel set must hold 10 listings across 9 issuers, got {len(self.sentinels)}")
        distinct_issuers = {member.issuer_id for member in self.sentinels}
        if len(distinct_issuers) != 9:
            raise ValueError(f"sentinel set must span 9 distinct issuers, got {len(distinct_issuers)}")
        for required_env in ("preview", "staging", "production"):
            if required_env not in self.environments:
                raise ValueError(f"environment contract is missing {required_env!r}")
        for job_name, basis in self.run_time_bounds.jobs.items():
            if basis.estimate_tick is not None and not self._tick_listings(basis.estimate_tick):
                raise ValueError(f"{job_name}: no environment declares the tick {basis.estimate_tick!r}")
        for env_name, spec in self.environments.items():
            if spec.live_vendor_calls and self.model_seat.limits_for(env_name) is None:
                raise ValueError(f"{env_name} calls live vendors and declares no model seat limits")
        unknown = sorted(set(self.model_seat.environments) - set(self.environments))
        if unknown:
            raise ValueError(f"model seat limits name unknown environments: {unknown}")
        if self.model_seat.seat not in self.vendor_seats:
            raise ValueError(f"the model seat {self.model_seat.seat!r} belongs to no vendor account")
        for spec in self.environments.values():
            for universe in spec.universes:
                if universe.unlock is None:
                    continue
                needed = universe.unlock.end_to_end
                if self.universe_status(needed.universe, needed.environment) != "scheduled":
                    raise ValueError(
                        f"{spec.environment}/{universe.key}: the unlock needs {needed.universe} end to end in"
                        f" {needed.environment}, and that universe is not scheduled there"
                    )
        return self

    def _tick_listings(self, tick: str) -> list[int]:
        return [spec.ticks[tick].estimated_listings for spec in self.environments.values() if tick in spec.ticks]

    def max_runtime_seconds(self, job_name: str) -> int | None:
        """Return the maximum runtime of ``job_name`` in seconds (#1442).

        The bound is ``margin`` times the longest successful run over all measured environments.
        It is never below ``floor_minutes``. A job that never ran takes the live-tick fit at the
        largest declared listing count of its tick. The return value is None for a job without
        an entry or with an ``unmeasured`` basis. The instance-wide limit then bounds the job.
        """
        bounds = self.run_time_bounds
        basis = bounds.jobs.get(job_name)
        if basis is None or basis.unmeasured is not None:
            return None
        if basis.measured is not None:
            base = max(Decimal(str(times.longest_minutes)) for times in basis.measured.values())
        else:
            fit = bounds.live_tick_fit
            listings = max(self._tick_listings(str(basis.estimate_tick)))
            base = Decimal(str(fit.intercept_minutes)) + Decimal(str(fit.minutes_per_listing)) * listings
        minutes = max(Decimal(str(bounds.margin)) * base, Decimal(str(bounds.floor_minutes)))
        return math.ceil(minutes * 60)

    @property
    def content_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))

    def normalize_environment(self, environment: str | None) -> str:
        env = (environment or "").strip().lower()
        return ENVIRONMENT_ALIASES.get(env, env)

    def environment_names(self, environment: str) -> tuple[str, ...]:
        """Every name that ``environment`` goes by: its own and its aliases."""
        return (environment, *(alias for alias, target in ENVIRONMENT_ALIASES.items() if target == environment))

    def record(self, key: str) -> UniverseRecord:
        """The record of the universe ``key``. A key outside the record raises (#1522)."""
        try:
            return self.universes[key]
        except KeyError:
            raise UnknownUniverse(
                f"{key!r} is no universe of the environment contract: {sorted(self.universes)}"
            ) from None

    def universe_of_id(self, universe_id: str) -> str:
        """The key of the universe whose ``id_prefix`` is the longest prefix of ``universe_id``. The id is not split."""
        matches = [key for key, record in self.universes.items() if universe_id.startswith(record.id_prefix)]
        if not matches:
            raise UnknownUniverse(
                f"{universe_id!r} starts with no universe id prefix: {sorted(r.id_prefix for r in self.universes.values())}"
            )
        return max(matches, key=lambda key: len(self.universes[key].id_prefix))

    def universe_of_lane(self, lane_name: str) -> str:
        """The key of the universe that the lanes call ``lane_name``. A name outside the records raises."""
        for key, record in self.universes.items():
            if record.lane_name == lane_name:
                return key
        raise UnknownUniverse(
            f"{lane_name!r} is the lane name of no universe: {sorted(r.lane_name for r in self.universes.values())}"
        )

    def universe_status(self, universe: str, environment: str | None) -> UniverseStatus | None:
        """The status of the universe ``universe`` (a key) in ``environment``, or None when it is not listed there."""
        self.record(universe)
        spec = self.environments.get(self.normalize_environment(environment))
        if spec is None:
            return None
        return next((entry.status for entry in spec.universes if entry.key == universe), None)

    def scheduled_universes(self, environment: str | None) -> tuple[str, ...]:
        """The keys of the universes that ``environment`` runs on a schedule, in contract order."""
        spec = self.environments.get(self.normalize_environment(environment))
        if spec is None:
            return ()
        return tuple(entry.key for entry in spec.universes if entry.status == "scheduled")

    @property
    def stopped_universe_keys(self) -> frozenset[str]:
        """The universes that some environment lists as stopped: they wait for their unlock."""
        return frozenset(
            entry.key for spec in self.environments.values() for entry in spec.universes if entry.status == "stopped"
        )

    def unlock_of(self, universe: str, environment: str | None) -> UniverseUnlock | None:
        """The precondition of a stopped ``universe`` in ``environment``, or None when it is not stopped there."""
        spec = self.environments.get(self.normalize_environment(environment))
        if spec is None:
            return None
        self.record(universe)
        return next((entry.unlock for entry in spec.universes if entry.key == universe), None)

    def expects_verdict(self, check: str, environment: str | None) -> bool:
        """Whether ``environment`` records the nightly verdict ``check``.

        A per-universe name (``check@lane_name``) is recorded only where the environment schedules that
        universe: staging runs TOPT only, so it records no QQQ verdict. No environment records a name whose
        lane is in no record: the stored row of a retired universe stays in the append-only table and is
        no check. Any other name, and any environment that the contract does not name, records it.
        """
        _, at, lane_name = check.partition("@")
        if not at:
            return True
        try:
            key = self.universe_of_lane(lane_name)
        except UnknownUniverse:
            return False
        if self.normalize_environment(environment) not in self.environments:
            return True
        return self.universe_status(key, environment) == "scheduled"

    def is_universe_scheduled(self, universe: str, environment: str) -> bool:
        """Whether the universe ``universe`` (a key) has an active schedule in ``environment`` (#1416 E1).

        A universe tick is active when the environment lists the universe as ``scheduled``. A sentinel
        tick (the canary) is active when the record holds its cron for the environment.
        """
        record = self.record(universe)
        spec = self.environments.get(self.normalize_environment(environment))
        if spec is None or universe not in spec.ticks:
            return False
        return record.is_sentinel or self.universe_status(universe, environment) == "scheduled"

    def unscheduled_id_prefixes(self) -> Mapping[str, tuple[str, ...]]:
        """For each environment that gates its heads: the id prefixes of the universes it does not tick (#1522).

        ``preview`` gates nothing and is absent. The Python rule ``is_head_scheduled`` and the SQL predicate of
        the output invariants (`truealpha_tools.output_invariants.head_is_scheduled_sql`) both read this one
        mapping, so a status that changes in the records changes both.
        """
        return {
            env: tuple(
                record.id_prefix for key, record in self.universes.items() if not self.is_universe_scheduled(key, env)
            )
            for env in self.environments
            if env != "preview"
        }

    def is_head_scheduled(self, universe_id: str, environment: str | None) -> bool:
        """Whether ``universe_id`` has a running schedule in ``environment`` (#1416 E1).

        Fails closed (returns True) when ``environment`` is omitted or unrecognised, and for an id that starts
        with no id prefix of the records. A head is not scheduled where its universe's prefix is in
        ``unscheduled_id_prefixes()`` for the environment.
        """
        prefixes = self.unscheduled_id_prefixes().get(self.normalize_environment(environment))
        return prefixes is None or not universe_id.startswith(prefixes)

    @property
    def vendor_seats(self) -> tuple[str, ...]:
        """Every ledger seat that draws from a vendor account, in contract order."""
        return tuple(seat for row in self.vendor_accounts.accounts.values() for seat in row.seats)

    def account_of_seat(self, seat: str) -> str:
        """The name of the one vendor account that the ledger seat ``seat`` draws from (#1520)."""
        return self.vendor_accounts.account_of_seat(seat)

    def account_share(self, account: str, environment: str | None) -> AccountShare:
        """What ``environment`` may spend of each limit of ``account`` (#1520).

        The share of a limit is ``amount * percent // 100``, an integer floor, in the window of the limit.
        The shares of the environments sum to at most the amount. The function never rounds up and never
        stretches a window. A share of 0 (percent 0, or a floor of 0) is no error: ``admits_a_call`` is False.
        A limit with the evidence kind ``self_imposed`` is whole for every environment. A limit with no amount
        has no share. An environment that the percent table does not name (local, CI) gets 100 percent of a
        ``host_ip`` account and 0 percent of any other account (Twelve Data, OpenFIGI and moomoo today: a local run
        draws no call from a key or an account that the environments share). An account with no limit that has an amount
        has nothing to refuse, so ``admits_a_call`` stays True for it in every environment: the model plan.
        The pace of the model seat is in ``model_seat.environments``, and it gives an unlisted environment no limit.
        """
        row = self.vendor_accounts.accounts.get(account)
        if row is None:
            raise UnknownVendorAccount(f"{account!r} is no vendor account: {sorted(self.vendor_accounts.accounts)}")
        named = self.normalize_environment(environment)
        percent = self.vendor_accounts.percent_of(account, named)
        if percent is None:
            percent = 100 if row.holder == "host_ip" else 0
        return AccountShare(
            account=account,
            environment=named,
            percent=percent,
            limits=tuple(
                LimitShare(
                    limit_id=limit.id,
                    unit=limit.unit,
                    window_seconds=limit.window_seconds,
                    amount=limit.amount,
                    share=limit.amount * percent // 100 if limit.amount is not None and limit.divided else limit.amount,
                    divided=limit.divided,
                )
                for limit in row.limits
            ),
        )


class SentinelKnownAnswer(_StrictFrozenModel):
    answer_id: str = Field(min_length=1)
    ticker: str = Field(min_length=1)
    issuer_id: str = Field(pattern=r"^issuer:cik:\d{10}$")
    cik: str = Field(pattern=r"^\d{10}$")
    metric: Literal["operating_branch", "gppe", "headcount", "revenue"]
    period_end: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    accession: str = Field(pattern=r"^\d{10}-\d{2}-\d{6}$")
    kind: Literal["exact", "band", "numeric"]
    expected_value: str | None = None
    lower_bound: str | None = None
    upper_bound: str | None = None
    relative_tolerance: str | None = None
    source_citation: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_bounds(self) -> Self:
        if self.kind == "exact" and not self.expected_value:
            raise ValueError(f"{self.answer_id}: exact answer requires expected_value")
        if self.kind == "band":
            if self.lower_bound is None or self.upper_bound is None:
                raise ValueError(f"{self.answer_id}: band answer requires lower_bound and upper_bound")
            if Decimal(self.lower_bound) > Decimal(self.upper_bound):
                raise ValueError(f"{self.answer_id}: lower_bound > upper_bound")
        if self.kind == "numeric":
            if self.expected_value is None or self.relative_tolerance is None:
                raise ValueError(f"{self.answer_id}: numeric answer requires expected_value and relative_tolerance")
            if Decimal(self.relative_tolerance) < Decimal(0):
                raise ValueError(f"{self.answer_id}: negative relative_tolerance")
        return self


class NegativeThemeClassificationAnswer(_StrictFrozenModel):
    answer_id: str = Field(min_length=1)
    ticker: str = Field(min_length=1)
    issuer_id: str = Field(pattern=r"^issuer:cik:\d{10}$")
    cik: str = Field(pattern=r"^\d{10}$")
    theme_key: str = Field(min_length=1)
    period_end: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    accession: str = Field(pattern=r"^\d{10}-\d{2}-\d{6}$")
    max_theme_share: str = Field(min_length=1)
    source_citation: str = Field(min_length=1)


class SentinelKnownAnswersCatalog(_StrictFrozenModel):
    schema_version: Literal[1]
    contract_version: str = Field(min_length=1)
    description: str = Field(min_length=1)
    answers: tuple[SentinelKnownAnswer, ...] = Field(min_length=9)
    negative_theme_classifications: tuple[NegativeThemeClassificationAnswer, ...] = Field(min_length=1)


@functools.cache
def load_environment_contract() -> EnvironmentContract:
    """Load and validate the packaged ``environment_contract.v1.json`` SSOT."""
    raw = (
        resources.files("truealpha_contracts.data").joinpath(ENVIRONMENT_CONTRACT_RESOURCE).read_text(encoding="utf-8")
    )
    return EnvironmentContract.model_validate_json(raw)


@functools.cache
def load_sentinel_known_answers() -> SentinelKnownAnswersCatalog:
    """Load and validate the packaged ``sentinel_known_answers.v1.json`` SSOT."""
    raw = (
        resources.files("truealpha_contracts.data")
        .joinpath(SENTINEL_KNOWN_ANSWERS_RESOURCE)
        .read_text(encoding="utf-8")
    )
    return SentinelKnownAnswersCatalog.model_validate_json(raw)
