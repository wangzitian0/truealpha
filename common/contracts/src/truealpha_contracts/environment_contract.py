"""Single source of truth for environment scope, sentinels, known answers, and vendor accounts (#1416, #1520)."""

from __future__ import annotations

import functools
import math
from collections.abc import Mapping
from decimal import Decimal
from importlib import resources
from typing import Literal, Self, get_args

from pydantic import Field, field_validator, model_validator

from truealpha_contracts.common import StrictFrozenModel, canonical_sha256

ENVIRONMENT_CONTRACT_RESOURCE = "environment_contract.v1.json"
SENTINEL_KNOWN_ANSWERS_RESOURCE = "sentinel_known_answers.v1.json"

EnvironmentKey = Literal["preview", "staging", "production"]
ENVIRONMENT_ALIASES: Mapping[str, str] = {"prod": "production"}
UniverseStatus = Literal["scheduled", "stopped"]
UniverseRole = Literal["strategy", "fund_consolidation", "sentinel"]
MembershipKind = Literal["frozen_corpus", "index_api", "nport", "static"]
DAY_SECONDS = 86_400
UTILIZATION_CEILING = 0.7
DEFAULT_DECODING_VERSION = "v1"
_ENVIRONMENT_KEYS: tuple[str, ...] = get_args(EnvironmentKey)
AccountHolder = Literal["api_key", "host_ip", "account"]
CountsPer = Literal["api_key", "host_ip", "account", "endpoint", "connection", "not_stated"]
LimitUnit = Literal["request", "distinct_stock"]
EvidenceKind = Literal["published", "measured", "none_published", "not_known", "self_imposed"]
_ISO_DATE = r"^\d{4}-\d{2}-\d{2}$"


class UnknownUniverse(LookupError):
    """A key, id or lane name that no record of the environment contract holds."""


class UnknownVendorAccount(LookupError):
    """An account or seat name that no row of ``vendor_accounts`` holds."""


def _require_unique(items: tuple[object, ...] | list[object], message: str) -> None:
    if len(set(items)) != len(items):
        raise ValueError(f"{message}: {items}")


def _check_percents(owner: str, percent: Mapping[EnvironmentKey, int]) -> None:
    if set(percent) != set(_ENVIRONMENT_KEYS):
        raise ValueError(f"{owner}: the percent table names {sorted(percent)}, not {sorted(_ENVIRONMENT_KEYS)}")
    if any(not 0 <= v <= 100 for v in percent.values()):
        raise ValueError(f"{owner}: a percent is an integer from 0 to 100: {dict(percent)}")
    if sum(percent.values()) > 100:
        raise ValueError(f"{owner}: the percents sum past 100: {dict(percent)}")


class SentinelMember(StrictFrozenModel):
    ticker: str = Field(min_length=1)
    issuer_id: str = Field(pattern=r"^issuer:cik:\d{10}$")
    listing_id: str = Field(pattern=r"^listing:x(?:nas|nys):[a-z0-9.]+$")
    trait: str = Field(min_length=1)
    defect_refs: tuple[str, ...] = Field(min_length=1)


class UniverseMembership(StrictFrozenModel):
    kind: MembershipKind
    url: str | None = Field(default=None, min_length=1)
    mic: str | None = Field(default=None, min_length=1)
    nport_ticker: str | None = Field(default=None, min_length=1)
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
        stray = sorted(n for n in ("url", "mic", "nport_ticker", "members") if getattr(self, n) and n not in used)
        if stray or (required is not None and not getattr(self, required)):
            raise ValueError(
                f"a {self.kind} membership needs {required or 'no field'} and holds {stray or 'nothing else'}"
            )
        return self


class UniverseRecord(StrictFrozenModel):
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
        return next((cron for name, cron in self.cron.items() if name == environment), None)

    @property
    def is_sentinel(self) -> bool:
        return "sentinel" in self.roles

    @property
    def runs_strategy(self) -> bool:
        return "strategy" in self.roles

    @property
    def consolidates_funds(self) -> bool:
        return "fund_consolidation" in self.roles


class EnvironmentTickContract(StrictFrozenModel):
    pass


class EnvironmentUniverse(StrictFrozenModel):
    key: str = Field(min_length=1)
    status: UniverseStatus


