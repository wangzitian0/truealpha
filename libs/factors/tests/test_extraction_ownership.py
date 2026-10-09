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

Three independent shapes are checked. The first two match the issue's own two examples:

1. The extractor-id literal itself (`rule:single-candidate:v1`) is hardcoded ONLY where
   it is minted (the primitive) or genuinely needs it as data, not logic (a confidence
   policy table keyed by extractor identity) or documents it as the designated adapter.
2. A `select_*`-named function that is not the primitive's own rule, the one designated
   SEC-filing adapter, or the model-backed selector — i.e., a NEW module deciding
   candidate selection under a name that looks like it belongs to this problem.

3. STRUCTURAL (#1130): a module that is named `*_extraction.py`, or that defines a
   function named `extract_*`, must import `factors.shared.extraction` and reference one
   of its public functions. The first two shapes match text. This shape matches intent: a
   module that extracts values and never calls the primitive is a second primitive,
   whatever it names its constants and functions. `supply_chain_extraction.py` passed both
   text shapes and still owns its own regex and candidate class. It sits in a shrink-only
   debt set until #773 D8 removes it. The scan covers `EXTRACTION_SCAN_ROOTS` only (#1144).

Checks 1 and 2 are exercised against the REAL repository content (not a synthetic fixture):
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

import pytest

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
#: visible diff. The set is empty. Its only member, `select_recapture`, went with the replay
#: harness in #1061.
KNOWN_UNRELATED_SELECT_FUNCTIONS: set[str] = set()

#: The roots the structural guard governs: metric extraction from filings and source documents.
#: The llm-service is a transport layer (AGENTS.md: no computation outside `libs/factors`). Its
#: `extract_*` helpers read identifiers from a user query. They extract no metric, so the guard
#: does not scan that service (#1144). Add a root only when it holds metric extraction code.
EXTRACTION_SCAN_ROOTS = ("libs/factors/src", "apps/data-engine/src")

PRIMITIVE_FILE = "libs/factors/src/factors/shared/extraction.py"
PRIMITIVE_MODULE = "factors.shared.extraction"
PRIMITIVE_PACKAGE = "factors.shared"
EXTRACTION_FILE_SUFFIX = "_extraction.py"
EXTRACT_NAME_RE = re.compile(r"^extract_[a-z_]*$")

SUPPLY_CHAIN_MODULE = "apps/data-engine/src/data_engine/datahub/standards/supply_chain_extraction.py"
FILING_MODULE = "apps/data-engine/src/data_engine/datahub/standards/filing_extraction.py"
SEGMENT_MODULE = "apps/data-engine/src/data_engine/datahub/standards/segment_extraction.py"

#: Modules that extract values and do not call the shared primitive yet. This set may only
#: shrink. A new module that bypasses the primitive must call it instead of joining this set.
#: The shrink test below fails when a listed module starts to call the primitive.
KNOWN_UNROUTED_EXTRACTION_MODULES: set[str] = {
    # Known debt, issue #773 D8: supply-chain relationship extraction keeps its own module-level
    # `re.compile` patterns and its own candidate class. D8 routes it through the primitive.
    SUPPLY_CHAIN_MODULE,
}


def _src_python_files(root: Path = REPO_ROOT) -> list[Path]:
    """Every production module under `libs/` and `apps/` — package source only: no
    tests (which legitimately reference the extractor id as fixture data or an imported
    constant, e.g. `apps/data-engine/tests/test_headcount_source_priority.py`), no
    `__pycache__`."""
    files: list[Path] = []
    for base in (root / "libs", root / "apps"):
        for path in base.rglob("*.py"):
            parts = path.relative_to(root).parts
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


def _primitive_entrypoints() -> frozenset[str]:
    """The public top-level functions of the primitive: the names a caller may use."""
    tree = ast.parse((REPO_ROOT / PRIMITIVE_FILE).read_text(), filename=PRIMITIVE_FILE)
    return frozenset(
        node.name for node in tree.body if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")
    )


def _dotted_name(node: ast.expr) -> str | None:
    """`a.b.c` for a chain of attribute reads on a plain name; `None` for anything else."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    return ".".join([node.id, *reversed(parts)])


def _extraction_triggers(rel: str, tree: ast.AST) -> list[str]:
    """Why this module counts as an extraction module. An empty list means it does not."""
    triggers = []
    if rel.endswith(EXTRACTION_FILE_SUFFIX):
        triggers.append(f"is named *{EXTRACTION_FILE_SUFFIX}")
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and EXTRACT_NAME_RE.match(node.name):
            triggers.append(f"defines {node.name}() at line {node.lineno}")
    return triggers


def _references_primitive(tree: ast.AST, entrypoints: frozenset[str]) -> bool:
    """True when the module imports the primitive AND uses one of its public functions.

    An import alone does not count: an unused import is not a call. A function with the
    same name from another module does not count either.
    """
    function_names: set[str] = set()
    module_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0:
            for alias in node.names:
                if node.module == PRIMITIVE_MODULE and alias.name in entrypoints:
                    function_names.add(alias.asname or alias.name)
                elif node.module == PRIMITIVE_PACKAGE and alias.name == "extraction":
                    module_names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == PRIMITIVE_MODULE:
                    module_names.add(alias.asname or alias.name)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in function_names:
            return True
        if isinstance(node, ast.Attribute) and node.attr in entrypoints and _dotted_name(node.value) in module_names:
            return True
    return False


def _module_violations(rel: str, source: str, entrypoints: frozenset[str] | None = None) -> list[str]:
    """One message when the module is an extraction module that never uses the primitive."""
    tree = ast.parse(source, filename=rel)
    triggers = _extraction_triggers(rel, tree)
    if not triggers:
        return []
    if _references_primitive(tree, _primitive_entrypoints() if entrypoints is None else entrypoints):
        return []
    return [f"{rel} {'; '.join(triggers)}, and never calls a function of {PRIMITIVE_MODULE}"]


def _extraction_scan_files(root: Path = REPO_ROOT) -> list[Path]:
    """The production modules the structural guard governs: those under `EXTRACTION_SCAN_ROOTS`."""
    return [
        path
        for path in _src_python_files(root)
        if path.relative_to(root).as_posix().startswith(tuple(f"{scan_root}/" for scan_root in EXTRACTION_SCAN_ROOTS))
    ]


def _scan_extraction_modules(*, debt: set[str], root: Path = REPO_ROOT) -> list[str]:
    violations = []
    entrypoints = _primitive_entrypoints()
    for path in _extraction_scan_files(root):
        rel = path.relative_to(root).as_posix()
        if rel == PRIMITIVE_FILE or rel in debt:
            continue
        violations.extend(_module_violations(rel, path.read_text(), entrypoints))
    return violations


def _extraction_module_paths(root: Path = REPO_ROOT) -> set[str]:
    """Every module under the scan roots that the structural guard treats as an extraction module."""
    found = set()
    for path in _extraction_scan_files(root):
        rel = path.relative_to(root).as_posix()
        if rel != PRIMITIVE_FILE and _extraction_triggers(rel, ast.parse(path.read_text(), filename=rel)):
            found.add(rel)
    return found


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


def test_the_primitive_exposes_the_entrypoints_the_structural_guard_expects() -> None:
    entrypoints = _primitive_entrypoints()
    assert {
        "extract_metric",
        "select_single_candidate",
        "select_exhaustive_partition",
        "select_first_balancing_set",
    } <= entrypoints, f"the primitive's public functions changed: {sorted(entrypoints)}"


def test_every_extraction_module_calls_the_shared_primitive() -> None:
    violations = _scan_extraction_modules(debt=KNOWN_UNROUTED_EXTRACTION_MODULES)
    assert not violations, "\n".join(violations)


def test_the_structural_guard_actually_fires_against_the_real_supply_chain_module() -> None:
    """Red-proof (#1130): `supply_chain_extraction.py` has its own regex and its own candidate
    class and never imports the primitive. Only its debt entry keeps the real scan green.
    Drop that entry and the same scan, over the same repository, must flag it."""
    assert SUPPLY_CHAIN_MODULE in KNOWN_UNROUTED_EXTRACTION_MODULES, (
        "supply_chain_extraction.py bypasses the primitive; it needs its debt entry (#773 D8)"
    )
    shrunk = KNOWN_UNROUTED_EXTRACTION_MODULES - {SUPPLY_CHAIN_MODULE}
    violations = _scan_extraction_modules(debt=shrunk)
    assert any(violation.startswith(SUPPLY_CHAIN_MODULE) for violation in violations), (
        "removing the debt entry did not fail the scan — the structural guard is vacuous"
    )


def test_every_debt_entry_still_bypasses_the_primitive() -> None:
    """The debt set may only shrink: a module that now calls the primitive must leave it."""
    flagged = {violation.split(" ", 1)[0] for violation in _scan_extraction_modules(debt=set())}
    stale = KNOWN_UNROUTED_EXTRACTION_MODULES - flagged
    assert not stale, f"these modules call the primitive now; remove them from the debt set: {sorted(stale)}"


def test_the_sec_filing_adapters_pass_the_guard_by_calling_the_primitive_not_by_debt() -> None:
    checked = _extraction_module_paths()
    assert {FILING_MODULE, SEGMENT_MODULE, SUPPLY_CHAIN_MODULE} <= checked, (
        f"the guard no longer sees the three adapters: {sorted(checked)}"
    )
    assert FILING_MODULE not in KNOWN_UNROUTED_EXTRACTION_MODULES
    assert SEGMENT_MODULE not in KNOWN_UNROUTED_EXTRACTION_MODULES
    flagged = {violation.split(" ", 1)[0] for violation in _scan_extraction_modules(debt=set())}
    assert FILING_MODULE not in flagged and SEGMENT_MODULE not in flagged


def test_a_new_extraction_module_with_its_own_regex_and_no_primitive_is_flagged(tmp_path) -> None:
    rogue = tmp_path / "foo_extraction.py"
    rogue.write_text(
        'import re\n\n_PATTERN = re.compile(r"\\d+ employees")\n\ndef find(text):\n    return _PATTERN.findall(text)\n'
    )
    rel = "apps/data-engine/src/data_engine/foo_extraction.py"
    violations = _module_violations(rel, rogue.read_text())
    assert len(violations) == 1 and violations[0].startswith(rel), violations
    assert "is named *_extraction.py" in violations[0]


def test_a_module_with_an_extract_function_and_no_primitive_is_flagged_whatever_its_file_name(tmp_path) -> None:
    rogue = tmp_path / "totals.py"
    rogue.write_text("def extract_total(text):\n    return int(text)\n")
    rel = "apps/data-engine/src/data_engine/totals.py"
    violations = _module_violations(rel, rogue.read_text())
    assert len(violations) == 1 and "defines extract_total() at line 1" in violations[0], violations


def test_a_module_that_imports_the_primitive_and_never_uses_it_is_flagged(tmp_path) -> None:
    rogue = tmp_path / "bar_extraction.py"
    rogue.write_text(
        "from factors.shared.extraction import extract_metric\n\ndef extract_bar(text):\n    return int(text)\n"
    )
    violations = _module_violations("libs/x/src/x/bar_extraction.py", rogue.read_text())
    assert len(violations) == 1, violations


def test_a_module_that_imports_only_a_type_of_the_primitive_is_flagged(tmp_path) -> None:
    rogue = tmp_path / "baz_extraction.py"
    rogue.write_text(
        "from factors.shared.extraction import Candidate\n"
        "\n"
        "def extract_baz(text):\n"
        "    return Candidate(int(text), text)\n"
    )
    violations = _module_violations("libs/x/src/x/baz_extraction.py", rogue.read_text())
    assert len(violations) == 1, violations


def test_a_same_named_function_from_another_module_is_flagged(tmp_path) -> None:
    rogue = tmp_path / "qux_extraction.py"
    rogue.write_text(
        "from somewhere_else import extract_metric\n\ndef extract_qux(candidates):\n    return extract_metric(candidates)\n"
    )
    violations = _module_violations("libs/x/src/x/qux_extraction.py", rogue.read_text())
    assert len(violations) == 1, violations


@pytest.mark.parametrize(
    "imports, call",
    [
        ("from factors.shared.extraction import extract_metric", "extract_metric(candidates)"),
        ("from factors.shared.extraction import extract_metric as run", "run(candidates)"),
        ("from factors.shared.extraction import select_single_candidate", "select_single_candidate(candidates)"),
        ("from factors.shared import extraction as primitive", "primitive.extract_metric(candidates)"),
        ("from factors.shared import extraction", "extraction.extract_metric(candidates)"),
        ("import factors.shared.extraction", "factors.shared.extraction.extract_metric(candidates)"),
        ("import factors.shared.extraction as primitive", "primitive.extract_metric(candidates)"),
    ],
)
def test_a_module_that_imports_and_calls_the_primitive_is_not_flagged(tmp_path, imports, call) -> None:
    adapter = tmp_path / "good_extraction.py"
    adapter.write_text(f"{imports}\n\ndef extract_good(candidates):\n    return {call}\n")
    assert _module_violations("libs/x/src/x/good_extraction.py", adapter.read_text()) == []


def test_a_module_that_is_not_an_extraction_module_is_not_checked(tmp_path) -> None:
    plain = tmp_path / "plain.py"
    plain.write_text("import re\n\n_PATTERN = re.compile(r'x')\n\ndef select_nothing():\n    return None\n")
    assert _module_violations("libs/x/src/x/plain.py", plain.read_text()) == []


def _write_module(root: Path, rel: str, source: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)


def test_the_scan_roots_are_the_two_packages_that_extract_metrics() -> None:
    assert EXTRACTION_SCAN_ROOTS == ("libs/factors/src", "apps/data-engine/src")


def test_a_rogue_extract_function_under_every_scan_root_is_flagged(tmp_path) -> None:
    """Red-proof (#1144): the scope change must not free a module inside a scanned root."""
    for scan_root in EXTRACTION_SCAN_ROOTS:
        rel = f"{scan_root}/pkg/rogue.py"
        _write_module(tmp_path, rel, "def extract_total(text):\n    return int(text)\n")
        violations = _scan_extraction_modules(debt=set(), root=tmp_path)
        assert any(violation.startswith(rel) for violation in violations), (scan_root, violations)


def test_a_module_under_the_llm_service_that_defines_extract_functions_is_not_scanned(tmp_path) -> None:
    """The llm-service is a transport layer. Its `extract_*` helpers read identifiers from a
    query. They extract no metric, so the guard must not scan them (#1144)."""
    rel = "apps/llm-service/src/llm_service/mcp_server.py"
    _write_module(
        tmp_path,
        rel,
        "def extract_fund_resolution_candidates(query):\n    return [query]\n"
        "\n"
        "def extract_issuer_resolution_candidates(query):\n    return [query]\n",
    )
    assert _scan_extraction_modules(debt=set(), root=tmp_path) == []
    assert _extraction_module_paths(root=tmp_path) == set()
    assert rel not in {path.relative_to(tmp_path).as_posix() for path in _extraction_scan_files(tmp_path)}


def test_a_file_named_extraction_under_the_llm_service_is_not_scanned_either(tmp_path) -> None:
    _write_module(tmp_path, "apps/llm-service/src/llm_service/query_extraction.py", "VALUE = 1\n")
    assert _scan_extraction_modules(debt=set(), root=tmp_path) == []
