from __future__ import annotations

import importlib.util
import json
import re
import runpy
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from truealpha_contracts import environment_contract as _ec

_S = importlib.util.spec_from_file_location("ch", Path(__file__).parent / "fixtures/contract_helpers.py")
assert _S and _S.loader
_H = importlib.util.module_from_spec(_S)
_S.loader.exec_module(_H)
_C, _apply_ops, _raw, _with = _H.CASES, _H.apply_ops, _H.raw_env_contract, _H.with_env_contract
ROOT = Path(__file__).resolve().parents[3]


def _cap(**ch: object) -> _ec.CodeServerCapacity:
    return _ec.CodeServerCapacity.model_validate({**_C["base_cap"], **ch})


def test_sentinels_universes_ticks_and_lanes(monkeypatch: pytest.MonkeyPatch) -> None:
    c, raw = _ec.load_environment_contract(), _raw()
    assert len(c.sentinels) == 10 and len({m.issuer_id for m in c.sentinels}) == 9
    assert [m.ticker for m in c.sentinels] == _C["sentinels"]
    assert all(c.is_head_scheduled(uid, env) is exp for uid, env, exp in _C["head_scheduled"])
    with pytest.raises(_ec.UnknownUniverse, match="starts with no universe id prefix"):
        c.universe_of_id("universe:unknown-us-2026-09-30")
    st, pu = c.environments["staging"], [(e.key, e.status) for e in c.environments["production"].universes]
    assert pu == [("topt", "scheduled"), ("qqq", "scheduled"), ("sp500", "stopped")]
    assert [(e.key, e.status) for e in st.universes] == [
        ("topt", "scheduled"),
        ("qqq", "scheduled"),
        ("sp500", "scheduled"),
    ] and st.list_refresh == ()
    assert c.is_universe_scheduled("qqq", "staging") and c.is_universe_list_refresh_scheduled("qqq", "staging")
    assert c.is_universe_scheduled("sp500", "staging") and c.is_universe_list_refresh_scheduled("sp500", "staging")
    assert set(st.ticks) == {"topt", "qqq", "sp500", "canary"}
    assert c.contract_version == raw["contract_version"] == "3.0.0" and c.schema_version == 1
    for s in raw["environments"].values():
        assert "strategy_universe" not in s and "target_strategy_universe" not in s
        assert all("scheduled" not in t for t in s["ticks"].values())
    assert [e for e in raw["environments"]["production"]["universes"] if e["key"] == "sp500"] == [
        {"key": "sp500", "status": "stopped"}
    ]
    assert _ec.EnvironmentUniverse(key="sp500", status="stopped").status == "stopped"
    assert c.universe_status("sp500", "production") == "stopped" and not c.is_universe_scheduled("sp500", "production")
    assert not c.is_head_scheduled("universe:sp500-us-2026-09-30", "production")
    assert all(c.scheduled_universes(env) == tuple(exp) for env, exp in _C["scheduled_envs"])
    c2 = _with(lambda r: r["environments"]["production"]["universes"][2].update(status="scheduled"))
    assert c2.is_universe_scheduled("sp500", "production") and c2.scheduled_universes("production") == (
        "topt",
        "qqq",
        "sp500",
    )
    assert c2.is_head_scheduled("universe:sp500-us-2026-09-30", "production")
    assert all(c.expects_verdict(chk, env) is exp for chk, env, exp in _C["expects_verdict"])
    with pytest.raises(_ec.UnknownUniverse, match="the lane name of no universe"):
        c.universe_of_lane("universe:adhoc")
    assert not any(c.expects_verdict("theme_purity@universe:adhoc", e) for e in ("staging", "production"))
    assert {k: [r.id_prefix, r.lane_name] for k, r in c.universes.items()} == _C["universe_map"]
    assert (
        c.universe_of_id("universe:topt-us-2026-03-31") == "topt" and c.universe_of_lane("universe-list:qqq") == "qqq"
    )
    assert [k for k, r in c.universes.items() if r.is_sentinel] == ["canary"]
    tools = json.loads((ROOT / "tools/nightly_verdicts.json").read_text("utf-8"))
    assert c.nightly_verdicts.model_dump(mode="json", exclude={"description"}) == {
        "checks": tools["checks"],
        "per_universe": tools["per_universe"],
    }
    export_py = Path(__file__).resolve().parents[1] / "conformance/export_environment_contract.py"
    exp_mod = runpy.run_path(str(export_py))
    assert (ROOT / "apps/app-web/src/contracts/environmentContract.generated.ts").read_text("utf-8") == exp_mod[
        "render"
    ]()
    monkeypatch.setattr("sys.argv", [str(export_py), "--check"])
    assert exp_mod["main"]() == 0
    obj = (ROOT / "apps/app-web/src/contracts/serviceObjectives.ts").read_text("utf-8")
    assert "isHeadScheduledFromContract" in obj and 'universeId.startsWith("universe:qqq-")' not in obj
    init_t, rel_t = (ROOT / "init.md").read_text("utf-8"), (ROOT / "docs/release-protocol.md").read_text("utf-8")
    head = init_t.split("## 1. Core Design Principles", 1)[0]
    assert "environment_contract.v1.json" in head and "S&P 500" in head
    assert "| Local | `local_dev`, `local_test` |" not in init_t and "TWELVE_DATA_ENVIRONMENT_SHARES" not in init_t
    assert "waits\n20 minutes" not in rel_t and "`DEFAULT_DAILY_CAP`" in rel_t and "owner's bound" not in rel_t
    assert re.search(r"daily cap\s*\(\s*(?:\d+|[a-z]+),", rel_t) is None


