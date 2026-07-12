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
