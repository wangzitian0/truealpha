"""SSOT for environment scope, sentinels, and vendor accounts (#1416)."""

from __future__ import annotations

import functools
import math
import re
from collections.abc import Mapping
from decimal import Decimal
from importlib import resources
from typing import Annotated, Any, Literal, Self, get_args

from pydantic import Field, field_validator, model_validator

from truealpha_contracts.common import AttrBox, CanonicalHash, StrictFrozenModel, box_attrs

ENVIRONMENT_CONTRACT_RESOURCE = "environment_contract.v1.json"
EnvironmentKey = Literal["preview", "staging", "production"]
ENVIRONMENT_ALIASES: Mapping[str, str] = {"prod": "production"}
UniverseStatus = Literal["scheduled", "stopped"]
UniverseRole = Literal["strategy", "fund_consolidation", "sentinel"]
DAY_SECONDS, UTILIZATION_CEILING, DEFAULT_DECODING_VERSION = 86_400, 0.7, "v1"
_ENVS: tuple[str, ...] = get_args(EnvironmentKey)
_NE = Annotated[str, Field(min_length=1)]
_DT = Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]
_EV_NEED = {
    "published": {"source", "checked_at"},
    "measured": {"method", "checked_at"},
    "none_published": {"reason"},
    "not_known": {"reason"},
    "self_imposed": {"reason", "checked_at"},
}
_EV_OPT = {"measured": {"source"}, "none_published": {"source", "checked_at"}}
_MEM_REQ = {"frozen_corpus": None, "nport": "nport_ticker", "static": "members"}
_MEM_USE = {
    "frozen_corpus": set(),
    "nport": {"nport_ticker", "mic"},
    "static": {"members", "mic"},
}
_MEAS_TXT, _MEAS_NUM = (
    ("at", "environment", "method", "source"),
    ("grpc_server_hwm_mib", "fresh_import_hwm_mib", "working_run_hwm_mib"),
)


class UnknownUniverse(LookupError):
    pass


class UnknownVendorAccount(LookupError):
    pass


def _uniq(items: Any, msg: str) -> None:
    if len(set(items)) != len(items):
        raise ValueError(f"{msg}: {items}")


def _keys(d: Mapping[str, Any], req: set[str], opt: set[str] = set(), name: str = "") -> None:
    if (req - set(d)) or (set(d) - req - opt):
        raise ValueError(f"{name}: missing {sorted(req - set(d))} extra {sorted(set(d) - req - opt)}")


def _pct(owner: str, pct: Mapping[str, Any]) -> None:
    if set(pct) != set(_ENVS):
        raise ValueError(f"{owner}: percent table names {sorted(pct)}")
    if any(not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= 100 for v in pct.values()):
        raise ValueError(f"{owner}: percents 0 to 100")
    if sum(pct.values()) > 100:
        raise ValueError(f"{owner}: sum past 100")


class EnvironmentUniverse(StrictFrozenModel):
    key: _NE
    status: UniverseStatus


class UniverseRecord(StrictFrozenModel):
    region: str = Field(pattern=r"^[a-z]{2}$")
    id_prefix: str = Field(pattern=r"^universe:[a-z0-9]+-[a-z]{2}-$")
    lane_name: _NE
    label: _NE
    membership: Any
    roles: tuple[UniverseRole, ...]
    price_series: bool
    report_sample: tuple[str, ...]
    cron: Mapping[EnvironmentKey, str] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_m(self) -> Self:
        m = self.membership
        if not isinstance(m, Mapping) or (k := m.get("kind")) not in _MEM_USE:
            raise ValueError("invalid membership")
        req, stray = (
            _MEM_REQ[k],
            sorted(n for n in ("url", "mic", "nport_ticker", "members") if m.get(n) and n not in _MEM_USE[k]),
        )
        if stray or (req and not m.get(req)):
            raise ValueError(f"a {k} membership needs {req or 'no field'} and holds {stray}")
        d = {"url": None, "mic": None, "nport_ticker": None, "members": (), **dict(m)}
        object.__setattr__(self, "membership", box_attrs(d))
        return self

    def cron_for(self, env: str) -> str | None:
        return next((c for n, c in self.cron.items() if n == env), None)

    is_sentinel = property(lambda self: "sentinel" in self.roles)
    runs_strategy = property(lambda self: "strategy" in self.roles)
    consolidates_funds = property(lambda self: "fund_consolidation" in self.roles)