@pytest.mark.parametrize(("ops", "msg"), _C["env_mutations"])
def test_universe_mutations_refused(ops: list[list[Any]], msg: str) -> None:
    with pytest.raises(ValidationError, match=msg):
        _with(lambda r: _apply_ops(r, ops))


def test_capacity_and_model_seat() -> None:
    c, raw = _ec.load_environment_contract(), _raw()
    jrb, cap = c.job_runtime_bounds, c.code_server_capacity
    assert (jrb.margin, jrb.floor_minutes, jrb.window_days) == (2.0, 10.0, 30)
    assert c.derive_max_runtime_seconds(1350) == 2700 and c.derive_max_runtime_seconds(30) == 600
    slots = cap.concurrent_runs * cap.processes_per_run
    held = cap.server_processes * cap.server_mib + slots * cap.run_mib
    assert cap.usable_mib == int(cap.ceiling_mib * _ec.UTILIZATION_CEILING) and held <= cap.usable_mib < held + slots
    assert cap.max_concurrent_runs == cap.concurrent_runs and cap.max_waiting_runs == cap.concurrent_runs - 1 >= 1
    grown = Decimal(str(cap.measured.grpc_server_hwm_mib)) * (1 + Decimal(str(cap.server_margin)))
    assert cap.server_mib == int(grown) + (grown % 1 > 0) and cap.server_mib > cap.measured.grpc_server_hwm_mib
    assert cap.run_mib >= cap.measured.working_run_hwm_mib >= cap.measured.fresh_import_hwm_mib
    rc = raw["code_server_capacity"]
    assert "utilization_limit" not in rc and "70%" not in json.dumps(rc)
    with pytest.raises(ValidationError, match="container_peak_mib"):
        _ec.CodeServerCapacity.model_validate({**rc, "measured": {**rc["measured"], "container_peak_mib": 1186.4}})
    fc = _cap()
    got = (fc.usable_mib, fc.server_mib, fc.run_mib, fc.max_concurrent_runs, fc.max_waiting_runs)
    assert got == (1075, 240, 417, 2, 1)
    assert all(_cap(**ch).run_mib == b for ch, b in _C["cap_run_mib"])
    assert all(_cap(server_margin=m).server_mib == s for m, s in _C["cap_margins"])
    for bad in _C["cap_invalid"]:
        with pytest.raises(ValidationError):
            _cap(**bad)
    with pytest.raises(ValidationError, match="below a fresh import"):
        _cap(measured={**_C["base_cap"]["measured"], "working_run_hwm_mib": 100.0})
    seat, retry = c.model_seat, c.model_seat.retry
    assert {n for n, s in c.environments.items() if s.live_vendor_calls} == {"staging", "production"}
    assert seat.limits_for("dev") is None
    for n in ("staging", "production"):
        lim = seat.limits_for(n)
        assert lim is not None and lim.max_concurrent_calls >= 1 and lim.calls_per_minute >= 1
    assert [retry.base_backoff_seconds(n) for n in range(1, retry.max_attempts)] == [5, 10, 20, 40, 40, 40]
    assert retry.base_total_wait_seconds == 155 >= retry.min_total_wait_seconds == 90
    assert retry.deadline_seconds >= retry.min_total_wait_seconds
    assert seat.probe_cron("production") != seat.probe_cron("staging") and seat.decoding_version_for("dev") == "v1"
    assert seat.probe_cron("production") == f"{seat.probe_offset_minutes('production')} {seat.probe.hour_utc} * * *"
    mins = sorted(seat.probe_offset_minutes(n) for n in ("production", "staging"))
    need = (retry.deadline_seconds + seat.probe.attempt_timeout_seconds + seat.probe.launch_margin_seconds) / 60
    assert mins[1] - mins[0] >= need
    d = json.loads(seat.environments["staging"].model_dump_json())
    d.pop("decoding_version")
    assert seat.environments["staging"].model_validate_json(json.dumps(d)).decoding_version == "v1"
    s_raw, r_raw = seat.model_dump(mode="json"), retry.model_dump(mode="json")
    for target, patch, msg in _C["bad_seats"]:
        pay = {**s_raw, "retry": {**r_raw, **patch}} if target == "retry" else {**s_raw, **patch}
        with pytest.raises(ValidationError, match=msg):
            _ec.ModelSeatContract.model_validate_json(json.dumps(pay))
    for bad_ver in ("2", "V2", "v", "v2.1", ""):
        s = json.loads(seat.model_dump_json())
        s["environments"]["staging"]["decoding_version"] = bad_ver
        with pytest.raises(ValidationError, match="decoding_version"):
            _ec.ModelSeatContract.model_validate_json(json.dumps(s))
    for ops, msg in _C["bad_env_seats"]:
        data = c.model_dump(mode="json")
        _apply_ops(data, ops)
        with pytest.raises(ValidationError, match=msg):
            _ec.EnvironmentContract.model_validate_json(json.dumps(data))


