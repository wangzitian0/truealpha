"""#997: content addressing is defined once, and it refuses what the copies disagreed about.

Nine copies of the same wrapper lived across the contract modules -- two of them named
`_content_address`, which is why no search for `_identify` found them -- while
`canonical_sha256`, the primitive they all called, had been shared from the start. Seven
accepted a supplied identity value that was falsy but not empty and two refused it, so the
same object was valid or invalid depending on which module happened to define it.

The guard below is written over the shape rather than over the nine names that existed on
2026-09-23: a tenth copy under an eleventh name still fails it.
"""

from __future__ import annotations

import ast
import pathlib

import pytest
from pydantic import BaseModel, ConfigDict, Field, model_validator
from truealpha_contracts.common import canonical_sha256, identify

#: Every package whose modules must not define their own content-addressing wrapper.
_SOURCE_ROOTS = (
    pathlib.Path(__file__).resolve().parents[3] / "libs" / "contracts" / "src",
    pathlib.Path(__file__).resolve().parents[3] / "libs" / "factors" / "src",
    pathlib.Path(__file__).resolve().parents[3] / "apps" / "data-engine" / "src",
    pathlib.Path(__file__).resolve().parents[3] / "apps" / "llm-service" / "src",
)

#: `common.identify` itself, which is the one definition this test exists to protect.
_THE_DEFINITION = "identify"

#: Definitions that stamp a content address but are NOT `identify` under another name, each
#: with the reason it cannot simply call it. Every entry is a known cost, not an exemption:
#: #997 tracks them, and anything not listed here fails.
_NOT_YET_MERGED = {
    # A different serializer, not a different style: `mode="python"` through a local
    # normalizer that renders datetime, Decimal, timedelta and set differently from
    # `mode="json"`. Its digests are therefore not the ones `identify` computes, and merging
    # it would change ids already minted under this module's rule.
    "policy_bundle.py._content_address": "hashes a differently normalized payload",
    # A second concept: the id is hashed over a DECLARED SUBSET of fields (a natural key)
    # while the hash covers the whole payload. `identify` has no such grain, so these cannot
    # call it -- but they duplicate each OTHER, in two variants that differ in whether the
    # identity payload is wrapped in a {"kind", "identity"} envelope. Merging them changes
    # minted ids, so it is its own change.
    "capture_control.py._freeze": "identity grain, no envelope",
    "capture_control.py._freeze_wrapped": "identity grain, enveloped",
    "datahub.py._freeze_identity": "identity grain, enveloped",
    "reconciliation.py._freeze_content": "identity grain, enveloped",
}