class _VendorLimit(AttrBox):
    @property
    def divided(self) -> bool:
        return self.amount is not None and self.evidence.kind != "self_imposed"


class _VendorAccounts(AttrBox):
    def account_of_seat(self, seat: str) -> str:
        for n, r in self.accounts.items():
            if seat in r.seats:
                return str(n)
        raise UnknownVendorAccount(f"{seat!r}: no vendor account")

    def percent_of(self, acc: str, env: str) -> int | None:
        pcts = self.accounts[acc].get("percent") or self.default_percent
        return next((int(v) for k, v in pcts.items() if k == env), None)


class _AccountShare(AttrBox):
    def share_in_window(self, ws: int) -> int | None:
        return next(
            (x.share for x in self.limits if x.unit == "request" and x.window_seconds == ws and x.share is not None),
            None,
        )

    admits_a_call = property(
        lambda self: all(x.share >= 1 for x in self.limits if x.unit == "request" and x.share is not None)
    )


class CodeServerCapacity(StrictFrozenModel):
    description: _NE
    ceiling_mib: int = Field(ge=1)
    ceiling_source: _NE
    server_margin: float = Field(gt=0)
    concurrent_runs: int = Field(ge=1)
    server_processes: int = Field(ge=1)
    processes_per_run: int = Field(ge=1)
    measured: Any

    @model_validator(mode="after")
    def _check_budget(self) -> Self:
        m = self.measured
        _keys(m, set(_MEAS_TXT + _MEAS_NUM), name="measured")
        bad_txt = any(not isinstance(m.get(k), str) or not m[k] for k in _MEAS_TXT)
        bad_num = any(not isinstance(m.get(k), (int, float)) or isinstance(m[k], bool) or m[k] <= 0 for k in _MEAS_NUM)
        if bad_txt or bad_num:
            raise ValueError("invalid measured")
        if m["working_run_hwm_mib"] < m["fresh_import_hwm_mib"]:
            raise ValueError("below a fresh import")
        if self.concurrent_runs < 2 or self.run_mib < m["working_run_hwm_mib"]:
            raise ValueError("capacity below concurrent runs")
        object.__setattr__(self, "measured", box_attrs(dict(m)))
        return self

    usable_mib = property(lambda self: math.floor(Decimal(self.ceiling_mib) * Decimal(str(UTILIZATION_CEILING))))
    server_mib = property(
        lambda self: math.ceil(
            Decimal(str(self.measured["grpc_server_hwm_mib"])) * (1 + Decimal(str(self.server_margin)))
        )
    )
    run_mib = property(
        lambda self: max(
            (self.usable_mib - self.server_processes * self.server_mib)
            // (self.concurrent_runs * self.processes_per_run),
            0,
        )
    )
    max_concurrent_runs = property(lambda self: self.concurrent_runs)
    max_waiting_runs = property(lambda self: self.concurrent_runs - 1)


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
            raise ValueError(f"retry {retry} < 1")
        return min(self.initial_backoff_seconds * self.backoff_factor ** (retry - 1), self.max_backoff_seconds)

    @property
    def base_total_wait_seconds(self) -> int:
        return sum(self.base_backoff_seconds(r) for r in range(1, self.max_attempts))

    @model_validator(mode="after")
    def _check_wait(self) -> Self:
        if set(self.transient_provider_codes) & set(self.terminal_provider_codes):
            raise ValueError("transient and terminal")
        if self.base_total_wait_seconds < self.min_total_wait_seconds:
            raise ValueError("below min_total_wait_seconds")
        if self.deadline_seconds < self.min_total_wait_seconds:
            raise ValueError(f"deadline_seconds {self.deadline_seconds} is below min_total_wait_seconds")
        return self


class ModelSeatEnvironmentLimits(StrictFrozenModel):
    max_concurrent_calls: int = Field(ge=1)
    calls_per_minute: int = Field(ge=1)
    probe_offset_minutes: int = Field(ge=0, le=59)
    decoding_version: str = Field(default=DEFAULT_DECODING_VERSION, pattern=r"^v[0-9]+$")


