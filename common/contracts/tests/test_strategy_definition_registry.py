from __future__ import annotations

import copy
import json
import runpy
from importlib import resources
from pathlib import Path

import pytest
from pydantic import ValidationError
from truealpha_contracts import strategy as _st
from truealpha_runtime.strategy_run import StrategyRunDefinition
from truealpha_runtime.testing import synthetic_provisional_strategy_definition

pytestmark = pytest.mark.contract

_V1 = _st.StrategyDefinitionIdentity("1.0.0", "a0c6f1f003373b5eff85872fd493de91e2fd6d9fa3d15e78e278f96b88bd8803", False)
_V11 = _st.StrategyDefinitionIdentity(
    "1.1.0", "b8768334a00f88f9f34244a758182454ef220343e161f6c5008dde5c9d4611e3", False
)
_DEL, _OUT = ("0.1.0", "e5c56da455a2845d99e67265d96b30e9b479384ef11a8a4d242494b1816fe79d"), "f" * 64
_EXPORT = Path(__file__).resolve().parents[1] / "conformance" / "export_strategy_definitions.py"


def _id(d: _st.LargeModelValueV0Definition) -> _st.StrategyDefinitionIdentity:
    return _st.StrategyDefinitionIdentity(d.definition_version, d.content_sha256, d.provisional_semantics)


def test_registry_review_state_scan_and_ts_export(tmp_path: Path) -> None:
    reg, live = _st.strategy_definition_registry(), _st.load_live_strategy_definition()
    assert _V1 in reg and _V11 in reg and _id(live) == _V11 and _id(live) in reg
    for ver, sha, exp in (
        (_V1.version, _V1.content_sha256, "reviewed"),
        (_V11.version, _V11.content_sha256, "reviewed"),
        (*_DEL, "unknown"),
        (_V1.version, _OUT, "unknown"),
        (_DEL[0], _V1.content_sha256, "unknown"),
    ):
        assert _st.definition_review_state(ver, sha) == exp
    for rs, (v, h) in (("reviewed", (_V1.version, _OUT)), ("provisional", _DEL)):
        with pytest.raises(ValidationError, match="review_state must be the registry state"):
            StrategyRunDefinition(version=v, content_sha256=h, review_state=rs)
    assert StrategyRunDefinition.resolve(version=_DEL[0], content_sha256=_DEL[1]).review_state == "unknown"
    syn = synthetic_provisional_strategy_definition()
    assert syn.provisional_semantics and syn.tier_valuation.provisional_thresholds and _id(syn) not in reg
    payload = copy.deepcopy(_st.load_live_strategy_corpus()["strategy_definition"])
    payload["provisional_semantics"] = True
    with pytest.raises(ValidationError, match="provisional_semantics must equal the tier valuation"):
        _st.LargeModelValueV0Definition.model_validate_json(json.dumps(payload))
    ver, sha = syn.definition_version, syn.content_sha256
    assert _st.definition_review_state(ver, sha) == "unknown"
    with _st.registered_strategy_definition(syn) as ident:
        assert (
            ident == _id(syn)
            and ident in _st.strategy_definition_registry()
            and _st.definition_review_state(ver, sha) == "provisional"
        )
    assert _st.definition_review_state(ver, sha) == "unknown" and _id(syn) not in _st.strategy_definition_registry()
    with pytest.raises(ValueError, match="the registry already holds version 1.1.0"):
        with _st.registered_strategy_definition(live):
            pass

    def _wr(p: Path, d: _st.LargeModelValueV0Definition) -> None:
        p.write_text(
            json.dumps(
                {
                    "strategy_definition": json.loads(d.model_dump_json()),
                    "expected": {"strategy_definition_id": d.strategy_definition_id},
                }
            ),
            encoding="utf-8",
        )

    (tmp_path / "first.json").write_bytes(
        resources.files("truealpha_contracts.data").joinpath("large_model_value_v0_definition_1_0_0.json").read_bytes()
    )
    (tmp_path / "a_run_report.json").write_bytes(
        (Path(__file__).resolve().parents[2] / "runtime/tests/fixtures/strategy_run_preview.v1.json").read_bytes()
    )
    _wr(tmp_path / "second.json", syn)
    assert _st.strategy_definitions_in(tmp_path) == tuple(sorted((_V1, _id(syn))))
    for n in ("first.json", "second.json"):
        _wr(tmp_path / n, live)
    with pytest.raises(ValueError, match="a second definition file has version 1.1.0"):
        _st.strategy_definitions_in(tmp_path)
    export = runpy.run_path(str(_EXPORT))
    rendered = export["rendered_files"]()
    reg_ts = rendered[export["TARGET"]]
    for e in _st.strategy_definition_registry():
        assert (
            f'{{ version: "{e.version}", content_sha256: "{e.content_sha256}", provisional_semantics: {str(e.provisional_semantics).lower()} }},'
            in reg_ts
        )
    assert (
        f'{{ version: "{syn.definition_version}", content_sha256: "{syn.content_sha256}", provisional_semantics: true }}'
        in rendered[export["SYNTHETIC_TARGET"]]
    )
    assert all(p.read_text(encoding="utf-8") == t for p, t in rendered.items())
