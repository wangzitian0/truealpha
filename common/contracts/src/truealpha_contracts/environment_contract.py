"""Single source of truth for environment scope, sentinels, known answers, and derived vendor shares (#1416).

Loads ``environment_contract.v1.json`` and ``sentinel_known_answers.v1.json`` from
``truealpha_contracts.data``. Both Python lanes and the generated TypeScript module
(``apps/app-web/src/contracts/environmentContract.generated.ts``) derive their rules
from this contract so no environment schedule or vendor share is hand-mirrored.

Each environment holds a list of universes it runs. A universe is ``scheduled`` or ``stopped``.
A stopped universe names the precondition that unlocks it. No other file says that a universe is
stopped (owner decision of 2026-10-21).

The contract also holds the measured run times of every deployed Dagster job. The data-engine
run launcher derives each run's maximum runtime from them (#1442). It holds the memory capacity
of the Dagster code server, and the data-engine run queue limit derives from it (#1467).
"""

from __future__ import annotations

import functools
import math
from collections.abc import Mapping
from decimal import Decimal
from importlib import resources
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from truealpha_contracts.common import canonical_sha256

ENVIRONMENT_CONTRACT_RESOURCE = "environment_contract.v1.json"
SENTINEL_KNOWN_ANSWERS_RESOURCE = "sentinel_known_answers.v1.json"

TickKey = Literal["topt", "qqq", "sp500", "canary"]
TickType = Literal["strategy", "fund", "index", "sentinel"]
EnvironmentKey = Literal["preview", "staging", "production"]
UniverseKey = Literal["topt", "qqq", "sp500"]
UniverseStatus = Literal["scheduled", "stopped"]


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class SentinelMember(_StrictFrozenModel):
    ticker: str = Field(min_length=1)
    issuer_id: str = Field(pattern=r"^issuer:cik:\d{10}$")
    listing_id: str = Field(pattern=r"^listing:x(?:nas|nys):[a-z0-9.]+$")
    trait: str = Field(min_length=1)
    defect_refs: tuple[str, ...] = Field(min_length=1)


class EnvironmentTickContract(_StrictFrozenModel):
    """The cron and size of one tick. A universe tick runs when its universe is ``scheduled``.

    A sentinel tick (the canary) is no universe and runs whenever it holds a cron.
    """

    cron: str = Field(min_length=1)
    tick_type: TickType
    estimated_listings: int = Field(ge=0)


class UnlockEndToEnd(_StrictFrozenModel):
    """The universe that must run end to end in ``environment`` before a stopped universe starts."""

    environment: EnvironmentKey
    universe: UniverseKey


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

    key: UniverseKey
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
    planned_daily_demand: Mapping[str, int]

    @model_validator(mode="after")
    def _universes_and_ticks_agree(self) -> Self:
        keys = [universe.key for universe in self.universes]
        if len(set(keys)) != len(keys):
            raise ValueError(f"{self.environment}: a universe is listed twice: {keys}")
        universe_ticks = {key for key, tick in self.ticks.items() if tick.tick_type != "sentinel"}
        if universe_ticks != set(keys):
            raise ValueError(
                f"{self.environment}: the universes {sorted(keys)} and the universe ticks {sorted(universe_ticks)} differ"
            )
        return self


class SharedSeatSpec(_StrictFrozenModel):
    shared_credential: str = Field(min_length=1)
    allocation_basis: Literal["planned_daily_demand"]


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
    estimate_tick: TickKey | None = None
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