class ModelSeatContract(StrictFrozenModel):
    description: _NE
    seat: _NE
    shared_credential: _NE
    measured_at: _DT
    source: _NE
    retry: ModelRetrySpec
    probe: Any
    environments: Mapping[str, ModelSeatEnvironmentLimits] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_slots(self) -> Self:
        p = self.probe
        ok_p = (
            0 <= p["hour_utc"] <= 23
            and p["slot_minutes"] >= 1
            and p["attempt_timeout_seconds"] >= 1
            and p["launch_margin_seconds"] >= 0
        )
        if set(p) != {"hour_utc", "slot_minutes", "attempt_timeout_seconds", "launch_margin_seconds"} or not ok_p:
            raise ValueError("invalid probe")
        fl = math.ceil((self.launch_margin_and_deadline_seconds + p["attempt_timeout_seconds"]) / 60)
        offs = sorted(x.probe_offset_minutes for x in self.environments.values())
        if p["slot_minutes"] < fl:
            raise ValueError(f"below the longest probe of {fl} minutes")
        if any(b - a < p["slot_minutes"] for a, b in zip(offs, offs[1:], strict=False)):
            raise ValueError("closer than slot_minutes")
        object.__setattr__(self, "probe", box_attrs(dict(p)))
        return self

    @property
    def launch_margin_and_deadline_seconds(self) -> int:
        return int(self.probe["launch_margin_seconds"]) + self.retry.deadline_seconds

    def limits_for(self, env: str) -> ModelSeatEnvironmentLimits | None:
        return self.environments.get(env)

    def decoding_version_for(self, env: str) -> str:
        return DEFAULT_DECODING_VERSION if (x := self.limits_for(env)) is None else x.decoding_version

    def probe_offset_minutes(self, env: str) -> int:
        return 0 if (x := self.limits_for(env)) is None else x.probe_offset_minutes

    def probe_cron(self, env: str) -> str:
        return f"{self.probe_offset_minutes(env)} {self.probe['hour_utc']} * * *"


def _lim_item(lim: Mapping[str, Any]) -> _VendorLimit:
    _keys(
        lim, {"id", "unit", "counts_per", "evidence"}, {"amount", "window_seconds", "applies_to"}, lim.get("id", "lim")
    )
    ev = {"checked_at": None, "method": None, "reason": None, **(lim.get("evidence") or {})}
    _keys(ev, {"kind"}, {"source", "checked_at", "method", "reason"}, "ev")
    ev["source"] = (ev["source"],) if isinstance(ev.get("source"), str) else tuple(ev.get("source") or ())
    k = str(ev.get("kind") or "")
    if k not in _EV_NEED:
        raise ValueError(f"Input should be {k}")
    need, held = _EV_NEED[k], {n for n in ("source", "checked_at", "method", "reason") if ev.get(n)}
    if (need - held) or (held - need - _EV_OPT.get(k, set())):
        raise ValueError(f"{k} evidence needs {sorted(need)}, stray {sorted(held - need)}")
    bad_dt = ev["checked_at"] is not None and not re.fullmatch(r"^\d{4}-\d{2}-\d{2}$", str(ev["checked_at"]))
    if bad_dt or any(not str(u).startswith("https://") for u in ev["source"]):
        raise ValueError(f"{k} date pattern or no https URL")
    amt, win, hf = lim.get("amount"), lim.get("window_seconds"), k in ("published", "measured", "self_imposed")
    if hf != (amt is not None):
        raise ValueError(f"{lim['id']}: {'needs an amount' if hf else 'carries no amount'}")
    bad_num = (amt is not None and (amt <= 0 or win is None)) or (win is not None and win <= 0)
    if bad_num or not re.fullmatch(r"^[a-z0-9_]+$", lim["id"]):
        raise ValueError(f"{lim['id']}: greater than 0 / needs a window")
    return _VendorLimit({"applies_to": None, "amount": amt, "window_seconds": win, **lim, "evidence": box_attrs(ev)})


