"""#1010: a stable identifier has one grammar, and every contract field that takes one agrees on it.

Sixteen Python copies of the pattern lived under five names -- `_STABLE_ID`, `_STABLE_ID_PATTERN`,
`_STABLE_KEY`, `_STABLE_COORDINATE`, `_ID_PATTERN` -- plus three written inline. Eleven agreed. The
`gates` copy required a lowercase first character and the `capture_contracts` copy lowercase
throughout, so four of the seven partition keys this repository's contracts use were valid on a
`PlannedDemandCell` -- whose `partition_key` checks only that it is non-empty -- and refused by the
`CaptureCell` that demand is supposed to become.

Two guards, because a copy can drift in two ways. The source guard finds a copy by what its
character class ADMITS, not by how it is spelled, so `a-zA-Z` versus `A-Za-z`, `+-` versus `+\\-`,
a reordering or a prefix in front of it are all still a copy. The field sweep finds every contract
field whose validation accepts a stable identifier and measures its language against the shared one,
through pydantic itself, so a field that narrows the grammar is caught even if it got there without
writing a pattern literal.
"""

from __future__ import annotations

import ast
import functools
import importlib
import pathlib
import pkgutil
import re
import string
from typing import Annotated, Any

import pytest
import truealpha_contracts
from pydantic import BaseModel, Field, TypeAdapter, ValidationError
from truealpha_contracts.capture_contracts import CaptureCell, CaptureRecordEvidence
from truealpha_contracts.common import STABLE_ID_BODY, STABLE_ID_PATTERN
from truealpha_contracts.data_quality import DataDomain
from truealpha_contracts.universe import SubjectKind, SubjectRef

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]

#: Every Python package whose modules must not spell the grammar themselves.
_PYTHON_ROOTS = (
    _REPO_ROOT / "libs" / "contracts" / "src",
    _REPO_ROOT / "libs" / "factors" / "src",
    _REPO_ROOT / "apps" / "data-engine" / "src",
    _REPO_ROOT / "apps" / "llm-service" / "src",
)

#: TypeScript sources, scanned too: `documents.ts` said it "mirrors access._stable_coordinate
#: exactly" and nothing checked that.
_TYPESCRIPT_ROOT = _REPO_ROOT / "apps" / "app-web" / "src"

_THE_DEFINITION = "libs/contracts/src/truealpha_contracts/common.py"

#: Copies that cannot import the definition, each with what holds it equal instead. Anything not
#: listed here fails.
_ACROSS_A_LANGUAGE_BOUNDARY = {
    "apps/app-web/src/server/documents.ts": (
        "TypeScript cannot import the Python constant; apps/app-web/tests/stable-identifier-parity.test.ts "
        "measures it against the pattern the conformance bundle exports, per character, in CI"
    ),
}

#: The seven marks the grammar admits after its first character. A character class that admits
#: all of them AND a letter and a digit is this grammar's continuation class -- provided it also
#: refuses most punctuation OUTSIDE the grammar. The letter and digit separate it from a tokenizer
#: such as `[._:/@+\\-]`, which splits ON these marks and is #1011's concern. The refusal separates it
#: from a negated class such as `[^>]`, which admits nearly everything: the first draft of this
#: guard flagged eleven HTML-scraping patterns in `inline_xbrl.py` on exactly that.
_SIGNATURE = (*"._:/@+-", "a", "0")
_OUTSIDE = tuple(mark for mark in string.punctuation if mark not in "._:/@+-")

#: Probes that characterise a pattern of the shape `^[first][rest]*$` completely: every printable
#: ASCII character, and three that are not ASCII at all, each tried as the first character and as
#: the second. Two patterns of that shape that agree on all of these accept the same strings.
_PROBE_CHARACTERS = (*string.printable, "é", "Ａ", "٣")


def _character_classes(text: str) -> list[str]:
    """Every bracket expression in `text`. None of the patterns here contain an escaped `]`."""
    return re.findall(r"\[(?:\\.|[^\]\\\n])*\]", text)


