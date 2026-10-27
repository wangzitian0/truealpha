"""One contract row per vendor account, one share function (#1520, step 1).

The oracle of every share is the raw JSON file and exact fraction arithmetic. The tests never read a
share through the function that they test.
"""

from __future__ import annotations

import json
import math
from fractions import Fraction
from importlib import resources
from typing import Any

import pytest
from pydantic import ValidationError
from truealpha_contracts.environment_contract import (
    DAY_SECONDS,
    ENVIRONMENT_CONTRACT_RESOURCE,
    EnvironmentContract,
    UnknownVendorAccount,
    load_environment_contract,
)

ENVIRONMENTS = ("preview", "staging", "production")


def _raw() -> dict[str, Any]:
    text = resources.files("truealpha_contracts.data").joinpath(ENVIRONMENT_CONTRACT_RESOURCE).read_text("utf-8")
    return json.loads(text)


def _contract_with(mutate) -> EnvironmentContract:
    raw = _raw()
    mutate(raw)
    return EnvironmentContract.model_validate_json(json.dumps(raw))


def _percent(raw: dict[str, Any], account: str, environment: str) -> int:
    table = raw["vendor_accounts"]["accounts"][account].get("percent") or raw["vendor_accounts"]["default_percent"]
    return table[environment]


def _limits(raw: dict[str, Any]):
    for name, row in raw["vendor_accounts"]["accounts"].items():
        for limit in row["limits"]:
            yield name, limit


# -- the row replaces the demand estimate ---------------------------------------------------------


def test_the_contract_keeps_no_demand_estimate() -> None:
    """The version of the contract has one pin: `test_environment_contract.py`."""
    raw = _raw()
    contract = load_environment_contract()

    text = json.dumps(raw)
    for predecessor in ("planned_daily_demand", "allocation_basis", "shared_physical_seats"):
        assert predecessor not in text, predecessor
    assert not hasattr(contract, "derived_environment_shares")
    assert not hasattr(contract, "shared_physical_seats")


def test_the_shipped_default_percent_is_20_20_60() -> None:
    """Owner decision D4 of 2026-10-26: the default of the table. A row may override it with a reason."""
    assert dict(load_environment_contract().vendor_accounts.default_percent) == {
        "preview": 20,
        "staging": 20,
        "production": 60,
    }


def test_every_override_of_the_percent_names_its_reason() -> None:
    rows = _raw()["vendor_accounts"]["accounts"]
    overridden = {name for name, row in rows.items() if "percent" in row}

    assert overridden, "the shipped table overrides at least one account, or this test proves nothing"
    assert all(rows[name].get("percent_reason") for name in overridden)
    assert not any("percent_reason" in row and "percent" not in row for row in rows.values())


# -- the rounding rule ------------------------------------------------------------------------------


def test_a_share_is_the_integer_floor_of_amount_times_percent_in_the_window_of_the_limit() -> None:
    raw = _raw()
    contract = load_environment_contract()
    checked = 0
    for name, limit in _limits(raw):
        for environment in ENVIRONMENTS:
            shares = {item.limit_id: item for item in contract.account_share(name, environment).limits}
            share = shares[limit["id"]]
            assert share.window_seconds == limit.get("window_seconds"), (name, limit["id"])
            if limit.get("amount") is None:
                assert share.share is None, (name, limit["id"], environment)
            elif limit["evidence"]["kind"] == "self_imposed":
                assert share.share == limit["amount"], "our own pace is whole for every environment"
            else:
                expected = math.floor(Fraction(limit["amount"] * _percent(raw, name, environment), 100))
                assert share.share == expected, (name, limit["id"], environment)
                checked += 1
    assert checked == 15, "five divided limits in three environments"


def test_the_shares_of_the_environments_never_sum_past_the_amount() -> None:
    raw = _raw()
    contract = load_environment_contract()
    for name, limit in _limits(raw):
        if limit.get("amount") is None or limit["evidence"]["kind"] == "self_imposed":
            continue
        shares = [
            next(i.share for i in contract.account_share(name, env).limits if i.limit_id == limit["id"])
            for env in ENVIRONMENTS
        ]
        assert sum(shares) <= limit["amount"], (name, limit["id"], shares)


