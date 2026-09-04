import hashlib
import re
from pathlib import Path

import truealpha_contracts
from pydantic import BaseModel


def test_public_api_exports_are_unique_and_resolvable() -> None:
    exported = truealpha_contracts.__all__

    assert len(exported) == len(set(exported))
    assert not [name for name in exported if not hasattr(truealpha_contracts, name)]


def _schema_references(value: object) -> set[str]:
    if isinstance(value, dict):
        references = {value["$ref"]} if isinstance(value.get("$ref"), str) else set()
        return references.union(*(_schema_references(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(_schema_references(item) for item in value))
    return set()


def _resolve_local_reference(schema: dict[str, object], reference: str) -> object:
    assert reference.startswith("#/"), f"public schemas cannot depend on external refs: {reference}"
    current: object = schema
    for raw_token in reference[2:].split("/"):
        token = raw_token.replace("~1", "/").replace("~0", "~")
        assert isinstance(current, dict) and token in current, f"unresolved schema ref: {reference}"
        current = current[token]
    return current


def test_every_public_pydantic_contract_has_a_closed_json_schema() -> None:
    for name in truealpha_contracts.__all__:
        contract = getattr(truealpha_contracts, name)
        if not isinstance(contract, type) or not issubclass(contract, BaseModel):
            continue
        schema = contract.model_json_schema(mode="validation")
        for reference in _schema_references(schema):
            _resolve_local_reference(schema, reference)


# --- #731: the aggregate is frozen ------------------------------------------------------

#: sha256 over the sorted `__all__` at the moment the aggregate was frozen. Adding a name
#: changes it; so does removing one, which is allowed only together with the symbol.
FROZEN_EXPORT_COUNT = 504
FROZEN_EXPORT_SHA256 = "1b172b3d61c75c1c2e062416143984550adcfd7963acba792b6a4d1e5545b3e2"

REPO_ROOT = Path(__file__).resolve().parents[3]
PACKAGE = REPO_ROOT / "libs/contracts/src/truealpha_contracts"


def test_the_aggregate_export_list_is_frozen() -> None:
    """No new name enters `truealpha_contracts.__all__`. New contracts are imported from
    their submodule; the aggregate was edited by every lane 23 times in sixty days."""
    exported = sorted(truealpha_contracts.__all__)
    digest = hashlib.sha256("\n".join(exported).encode()).hexdigest()
    assert (len(exported), digest) == (FROZEN_EXPORT_COUNT, FROZEN_EXPORT_SHA256), (
        f"truealpha_contracts.__all__ changed ({len(exported)} names, sha256 {digest[:12]}…): the aggregate is "
        f"frozen (#731). Import the new contract from its submodule instead of adding it here; if a name was "
        f"deliberately deleted with its symbol, update FROZEN_EXPORT_COUNT/SHA256 in the same change"
    )


def _submodules() -> set[str]:
    return {path.stem for path in PACKAGE.glob("*.py") if path.stem != "__init__"}


def test_no_code_imports_symbols_through_the_aggregate() -> None:
    """`from truealpha_contracts import Fact` couples every importer to the frozen
    aggregate; `from truealpha_contracts.models import Fact` names the owner. Importing a
    *submodule* through the package (`from truealpha_contracts import topt_read`) is
    fine and stays allowed."""
    pattern = re.compile(r"^from truealpha_contracts import (\((?:[^)]*)\)|[^\n]+)$", re.M)
    submodules = _submodules()
    offenders: list[str] = []
    for top in ("apps", "libs", "tools"):
        for path in (REPO_ROOT / top).rglob("*.py"):
            if path == PACKAGE / "__init__.py" or "node_modules" in path.parts or ".venv" in path.parts:
                continue
            for match in pattern.finditer(path.read_text()):
                imported = [
                    n.strip().split(" as ")[0] for n in match.group(1).strip("()").replace("\n", " ").split(",")
                ]
                symbols = [n for n in imported if n and n not in submodules]
                if symbols:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}: {', '.join(symbols)}")
    assert not offenders, (
        "symbols imported through the frozen aggregate (#731); import them from their submodule:\n  "
        + "\n  ".join(offenders)
    )
