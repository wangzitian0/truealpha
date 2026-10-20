"""Single source of truth for environment scope, sentinels, known answers, and derived vendor shares (#1416).

Loads ``environment_contract.v1.json`` and ``sentinel_known_answers.v1.json`` from
``truealpha_contracts.data``. Both Python lanes and the generated TypeScript module
(``apps/app-web/src/contracts/environmentContract.generated.ts``) derive their rules
from this contract so no environment schedule or vendor share is hand-mirrored.

The contract also holds the measured run times of every deployed Dagster job. The data-engine
run launcher derives each run's maximum runtime from them (#1442).
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


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class SentinelMember(_StrictFrozenModel):
    ticker: str = Field(min_length=1)
    issuer_id: str = Field(pattern=r"^issuer:cik:\d{10}$")
    listing_id: str = Field(pattern=r"^listing:x(?:nas|nys):[a-z0-9.]+$")
    trait: str = Field(min_length=1)
    defect_refs: tuple[str, ...] = Field(min_length=1)


class EnvironmentTickContract(_StrictFrozenModel):
    scheduled: bool
    cron: str | None = None
    tick_type: TickType
    estimated_listings: int = Field(ge=0)


class EnvironmentSpec(_StrictFrozenModel):
    environment: EnvironmentKey
    trigger: str = Field(min_length=1)
    live_vendor_calls: bool
    strategy_universe: str | None = None
    target_strategy_universe: str | None = None
    ticks: Mapping[str, EnvironmentTickContract]
    planned_daily_demand: Mapping[str, int]


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

    measured: Mapping[EnvironmentKey, MeasuredRunTimes] | None = None
    estimate_tick: TickKey | None = None
    unmeasured: str | None = None

    @model_validator(mode="after")
    def _declare_exactly_one_basis(self) -> Self:
        declared = [name for name in ("measured", "estimate_tick", "unmeasured") if getattr(self, name)]
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


class EnvironmentContract(_StrictFrozenModel):
    schema_version: Literal[1]
    contract_version: str = Field(min_length=1)
    description: str = Field(min_length=1)
    sentinels: tuple[SentinelMember, ...]
    environments: Mapping[str, EnvironmentSpec]
    shared_physical_seats: Mapping[str, SharedSeatSpec]
    run_time_bounds: RunTimeBounds

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

    def is_universe_scheduled(self, universe: str, environment: str) -> bool:
        """Whether ``universe`` has an active schedule in ``environment`` (#1416 E1)."""
        env = self.normalize_environment(environment)
        spec = self.environments.get(env)
        if spec is None:
            return False
        key = universe.removeprefix("universe-list:")
        tick = spec.ticks.get(key)
        if tick is None or tick.cron is None:
            return False
        return tick.scheduled

    def is_head_scheduled(self, universe_id: str, environment: str | None) -> bool:
        """Whether ``universe_id`` has a running schedule in ``environment`` (#1416 E1).

        Fails closed (returns True) when ``environment`` is omitted or unrecognised, or when
        ``universe_id`` does not map to a declared tick key in the contract.
        """
        env = self.normalize_environment(environment)
        spec = self.environments.get(env)
        if spec is None or env == "preview":
            return True
        key = universe_id.removeprefix("universe:").split("-", 1)[0]
        tick = spec.ticks.get(key)
        if tick is None:
            return True
        return bool(tick.scheduled and tick.cron is not None)

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
