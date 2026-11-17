"""#997: content addressing is defined once, and refuses copies under any name."""

from __future__ import annotations

import ast
import functools
import hashlib
import json
import pathlib

import pytest
from pydantic import BaseModel, ConfigDict, Field, model_validator
from truealpha_contracts.common import canonical_sha256, identify

_ROOT = pathlib.Path(__file__).resolve().parents[3]
_SRC_DIRS = (
    "common/contracts/src",
    "common/factors/src",
    "common/runtime/src",
    "apps/data-engine/src",
    "apps/llm-service/src",
)
_SRC = tuple(_ROOT / p for p in _SRC_DIRS)
_DEFS = {
    "common/contracts/src/truealpha_contracts/common.py.identify",
    "common/contracts/src/truealpha_contracts/common.py.identify_by_grain",
}
_EXEMPT = {"apps/data-engine/src/data_engine/contracts/capture_control.py._freeze"}
_SNIPS = json.loads((pathlib.Path(__file__).parent / "fixtures/contract_cases.json").read_text("utf-8"))["ca_snippets"]


class _Addressed(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    thing_id: str = Field(default="", pattern=r"^(?:|thing:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    subject: str

    @model_validator(mode="after")
    def _address(self) -> _Addressed:
        identify(self, id_field="thing_id", prefix="thing")
        return self


def test_content_addressing_semantics() -> None:
    d1, d2 = canonical_sha256({"subject": "one"}), canonical_sha256({"subject": "two"})
    m1, m2 = _Addressed(subject="one"), _Addressed(subject="two", thing_id=f"thing:{d2}", content_sha256=d2)
    assert (m1.thing_id, m1.content_sha256) == (f"thing:{d1}", d1)
    assert (m2.thing_id, m2.content_sha256) == (f"thing:{d2}", d2)
    for f, wrong in (("thing_id", "thing:" + "0" * 64), ("content_sha256", "0" * 64)):
        with pytest.raises(ValueError, match=f"{f} does not match canonical content"):
            _Addressed(subject="three", **{f: wrong})
    for bad in (None, 0, False, [], (), {"not": "a hash"}):
        m = _Addressed(subject="four")
        object.__setattr__(m, "content_sha256", bad)
        with pytest.raises(ValueError, match="content_sha256 does not match canonical content"):
            identify(m, id_field="thing_id", prefix="thing")


def _stamps_a_content_address(node: ast.FunctionDef) -> bool:
    w = list(ast.walk(node))
    called = set()
    interp = set()
    for c in w:
        if isinstance(c, ast.Call) and isinstance(c.func, (ast.Name, ast.Attribute)):
            called.add(c.func.id if isinstance(c.func, ast.Name) else c.func.attr)
        if isinstance(c, ast.Assign) and isinstance(c.value, ast.JoinedStr):
            interp.update(t.id for t in c.targets if isinstance(t, ast.Name))
    writes_id = False
    for c in w:
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and c.func.attr == "__setattr__" and c.args:
            a = c.args[-1]
            writes_id |= isinstance(a, ast.JoinedStr) or (isinstance(a, ast.Name) and a.id in interp)
    return "canonical_sha256" in called and "__setattr__" in called and writes_id


@functools.cache
def _wrapper_definitions() -> tuple[str, ...]:
    out = []
    for r in _SRC:
        for p in sorted(r.rglob("*.py")):
            for n in ast.parse(p.read_text("utf-8")).body:
                if isinstance(n, ast.FunctionDef) and _stamps_a_content_address(n):
                    out.append(f"{p.relative_to(_ROOT).as_posix()}.{n.name}")
    return tuple(out)


def test_content_addressing_guards() -> None:
    assert all(r.is_dir() for r in _SRC)
    defs = _wrapper_definitions()
    assert set(defs) == (_DEFS | _EXEMPT) and all("/" in n for n in defs)
    assert all(
        isinstance(n := ast.parse(s).body[0], ast.FunctionDef) and _stamps_a_content_address(n) for s in _SNIPS["pos"]
    )
    n1, n2 = (ast.parse(s).body[0] for s in _SNIPS["neg"])
    assert isinstance(n1, ast.FunctionDef) and not _stamps_a_content_address(n1)
    assert isinstance(n2, ast.FunctionDef) and any(isinstance(c, ast.JoinedStr) for c in ast.walk(n2))
    assert not _stamps_a_content_address(n2)


def _normalised(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    tree = ast.parse(ast.unparse(node)).body[0]
    for c in ast.walk(tree):
        if isinstance(c, ast.Name):
            c.id = "_"
        elif isinstance(c, ast.arg):
            c.arg, c.annotation = "_", None
        elif isinstance(c, ast.Attribute):
            c.attr = "_"
        elif isinstance(c, ast.Constant):
            c.value = None
        elif isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef)):
            c.name = "_"
    return ast.dump(tree)


def test_no_function_body_is_duplicated_and_canonical_sha256_is_unique() -> None:
    by_shape: dict[str, list[str]] = {}
    sha_defs = []
    for root in _SRC:
        for path in sorted(root.rglob("*.py")):
            rel = path.relative_to(_ROOT).as_posix()
            for node in ast.walk(ast.parse(path.read_text("utf-8"))):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if len(node.body) >= 4:
                        by_shape.setdefault(_normalised(node), []).append(f"{rel}:{node.lineno}::{node.name}")
                    if node.name.lstrip("_") == "canonical_sha256":
                        sha_defs.append(f"{rel}::{node.name}")
    dups = [sorted(m) for m in by_shape.values() if len({x.split("::")[0].rsplit(":", 1)[0] for x in m}) > 1]
    assert by_shape and not dups
    assert sha_defs == ["common/contracts/src/truealpha_contracts/common.py::canonical_sha256"]
    for s in ({"b": 2, "a": 1}, {"nested": ["alpha", "beta"], "unicode": "café — 東京 — 🚀"}):
        raw = json.dumps(s, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        assert canonical_sha256(s) == hashlib.sha256(raw).hexdigest()
