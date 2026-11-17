"""Shared JSON/mutation helpers for common/contracts tests."""

from __future__ import annotations

import copy
import importlib.util
import json
from importlib import resources
from pathlib import Path
from typing import Any

from truealpha_contracts.environment_contract import ENVIRONMENT_CONTRACT_RESOURCE, EnvironmentContract

CASES = json.loads((Path(__file__).parent / "contract_cases.json").read_text(encoding="utf-8"))


def load_fixture_mod(filename: str) -> Any:
    s = importlib.util.spec_from_file_location(filename.removesuffix(".py"), Path(__file__).parent / filename)
    assert s and s.loader
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m


def raw_env_contract() -> dict[str, Any]:
    raw_txt = resources.files("truealpha_contracts.data").joinpath(ENVIRONMENT_CONTRACT_RESOURCE).read_text("utf-8")
    return json.loads(raw_txt)


def apply_ops(r: dict[str, Any], ops: list[list[Any]]) -> None:
    sv1 = CASES["strategy_v1"]
    subs = {"$UNLOCK": CASES["unlock"], "$PS_TV": sv1["ps_tier_valuation"], "$FF": sv1["ff"]}
    for op in ops:
        tgt, parts = r, str(op[1]).split(".")
        for p in parts[:-1]:
            tgt = tgt[int(p)] if isinstance(tgt, list) else tgt[p]
        k = int(parts[-1]) if isinstance(tgt, list) else parts[-1]
        v = (
            subs[op[2]]
            if len(op) > 2 and isinstance(op[2], str) and op[2] in subs
            else (op[2] if len(op) > 2 else None)
        )
        val = copy.deepcopy(v)
        if op[0] == "pop":
            tgt.pop(k)
        elif op[0] == "append":
            tgt[k].append(val)
        elif op[0] == "upd":
            tgt[k].update(val)
        else:
            tgt[k] = val


def with_env_contract(mutate: Any) -> EnvironmentContract:
    r = raw_env_contract()
    mutate(r)
    return EnvironmentContract.model_validate_json(json.dumps(r))