def _is_the_grammar(character_class: str) -> bool:
    try:
        compiled = re.compile(character_class)
    except re.error:
        return False
    admits_the_signature = all(compiled.fullmatch(mark) for mark in _SIGNATURE)
    # More than half, not all: a copy that drifted by admitting one extra mark is still a copy.
    refuses_what_it_should = sum(compiled.fullmatch(mark) is None for mark in _OUTSIDE) > len(_OUTSIDE) / 2
    return admits_the_signature and refuses_what_it_should


def _spells_the_grammar(text: str) -> bool:
    return any(_is_the_grammar(character_class) for character_class in _character_classes(text))


def _python_constants(path: pathlib.Path) -> list[str]:
    """Every string literal in the module, including the literal parts of an f-string. Scanning
    constants rather than source text keeps comments out: a comment explaining history may quote
    the old pattern without being one."""
    return [
        node.value
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


@functools.cache
def _copies() -> tuple[str, ...]:
    found: list[str] = []
    for root in (*_PYTHON_ROOTS, _TYPESCRIPT_ROOT):
        # `rglob` on a missing path yields nothing and raises nothing, so a moved package would
        # leave this reporting the remaining roots as clean.
        assert root.is_dir(), f"source root {root} is missing, so this guard is not scanning it"
    for root in _PYTHON_ROOTS:
        for path in sorted(root.rglob("*.py")):
            if any(_spells_the_grammar(constant) for constant in _python_constants(path)):
                found.append(path.relative_to(_REPO_ROOT).as_posix())
    for path in sorted(_TYPESCRIPT_ROOT.rglob("*.ts*")):
        if _spells_the_grammar(path.read_text(encoding="utf-8")):
            found.append(path.relative_to(_REPO_ROOT).as_posix())
    return tuple(found)


def test_the_grammar_is_spelled_once() -> None:
    """Seventeen spellings on 2026-09-23, two of them narrower than the rest. Every Python module
    now imports `STABLE_ID_PATTERN` or `STABLE_ID_BODY`; a new copy, under any name and in any
    spelling, fails here."""
    copies = _copies()
    unexpected = [path for path in copies if path != _THE_DEFINITION and path not in _ACROSS_A_LANGUAGE_BOUNDARY]
    assert not unexpected, (
        f"the stable-identifier grammar is defined in {_THE_DEFINITION} and nowhere else; "
        f"import STABLE_ID_PATTERN (or STABLE_ID_BODY to embed it) instead: {unexpected}"
    )
    assert _THE_DEFINITION in copies, (
        f"the guard did not find the definition itself, so it is asserting nothing; found {copies}"
    )


def test_the_boundary_list_has_no_entry_that_is_gone() -> None:
    """An exemption that outlives what it excuses turns into permission."""
    stale = sorted(set(_ACROSS_A_LANGUAGE_BOUNDARY) - set(_copies()))
    assert not stale, f"these no longer spell the grammar and must leave the list: {stale}"


@pytest.mark.parametrize(
    "copy",
    [
        r"^[a-zA-Z0-9][a-zA-Z0-9._:/@+-]*$",  # the second spelling eleven sites used
        r"^[0-9A-Za-z][-+@/:_.0-9A-Za-z]*$",  # reordered
        r"^[a-z0-9][a-z0-9._:/@+\-]*$",  # the narrowed capture_contracts copy
        r"^raw(?:\.[a-z][a-z0-9_]*)+:[A-Za-z0-9][A-Za-z0-9._:/@+\-]*$",  # embedded behind a prefix
        r"[A-Za-z0-9][A-Za-z0-9._:/@+\-]*",  # unanchored, as capture_control wrote it
        r"^[A-Za-z0-9][A-Za-z0-9._:/@+~\-]*$",  # drifted by one extra mark
    ],
)
def test_a_copy_is_recognised_however_it_is_spelled(copy: str) -> None:
    """Each of these was, or is one edit away from, a real copy. The narrowed one matters most:
    a guard that recognised only the majority reading would have passed the two copies this issue
    is about."""
    assert _spells_the_grammar(copy)


@pytest.mark.parametrize(
    "not_a_copy",
    [
        r"[._:/@+\-]",  # a tokenizer: splits on the marks, admits no letter (#1011)
        r"[^a-z0-9]+",  # a negated tokenizer
        r"<[^>]+>",  # a negated class admits the signature and nearly everything else
        r"\s*([^<\s]+)\s*<",
        r"^[a-z][a-z0-9._:/-]*$",  # gates' signature ids, deliberately lowercase and without @ +
        r"^[0-9A-Za-z][0-9A-Za-z._/-]{0,199}$",  # release refs
        r"^[A-Za-z_][A-Za-z0-9_.-]*$",  # field paths
    ],
)
def test_a_neighbouring_pattern_is_not_called_a_copy(not_a_copy: str) -> None:
    """A guard that fires on a tokenizer or on a different identifier family gets turned off."""
    assert not _spells_the_grammar(not_a_copy)


# -- The field sweep ------------------------------------------------------------------------------

_SHARED = TypeAdapter(Annotated[str, Field(pattern=STABLE_ID_PATTERN)])

#: A value every stable-identifier field accepts under either narrow reading too. A field whose
#: validation takes this is a stable-identifier field, whatever its pattern is spelled like.
_EVERY_MARK = "a.b_c:d/e@f+g-h0"


def _accepts(adapter: TypeAdapter[Any], value: str) -> bool:
    try:
        adapter.validate_python(value)
    except ValidationError:
        return False
    return True


def _contract_models() -> list[type[BaseModel]]:
    models: list[type[BaseModel]] = []
    for info in pkgutil.iter_modules(truealpha_contracts.__path__):
        module = importlib.import_module(f"truealpha_contracts.{info.name}")
        for value in vars(module).values():
            if isinstance(value, type) and issubclass(value, BaseModel) and value.__module__ == module.__name__:
                models.append(value)
    return models


@functools.cache
def _stable_identifier_fields() -> dict[str, TypeAdapter[Any]]:
    """Every contract field declared with a pattern that accepts a BARE stable identifier,
    validated by pydantic through the field's own metadata -- not by re-reading its pattern with
    `re`, which would measure a replica of the check rather than the check.

    `capture_contracts.CaptureRecordEvidence.raw_id` and `.normalized_id` embed the grammar behind
    a mandatory literal prefix (`raw.sec:...`, `normalized-record-type:...`) rather than accepting
    it bare, so a lone `"a"` correctly fails them and would show up here as a false disagreement
    with `STABLE_ID_PATTERN`. `test_a_prefixed_identifier_embeds_the_same_grammar_after_its_prefix`
    covers those two directly instead.
    """
    fields: dict[str, TypeAdapter[Any]] = {}
    for model in _contract_models():
        for name, info in model.model_fields.items():
            if not any(isinstance(getattr(meta, "pattern", None), str) for meta in info.metadata):
                continue
            adapter = TypeAdapter(Annotated[(str, *info.metadata)])
            if _accepts(adapter, _EVERY_MARK) and _accepts(adapter, "a"):
                fields[f"{model.__module__}.{model.__qualname__}.{name}"] = adapter
    return fields


def test_the_sweep_reaches_the_fields_that_disagreed() -> None:
    """Named anchors, so the sweep cannot go quiet by selecting nothing: the two narrow copies and
    one field from the majority must all be in it."""
    fields = _stable_identifier_fields()
    for anchor in (
        "truealpha_contracts.capture_contracts.CaptureCell.partition_key",
        "truealpha_contracts.gates.ComparisonCriterion.metric_id",
        "truealpha_contracts.usage.DataRequirement.valid_period_rule_id",
    ):
        assert anchor in fields, f"the sweep did not select {anchor}; it selected {len(fields)} fields"


def test_every_stable_identifier_field_accepts_exactly_the_shared_grammar() -> None:
    """Measured per character, first position and second, over printable ASCII and three
    non-ASCII probes. For a pattern of the shape `^[first][rest]*$` that is its whole language."""
    disagreements: dict[str, list[str]] = {}
    for where, adapter in _stable_identifier_fields().items():
        for character in _PROBE_CHARACTERS:
            for probe in (character, f"a{character}"):
                if _accepts(adapter, probe) != _accepts(_SHARED, probe):
                    disagreements.setdefault(where, []).append(probe)
    assert not disagreements, (
        "these fields accept a different set of stable identifiers from STABLE_ID_PATTERN "
        f"(field: probes it disagrees on): {disagreements}"
    )


# -- The symptom ----------------------------------------------------------------------------------


@functools.cache
def _partition_keys_in_use() -> tuple[str, ...]:
    """Every literal `partition_key=` this repository passes, in source, tests and the conformance
    export -- measured, so a new form is covered the day someone writes it."""
    roots = (
        *_PYTHON_ROOTS,
        _REPO_ROOT / "libs" / "contracts" / "tests",
        _REPO_ROOT / "libs" / "contracts" / "conformance",
    )
    keys: set[str] = set()
    for root in roots:
        for path in root.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if (
                    isinstance(node, ast.keyword)
                    and node.arg == "partition_key"
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)
                ):
                    keys.add(node.value.value)
    return tuple(sorted(keys))


