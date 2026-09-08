"""Ownership guard for the shared structured-extraction primitive (#769, #70).

Same shape as `tools/check_factor_contract.py`'s freezes and
`tools/reachability_ratchet.py`'s census: a static, stdlib-only scan over every source
file (no database, no import of the scanned packages), asserting a property of the WHOLE
repository rather than one module's own behaviour.

What #769 fixed: `libs/factors/src/factors/shared/extraction.py` was a stub
(`extract_metric` raised `NotImplementedError`) while the real rule — recall enumerates
candidates, exactly one distinct value needs no judgement — was built entirely inside
`apps/data-engine/.../filing_extraction.py`: its own `RULE_SINGLE_CANDIDATE =
"rule:single-candidate:v1"` module constant and its own `select_total()` deciding the
rule. AGENTS.md ("libs/factors/shared"): "the shared structured-extraction primitive. Do
not reimplement extraction per factor." This is the standing check for that sentence: a
third module (a future segment-revenue adapter, #769 acceptance criterion 2) doing the
same thing filing_extraction.py used to do goes red here, in review, rather than shipping
unnoticed beside the primitive a second time.

Two independent shapes are checked, matching the issue's own two examples:

1. The extractor-id literal itself (`rule:single-candidate:v1`) is hardcoded ONLY where
   it is minted (the primitive) or genuinely needs it as data, not logic (a confidence
   policy table keyed by extractor identity) or documents it as the designated adapter.
2. A `select_*`-named function that is not the primitive's own rule, the one designated
   SEC-filing adapter, or the model-backed selector — i.e., a NEW module deciding
   candidate selection under a name that looks like it belongs to this problem.

Both checks are exercised against the REAL repository content (not a synthetic fixture):
`test_the_literal_guard_actually_fires_...` and `test_the_selector_name_guard_actually_
fires_...` re-run the same scan with `filing_extraction.py`'s entry removed from the
allowlist and assert it goes red — proving the guard has teeth on the code that shipped
on `main` before this PR (that file's pre-#769 shape is exactly what a shrunk allowlist
now catches), not only on hypothetical fixtures.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

#: An extractor id shaped like the deterministic rule's own identity. Deliberately scoped
#: to `rule:` (not `model:`): a model's extractor id is always built at runtime from the
#: served model name and a prompt digest (`data_engine.sources.llm.ModelSelection
#: .extractor`) — a LITERAL one would itself be exactly the kind of hardcoding this guards
#: against, and none exists in the repository today.
RULE_LITERAL_RE = re.compile(r"rule:[a-z0-9][a-z0-9-]*:v\d+")

#: Files allowed to contain the rule literal: the primitive that mints it, the one
#: designated SEC-filing adapter that names it in its own docstring and re-exports it
#: (never redefines it — see `RULE_SINGLE_CANDIDATE = extraction_primitive
#: .RULE_SINGLE_CANDIDATE` there), and the confidence-policy table that must key on the
#: extractor's exact identity (`libs/contracts` cannot import `libs/factors` — the
#: dependency direction is the other way — so this one literal cannot be a re-export).
ALLOWED_RULE_LITERAL_FILES = {
    "libs/factors/src/factors/shared/extraction.py",
    "apps/data-engine/src/data_engine/datahub/standards/filing_extraction.py",
    "libs/contracts/src/truealpha_contracts/standards.py",
}

SELECT_NAME_RE = re.compile(r"^select_[a-z_]*$")

#: Files allowed to define a `select_*` function that decides candidate selection: the
#: primitive itself, the one designated SEC-filing adapter, and the model-backed selector
#: (data_engine.sources.llm.select_headcount) that implements the `Selector` protocol the
#: primitive declares but never imports a concrete instance of.
ALLOWED_SELECTOR_FILES = {
    "libs/factors/src/factors/shared/extraction.py",
    "apps/data-engine/src/data_engine/datahub/standards/filing_extraction.py",
    "apps/data-engine/src/data_engine/sources/llm.py",
}

#: `select_*`-named functions that are not about extraction candidate selection at all,
#: reviewed and named here rather than excluded by file so an addition to this set is a
#: visible diff. `select_recapture` (data_engine/datahub/tiny_replay.py) picks which
#: CAPTURE OBLIGATION a recapture scenario replays — no candidate, no extractor, no
#: relationship to #769's scope.
KNOWN_UNRELATED_SELECT_FUNCTIONS = {"select_recapture"}


def _src_python_files() -> list[Path]:
    """Every production module under `libs/` and `apps/` — package source only: no
    tests (which legitimately reference the extractor id as fixture data or an imported
    constant, e.g. `apps/data-engine/tests/test_headcount_source_priority.py`), no
    `__pycache__`."""
    files: list[Path] = []
    for base in (REPO_ROOT / "libs", REPO_ROOT / "apps"):
        for path in base.rglob("*.py"):
            parts = path.relative_to(REPO_ROOT).parts
            if "src" not in parts or "tests" in parts or "__pycache__" in parts:
                continue
            files.append(path)
    return sorted(files)


def _scan_rule_literal(*, allowed: set[str]) -> list[str]:
    violations = []
    for path in _src_python_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel in allowed:
            continue
        if RULE_LITERAL_RE.search(path.read_text()):
            violations.append(f"{rel} hardcodes a rule:...:vN extractor literal outside libs/factors/shared")
    return violations


def _scan_selector_functions(*, allowed: set[str]) -> list[str]:
    violations = []
    for path in _src_python_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel in allowed:
            continue
        tree = ast.parse(path.read_text(), filename=rel)
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or not SELECT_NAME_RE.match(node.name):
                continue
            if node.name in KNOWN_UNRELATED_SELECT_FUNCTIONS:
                continue
            violations.append(
                f"{rel}:{node.lineno} defines {node.name}(), a candidate-selection-shaped "
                "function outside libs/factors/shared and its designated adapters"
            )
    return violations


def test_no_module_outside_the_primitive_hardcodes_the_rule_literal() -> None:
    violations = _scan_rule_literal(allowed=ALLOWED_RULE_LITERAL_FILES)
    assert not violations, "\n".join(violations)


def test_no_module_outside_the_primitive_defines_an_orphan_selector_function() -> None:
    violations = _scan_selector_functions(allowed=ALLOWED_SELECTOR_FILES)
    assert not violations, "\n".join(violations)


def test_the_literal_guard_actually_fires_against_the_real_pre_769_adapter_shape() -> None:
    """Red-proof: `filing_extraction.py` genuinely still names the literal (in its own
    docstring, documenting the rule it delegates to) — only its allowlist entry keeps the
    real scan green. Drop that one entry and the same scan, over the same repository,
    must fail; this is what `main` looked like before #769 gave the literal a single
    owner."""
    shrunk = ALLOWED_RULE_LITERAL_FILES - {"apps/data-engine/src/data_engine/datahub/standards/filing_extraction.py"}
    violations = _scan_rule_literal(allowed=shrunk)
    assert any("filing_extraction.py" in violation for violation in violations), (
        "removing filing_extraction.py's allowlist entry did not fail the scan — the guard is vacuous"
    )


def test_the_selector_guard_actually_fires_against_the_real_pre_769_adapter_shape() -> None:
    """Red-proof for the second shape: `filing_extraction.py` still defines `select_total`
    (tests import it directly and call it with raw `FilingCandidate` lists) — a name this
    guard treats as suspicious anywhere it is not the designated adapter. Drop the
    allowlist entry and the real scan must flag it."""
    shrunk = ALLOWED_SELECTOR_FILES - {"apps/data-engine/src/data_engine/datahub/standards/filing_extraction.py"}
    violations = _scan_selector_functions(allowed=shrunk)
    assert any("select_total" in violation for violation in violations), (
        "removing filing_extraction.py's allowlist entry did not fail the scan — the guard is vacuous"
    )


def test_a_brand_new_module_reimplementing_the_rule_is_caught_by_at_least_one_scan(tmp_path) -> None:
    """The concrete drift this whole guard exists for: a hypothetical THIRD adapter
    (e.g. segment revenue, #769 acceptance criterion 2) copies filing_extraction.py's
    pre-#769 shape instead of calling the primitive — its own rule constant and its own
    deciding function. Neither needs to be a real repository file to prove the scanners'
    logic catches this shape; `tmp_path` stands in for "some module outside the
    allowlist."""
    rogue = tmp_path / "segment_revenue_extraction.py"
    rogue.write_text(
        'RULE_SINGLE_CANDIDATE = "rule:single-candidate:v1"\n'
        "\n"
        "def select_segment_total(found):\n"
        "    distinct = {c.value for c in found}\n"
        "    if len(distinct) == 1:\n"
        "        return 'resolved', found[0]\n"
        "    return 'needs_model_selection', None\n"
    )
    rogue_text = rogue.read_text()
    assert RULE_LITERAL_RE.search(rogue_text), "the literal scan's own regex does not match its own target pattern"
    tree = ast.parse(rogue_text)
    name_violations = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and SELECT_NAME_RE.match(node.name)
        and node.name not in KNOWN_UNRELATED_SELECT_FUNCTIONS
    ]
    assert name_violations == ["select_segment_total"], "the selector-name scan's own regex does not match select_*"