def _validate_vendor_accounts(va: Mapping[str, Any]) -> _VendorAccounts:
    if not va.get("description") or not va.get("accounts"):
        raise ValueError("vendor_accounts invalid")
    _pct("default", va["default_percent"])
    seats, accounts = [], {}
    for name, row in va["accounts"].items():
        _keys(row, {"holder", "seats", "outside_consumers", "limits"}, {"percent", "percent_reason", "note"}, name)
        if row.get("holder") not in ("api_key", "host_ip", "account") or not row.get("seats") or not row.get("limits"):
            raise ValueError(f"{name}: invalid row")
        _uniq(list(row["seats"]), "duplicate seat")
        seats.extend(row["seats"])
        if (row.get("percent") is None) != (row.get("percent_reason") is None):
            raise ValueError("percent_reason go together")
        if row.get("percent") is not None:
            if row.get("outside_consumers"):
                raise ValueError("outside consumers has no percent")
            _pct("override", row["percent"])
        lims = tuple(_lim_item(lim) for lim in row["limits"])
        _uniq([x.id for x in lims], "limit id is listed twice")
        _uniq([x.window_seconds for x in lims if x.unit == "request" and x.amount is not None], "share one window")
        accounts[name] = box_attrs({"percent": None, "percent_reason": None, "note": None, **row, "limits": lims})
    _uniq(seats, "belongs to two accounts")
    return _VendorAccounts(
        {"description": va["description"], "default_percent": dict(va["default_percent"]), "accounts": accounts}
    )


def _check_env(
    env: str, rs: Mapping[str, Any], universes: Mapping[str, UniverseRecord], seat: ModelSeatContract
) -> Any:
    _keys(rs, {"environment", "trigger", "live_vendor_calls", "universes", "ticks"}, {"list_refresh"}, env)
    univs = tuple(EnvironmentUniverse.model_validate(u) for u in rs["universes"])
    lr, ticks = tuple(rs.get("list_refresh", ())), box_attrs(dict(rs["ticks"]))
    for tk, tv in ticks.items():
        _keys(tv, set(), set(), f"{env}.{tk}")
    _uniq([u.key for u in univs], f"{env}: listed twice")
    _uniq(list(lr), f"{env}: list_refresh listed twice")
    listed = {u.key for u in univs}
    if unknown := sorted((listed | set(lr) | set(ticks)) - set(universes)):
        raise ValueError(f"{env}: {unknown} have no record")
    if set(ticks) != {k for k, r in universes.items() if env in r.cron} or listed != {
        k for k in ticks if not universes[k].is_sentinel
    }:
        raise ValueError(f"{env}: differ")
    if any(universes[rk].membership.kind != "nport" for rk in lr):
        raise ValueError(f"{env}: invalid list_refresh")
    if rs["live_vendor_calls"] and seat.limits_for(env) is None:
        raise ValueError(f"{env} calls live vendors and declares no model seat limits")
    return box_attrs({**rs, "environment": env, "universes": univs, "list_refresh": lr, "ticks": ticks})


class MetricApplicabilityExclusion(StrictFrozenModel):
    metric: Literal["gppe"]
    universe: _NE
    ticker: _NE
    cik: str = Field(pattern=r"^\d{10}$")
    lei: str = Field(pattern=r"^[0-9A-Z]{20}$")
    legacy_ids: tuple[str, ...] = Field(min_length=1)
    status: Literal["not_applicable"] = "not_applicable"
    reason: _NE
    approved_by: Literal["owner"]
    approved_at: _NE
    tracking_issue: str = Field(pattern=r"^#\d+$")

    @field_validator("legacy_ids")
    @classmethod
    def _validate_legacy_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        pat = re.compile(r"^issuer:(?:cik:\d{10}|lei:[0-9A-Z]{20})$")
        if any(not pat.match(item) for item in value):
            raise ValueError("legacy_id must match issuer:cik:<10d> or issuer:lei:<20c>")
        _uniq(value, "duplicate legacy_ids")
        return value