def test_the_floor_never_rounds_up_on_the_default_split() -> None:
    """The A2 table of #1520: 8 a minute at 20/20/60 is 1/1/4 (sum 6), never 2/2/5."""

    def drop_the_override(raw: dict[str, Any]) -> None:
        row = raw["vendor_accounts"]["accounts"]["twelvedata"]
        row.pop("percent")
        row.pop("percent_reason")

    contract = _contract_with(drop_the_override)

    def shares(account: str) -> dict[str, list[int | None]]:
        found: dict[str, list[int | None]] = {}
        for env in ENVIRONMENTS:
            for item in contract.account_share(account, env).limits:
                found.setdefault(item.limit_id, []).append(item.share)
        return found

    assert shares("twelvedata") == {"per_minute": [1, 1, 4], "per_day": [160, 160, 480]}
    assert shares("sec_edgar") == {"per_second": [2, 2, 6]}
    assert shares("openfigi") == {"per_minute": [50, 50, 150]}
    assert shares("moomoo_opend") == {"burst": [6, 6, 18], "kline_stocks": [None, None, None]}


def test_a_percent_of_zero_loads_and_admits_no_call() -> None:
    contract = load_environment_contract()
    preview = contract.account_share("twelvedata", "preview")

    assert preview.percent == 0
    assert [item.share for item in preview.limits] == [0, 0]
    assert preview.admits_a_call is False
    assert contract.account_share("twelvedata", "staging").admits_a_call is True


def test_a_floor_of_zero_in_one_window_loads_and_admits_no_call() -> None:
    """10 percent of 8 a minute floors to 0. The day still holds 80. The account gives no call."""
    contract = _contract_with(
        lambda raw: raw["vendor_accounts"]["accounts"]["twelvedata"].update(
            percent={"preview": 10, "staging": 40, "production": 50}
        )
    )

    preview = contract.account_share("twelvedata", "preview")
    assert {item.limit_id: item.share for item in preview.limits} == {"per_minute": 0, "per_day": 80}
    assert preview.admits_a_call is False
    assert preview.share_in_window(DAY_SECONDS) == 80


