# ruff: noqa: I001, E401, E402, E701, E702, E731, F401, F811
# fmt: off
from __future__ import annotations; from pathlib import Path; from truealpha_runtime.testing import load_tool; REPO_ROOT = Path(__file__).resolve().parents[3]; _module = load_tool("mutation_reproof"); STRUCTURAL_GUARDS = ( "apps/app-web/tests/source-contracts.test.ts", "apps/app-web/tests/route-tree-freeze.test.ts", "common/runtime/tests/test_ci_workflows.py", "common/runtime/tests/test_output_invariants.py", )
def test_every_structural_guard_has_a_mutation() -> None:
    covered = {mutation.guard for mutation in _module.load()}; missing = [guard for guard in STRUCTURAL_GUARDS if guard not in covered]
    assert not missing, ( f'{missing} have no declared mutation, so nothing would notice them going inert. Add an edit to tools/mutations.json that each one must catch (#582)' )
def test_every_mutation_still_describes_the_code() -> None:
    stale = []
    for mutation in _module.load():
        source = (REPO_ROOT / mutation.file).read_text(encoding="utf-8")
        if mutation.find not in source: stale.append(f"{mutation.id} -> {mutation.file}")
    assert not stale, ( f'{stale}: the anchor is gone, so the mutation no longer applies. Update it to an edit that still breaks the property, or the guard is unproven' )
def test_every_mutation_targets_a_file_that_exists() -> None:
    for mutation in _module.load(): assert (REPO_ROOT / mutation.file).exists(), f"{mutation.id} targets a missing file"; assert (REPO_ROOT / mutation.guard).exists(), f"{mutation.id} names a missing guard"
def test_an_inert_guard_is_reported_and_fails(capsys) -> None: inert = _module.Mutation( id="probe", guard="somewhere", file="tools/mutations.json", find="mutations", replace="mutations", command=("python3", "-c", "pass"), cwd=".", ); assert _module.run(mutations=[inert]) == 1; assert "the guard passed with the property broken" in capsys.readouterr().err
def test_a_hanging_guard_is_a_finding_and_the_run_continues(capsys, monkeypatch) -> None: monkeypatch.setattr(_module, "TIMEOUT_SECONDS", 1); hanging = _module.Mutation( id="hangs", guard="somewhere", file="tools/mutations.json", find="mutations", replace="mutations", command=("python3", "-c", "import time; time.sleep(30)"), cwd=".", ); after = _module.Mutation( id="after", guard="somewhere", file="tools/mutations.json", find="mutations", replace="mutations", command=("python3", "-c", "raise SystemExit(1)"), cwd=".", ); assert _module.run(mutations=[hanging, after]) == 1; captured = capsys.readouterr(); assert "did not finish within" in captured.err; assert "caught   after" in captured.out, "one hang must not stop the remaining guards"
def test_the_manifest_file_survives_a_hanging_guard() -> None: original = (REPO_ROOT / "tools/mutations.json").read_text(encoding="utf-8"); assert '"mutations"' in original
def test_a_caught_mutation_passes(capsys) -> None: caught = _module.Mutation( id="probe", guard="somewhere", file="tools/mutations.json", find="mutations", replace="mutations", command=("python3", "-c", "raise SystemExit(1)"), cwd=".", ); assert _module.run(mutations=[caught]) == 0; assert "caught   probe" in capsys.readouterr().out
def test_a_guard_skipping_under_the_db_write_gate_is_reported_as_inert(capsys) -> None: skipped = _module.Mutation( id="db-skipped", guard="somewhere", file="tools/mutations.json", find="mutations", replace="mutations", command=( "python3", "-c", "import sys; sys.stderr.write('UserWarning: DB-backed test skipped: set CI=true or ALLOW_CLEAR_TEST=1\\n')", ), cwd=".", ); assert _module.run(mutations=[skipped]) == 1; assert "skipped under the DB write gate instead of proving the property" in capsys.readouterr().err