def test_the_partition_keys_in_use_include_the_forms_that_were_refused() -> None:
    """Without an uppercase key the next test could pass against the narrowed copy."""
    keys = _partition_keys_in_use()
    assert any(key != key.lower() for key in keys), f"no uppercase partition key in use, found {keys}"


@pytest.mark.parametrize("partition_key", _partition_keys_in_use())
def test_every_partition_key_in_use_can_become_a_capture_cell(partition_key: str) -> None:
    """The divergence as a caller meets it. `2025FY`, `2026FY`, `2026Q2` and `FY2025` were valid on
    every demand-side model -- `PlannedDemandCell`, `SnapshotDemandCell`, `SourceCoverageEntry` check
    only that a partition key is non-empty -- and refused here."""
    cell = CaptureCell(
        subject=SubjectRef(kind=SubjectKind.ISSUER, id="issuer:alphabet"),
        domain=DataDomain.FINANCIAL_FACTS,
        partition_key=partition_key,
        capture_requirement_id="capture-requirement:" + "0" * 64,
        applicability="required",
        status="missing",
    )
    assert cell.partition_key == partition_key


@pytest.mark.parametrize("field", ["raw_id", "normalized_id"])
def test_a_prefixed_identifier_embeds_the_same_grammar_after_its_prefix(field: str) -> None:
    """`raw_id` and `normalized_id` put a namespace in front of the grammar. They embedded the
    majority reading while every other field in the same module used the lowercase one; they now
    embed `STABLE_ID_BODY`, and after the prefix they must accept exactly what a bare field does."""
    adapter = TypeAdapter(Annotated[(str, *CaptureRecordEvidence.model_fields[field].metadata)])
    prefix = "raw.sec:" if field == "raw_id" else "normalized:"
    assert _accepts(adapter, prefix + "CIK0000320193"), "the prefixed field refuses an uppercase identifier"
    disagreements = [
        probe
        for character in _PROBE_CHARACTERS
        for probe in (character, f"a{character}")
        if _accepts(adapter, prefix + probe) != _accepts(_SHARED, probe)
    ]
    assert not disagreements, f"{field} disagrees with STABLE_ID_PATTERN after its prefix on {disagreements}"


def test_the_embedding_form_is_the_same_grammar() -> None:
    """`STABLE_ID_BODY` is what a prefixed pattern embeds and `STABLE_ID_PATTERN` is it anchored;
    if the two drift, prefixed ids and bare ids stop agreeing again."""
    assert STABLE_ID_PATTERN == f"^{STABLE_ID_BODY}$"


def test_a_value_the_shared_grammar_refuses_is_refused_where_it_is_embedded() -> None:
    """The embedded form still refuses what the bare one refuses -- widening one did not loosen
    the other past the grammar."""
    cell_kwargs: dict[str, Any] = {
        "subject": SubjectRef(kind=SubjectKind.ISSUER, id="issuer:alphabet"),
        "domain": DataDomain.FINANCIAL_FACTS,
        "capture_requirement_id": "capture-requirement:" + "0" * 64,
        "applicability": "required",
        "status": "missing",
    }
    for refused in ("-leading-mark", "has space", ""):
        with pytest.raises(ValidationError):
            CaptureCell(partition_key=refused, **cell_kwargs)