# -- the rules of the table ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda raw: raw["vendor_accounts"]["default_percent"].update(production=61), "sum past 100"),
        (lambda raw: raw["vendor_accounts"]["default_percent"].update(preview=-1), "0 to 100"),
        (lambda raw: raw["vendor_accounts"]["default_percent"].pop("preview"), "percent table names"),
        (lambda raw: raw["vendor_accounts"]["default_percent"].update(dev=0), "percent table names|Input should be"),
        (
            lambda raw: raw["vendor_accounts"]["accounts"]["twelvedata"]["percent"].update(production=61),
            "sum past 100",
        ),
        (
            lambda raw: raw["vendor_accounts"]["accounts"]["twelvedata"].pop("percent_reason"),
            "percent_reason go together",
        ),
        (
            lambda raw: raw["vendor_accounts"]["accounts"]["openfigi"].update(percent_reason="Because."),
            "percent_reason go together",
        ),
        (
            lambda raw: raw["vendor_accounts"]["accounts"]["model_plan"].update(
                percent={"preview": 0, "staging": 0, "production": 0}, percent_reason="Because."
            ),
            "outside consumers has no percent",
        ),
        (
            lambda raw: raw["vendor_accounts"]["accounts"]["yahoo"]["seats"].append("sec"),
            "belongs to two accounts",
        ),
        (
            lambda raw: raw["vendor_accounts"]["accounts"]["model_plan"].update(seats=["another-seat"]),
            "belongs to no vendor account",
        ),
        (
            lambda raw: raw["vendor_accounts"]["accounts"]["twelvedata"]["limits"][1].update(id="per_minute"),
            "limit id is listed twice",
        ),
        (
            lambda raw: raw["vendor_accounts"]["accounts"]["twelvedata"]["limits"][1].update(window_seconds=60),
            "share one window",
        ),
    ],
    ids=[
        "default-sum-past-100",
        "default-negative",
        "default-missing-environment",
        "default-unknown-environment",
        "override-sum-past-100",
        "override-without-reason",
        "reason-without-override",
        "outside-consumers-with-percent",
        "seat-in-two-accounts",
        "model-seat-in-no-account",
        "duplicate-limit-id",
        "two-limits-one-window",
    ],
)
def test_a_table_that_breaks_a_rule_is_refused(mutate, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        _contract_with(mutate)


def _evidence(mutate):
    def apply(raw: dict[str, Any]) -> None:
        mutate(raw["vendor_accounts"]["accounts"]["twelvedata"]["limits"][0])

    return apply


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (_evidence(lambda limit: limit["evidence"].pop("checked_at")), "published evidence needs"),
        (_evidence(lambda limit: limit["evidence"].pop("source")), "published evidence needs"),
        (_evidence(lambda limit: limit["evidence"].update(source=["http://twelvedata.com/pricing"])), "no https URL"),
        (_evidence(lambda limit: limit["evidence"].update(method="a page read")), "stray"),
        (_evidence(lambda limit: limit["evidence"].update(checked_at="26-10-27")), "pattern"),
        (_evidence(lambda limit: limit.update(amount=None)), "needs an amount"),
        (_evidence(lambda limit: limit.pop("window_seconds")), "needs a window"),
        (_evidence(lambda limit: limit.update(amount=0)), "greater than 0"),
        (_evidence(lambda limit: limit.update(evidence={"kind": "not_known", "reason": "None."})), "carries no amount"),
        (
            _evidence(lambda limit: limit.update(evidence={"kind": "none_published", "reason": "None."})),
            "carries no amount",
        ),
        (_evidence(lambda limit: limit.update(evidence={"kind": "not_known"})), "not_known evidence needs"),
        (
            _evidence(lambda limit: limit.update(evidence={"kind": "measured", "checked_at": "2026-10-27"})),
            "measured evidence needs",
        ),
        (
            _evidence(lambda limit: limit.update(evidence={"kind": "self_imposed", "checked_at": "2026-10-27"})),
            "self_imposed evidence needs",
        ),
        (_evidence(lambda limit: limit.update(evidence={"kind": "guessed"})), "Input should be"),
    ],
    ids=[
        "published-without-date",
        "published-without-source",
        "source-not-https",
        "published-with-a-method",
        "date-not-iso",
        "published-without-amount",
        "amount-without-window",
        "amount-zero",
        "not-known-with-amount",
        "none-published-with-amount",
        "not-known-without-reason",
        "measured-without-method",
        "self-imposed-without-reason",
        "unknown-kind",
    ],
)
def test_evidence_that_breaks_a_rule_is_refused(mutate, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        _contract_with(mutate)


def test_a_measured_figure_names_its_method_and_date() -> None:
    contract = _contract_with(
        lambda raw: raw["vendor_accounts"]["accounts"]["twelvedata"]["limits"][0].update(
            evidence={"kind": "measured", "method": "a read of the 429 answers", "checked_at": "2026-10-27"}
        )
    )

    assert contract.vendor_accounts.accounts["twelvedata"].limits[0].evidence.kind == "measured"


def test_a_figure_has_its_evidence_and_a_limit_without_a_figure_is_shown_not_invented() -> None:
    kinds_without_amount: set[str] = set()
    for name, limit in _limits(_raw()):
        evidence = limit["evidence"]
        if limit.get("amount") is None:
            assert evidence["kind"] in ("none_published", "not_known"), (name, limit["id"])
            kinds_without_amount.add(evidence["kind"])
        elif evidence["kind"] == "published":
            assert evidence["source"] and evidence["checked_at"], (name, limit["id"])
        else:
            assert evidence["kind"] in ("measured", "self_imposed"), (name, limit["id"])
    assert kinds_without_amount == {"none_published", "not_known"}, "the table shows both kinds today"


def test_a_self_imposed_limit_is_never_divided() -> None:
    contract = load_environment_contract()
    pace = next(limit for limit in contract.vendor_accounts.accounts["yahoo"].limits if limit.id == "pace")

    assert pace.evidence.kind == "self_imposed" and pace.divided is False
    assert {contract.account_share("yahoo", env).share_in_window(1) for env in ENVIRONMENTS} == {pace.amount}


# -- who may call, and where the table is silent -----------------------------------------------------------


def test_a_seat_has_one_account_and_an_unknown_seat_or_account_raises() -> None:
    contract = load_environment_contract()

    assert contract.account_of_seat("sec") == contract.account_of_seat("nport") == "sec_edgar"
    assert contract.account_of_seat(contract.model_seat.seat) == "model_plan"
    with pytest.raises(UnknownVendorAccount):
        contract.account_of_seat("search")
    with pytest.raises(UnknownVendorAccount):
        contract.account_share("no-such-account", "production")


def test_an_environment_that_the_table_does_not_name_gets_a_host_ip_account_whole_and_no_other() -> None:
    """Local runs and CI use another host and no shared key (A8 item 2 of #1520)."""
    contract = load_environment_contract()
    raw = _raw()["vendor_accounts"]["accounts"]

    for name, row in raw.items():
        share = contract.account_share(name, "local_dev")
        if row["holder"] == "host_ip":
            assert share.percent == 100, name
            assert share.admits_a_call is True, name
        else:
            assert share.percent == 0, name
    assert contract.account_share("sec_edgar", "local_dev").share_in_window(1) == 10
    assert contract.account_share("twelvedata", "github_ci").admits_a_call is False
    assert contract.account_share("openfigi", "local_test").admits_a_call is False
    assert contract.account_share("moomoo_opend", "local_dev").admits_a_call is False


def test_the_alias_of_an_environment_reads_the_same_share() -> None:
    contract = load_environment_contract()

    assert contract.account_share("sec_edgar", "prod") == contract.account_share("sec_edgar", "production")
    assert contract.account_share("sec_edgar", " Staging ").environment == "staging"


# -- a loaded table must leave staging and production able to call -------------------------------------------


def _windows_without_a_call(raw: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Every (account, limit, environment) of staging or production whose share floors below one call.

    The oracle is exact fraction arithmetic on the raw table. It does not call `account_share`. The model plan
    is out: the provider states no figure. A `self_imposed` pace is whole, so it never floors to zero.
    """
    model_seat = raw["model_seat"]["seat"]
    found = []
    for name, row in raw["vendor_accounts"]["accounts"].items():
        if model_seat in row["seats"]:
            continue
        for limit in row["limits"]:
            if limit.get("amount") is None or limit["evidence"]["kind"] == "self_imposed":
                continue
            for environment in ("staging", "production"):
                if math.floor(Fraction(limit["amount"] * _percent(raw, name, environment), 100)) < 1:
                    found.append((name, limit["id"], environment))
    return found


def test_staging_and_production_get_at_least_one_call_in_every_window_of_every_account() -> None:
    """A zero share loads by design (#1520). This test keeps the shipped table from loading one for the
    two environments that must call: a percent of 0, or a new limit that floors to 0, refuses every call there."""
    assert _windows_without_a_call(_raw()) == []


def test_the_check_of_the_shipped_table_names_a_percent_of_zero_and_a_floor_of_zero() -> None:
    zero_percent = _raw()
    zero_percent["vendor_accounts"]["default_percent"].update(preview=40, staging=60, production=0)
    found = _windows_without_a_call(zero_percent)
    assert ("sec_edgar", "per_second", "production") in found
    assert ("openfigi", "per_minute", "production") in found

    floor_of_zero = _raw()
    floor_of_zero["vendor_accounts"]["accounts"]["sec_edgar"]["limits"].append(
        {
            "id": "per_ten_seconds_probe",
            "unit": "request",
            "amount": 1,
            "window_seconds": 10,
            "counts_per": "not_stated",
            "evidence": {"kind": "published", "source": ["https://example.com/limits"], "checked_at": "2026-10-27"},
        }
    )
    assert _windows_without_a_call(floor_of_zero) == [
        ("sec_edgar", "per_ten_seconds_probe", "staging"),
        ("sec_edgar", "per_ten_seconds_probe", "production"),
    ]


def test_the_model_plan_has_nothing_to_refuse_in_any_environment() -> None:
    """The model plan holds no figure with an amount, so `account_share` has no window to floor to zero.
    The pace of the model seat is `model_seat.environments`, and it gives an unlisted environment no limit."""
    contract = load_environment_contract()

    for environment in (*ENVIRONMENTS, "local_dev", "github_ci"):
        assert contract.account_share("model_plan", environment).admits_a_call is True, environment
