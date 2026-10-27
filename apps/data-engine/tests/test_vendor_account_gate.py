"""The gate spends the share of its environment in one vendor account (#1520, step 1).

The oracle of every figure is the raw `environment_contract.v1.json` and exact arithmetic. The tests drive
the deployed path: `capacity_scope` binds a `CapacityGate` over the deployed `CAPACITIES`, and `record_call`
admits each call through it before the request is made.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from importlib import resources
from typing import Any

import pytest
from data_engine.datahub.fetch_key import FetchKey
from data_engine.datahub.production_topt import source_registrations as registry
from data_engine.datahub.production_topt.corroboration_audit import corroboration_tally
from data_engine.sources import gateway
from truealpha_contracts.environment_contract import (
    DAY_SECONDS,
    ENVIRONMENT_CONTRACT_RESOURCE,
    load_environment_contract,
)

ENVIRONMENTS = ("preview", "staging", "production")
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)


def _raw() -> dict[str, Any]:
    text = resources.files("truealpha_contracts.data").joinpath(ENVIRONMENT_CONTRACT_RESOURCE).read_text("utf-8")
    return json.loads(text)["vendor_accounts"]


def _percent(table: dict[str, Any], account: str, environment: str) -> int:
    return (table["accounts"][account].get("percent") or table["default_percent"])[environment]


def _share(table: dict[str, Any], account: str, environment: str, limit: dict[str, Any]) -> int:
    """The oracle: exact fraction arithmetic, a floor, and no division of our own pace."""
    if limit["evidence"]["kind"] == "self_imposed":
        return limit["amount"]
    return math.floor(Fraction(limit["amount"] * _percent(table, account, environment), 100))


def _rate_cases() -> list[Any]:
    """Every (account, environment, seats, window, share) with a rate window, a figure and a share of 1 or more."""
    table = _raw()
    cases = []
    for account, row in table["accounts"].items():
        for limit in row["limits"]:
            if limit["unit"] != "request" or limit.get("amount") is None or limit["window_seconds"] == DAY_SECONDS:
                continue
            for environment in ENVIRONMENTS:
                share = _share(table, account, environment, limit)
                if share >= 1:
                    case = (account, environment, tuple(row["seats"]), limit["window_seconds"], share)
                    cases.append(pytest.param(*case, id=f"{account}-{limit['id']}-{environment}"))
    return cases


class _Clock:
    """A monotonic clock that only `sleep` moves, and the sleeps it saw."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _call(seat: str) -> None:
    identity = FetchKey(seat, "probe", "symbol:TEST", "2026-10-27")
    with gateway.record_call(seat, "probe", identity=identity, caller="test") as call:
        call.observe(status_code=200, body=b"{}")


def _gate(environment: str, clock: _Clock | None = None, **overrides: Any) -> gateway.CapacityGate:
    clock = clock or _Clock()
    return gateway.CapacityGate(
        environment=environment,
        recent_calls=overrides.pop("recent_calls", None),
        spent_since=overrides.pop("spent_since", lambda _seat, _since: 0),
        clock=clock.monotonic,
        sleep=clock.sleep,
        now=overrides.pop("now", lambda: NOW),
        **overrides,
    )


# -- 1a: one row per account, every seat of the registry listed by exactly one account -----------------


def test_every_seat_of_the_registry_belongs_to_exactly_one_account() -> None:
    table = _raw()["accounts"]
    owners = {
        seat: [name for name, row in table.items() if seat in row["seats"]] for seat in registry.LEDGER_CAPACITIES
    }

    assert all(len(names) == 1 for names in owners.values()), owners
    assert set(registry.LEDGER_CAPACITIES) == {seat for row in table.values() for seat in row["seats"]}
    for seat, declaration in registry.LEDGER_CAPACITIES.items():
        if seat != load_environment_contract().model_seat.seat:
            assert declaration.account == owners[seat][0], seat


def test_the_whole_account_figures_of_the_registry_are_the_figures_of_the_row() -> None:
    table = _raw()["accounts"]
    for seat, declaration in registry.LEDGER_CAPACITIES.items():
        owner = next(name for name, row in table.items() if seat in row["seats"])
        figures = [
            limit for limit in table[owner]["limits"] if limit["unit"] == "request" and limit.get("amount") is not None
        ]
        rate = [limit for limit in figures if limit["window_seconds"] != DAY_SECONDS]
        day = [limit for limit in figures if limit["window_seconds"] == DAY_SECONDS]
        if not figures:
            continue  # the model seat: the provider states no figure
        assert (declaration.calls_per_window, declaration.window_seconds) == (
            rate[0]["amount"],
            rate[0]["window_seconds"],
        ), seat
        assert declaration.daily_budget == (day[0]["amount"] if day else None), seat
        assert gateway.CAPACITIES[seat].pool == tuple(table[owner]["seats"]), seat


@pytest.mark.parametrize("name", ["environment_share", "derived_environment_shares", "TWELVE_DATA_ENVIRONMENT_SHARES"])
def test_the_predecessor_functions_of_the_vendor_account_row_are_gone(name: str) -> None:
    assert not hasattr(registry, name)
    assert not hasattr(load_environment_contract(), name)


def test_the_predecessor_fields_of_the_vendor_account_row_are_gone() -> None:
    assert "environment_shares" not in registry.CapacityDeclaration.__dataclass_fields__
    assert "environment_shares" not in gateway.SourceCapacity.__dataclass_fields__
    assert "search" not in registry.LEDGER_CAPACITIES, "the placeholder seat had no caller"


# -- 1b: after the share in one window the next call waits ----------------------------------------------


