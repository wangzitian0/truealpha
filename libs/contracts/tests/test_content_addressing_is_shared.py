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
import functools
import pathlib

import pytest
from pydantic import BaseModel, ConfigDict, Field, model_validator
from truealpha_contracts.common import canonical_sha256, identify

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]

#: Every package whose modules must not define their own content-addressing wrapper.
_SOURCE_ROOTS = (
    _REPO_ROOT / "libs" / "contracts" / "src",
    _REPO_ROOT / "libs" / "factors" / "src",
    _REPO_ROOT / "apps" / "data-engine" / "src",
    _REPO_ROOT / "apps" / "llm-service" / "src",
)

#: The one definition this test exists to protect, as a repository-relative path so that a
#: second function named `identify`, in any other module, is a copy and not the original.
_THE_DEFINITION = "libs/contracts/src/truealpha_contracts/common.py.identify"

#: Definitions that stamp a content address but are NOT `identify` under another name, each
#: with the reason it cannot simply call it. Every entry is a known cost, not an exemption:
#: #997 tracks them, and anything not listed here fails.
_NOT_YET_MERGED = {
    # A different serializer, not a different style: `mode="python"` through a local
    # normalizer that renders datetime, Decimal, timedelta and set differently from
    # `mode="json"`. Its digests are therefore not the ones `identify` computes, and merging
    # it would change ids already minted under this module's rule.
    "libs/contracts/src/truealpha_contracts/policy_bundle.py._content_address": (
        "hashes a differently normalized payload"
    ),
    # A second concept: the id is hashed over a DECLARED SUBSET of fields (a natural key)
    # while the hash covers the whole payload. `identify` has no such grain, so these cannot
    # call it -- but they duplicate each OTHER, in two variants that differ in whether the
    # identity payload is wrapped in a {"kind", "identity"} envelope. Merging them changes
    # minted ids, so it is its own change.
    "libs/contracts/src/truealpha_contracts/capture_control.py._freeze": "identity grain, no envelope",
    "libs/contracts/src/truealpha_contracts/capture_control.py._freeze_wrapped": "identity grain, enveloped",
    "libs/contracts/src/truealpha_contracts/datahub.py._freeze_identity": "identity grain, enveloped",
    "libs/contracts/src/truealpha_contracts/reconciliation.py._freeze_content": "identity grain, enveloped",
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


def _stamps_a_content_address(node: ast.FunctionDef) -> bool:
    """Whether this function hashes content, builds an id from it and writes the id back.

    Both call forms count, because they are the same call: `canonical_sha256(...)` after a
    from-import and `common.canonical_sha256(...)` after a module import. Matching only the
    first left the attribute form as a way to write a copy the guard could not see -- the
    same hole one hop over as matching a bare function name (#1000 review).

    The f-string is what separates stamping from checking: every wrapper builds
    `f"{prefix}:{digest}"`. Without it a validator that merely compares a stored hash and
    sorts a tuple through `object.__setattr__` is swept up as a copy, which
    `catalog.sort_and_validate` was.
    """
    called = {
        child.func.id if isinstance(child.func, ast.Name) else child.func.attr
        for child in ast.walk(node)
        if isinstance(child, ast.Call) and isinstance(child.func, (ast.Name, ast.Attribute))
    }
    return "canonical_sha256" in called and "__setattr__" in called and _writes_an_interpolated_id(node)


def _writes_an_interpolated_id(node: ast.FunctionDef) -> bool:
    """Whether an f-string this function builds is what it writes through `object.__setattr__`.

    "Contains an f-string anywhere" was too loose: a function that hashes, writes a field and
    formats an ERROR MESSAGE with an f-string matched it, so the guard could fail CI on
    something that stamps nothing (#1000 review). What every wrapper actually does is build
    `f"{prefix}:{digest}"` and write that -- sometimes inline, more often through a local. Both
    forms count; an f-string that only ever reaches a `raise` does not.
    """
    interpolated_names = {
        target.id
        for child in ast.walk(node)
        if isinstance(child, ast.Assign) and isinstance(child.value, ast.JoinedStr)
        for target in child.targets
        if isinstance(target, ast.Name)
    }
    for child in ast.walk(node):
        if not (isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)):
            continue
        if child.func.attr != "__setattr__":
            continue
        written = child.args[-1] if child.args else None
        if isinstance(written, ast.JoinedStr):
            return True
        if isinstance(written, ast.Name) and written.id in interpolated_names:
            return True
    return False


@functools.cache
def _wrapper_definitions() -> tuple[str, ...]:
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
        # `rglob` on a path that does not exist yields nothing and raises nothing, so a moved
        # or renamed package would leave this guard scanning three roots and reporting the
        # fourth as clean.
        assert root.is_dir(), f"source root {root} is missing, so this guard is not scanning it"
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in tree.body:
                if not isinstance(node, ast.FunctionDef):
                    continue
                if _stamps_a_content_address(node):
                    found.append(f"{path.relative_to(_REPO_ROOT).as_posix()}.{node.name}")
    # A tuple, because the result is cached and a caller must not be able to mutate what the
    # next caller sees.
    return tuple(found)


