"""The reason registry: one entry per reason code, its kind and its class (#1851).

An outcome code says why a cell has no value or why its value is disputed. An evidence code
describes a served value. `registration` resolves a code exactly, then by its listed prefix;
`excluded:<code>` and `reason:<code>` take the entry of `<code>`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from truealpha_contracts.common import canonical_sha256, load_data_json


class ReasonKind(StrEnum):
    OUTCOME = "outcome"
    EVIDENCE = "evidence"


class ReasonClass(StrEnum):
    WORLD_ABSENCE = "world_absence"
    NOT_APPLICABLE = "not_applicable"
    PIPELINE_GAP = "pipeline_gap"
    TRANSIENT = "transient"
    DISPUTED = "disputed"


@dataclass(frozen=True)
class Registration:
    code: str
    kind: ReasonKind
    reason_class: ReasonClass | None
    tracking_issue: str | None
    disposition: str | None


_SECTION: dict[str, Any] = load_data_json("registries.v1.json")["reasons"]
#: The content hash of the registry; a report records it with the counts it made.
REGISTRY_SHA256 = canonical_sha256(_SECTION)
_DELEGATE = "class_of_suffix"


def _entry(code: str, entry: dict[str, Any]) -> Registration:
    reason_class = entry.get("class")
    return Registration(
        code=code,
        kind=ReasonKind(entry["kind"]),
        reason_class=None if reason_class is None else ReasonClass(reason_class),
        tracking_issue=entry.get("tracking_issue"),
        disposition=entry.get("disposition"),
    )


def registration(code: str) -> Registration | None:
    """The entry of `code`, or None when the registry does not list it."""
    entry = _SECTION["codes"].get(code)
    if entry is not None:
        return _entry(code, entry)
    # No listed prefix starts another (a test holds this), so at most one matches.
    prefix = next((p for p in _SECTION["prefixes"] if code.startswith(p) and len(code) > len(p)), None)
    if prefix is None:
        return None
    entry = _SECTION["prefixes"][prefix]
    if entry.get(_DELEGATE):
        inner = registration(code[len(prefix) :])
        return None if inner is None else Registration(code, inner.kind, inner.reason_class, inner.tracking_issue, None)
    return _entry(code, entry)


def is_registered(code: str) -> bool:
    return registration(code) is not None


def dispositions() -> dict[str, str]:
    """The capture obligation codes and their dispositions."""
    return {code: entry["disposition"] for code, entry in _SECTION["codes"].items() if "disposition" in entry}


def class_meanings() -> dict[ReasonClass, str]:
    return {ReasonClass(name): meaning for name, meaning in _SECTION["classes"].items()}