class EnvironmentContract(_StrictFrozenModel):
    schema_version: Literal[1]
    contract_version: str = Field(min_length=1)
    description: str = Field(min_length=1)
    sentinels: tuple[SentinelMember, ...]
    environments: Mapping[str, EnvironmentSpec]
    shared_physical_seats: Mapping[str, SharedSeatSpec]
    run_time_bounds: RunTimeBounds
    code_server_capacity: CodeServerCapacity

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
        if env == "prod":
            return "production"
        return env

    @property
    def known_tick_keys(self) -> frozenset[str]:
        """Every tick key that some environment declares."""
        return frozenset(key for spec in self.environments.values() for key in spec.ticks)

    def universe_status(self, universe: str, environment: str | None) -> UniverseStatus | None:
        """The status of ``universe`` in ``environment``, or None when that environment does not list it."""
        spec = self.environments.get(self.normalize_environment(environment))
        if spec is None:
            return None
        key = universe.removeprefix("universe-list:")
        return next((entry.status for entry in spec.universes if entry.key == key), None)

    def scheduled_universes(self, environment: str | None) -> tuple[UniverseKey, ...]:
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
        key = universe.removeprefix("universe-list:")
        return next((entry.unlock for entry in spec.universes if entry.key == key), None)

    def expects_verdict(self, check: str, environment: str | None) -> bool:
        """Whether ``environment`` records the nightly verdict ``check``.

        A per-universe name (``check@universe``) of a universe that some environment lists is recorded
        only where the environment schedules that universe: staging runs TOPT only, so it records no
        QQQ verdict. Any other name, and any environment that the contract does not name, records it.
        """
        _, at, universe = check.partition("@")
        if not at:
            return True
        key = universe.removeprefix("universe-list:")
        if key not in {entry.key for spec in self.environments.values() for entry in spec.universes}:
            return True
        if self.normalize_environment(environment) not in self.environments:
            return True
        return self.universe_status(key, environment) == "scheduled"

    def is_universe_scheduled(self, universe: str, environment: str) -> bool:
        """Whether ``universe`` has an active schedule in ``environment`` (#1416 E1).

        A universe tick is active when the environment lists the universe as ``scheduled``. A sentinel
        tick (the canary) is active when the environment declares its cron.
        """
        spec = self.environments.get(self.normalize_environment(environment))
        if spec is None:
            return False
        key = universe.removeprefix("universe-list:")
        tick = spec.ticks.get(key)
        if tick is None:
            return False
        return tick.tick_type == "sentinel" or self.universe_status(key, environment) == "scheduled"

    def is_head_scheduled(self, universe_id: str, environment: str | None) -> bool:
        """Whether ``universe_id`` has a running schedule in ``environment`` (#1416 E1).

        Fails closed (returns True) when ``environment`` is omitted or unrecognised, or when
        ``universe_id`` does not map to a tick key that some environment declares. A tick key that
        another environment declares and this one does not is not scheduled here.
        """
        env = self.normalize_environment(environment)
        if env not in self.environments or env == "preview":
            return True
        key = universe_id.removeprefix("universe:").split("-", 1)[0]
        if key not in self.known_tick_keys:
            return True
        return self.is_universe_scheduled(key, env)

    def derived_environment_shares(self, seat: str) -> tuple[tuple[str, int], ...]:
        """Derive percentage shares ``(environment, percent)`` for a shared physical seat (#1416 E3).

        Each active live environment's planned daily demand for ``seat`` is divided by the total
        planned demand across live environments and floored to integer percentages summing to <= 100.
        """
        if seat not in self.shared_physical_seats:
            return ()
        demands: list[tuple[str, int]] = []
        for env_name in ("production", "staging"):
            spec = self.environments[env_name]
            demand = int(spec.planned_daily_demand.get(seat, 0))
            if demand > 0:
                demands.append((env_name, demand))
        total = sum(demand for _, demand in demands)
        if total <= 0:
            return ()
        shares: list[tuple[str, int]] = []
        allocated = 0
        for index, (env_name, demand) in enumerate(demands):
            if index == len(demands) - 1:
                percent = max(1, 100 - allocated)
            else:
                percent = max(1, round(demand * 100 / total))
                allocated += percent
            shares.append((env_name, percent))
        return tuple(shares)


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
