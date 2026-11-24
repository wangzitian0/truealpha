"""Verify closed JSON schemas, frozen module list, and zero aggregate __init__ imports."""

import hashlib
import importlib
import pkgutil
import re
from pathlib import Path

import truealpha_contracts
from pydantic import BaseModel

REPO_ROOT = Path(__file__).resolve().parents[3]
PACKAGE = REPO_ROOT / "common/contracts/src/truealpha_contracts"
FROZEN_MODS = (17, "c0a288f254eba1d62573c4285e16efab5721143ff71d0160f22015d28aa6563d")


def _schema_references(v: object) -> set[str]:
    if isinstance(v, dict):
        refs = {v["$ref"]} if isinstance(v.get("$ref"), str) else set()
        return refs.union(*(_schema_references(x) for x in v.values()))
    return set().union(*(_schema_references(x) for x in v)) if isinstance(v, list) else set()


def _resolve_local_reference(schema: dict[str, object], reference: str) -> object:
    assert reference.startswith("#/"), f"public schemas cannot depend on external refs: {reference}"
    cur: object = schema
    for raw in reference[2:].split("/"):
        tok = raw.replace("~1", "/").replace("~0", "~")
        assert isinstance(cur, dict) and tok in cur, f"unresolved schema ref: {reference}"
        cur = cur[tok]
    return cur


def test_every_public_pydantic_contract_has_a_closed_json_schema() -> None:
    pkgs = pkgutil.iter_modules(truealpha_contracts.__path__, "truealpha_contracts.")
    mods = sorted(i.name for i in pkgs if not i.name.endswith(".data"))
    assert not hasattr(truealpha_contracts, "__all__")
    assert (len(mods), hashlib.sha256("\n".join(mods).encode()).hexdigest()) == FROZEN_MODS
    count = 0
    for m in mods:
        for obj in vars(importlib.import_module(m)).values():
            if isinstance(obj, type) and issubclass(obj, BaseModel) and obj is not BaseModel:
                count += 1
                schema = obj.model_json_schema(mode="validation")
                for ref in _schema_references(schema):
                    _resolve_local_reference(schema, ref)
    assert count >= 50, f"Expected >= 50 Pydantic contracts, examined {count}"


def test_no_code_imports_symbols_through_the_aggregate() -> None:
    pat = re.compile(r"^[ \t]*from truealpha_contracts import (\((?:[^)]*)\)|[^\n]+)$", re.M)
    subs = {p.stem for p in PACKAGE.glob("*.py") if p.stem != "__init__"}
    offenders: list[str] = []
    for top in ("apps", "common", "tools"):
        for p in (REPO_ROOT / top).rglob("*.py"):
            if p == PACKAGE / "__init__.py" or "node_modules" in p.parts or ".venv" in p.parts:
                continue
            for m in pat.finditer(p.read_text("utf-8")):
                body = re.sub(r"#[^\n]*", "", m.group(1)).strip("()").replace("\n", " ")
                syms = [x for n in body.split(",") if (x := n.strip().split(" as ")[0]) and x not in subs]
                if syms:
                    offenders.append(f"{p.relative_to(REPO_ROOT)}: {', '.join(syms)}")
    assert not offenders, "symbols imported through aggregate __init__:\n  " + "\n  ".join(offenders)