def test_only_one_module_defines_content_addressing() -> None:
    """Ten modules defined this wrapper under three names on 2026-09-23 and all ten now call
    the shared one. What remains is listed above with the reason it cannot, so a new copy --
    under any name, in any of the four source roots -- fails here."""
    definitions = _wrapper_definitions()
    unexpected = [name for name in definitions if name != _THE_DEFINITION and name not in _NOT_YET_MERGED]
    assert not unexpected, (
        "content addressing must be defined once, in truealpha_contracts.common.identify; "
        f"these define their own: {unexpected}"
    )
    assert _THE_DEFINITION in definitions, (
        f"the guard did not find the shared definition itself, so it is asserting nothing; it found {definitions}"
    )


def test_the_allowlist_has_no_entry_that_has_already_been_merged() -> None:
    """An allowlist that outlives what it excuses turns into permission. Each entry must still
    name a real definition; one that has been merged away has to leave the list."""
    definitions = set(_wrapper_definitions())
    stale = sorted(set(_NOT_YET_MERGED) - definitions)
    assert not stale, f"these are no longer defined and must leave the allowlist: {stale}"


def test_the_guard_identifies_a_definition_by_path_and_not_by_name() -> None:
    """A copy is a copy even when it borrows the shared function's name. Matching on the bare
    name let `usage.identify` pass while `usage._stamp_it` failed, which is the wrong way
    round -- the closer the copy, the more it looked compliant. Every key the guard compares
    is a repository-relative path, and this refuses a regression to name matching."""
    compared = [_THE_DEFINITION, *_NOT_YET_MERGED, *_wrapper_definitions()]
    bare = [name for name in compared if "/" not in name]
    assert not bare, f"these are matched by name, so a copy under the same name passes: {bare}"


_BOTH_CALL_FORMS = {
    "from-import": "digest = canonical_sha256(payload)",
    "module attribute": "digest = common.canonical_sha256(payload)",
}


@pytest.mark.parametrize("form", sorted(_BOTH_CALL_FORMS))
def test_a_copy_is_seen_through_either_call_form(form: str) -> None:
    """Confirmed against the unfixed predicate: the module-attribute form returned False,
    because its set of `ast.Name` calls is empty. A copy could therefore evade the guard by
    importing the module instead of the function."""
    source = f"""
def a_copy(model, *, id_field, prefix):
    payload = model.model_dump(mode="json")
    {_BOTH_CALL_FORMS[form]}
    object.__setattr__(model, "content_sha256", digest)
    object.__setattr__(model, id_field, f"{{prefix}}:{{digest}}")
"""
    node = ast.parse(source).body[0]
    assert isinstance(node, ast.FunctionDef)
    assert _stamps_a_content_address(node), f"the {form} call form evades the guard"


def test_a_validator_that_only_checks_a_hash_is_not_called_a_copy() -> None:
    """`catalog.sort_and_validate` compares a stored digest and sorts a tuple through
    `object.__setattr__`. It stamps nothing, and a guard that swept it up would have to be
    narrowed by name, which is how allowlists start."""
    source = """
def only_checks(self):
    parameters = tuple(sorted(self.parameters, key=lambda item: item.name))
    if canonical_sha256(self.decoded) != self.expected_sha256:
        raise ValueError("parameters do not match")
    object.__setattr__(self, "parameters", parameters)
"""
    node = ast.parse(source).body[0]
    assert isinstance(node, ast.FunctionDef)
    assert not _stamps_a_content_address(node)


def test_an_f_string_used_only_for_an_error_message_is_not_a_stamp() -> None:
    """The looser predicate -- any f-string anywhere -- matched this, so the guard could have
    failed CI on a function that stamps nothing. A guard that fires wrongly is a guard someone
    turns off."""
    source = """
def only_reports(self, model, *, id_field):
    digest = canonical_sha256(model.model_dump(mode="json"))
    if digest != self.expected:
        raise ValueError(f"{id_field} does not match {digest}")
    object.__setattr__(self, "checked", True)
"""
    node = ast.parse(source).body[0]
    assert isinstance(node, ast.FunctionDef)
    assert any(isinstance(child, ast.JoinedStr) for child in ast.walk(node)), (
        "this fixture must contain an f-string, or it is not testing the distinction"
    )
    assert not _stamps_a_content_address(node)


@pytest.mark.parametrize("written", ["expected_id", 'f"{prefix}:{digest}"'])
def test_an_id_is_recognised_whether_it_is_written_inline_or_through_a_local(written: str) -> None:
    """Every merged copy assigned the f-string first and wrote the local; the copies planted to
    reverse-verify the guard wrote it inline. Both are the same stamp."""
    source = f"""
def a_copy(model, *, id_field, prefix):
    digest = canonical_sha256(model.model_dump(mode="json"))
    expected_id = f"{{prefix}}:{{digest}}"
    object.__setattr__(model, "content_sha256", digest)
    object.__setattr__(model, id_field, {written})
"""
    node = ast.parse(source).body[0]
    assert isinstance(node, ast.FunctionDef)
    assert _stamps_a_content_address(node)