@pytest.mark.parametrize(("account", "environment", "seats", "window", "share"), _rate_cases())
def test_after_the_share_in_one_window_the_next_call_waits(
    account: str, environment: str, seats: tuple[str, ...], window: int, share: int
) -> None:
    clock = _Clock()
    with gateway.capacity_scope(_gate(environment, clock)):
        for index in range(share):
            _call(seats[index % len(seats)])  # the seats of one account alternate: they count together
        assert clock.slept == [], f"{account}: {share} calls fit the share of {environment} without a wait"
        _call(seats[0])

    assert len(clock.slept) == 1, f"{account}: call {share + 1} in {environment} waited {clock.slept}"
    assert clock.slept[0] == pytest.approx(window), f"the wait is the window of the limit ({window} s)"


def test_the_seats_of_one_account_count_together_in_the_ledger(call_ledger: gateway.MemoryLedger) -> None:
    """Production gets 6 SEC calls a second. Three recorded by `sec` and three by `nport` fill the window."""
    recent = NOW - timedelta(seconds=0.5)
    call_ledger.extend(
        gateway.CallRecord(source=seat, endpoint="x", caller="earlier", called_at=recent, ok=True)
        for seat in ("sec", "nport", "sec", "nport", "sec", "nport")
    )
    full = _gate(
        "production", recent_calls=call_ledger.window, spent_since=call_ledger.calls_since, max_wait_seconds=0.1
    )
    with pytest.raises(gateway.CapacityExceeded, match="rate window .* stayed full"):
        full.admit("nport")
    with pytest.raises(gateway.CapacityExceeded, match="rate window .* stayed full"):
        full.admit("sec")

    call_ledger.pop()  # five in the window: one more fits
    room = _gate(
        "production", recent_calls=call_ledger.window, spent_since=call_ledger.calls_since, max_wait_seconds=0.1
    )
    room.admit("sec")


def test_the_daily_budget_is_the_share_of_the_environment_not_the_whole_account(call_ledger) -> None:
    """Twelve Data staging spends 320 of the 800 a day: the 321st call of the day is refused."""
    spent = [320]
    gate = _gate("staging", spent_since=lambda seat, _since: spent[0] if seat == "twelvedata" else 0)

    with pytest.raises(gateway.BudgetExhausted, match=r"daily budget 320 spent \(320 calls today\) in staging"):
        gate.admit("twelvedata")
    spent[0] = 319
    gate.admit("twelvedata")


# -- 1c: a zero share loads and refuses -----------------------------------------------------------------


def test_a_zero_share_refuses_the_call_with_its_reason_and_sends_nothing(call_ledger) -> None:
    sent: list[str] = []
    identity = FetchKey("twelvedata", "eod", "symbol:TEST", "2026-10-27")
    with corroboration_tally() as tally, gateway.capacity_scope(_gate("preview")):
        with pytest.raises(gateway.ShareBelowOneCall) as refused:
            with gateway.record_call("twelvedata", "eod", identity=identity, caller="test"):
                sent.append("the request")

    assert refused.value.reason.startswith(gateway.SHARE_BELOW_ONE_CALL) and "preview" in refused.value.reason
    assert sent == [] and list(call_ledger) == [], "nothing was sent and nothing was recorded"
    assert tally.refusals() == "capacity refused 1 (twelvedata capacity 1)"


@pytest.mark.parametrize("environment", [*ENVIRONMENTS, "local_dev", "github_ci"])
def test_a_seat_has_a_share_in_an_environment_exactly_when_the_oracle_gives_each_window_a_call(
    environment: str,
) -> None:
    table = _raw()
    for seat, capacity in gateway.CAPACITIES.items():
        if capacity.account is None:
            continue
        row = table["accounts"][capacity.account]
        if environment in ENVIRONMENTS:
            shares = [
                _share(table, capacity.account, environment, limit)
                for limit in row["limits"]
                if limit["unit"] == "request" and limit.get("amount") is not None
            ]
            admits = all(share >= 1 for share in shares)
        else:
            admits = row["holder"] == "host_ip"
        assert (capacity.in_environment(environment) is not None) == admits, (seat, environment)


def test_an_environment_that_the_table_does_not_name_spends_a_host_ip_account_whole_and_no_other() -> None:
    clock = _Clock()
    with gateway.capacity_scope(_gate("local_dev", clock)):
        for _ in range(10):
            _call("sec")  # the whole SEC figure: 10 a second
        assert clock.slept == []
        _call("nport")
        assert clock.slept == [pytest.approx(1.0)], "the 11th call of the pooled SEC seats waits one window"
        for seat in ("openfigi", "moomoo", "twelvedata"):
            with pytest.raises(gateway.ShareBelowOneCall):
                _call(seat)


class _Connection:
    """The caller's connection of `SourceGateway`: it counts no earlier call."""

    def execute(self, _sql: str, _params: tuple) -> Any:
        class _Row:
            def fetchone(self) -> tuple[int]:
                return (0,)

        return _Row()


def test_the_gateway_of_the_standards_lane_admits_with_the_same_share(call_ledger) -> None:
    """`SourceGateway.call` is the second way in. Its gate pools the seats of one account like the scope does."""
    clock = _Clock()
    explicit = gateway.SourceGateway(
        _Connection(),
        caller="test",
        clock=clock.monotonic,
        sleep=clock.sleep,
        now=lambda: NOW,
        environment="staging",
        recent_calls=None,
    )
    for index, seat in enumerate(("sec", "nport")):  # staging: 2 a second for sec and nport together
        explicit.call(seat, "probe", lambda: b"{}", identity=FetchKey(seat, "probe", f"symbol:{index}", "2026-10-27"))
    assert clock.slept == []

    explicit.call("sec", "probe", lambda: b"{}", identity=FetchKey("sec", "probe", "symbol:2", "2026-10-27"))

    assert clock.slept == [pytest.approx(1.0)]
