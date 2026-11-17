"""#1010: stable identifier grammar has one shared definition and pydantic language."""

from __future__ import annotations

import ast
import functools
import importlib
import json
import pathlib
import pkgutil
import re
import string
from typing import Annotated, Any

import data_engine.contracts
import truealpha_contracts
import truealpha_runtime
from pydantic import BaseModel, Field, TypeAdapter, ValidationError
from truealpha_contracts.common import STABLE_ID_BODY, STABLE_ID_PATTERN

_ROOT = pathlib.Path(__file__).resolve().parents[3]
_PY_ROOTS = (
    _ROOT / "common/contracts/src",
    _ROOT / "common/factors/src",
    _ROOT / "common/runtime/src",
    _ROOT / "apps/data-engine/src",
    _ROOT / "apps/llm-service/src",
)
_TS_ROOT = _ROOT / "apps/app-web/src"
_THE_DEF = "common/contracts/src/truealpha_contracts/common.py"
_OUTSIDE = tuple(m for m in string.punctuation if m not in "._:/@+-")
_PROBES_CH = (*string.printable, "é", "Ａ", "٣")
_SHARED = TypeAdapter(Annotated[str, Field(pattern=STABLE_ID_PATTERN)])
_PROBES = json.loads((pathlib.Path(__file__).parent / "fixtures" / "contract_cases.json").read_text("utf-8"))[
    "stable_id_probes"
]


def _is_grammar(cc: str) -> bool:
    try:
        c = re.compile(cc)
    except re.error:
        return False
    return (
        all(c.fullmatch(m) for m in (*"._:/@+-", "a", "0"))
        and sum(c.fullmatch(m) is None for m in _OUTSIDE) > len(_OUTSIDE) / 2
    )


def _spells(text: str) -> bool:
    return any(_is_grammar(cc) for cc in re.findall(r"\[(?:\\.|[^\]\\\n])*\]", text))


def _accepts(adapter: TypeAdapter[Any], value: str) -> bool:
    try:
        adapter.validate_python(value)
    except ValidationError:
        return False
    return True


@functools.cache
def _fields() -> dict[str, TypeAdapter[Any]]:
    out: dict[str, TypeAdapter[Any]] = {}
    for pkg in (truealpha_contracts, truealpha_runtime, data_engine.contracts):
        for info in pkgutil.iter_modules(pkg.__path__):
            mod = importlib.import_module(f"{pkg.__name__}.{info.name}")
            for val in vars(mod).values():
                if isinstance(val, type) and issubclass(val, BaseModel) and val.__module__ == mod.__name__:
                    for name, fi in val.model_fields.items():
                        if any(isinstance(getattr(m, "pattern", None), str) for m in fi.metadata):
                            ad = TypeAdapter(Annotated[(str, *fi.metadata)])
                            if _accepts(ad, "a.b_c:d/e@f+g-h0") and _accepts(ad, "a"):
                                out[f"{val.__module__}.{val.__qualname__}.{name}"] = ad
    return out


def test_the_grammar_is_spelled_once_and_matches_fields() -> None:
    assert all(r.is_dir() for r in (*_PY_ROOTS, _TS_ROOT))
    copies = [
        p.relative_to(_ROOT).as_posix()
        for r in _PY_ROOTS
        for p in sorted(r.rglob("*.py"))
        if any(
            _spells(n.value)
            for n in ast.walk(ast.parse(p.read_text("utf-8")))
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
        )
    ]
    copies.extend(
        p.relative_to(_ROOT).as_posix() for p in sorted(_TS_ROOT.rglob("*.ts*")) if _spells(p.read_text("utf-8"))
    )
    assert (
        copies == [_THE_DEF]
        and _PROBES["copies"]
        and _PROBES["non_copies"]
        and all(_spells(p) for p in _PROBES["copies"])
        and not any(_spells(p) for p in _PROBES["non_copies"])
    )
    fields = _fields()
    for a in (
        "data_engine.contracts.datahub.CaptureSchedulePolicy.policy_version",
        "data_engine.contracts.evidence_graph.CurrentPointerKey.universe_id",
    ):
        assert a in fields, f"missing anchor {a}; selected {len(fields)}"
    bad = {
        w: [p for ch in _PROBES_CH for p in (ch, f"a{ch}") if _accepts(ad, p) != _accepts(_SHARED, p)]
        for w, ad in fields.items()
    }
    assert not {k: v for k, v in bad.items() if v} and STABLE_ID_PATTERN == f"^{STABLE_ID_BODY}$"
