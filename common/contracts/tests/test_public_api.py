"""Verify every public Pydantic contract emits a closed JSON schema."""

import importlib
import pkgutil

import pytest
import truealpha_contracts
from pydantic import BaseModel

pytestmark = pytest.mark.contract


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
    count = 0
    for m in mods:
        for obj in vars(importlib.import_module(m)).values():
            if isinstance(obj, type) and issubclass(obj, BaseModel) and obj is not BaseModel:
                count += 1
                schema = obj.model_json_schema(mode="validation")
                for ref in _schema_references(schema):
                    _resolve_local_reference(schema, ref)
    assert count >= 50, f"Expected >= 50 Pydantic contracts, examined {count}"