class EnvironmentContract(CanonicalHash, StrictFrozenModel):
    schema_version: Literal[1]
    contract_version: _NE
    description: _NE
    sentinels: tuple[Any, ...]
    universes: Mapping[str, UniverseRecord] = Field(min_length=1)
    environments: Mapping[str, Any]
    vendor_accounts: Any
    run_time_bounds: Any
    code_server_capacity: CodeServerCapacity
    model_seat: ModelSeatContract
    nightly_verdicts: Any
    metric_applicability: tuple[MetricApplicabilityExclusion, ...] = ()

    @model_validator(mode="after")
    def _validate_metric_applicability(self) -> Self:
        seen: set[tuple[str, str, str]] = set()
        for e in self.metric_applicability:
            if e.universe not in self.universes:
                raise ValueError(f"metric_applicability names unknown universe {e.universe!r}")
            if (key := (e.metric, e.universe, e.ticker)) in seen:
                raise ValueError(f"duplicate metric_applicability entry {key!r}")
            seen.add(key)
        return self

    def not_applicable_identities(
        self, metric: str, universe: str = "topt"
    ) -> tuple[MetricApplicabilityExclusion, ...]:
        return tuple(e for e in self.metric_applicability if e.metric == metric and e.universe == universe)

    def not_applicable_for_universe_id(
        self, metric: str, universe_id: str | None
    ) -> tuple[MetricApplicabilityExclusion, ...]:
        if universe_id is None:
            return self.not_applicable_identities(metric, "topt")
        matches = [k for k, r in self.universes.items() if universe_id.startswith(r.id_prefix)]
        return (
            self.not_applicable_identities(metric, max(matches, key=lambda k: len(self.universes[k].id_prefix)))
            if matches
            else ()
        )

    @model_validator(mode="after")
    def _validate_contract(self) -> Self:
        sent = tuple(box_attrs(dict(m)) for m in self.sentinels)
        bad_s = any(
            not (m.get("ticker") and m.get("trait") and m.get("defect_refs"))
            or not re.fullmatch(r"^issuer:cik:\d{10}$", str(m.get("issuer_id")))
            or not re.fullmatch(r"^listing:x(?:nas|nys):[a-z0-9.]+$", str(m.get("listing_id")))
            for m in sent
        )
        if (
            bad_s
            or len(sent) != 10
            or len({m.issuer_id for m in sent}) != 9
            or not set(_ENVS) <= set(self.environments)
        ):
            raise ValueError("invalid sentinel/env")
        for f in ("id_prefix", "lane_name"):
            _uniq([getattr(r, f) for r in self.universes.values()], f"share a {f}")
        envs = {env: _check_env(env, rs, self.universes, self.model_seat) for env, rs in self.environments.items()}
        rtb, nv = box_attrs(dict(self.run_time_bounds)), box_attrs(dict(self.nightly_verdicts))
        bad_rtb = rtb["margin"] <= 1 or rtb["floor_minutes"] <= 0 or rtb["window_days"] < 1
        if bad_rtb or not nv.get("checks") or not nv.get("per_universe"):
            raise ValueError("invalid bounds/verdicts")
        rtb["margin"], rtb["floor_minutes"] = float(rtb["margin"]), float(rtb["floor_minutes"])
        va = _validate_vendor_accounts(self.vendor_accounts)
        for attr, val in (
            ("sentinels", sent),
            ("environments", envs),
            ("vendor_accounts", va),
            ("run_time_bounds", rtb),
            ("nightly_verdicts", nv),
        ):
            object.__setattr__(self, attr, val)
        if unknown := sorted(set(self.model_seat.environments) - set(envs)):
            raise ValueError(f"name unknown environments: {unknown}")
        if self.model_seat.seat not in self.vendor_seats:
            raise ValueError("belongs to no vendor account")
        return self

    def _spec(self, env: str | None) -> Any:
        return self.environments.get(self.normalize_environment(env))

    @property
    def job_runtime_bounds(self) -> Any:
        return self.run_time_bounds

    def derive_max_runtime_seconds(self, longest_seconds: float) -> int:
        scaled = Decimal(str(self.run_time_bounds["margin"])) * Decimal(str(longest_seconds)) / Decimal("60")
        return math.ceil(max(scaled, Decimal(str(self.run_time_bounds["floor_minutes"]))) * 60)

    def normalize_environment(self, env: str | None) -> str:
        return ENVIRONMENT_ALIASES.get(n := (env or "").strip().lower(), n)

    def environment_names(self, env: str) -> tuple[str, ...]:
        return (env, *(a for a, t in ENVIRONMENT_ALIASES.items() if t == env))

    def record(self, key: str) -> UniverseRecord:
        if key not in self.universes:
            raise UnknownUniverse(f"{key!r} is no universe")
        return self.universes[key]

    def universe_of_id(self, universe_id: str) -> str:
        matches = [k for k, r in self.universes.items() if universe_id.startswith(r.id_prefix)]
        if not matches:
            raise UnknownUniverse(f"{universe_id!r} starts with no universe id prefix")
        return max(matches, key=lambda k: len(self.universes[k].id_prefix))

    def universe_of_lane(self, lane_name: str) -> str:
        for k, r in self.universes.items():
            if r.lane_name == lane_name:
                return k
        raise UnknownUniverse(f"{lane_name!r} is the lane name of no universe")

    def universe_status(self, universe: str, env: str | None) -> UniverseStatus | None:
        self.record(universe)
        return (
            None if (s := self._spec(env)) is None else next((e.status for e in s.universes if e.key == universe), None)
        )

    def scheduled_universes(self, env: str | None) -> tuple[str, ...]:
        return () if (s := self._spec(env)) is None else tuple(e.key for e in s.universes if e.status == "scheduled")

    def expects_verdict(self, check: str, env: str | None) -> bool:
        p, at, ln = check.partition("@")
        if not at:
            return p in self.nightly_verdicts["checks"]
        if p not in self.nightly_verdicts["per_universe"]:
            return False
        try:
            k = self.universe_of_lane(ln)
        except UnknownUniverse:
            return False
        return True if self._spec(env) is None else self.universe_status(k, env) == "scheduled"

    def is_universe_scheduled(self, universe: str, env: str) -> bool:
        rec = self.record(universe)
        return (
            (s := self._spec(env)) is not None
            and universe in s.ticks
            and (rec.is_sentinel or self.universe_status(universe, env) == "scheduled")
        )

    def is_universe_list_refresh_scheduled(self, universe: str, env: str) -> bool:
        self.record(universe)
        return self.is_universe_scheduled(universe, env) or (
            (s := self._spec(env)) is not None and universe in s.list_refresh
        )

    def unscheduled_id_prefixes(self) -> Mapping[str, tuple[str, ...]]:
        return {
            e: tuple(r.id_prefix for k, r in self.universes.items() if not self.is_universe_scheduled(k, e))
            for e in self.environments
            if e != "preview"
        }

    def is_head_scheduled(self, universe_id: str, env: str | None) -> bool:
        p = self.unscheduled_id_prefixes().get(self.normalize_environment(env))
        return p is None or not universe_id.startswith(p)

    @property
    def vendor_seats(self) -> tuple[str, ...]:
        return tuple(s for row in self.vendor_accounts.accounts.values() for s in row.seats)

    def account_of_seat(self, seat: str) -> str:
        return str(self.vendor_accounts.account_of_seat(seat))

    def account_share(self, account: str, env: str | None) -> _AccountShare:
        if (row := self.vendor_accounts.accounts.get(account)) is None:
            raise UnknownVendorAccount(f"{account!r} is no vendor account")
        named = self.normalize_environment(env)
        p0 = self.vendor_accounts.percent_of(account, named)
        pct = (100 if row.holder == "host_ip" else 0) if p0 is None else p0
        lims = tuple(
            box_attrs(
                {k: getattr(x, k) for k in ("unit", "window_seconds", "amount", "divided")}
                | {"limit_id": x.id, "share": x.amount * pct // 100 if x.amount is not None and x.divided else x.amount}
            )
            for x in row.limits
        )
        return _AccountShare(account=account, environment=named, percent=pct, limits=lims)


@functools.cache
def load_environment_contract() -> EnvironmentContract:
    return EnvironmentContract.model_validate_json(
        resources.files("truealpha_contracts.data").joinpath(ENVIRONMENT_CONTRACT_RESOURCE).read_text(encoding="utf-8")
    )