class _Addressed(BaseModel):
    """A frozen model shaped like the contracts this wrapper serves."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    thing_id: str = Field(default="", pattern=r"^(?:|thing:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    subject: str

    @model_validator(mode="after")
    def _address(self):
        identify(self, id_field="thing_id", prefix="thing")
        return self


def _expected(subject: str) -> tuple[str, str]:
    digest = canonical_sha256({"subject": subject})
    return f"thing:{digest}", digest


def test_an_unsupplied_identity_is_stamped_from_the_content() -> None:
    model = _Addressed(subject="one")
    expected_id, expected_hash = _expected("one")
    assert (model.thing_id, model.content_sha256) == (expected_id, expected_hash)


def test_a_supplied_identity_that_matches_is_accepted() -> None:
    expected_id, expected_hash = _expected("two")
    model = _Addressed(subject="two", thing_id=expected_id, content_sha256=expected_hash)
    assert (model.thing_id, model.content_sha256) == (expected_id, expected_hash)


@pytest.mark.parametrize("field", ["thing_id", "content_sha256"])
def test_a_supplied_identity_that_disagrees_names_which_field_is_wrong(field: str) -> None:
    """The two checks stay separate: seven of the nine copies had them that way, and a caller
    told only "identity does not match" cannot tell a stale hash from a stale id."""
    expected_id, expected_hash = _expected("three")
    wrong = {"thing_id": "thing:" + "0" * 64, "content_sha256": "0" * 64}[field]
    with pytest.raises(ValueError, match=f"{field} does not match canonical content"):
        _Addressed(subject="three", **{field: wrong})


@pytest.mark.parametrize("supplied", [None, 0, False, [], ()])
def test_a_falsy_but_not_empty_identity_is_refused(supplied: object) -> None:
    """The divergence this issue is about. Seven copies guarded with `if supplied and
    supplied != expected`, so anything falsy skipped the check and was silently overwritten;
    two used a membership test and refused it. The shared definition keeps the stricter
    reading -- "not supplied" is the empty string and nothing else.

    Pydantic refuses these at the field before the validator runs, so they are placed the way
    the wrapper itself writes a frozen field. That is the only route by which a caller can
    reach the check, and it is the route the nine copies disagreed on.
    """
    model = _Addressed(subject="four")
    object.__setattr__(model, "content_sha256", supplied)
    with pytest.raises(ValueError, match="content_sha256 does not match canonical content"):
        identify(model, id_field="thing_id", prefix="thing")


def test_an_unhashable_supplied_value_is_refused_rather_than_raising_typeerror() -> None:
    """Membership is tested against a tuple, not a set: a set test would raise TypeError from
    the guard itself, which reads as a crash rather than as a refused object."""
    model = _Addressed(subject="five")
    object.__setattr__(model, "content_sha256", {"not": "a hash"})
    with pytest.raises(ValueError, match="content_sha256 does not match canonical content"):
        identify(model, id_field="thing_id", prefix="thing")


def _wrapper_definitions() -> list[str]:
    """Every MODULE-LEVEL function that stamps a model with `{prefix}:{canonical_sha256(...)}`,
    found by shape: it calls `canonical_sha256` and writes a field through `object.__setattr__`
    while building an id. Name, argument names and module are irrelevant, which is the point --
    `_content_address` and `_bind_content_address` both evaded a search for `_identify`.

    Module level is the scope on purpose. A reusable wrapper is the thing that must be shared,
    and sharing it is what this change did. A model's own `@model_validator` may still stamp
    inline after doing model-specific work -- `release.ReleaseManifest.freeze_and_identify`
    sorts four collections before it addresses itself, over `manifest_sha256` rather than
    `content_sha256`. Those are inventoried on #997 with what each would have to pass to
    `identify`; they are not what this guard is for, and pretending otherwise would make it
    fire on every model that has a validator."""
    found: list[str] = []
    for root in _SOURCE_ROOTS:
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in tree.body:
                if not isinstance(node, ast.FunctionDef):
                    continue
                calls = {
                    child.func.id
                    for child in ast.walk(node)
                    if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
                }
                attribute_calls = {
                    child.func.attr
                    for child in ast.walk(node)
                    if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                }
                # The f-string is what separates stamping from checking: every wrapper builds
                # `f"{prefix}:{digest}"`. Without it, a validator that merely compares a stored
                # hash and sorts a tuple through `object.__setattr__` is swept up as a copy
                # (`catalog.sort_and_validate` was).
                builds_an_id = any(isinstance(child, ast.JoinedStr) for child in ast.walk(node))
                if "canonical_sha256" in calls and "__setattr__" in attribute_calls and builds_an_id:
                    found.append(f"{path.name}.{node.name}")
    return found


def test_only_one_module_defines_content_addressing() -> None:
    """Ten modules defined this wrapper under three names on 2026-09-23 and all ten now call
    the shared one. What remains is listed above with the reason it cannot, so a new copy --
    under any name, in any of the four source roots -- fails here."""
    definitions = _wrapper_definitions()
    unexpected = [
        name for name in definitions if not name.endswith(f".{_THE_DEFINITION}") and name not in _NOT_YET_MERGED
    ]
    assert not unexpected, (
        "content addressing must be defined once, in truealpha_contracts.common.identify; "
        f"these define their own: {unexpected}"
    )
    assert definitions, "the guard found no definition at all, so it is asserting nothing"


def test_the_allowlist_has_no_entry_that_has_already_been_merged() -> None:
    """An allowlist that outlives what it excuses turns into permission. Each entry must still
    name a real definition; one that has been merged away has to leave the list."""
    definitions = set(_wrapper_definitions())
    stale = sorted(set(_NOT_YET_MERGED) - definitions)
    assert not stale, f"these are no longer defined and must leave the allowlist: {stale}"