def test_metric_applicability_declares_owner_approved_topt_gppe_exclusions() -> None:
    c = _ec.load_environment_contract()
    gppe = c.not_applicable_for_universe_id("gppe", "universe:topt-us-2026-03-31")
    assert {e.ticker for e in gppe} == {"XOM", "V", "MA", "BRK.B"} and all(e.status == "not_applicable" for e in gppe)
    sp500_gppe = c.not_applicable_for_universe_id("gppe", "universe:sp500-us-2026-09-30")
    assert {e.ticker for e in sp500_gppe} == {"XOM", "V", "MA", "BRK.B"} and all(
        e.status == "not_applicable" for e in sp500_gppe
    )
    assert c.not_applicable_for_universe_id("gppe", "universe:qqq-us-2026-06-30") == ()
    for ops, msg in _C["bad_applicability"]:
        data = c.model_dump(mode="json")
        _apply_ops(data, ops)
        with pytest.raises(ValidationError, match=msg):
            _ec.EnvironmentContract.model_validate_json(json.dumps(data))
    dup = c.model_dump(mode="json")
    dup["metric_applicability"].append(dup["metric_applicability"][0])
    with pytest.raises(ValidationError, match="duplicate metric_applicability"):
        _ec.EnvironmentContract.model_validate_json(json.dumps(dup))
