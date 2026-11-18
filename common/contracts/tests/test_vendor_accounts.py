from __future__ import annotations

import importlib.util
import json
import math
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from truealpha_contracts.environment_contract import DAY_SECONDS, UnknownVendorAccount, load_environment_contract

ENVIRONMENTS = ("preview", "staging", "production")
_S = importlib.util.spec_from_file_location("ch", Path(__file__).parent / "fixtures/contract_helpers.py")
assert _S and _S.loader
_H = importlib.util.module_from_spec(_S)
_S.loader.exec_module(_H)
_C, _apply_ops, _raw, _with = _H.CASES, _H.apply_ops, _H.raw_env_contract, _H.with_env_contract


def _pct(raw: dict[str, Any], acc: str, env: str) -> int:
    row = raw["vendor_accounts"]["accounts"][acc]
    return (row.get("percent") or raw["vendor_accounts"]["default_percent"])[env]


def _no_call(r: dict[str, Any]) -> list[tuple[str, str, str]]:
    seat, out = r["model_seat"]["seat"], []
    for n, row in r["vendor_accounts"]["accounts"].items():
        if seat in row["seats"]:
            continue
        for l0 in row["limits"]:
            if l0.get("amount") is not None and l0["evidence"]["kind"] != "self_imposed":
                for e in ("staging", "production"):
                    if math.floor(Fraction(l0["amount"] * _pct(r, n, e), 100)) < 1:
                        out.append((n, l0["id"], e))
    return out


def test_vendor_accounts_shares_seats_and_live_readiness() -> None:
    raw, contract = _raw(), load_environment_contract()
    text, rows = json.dumps(raw), raw["vendor_accounts"]["accounts"]
    assert all(old not in text for old in ("planned_daily_demand", "allocation_basis", "shared_physical_seats"))
    assert not hasattr(contract, "derived_environment_shares") and not hasattr(contract, "shared_physical_seats")
    assert dict(contract.vendor_accounts.default_percent) == {"preview": 20, "staging": 20, "production": 60}
    over = {n for n, r in rows.items() if "percent" in r}
    assert over and all(rows[n].get("percent_reason") for n in over)
    assert not any("percent_reason" in r and "percent" not in r for r in rows.values())
    limits = [(name, lim) for name, row in rows.items() for lim in row["limits"]]
    checked = 0
    for name, lim in limits:
        totals = []
        for env in ENVIRONMENTS:
            sh = {i.limit_id: i for i in contract.account_share(name, env).limits}[lim["id"]]
            assert sh.window_seconds == lim.get("window_seconds")
            if lim.get("amount") is None:
                assert sh.share is None
            elif lim["evidence"]["kind"] == "self_imposed":
                assert sh.share == lim["amount"]
            else:
                assert sh.share == math.floor(Fraction(lim["amount"] * _pct(raw, name, env), 100))
                totals.append(sh.share)
                checked += 1
        assert not totals or sum(totals) <= lim["amount"]
    assert checked == 15
    ops = [
        ["pop", "vendor_accounts.accounts.twelvedata.percent"],
        ["pop", "vendor_accounts.accounts.twelvedata.percent_reason"],
    ]
    c = _with(lambda r: _apply_ops(r, ops))
    for acc, exp in _C["vendor_shares"].items():
        got = {
            k: [i.share for e in ENVIRONMENTS for i in c.account_share(acc, e).limits if i.limit_id == k] for k in exp
        }
        assert got == exp
    pv = contract.account_share("twelvedata", "preview")
    assert pv.percent == 0 and [i.share for i in pv.limits] == [0, 0] and not pv.admits_a_call
    assert contract.account_share("twelvedata", "staging").admits_a_call
    p2_r = _with(
        lambda r: _apply_ops(
            r,
            [["set", "vendor_accounts.accounts.twelvedata.percent", {"preview": 10, "staging": 40, "production": 50}]],
        )
    )
    p2 = p2_r.account_share("twelvedata", "preview")
    assert {i.limit_id: i.share for i in p2.limits} == {"per_minute": 0, "per_day": 80} and not p2.admits_a_call
    assert p2.share_in_window(DAY_SECONDS) == 80
    ev = {"kind": "measured", "method": "429", "checked_at": "2026-10-27"}
    cm = _with(lambda r: r["vendor_accounts"]["accounts"]["twelvedata"]["limits"][0].update(evidence=ev))
    assert cm.vendor_accounts.accounts["twelvedata"].limits[0].evidence.kind == "measured"
    assert {lim["evidence"]["kind"] for _n, lim in limits if lim.get("amount") is None} == {
        "none_published",
        "not_known",
    }
    for _n, lim in limits:
        if lim.get("amount") is not None:
            e = lim["evidence"]
            ok = (
                (e["source"] and e["checked_at"])
                if e["kind"] == "published"
                else e["kind"] in ("measured", "self_imposed")
            )
            assert ok
    pace = next(x for x in contract.vendor_accounts.accounts["yahoo"].limits if x.id == "pace")
    assert pace.evidence.kind == "self_imposed" and not pace.divided
    assert {contract.account_share("yahoo", e).share_in_window(1) for e in ENVIRONMENTS} == {pace.amount}
    assert contract.account_of_seat("sec") == contract.account_of_seat("nport") == "sec_edgar"
    assert contract.account_of_seat(contract.model_seat.seat) == "model_plan"
    for fn in (
        lambda: contract.account_of_seat("search"),
        lambda: contract.account_share("no-such-account", "production"),
    ):
        with pytest.raises(UnknownVendorAccount):
            fn()
    for n, row in rows.items():
        s0 = contract.account_share(n, "local_dev")
        assert (s0.percent, s0.admits_a_call) == ((100, True) if row["holder"] == "host_ip" else (0, s0.admits_a_call))
    assert contract.account_share("sec_edgar", "local_dev").share_in_window(1) == 10
    for a, e in (("twelvedata", "github_ci"), ("openfigi", "local_test"), ("moomoo_opend", "local_dev")):
        assert not contract.account_share(a, e).admits_a_call
    assert contract.account_share("sec_edgar", "prod") == contract.account_share("sec_edgar", "production")
    assert contract.account_share("sec_edgar", " Staging ").environment == "staging"
    assert all(contract.account_share("model_plan", e).admits_a_call for e in (*ENVIRONMENTS, "local_dev", "github_ci"))
    assert _no_call(raw) == []
    zp = _raw()
    zp["vendor_accounts"]["default_percent"].update(preview=40, staging=60, production=0)
    assert ("openfigi", "per_minute", "production") in set(_no_call(zp))
    assert ("sec_edgar", "per_second", "production") not in set(_no_call(zp))
    fz = _raw()
    fz["vendor_accounts"]["accounts"]["sec_edgar"]["limits"].append(_C["vendor_fz_limit"])
    assert _no_call(fz) == [
        ("sec_edgar", "per_ten_seconds_probe", "staging"),
        ("sec_edgar", "per_ten_seconds_probe", "production"),
    ]


@pytest.mark.parametrize(("ops", "msg"), _C["vendor_invalid"])
def test_vendor_rules_refused(ops: list[list[Any]], msg: str) -> None:
    with pytest.raises(ValidationError, match=msg):
        _with(lambda r: _apply_ops(r, ops))