class EnvironmentSpec(StrictFrozenModel):
    environment: EnvironmentKey
    trigger: str = Field(min_length=1)
    live_vendor_calls: bool
    universes: tuple[EnvironmentUniverse, ...]
    list_refresh: tuple[str, ...] = ()
    ticks: Mapping[str, EnvironmentTickContract]

    @model_validator(mode="after")
    def _a_universe_is_listed_once(self) -> Self:
        _require_unique([u.key for u in self.universes], f"{self.environment}: a universe is listed twice")
        _require_unique(self.list_refresh, f"{self.environment}: a list_refresh universe is listed twice")
        return self


class LimitEvidence(StrictFrozenModel):
    kind: EvidenceKind
    source: tuple[str, ...] = ()
    checked_at: str | None = Field(default=None, pattern=_ISO_DATE)
    method: str | None = Field(default=None, min_length=1)
    reason: str | None = Field(default=None, min_length=1)

    @field_validator("source", mode="before")
    @classmethod
    def _one_source_may_be_a_bare_string(cls, value: object) -> object:
        return (value,) if isinstance(value, str) else (tuple(value) if isinstance(value, list) else value)

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


class VendorLimit(StrictFrozenModel):
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
        return self.amount is not None and self.evidence.kind != "self_imposed"


class VendorAccount(StrictFrozenModel):
    holder: AccountHolder
    seats: tuple[str, ...] = Field(min_length=1)
    outside_consumers: bool
    percent: Mapping[EnvironmentKey, int] | None = None
    percent_reason: str | None = Field(default=None, min_length=1)
    note: str | None = Field(default=None, min_length=1)
    limits: tuple[VendorLimit, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _the_row_is_consistent(self) -> Self:
        _require_unique(self.seats, "a seat is listed twice")
        _require_unique([lim.id for lim in self.limits], "a limit id is listed twice")
        _require_unique(
            [lim.window_seconds for lim in self.limits if lim.unit == "request" and lim.amount is not None],
            "two request limits share one window",
        )
        if (self.percent is None) != (self.percent_reason is None):
            raise ValueError("a percent override and its percent_reason go together")
        if self.percent is not None:
            if self.outside_consumers:
                raise ValueError("an account with outside consumers has no percent")
            _check_percents("percent override", self.percent)
        return self


class VendorAccounts(StrictFrozenModel):
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
        for name, row in self.accounts.items():
            if seat in row.seats:
                return name
        raise UnknownVendorAccount(f"{seat!r} is the seat of no vendor account: {sorted(self.accounts)}")

    def percent_of(self, account: str, environment: str) -> int | None:
        row = self.accounts[account]
        table = row.percent if row.percent is not None else self.default_percent
        return next((v for k, v in table.items() if k == environment), None)


class LimitShare(StrictFrozenModel):
    limit_id: str
    unit: LimitUnit
    window_seconds: int | None
    amount: int | None
    share: int | None
    divided: bool


class AccountShare(StrictFrozenModel):
    account: str
    environment: str
    percent: int
    limits: tuple[LimitShare, ...]

    def share_in_window(self, window_seconds: int) -> int | None:
        return next(
            (
                lim.share
                for lim in self.limits
                if lim.unit == "request" and lim.window_seconds == window_seconds and lim.share is not None
            ),
            None,
        )

    @property
    def admits_a_call(self) -> bool:
        return all(lim.share >= 1 for lim in self.limits if lim.unit == "request" and lim.share is not None)


class RunTimeBounds(StrictFrozenModel):
    description: str = Field(min_length=1)
    margin: float = Field(gt=1)
    floor_minutes: float = Field(gt=0)
    window_days: int = Field(ge=1)


class MeasuredProcessMemory(StrictFrozenModel):
    at: str = Field(pattern=_ISO_DATE)
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


class CodeServerCapacity(StrictFrozenModel):
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
        return math.floor(Decimal(self.ceiling_mib) * Decimal(str(UTILIZATION_CEILING)))

    @property
    def server_mib(self) -> int:
        return math.ceil(Decimal(str(self.measured.grpc_server_hwm_mib)) * (1 + Decimal(str(self.server_margin))))

    @property
    def run_mib(self) -> int:
        free = self.usable_mib - self.server_processes * self.server_mib
        return max(free // (self.concurrent_runs * self.processes_per_run), 0)

    @property
    def max_concurrent_runs(self) -> int:
        return self.concurrent_runs

    @property
    def max_waiting_runs(self) -> int:
        return self.concurrent_runs - 1


class ModelRetrySpec(StrictFrozenModel):
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
        if retry < 1:
            raise ValueError(f"retry is 1-based, got {retry}")
        return min(self.initial_backoff_seconds * self.backoff_factor ** (retry - 1), self.max_backoff_seconds)

    @property
    def base_total_wait_seconds(self) -> int:
        return sum(self.base_backoff_seconds(r) for r in range(1, self.max_attempts))

    @model_validator(mode="after")
    def _the_policy_outlasts_one_rate_window(self) -> Self:
        overlap = set(self.transient_provider_codes) & set(self.terminal_provider_codes)
        if overlap:
            raise ValueError(f"a provider code cannot be transient and terminal: {sorted(overlap)}")
        if self.base_total_wait_seconds < self.min_total_wait_seconds:
            raise ValueError(
                f"the waits sum to {self.base_total_wait_seconds} s, below min_total_wait_seconds {self.min_total_wait_seconds}"
            )
        if self.deadline_seconds < self.min_total_wait_seconds:
            raise ValueError(
                f"deadline_seconds {self.deadline_seconds} is below min_total_wait_seconds {self.min_total_wait_seconds}"
            )
        return self


class ModelProbeSlots(StrictFrozenModel):
    hour_utc: int = Field(ge=0, le=23)
    slot_minutes: int = Field(ge=1)
    attempt_timeout_seconds: int = Field(ge=1)
    launch_margin_seconds: int = Field(ge=0)


class ModelSeatEnvironmentLimits(StrictFrozenModel):
    max_concurrent_calls: int = Field(ge=1)
    calls_per_minute: int = Field(ge=1)
    probe_offset_minutes: int = Field(ge=0, le=59)
    decoding_version: str = Field(default=DEFAULT_DECODING_VERSION, pattern=r"^v[0-9]+$")


class ModelSeatContract(StrictFrozenModel):
    description: str = Field(min_length=1)
    seat: str = Field(min_length=1)
    shared_credential: str = Field(min_length=1)
    measured_at: str = Field(pattern=_ISO_DATE)
    source: str = Field(min_length=1)
    retry: ModelRetrySpec
    probe: ModelProbeSlots
    environments: Mapping[str, ModelSeatEnvironmentLimits] = Field(min_length=1)

    @model_validator(mode="after")
    def _probe_slots_hold_the_whole_retry_deadline(self) -> Self:
        slot_floor = math.ceil((self.launch_margin_and_deadline_seconds + self.probe.attempt_timeout_seconds) / 60)
        if self.probe.slot_minutes < slot_floor:
            raise ValueError(
                f"slot_minutes {self.probe.slot_minutes} is below the longest probe of {slot_floor} minutes "
                "(launch margin, retry deadline and one attempt)"
            )
        offsets = sorted(lim.probe_offset_minutes for lim in self.environments.values())
        if any(later - earlier < self.probe.slot_minutes for earlier, later in zip(offsets, offsets[1:], strict=False)):
            raise ValueError(f"probe offsets {offsets} are closer than slot_minutes {self.probe.slot_minutes}")
        return self

    @property
    def launch_margin_and_deadline_seconds(self) -> int:
        return self.probe.launch_margin_seconds + self.retry.deadline_seconds

    def limits_for(self, environment: str) -> ModelSeatEnvironmentLimits | None:
        return self.environments.get(environment)

    def decoding_version_for(self, environment: str) -> str:
        limits = self.limits_for(environment)
        return DEFAULT_DECODING_VERSION if limits is None else limits.decoding_version

    def probe_offset_minutes(self, environment: str) -> int:
        limits = self.limits_for(environment)
        return 0 if limits is None else limits.probe_offset_minutes

    def probe_cron(self, environment: str) -> str:
        return f"{self.probe_offset_minutes(environment)} {self.probe.hour_utc} * * *"


class NightlyVerdictExpectationSpec(StrictFrozenModel):
    cadence_hours: int = Field(gt=0)
    job: str = Field(min_length=1)


class NightlyVerdictsContract(StrictFrozenModel):
    description: str = Field(min_length=1)
    checks: Mapping[str, NightlyVerdictExpectationSpec] = Field(min_length=1)
    per_universe: Mapping[str, NightlyVerdictExpectationSpec] = Field(min_length=1)


class EnvironmentContract(StrictFrozenModel):
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
    nightly_verdicts: NightlyVerdictsContract

    @model_validator(mode="after")
    def _the_records_agree_with_the_environments(self) -> Self:
        for field in ("id_prefix", "lane_name"):
            values = [getattr(r, field) for r in self.universes.values()]
            if len(set(values)) != len(values):
                raise ValueError(f"two universe records share a {field}: {sorted(values)}")
        for env_name, spec in self.environments.items():
            ticking = {k for k, r in self.universes.items() if env_name in r.cron}
            if set(spec.ticks) != ticking:
                raise ValueError(
                    f"{env_name}: the ticks {sorted(spec.ticks)} and the universe records with a cron {sorted(ticking)} differ"
                )
            listed = {e.key for e in spec.universes}
            unknown = sorted((listed | set(spec.list_refresh)) - set(self.universes))
            if unknown:
                raise ValueError(f"{env_name}: the environment names universes with no record: {unknown}")
            for rk in spec.list_refresh:
                if self.universes[rk].membership.kind not in ("index_api", "nport"):
                    raise ValueError(
                        f"{env_name}: list_refresh names {rk!r}, whose membership is not index_api or nport"
                    )
            served_ticks = {k for k in spec.ticks if not self.universes[k].is_sentinel}
            if listed != served_ticks:
                raise ValueError(
                    f"{env_name}: the universes {sorted(listed)} and the universe ticks {sorted(served_ticks)} differ"
                )
        return self

    @model_validator(mode="after")
    def _validate_sentinels_and_environments(self) -> Self:
        if len(self.sentinels) != 10:
            raise ValueError(f"sentinel set must hold 10 listings across 9 issuers, got {len(self.sentinels)}")
        if len({m.issuer_id for m in self.sentinels}) != 9:
            raise ValueError(
                f"sentinel set must span 9 distinct issuers, got {len({m.issuer_id for m in self.sentinels})}"
            )
        for req_env in ("preview", "staging", "production"):
            if req_env not in self.environments:
                raise ValueError(f"environment contract is missing {req_env!r}")
        for env_name, spec in self.environments.items():
            if spec.live_vendor_calls and self.model_seat.limits_for(env_name) is None:
                raise ValueError(f"{env_name} calls live vendors and declares no model seat limits")
        unknown = sorted(set(self.model_seat.environments) - set(self.environments))
        if unknown:
            raise ValueError(f"model seat limits name unknown environments: {unknown}")
        if self.model_seat.seat not in self.vendor_seats:
            raise ValueError(f"the model seat {self.model_seat.seat!r} belongs to no vendor account")
        return self

    @property
    def job_runtime_bounds(self) -> RunTimeBounds:
        return self.run_time_bounds

    def derive_max_runtime_seconds(self, longest_seconds: float) -> int:
        minutes = max(
            Decimal(str(self.run_time_bounds.margin)) * Decimal(str(longest_seconds)) / Decimal("60"),
            Decimal(str(self.run_time_bounds.floor_minutes)),
        )
        return math.ceil(minutes * 60)

    @property
    def content_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))

    def normalize_environment(self, environment: str | None) -> str:
        env = (environment or "").strip().lower()
        return ENVIRONMENT_ALIASES.get(env, env)

    def environment_names(self, environment: str) -> tuple[str, ...]:
        return (environment, *(alias for alias, target in ENVIRONMENT_ALIASES.items() if target == environment))

    def record(self, key: str) -> UniverseRecord:
        if key not in self.universes:
            raise UnknownUniverse(f"{key!r} is no universe of the environment contract: {sorted(self.universes)}")
        return self.universes[key]

    def universe_of_id(self, universe_id: str) -> str:
        matches = [k for k, r in self.universes.items() if universe_id.startswith(r.id_prefix)]
        if not matches:
            raise UnknownUniverse(
                f"{universe_id!r} starts with no universe id prefix: {sorted(r.id_prefix for r in self.universes.values())}"
            )
        return max(matches, key=lambda k: len(self.universes[k].id_prefix))

    def universe_of_lane(self, lane_name: str) -> str:
        for k, r in self.universes.items():
            if r.lane_name == lane_name:
                return k
        raise UnknownUniverse(
            f"{lane_name!r} is the lane name of no universe: {sorted(r.lane_name for r in self.universes.values())}"
        )

    def universe_status(self, universe: str, environment: str | None) -> UniverseStatus | None:
        self.record(universe)
        spec = self.environments.get(self.normalize_environment(environment))
        return None if spec is None else next((e.status for e in spec.universes if e.key == universe), None)

    def scheduled_universes(self, environment: str | None) -> tuple[str, ...]:
        spec = self.environments.get(self.normalize_environment(environment))
        return () if spec is None else tuple(e.key for e in spec.universes if e.status == "scheduled")

    def expects_verdict(self, check: str, environment: str | None) -> bool:
        prefix, at, lane_name = check.partition("@")
        if not at:
            return prefix in self.nightly_verdicts.checks
        if prefix not in self.nightly_verdicts.per_universe:
            return False
        try:
            key = self.universe_of_lane(lane_name)
        except UnknownUniverse:
            return False
        return (
            True
            if self.normalize_environment(environment) not in self.environments
            else self.universe_status(key, environment) == "scheduled"
        )

    def is_universe_scheduled(self, universe: str, environment: str) -> bool:
        record = self.record(universe)
        spec = self.environments.get(self.normalize_environment(environment))
        return (
            spec is not None
            and universe in spec.ticks
            and (record.is_sentinel or self.universe_status(universe, environment) == "scheduled")
        )

    def is_universe_list_refresh_scheduled(self, universe: str, environment: str) -> bool:
        self.record(universe)
        spec = self.environments.get(self.normalize_environment(environment))
        return self.is_universe_scheduled(universe, environment) or (spec is not None and universe in spec.list_refresh)

    def unscheduled_id_prefixes(self) -> Mapping[str, tuple[str, ...]]:
        return {
            env: tuple(r.id_prefix for k, r in self.universes.items() if not self.is_universe_scheduled(k, env))
            for env in self.environments
            if env != "preview"
        }

    def is_head_scheduled(self, universe_id: str, environment: str | None) -> bool:
        prefixes = self.unscheduled_id_prefixes().get(self.normalize_environment(environment))
        return prefixes is None or not universe_id.startswith(prefixes)

    @property
    def vendor_seats(self) -> tuple[str, ...]:
        return tuple(seat for row in self.vendor_accounts.accounts.values() for seat in row.seats)

    def account_of_seat(self, seat: str) -> str:
        return self.vendor_accounts.account_of_seat(seat)

    def account_share(self, account: str, environment: str | None) -> AccountShare:
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
                    limit_id=lim.id,
                    unit=lim.unit,
                    window_seconds=lim.window_seconds,
                    amount=lim.amount,
                    share=lim.amount * percent // 100 if lim.amount is not None and lim.divided else lim.amount,
                    divided=lim.divided,
                )
                for lim in row.limits
            ),
        )


