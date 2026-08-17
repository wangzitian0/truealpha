"""Tests for tools/mutation_reproof.py — #582.

The job itself is the real test: it breaks each declared property and requires
the guard to notice. These cover the parts that job cannot check about itself —
that the manifest still describes the code, and that a structural guard cannot
be added without a mutation.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = REPO_ROOT / "tools/mutation_reproof.py"
SPEC = importlib.util.spec_from_file_location("truealpha_mutation_reproof", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = _module
SPEC.loader.exec_module(_module)

#: A guard whose whole value is catching a class of defect statically. Adding one
#: without a mutation makes it decoration the day someone weakens it, which is
#: the thing #582 exists to prevent — so the omission fails here, in review.
STRUCTURAL_GUARDS = (
    "apps/app-web/tests/source-contracts.test.ts",
    "apps/app-web/tests/route-tree-freeze.test.ts",
    "libs/runtime/tests/test_ci_workflows.py",
    "libs/runtime/tests/test_output_invariants.py",
)


def test_every_structural_guard_has_a_mutation() -> None:
    covered = {mutation.guard for mutation in _module.load()}
    missing = [guard for guard in STRUCTURAL_GUARDS if guard not in covered]
    assert not missing, (
        f"{missing} have no declared mutation, so nothing would notice them going inert. "
        f"Add an edit to tools/mutations.json that each one must catch (#582)"
    )


def test_every_mutation_still_describes_the_code() -> None:
    """An anchor that has drifted means the mutation silently stops applying, and
    the job would report a guard as caught when it was never exercised."""
    stale = []
    for mutation in _module.load():
        source = (REPO_ROOT / mutation.file).read_text(encoding="utf-8")
        if mutation.find not in source:
            stale.append(f"{mutation.id} -> {mutation.file}")
    assert not stale, (
        f"{stale}: the anchor is gone, so the mutation no longer applies. Update it to an edit "
        f"that still breaks the property, or the guard is unproven"
    )


def test_every_mutation_targets_a_file_that_exists() -> None:
    for mutation in _module.load():
        assert (REPO_ROOT / mutation.file).exists(), f"{mutation.id} targets a missing file"
        assert (REPO_ROOT / mutation.guard).exists(), f"{mutation.id} names a missing guard"


def test_an_inert_guard_is_reported_and_fails(capsys) -> None:  # noqa: ANN001
    """The one output this job exists to produce."""
    inert = _module.Mutation(
        id="probe",
        guard="somewhere",
        file="tools/mutations.json",
        find="mutations",
        replace="mutations",  # a no-op edit: the command below cannot fail
        command=("python3", "-c", "pass"),
        cwd=".",
    )
    assert _module.run(mutations=[inert]) == 1
    assert "the guard passed with the property broken" in capsys.readouterr().err


def test_a_caught_mutation_passes(capsys) -> None:  # noqa: ANN001
    caught = _module.Mutation(
        id="probe",
        guard="somewhere",
        file="tools/mutations.json",
        find="mutations",
        replace="mutations",
        command=("python3", "-c", "raise SystemExit(1)"),
        cwd=".",
    )
    assert _module.run(mutations=[caught]) == 0
    assert "caught   probe" in capsys.readouterr().out