class SentinelKnownAnswer(StrictFrozenModel):
    answer_id: str = Field(min_length=1)
    ticker: str = Field(min_length=1)
    issuer_id: str = Field(pattern=r"^issuer:cik:\d{10}$")
    cik: str = Field(pattern=r"^\d{10}$")
    metric: Literal["operating_branch", "gppe", "headcount", "revenue"]
    period_end: str = Field(pattern=_ISO_DATE)
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


class NegativeThemeClassificationAnswer(StrictFrozenModel):
    answer_id: str = Field(min_length=1)
    ticker: str = Field(min_length=1)
    issuer_id: str = Field(pattern=r"^issuer:cik:\d{10}$")
    cik: str = Field(pattern=r"^\d{10}$")
    theme_key: str = Field(min_length=1)
    period_end: str = Field(pattern=_ISO_DATE)
    accession: str = Field(pattern=r"^\d{10}-\d{2}-\d{6}$")
    max_theme_share: str = Field(min_length=1)
    source_citation: str = Field(min_length=1)


class SentinelKnownAnswersCatalog(StrictFrozenModel):
    schema_version: Literal[1]
    contract_version: str = Field(min_length=1)
    description: str = Field(min_length=1)
    answers: tuple[SentinelKnownAnswer, ...] = Field(min_length=9)
    negative_theme_classifications: tuple[NegativeThemeClassificationAnswer, ...] = Field(min_length=1)


def _read_data(resource_name: str) -> str:
    return resources.files("truealpha_contracts.data").joinpath(resource_name).read_text(encoding="utf-8")


@functools.cache
def load_environment_contract() -> EnvironmentContract:
    return EnvironmentContract.model_validate_json(_read_data(ENVIRONMENT_CONTRACT_RESOURCE))


@functools.cache
def load_sentinel_known_answers() -> SentinelKnownAnswersCatalog:
    return SentinelKnownAnswersCatalog.model_validate_json(_read_data(SENTINEL_KNOWN_ANSWERS_RESOURCE))
