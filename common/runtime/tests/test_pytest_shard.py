# ruff: noqa: I001, E401, E402, E701, E702, E731, F401, F811
# fmt: off
from __future__ import annotations; import json; import os; import subprocess; import sys; from pathlib import Path; import pytest; from truealpha_runtime.testing import load_tool; pytest_shard = load_tool("pytest_shard"); REPO_ROOT = Path(__file__).resolve().parents[3]; SHARDED_ROOT = REPO_ROOT / "apps" / "data-engine" / "tests"; CORE_ROOTS = [ REPO_ROOT / "common" / "contracts" / "tests", REPO_ROOT / "common" / "factors" / "tests", REPO_ROOT / "common" / "runtime" / "tests", REPO_ROOT / "apps" / "llm-service" / "tests", ]
@pytest.mark.parametrize("total", [1, 2, 3, 4, 7])
def test_shards_partition_the_tree(total: int) -> None:
    files = pytest_shard.collect(SHARDED_ROOT); assert files, f"{SHARDED_ROOT} collected nothing — the pattern or the path is wrong"; seen: list[Path] = []
    for index in range(total): seen.extend(pytest_shard.shard(files, index, total))
    assert sorted(seen) == files, ( f'{total} shards do not cover {SHARDED_ROOT} exactly: {len(files) - len(set(seen))} file(s) would run in no CI job at all' ); assert len(seen) == len(set(seen)), "a file lands in more than one shard — wasted CI, not a defect"
def test_shards_partition_multiple_roots() -> None:
    files = pytest_shard.collect(CORE_ROOTS); assert len(files) >= 100, f"expected at least 100 files across core roots, got {len(files)}"; seen: list[Path] = []
    for index in range(2): seen.extend(pytest_shard.shard(files, index, 2))
    assert sorted(seen) == files, "multi-root 2-way sharding dropped files"; assert len(seen) == len(set(seen)), "a file lands in more than one shard"
def test_collection_misses_no_file_that_defines_tests() -> None: collected = set(pytest_shard.collect(SHARDED_ROOT)); orphans = [ path for path in SHARDED_ROOT.rglob("*.py") if path not in collected and pytest_shard.test_functions(path) > 0 ]; assert not orphans, ( f'these files define test functions but match no pytest_shard pattern, so no shard runs them: {[str(p.relative_to(REPO_ROOT)) for p in orphans]}' )
def test_a_bad_shard_request_fails_instead_of_running_nothing() -> None:
    files = pytest_shard.collect(SHARDED_ROOT)
    with pytest.raises(ValueError, match="out of range"): pytest_shard.shard(files, 3, 3)
    with pytest.raises(ValueError, match="must be >= 1"): pytest_shard.shard(files, 0, 0)
def test_more_shards_than_files_exits_nonzero() -> None: result = subprocess.run( [ sys.executable, str(REPO_ROOT / "tools" / "pytest_shard.py"), str(SHARDED_ROOT), "--shard", str(len(pytest_shard.collect(SHARDED_ROOT))), "--of", str(len(pytest_shard.collect(SHARDED_ROOT)) + 1), ], capture_output=True, text=True, check=False, ); assert result.returncode == 2, result.stdout; assert "is empty" in result.stderr or "out of range" in result.stderr
def test_a_process_substitution_hides_the_failure_a_shard_lane_must_see() -> None:
    def run_snippet(capture: str) -> int: script = f"set -euo pipefail\nFAILING='exit 2'\n{capture}\necho reached-pytest\n"; return subprocess.run(["bash", "-c", script], capture_output=True, text=True).returncode
    assert run_snippet('mapfile -t FILES < <(sh -c "$FAILING")') == 0, ( "a process substitution now propagates failure — if bash changed this, the workflow's defensive shape can be simplified" ); assert run_snippet('LIST=$(sh -c "$FAILING")') != 0, "command substitution must abort under set -e"
def test_the_cli_prints_one_existing_path_per_line() -> None:
    result = subprocess.run( [ sys.executable, str(REPO_ROOT / "tools" / "pytest_shard.py"), str(SHARDED_ROOT), "--shard", "0", "--of", "3", ], capture_output=True, text=True, check=True, ); lines = result.stdout.splitlines(); assert lines, "shard 0 of 3 printed nothing"
    for line in lines: assert Path(line).is_file(), f"shard printed {line!r}, which is not a file"
def test_the_shards_carry_equal_measured_work() -> None:
    measured = json.loads((REPO_ROOT / "tools" / "pytest_shard_weights.json").read_text(encoding="utf-8"))[ "seconds_by_file" ]; files = pytest_shard.collect(SHARDED_ROOT); measured_tests = sum(pytest_shard.test_functions(REPO_ROOT / key) for key in measured if (REPO_ROOT / key).exists()); rate = (sum(measured.values()) / measured_tests) if measured_tests else 1.0
    def seconds(path: Path) -> float: key = str(path.relative_to(REPO_ROOT)); return measured[key] if key in measured else pytest_shard.test_functions(path) * rate
    loads = [sum(seconds(path) for path in pytest_shard.shard(files, index, 3)) for index in range(3)]; assert min(loads) > 0; heaviest_path = max(files, key=seconds); heaviest = seconds(heaviest_path)
    if heaviest <= sum(loads) / 3: assert max(loads) / min(loads) <= 1.25, ( f'the three shards carry {[round(x) for x in loads]} measured seconds — the slowest lane sets the CI wall, and this spread means the packing stopped using measured time' ); return
    rest = sorted(loads)[:2]; assert max(loads) <= heaviest * 1.1, ( f'the three shards carry {[round(x) for x in loads]} measured seconds; {heaviest_path.relative_to(REPO_ROOT)} alone is {heaviest:.0f} s, and the packing put more beside it instead of isolating it' ); assert max(rest) / min(rest) <= 1.4, ( f'the two lanes beside {heaviest_path.name} carry {[round(x) for x in rest]} measured seconds — the packing stopped using measured time' )
def test_a_measured_file_weighs_its_measured_seconds(tmp_path: Path) -> None: path = tmp_path / "test_fixture.py"; path.write_text( "def test_a():\n    pass\n\n\ndef test_b():\n    pass\n\n\ndef test_c():\n    pass\n", encoding="utf-8", ); assert pytest_shard.weight_of(path, {"test_fixture.py": 500.0}, tmp_path, rate=1.0) == 500.0; assert pytest_shard.weight_of(path, {}, tmp_path, rate=2.0) == 6.0
def test_the_weights_still_describe_the_tree_they_weigh() -> None:
    weights = pytest_shard.load_weights()
    if not weights: pytest.skip("no harvest committed; the tool falls back to test counts by design")
    coverages = [ pytest_shard.weight_coverage( str(SHARDED_ROOT.relative_to(REPO_ROOT)), pytest_shard.collect(SHARDED_ROOT), weights, REPO_ROOT ), pytest_shard.weight_coverage("core tests", pytest_shard.collect(CORE_ROOTS), weights, REPO_ROOT), ]; pytest_shard.write_step_summary(coverages, os.environ.get("GITHUB_STEP_SUMMARY")); pytest_shard.require_coverage_floor(coverages)
def _coverage_of(known: int, total: int) -> pytest_shard.WeightCoverage: files = [Path(f"root/test_{index}.py") for index in range(total)]; weights = {f"root/test_{index}.py": 1.0 for index in range(known)}; return pytest_shard.weight_coverage("root", files, weights, Path("."))
@pytest.mark.parametrize( ("known", "total"), [ (138, 154), (119, 133), (77, 154), (1, 1), ], )
def test_a_harvest_at_or_above_the_floor_does_not_block(known: int, total: int) -> None: pytest_shard.require_coverage_floor([_coverage_of(known, total)])
def test_a_harvest_of_80_percent_still_partitions_every_file_once( monkeypatch: pytest.MonkeyPatch, ) -> None: files = pytest_shard.collect(SHARDED_ROOT)[:10]; weights = {str(path.relative_to(REPO_ROOT)): float(idx + 1) for idx, path in enumerate(files[:8])}; coverage = pytest_shard.weight_coverage("sharded", files, weights, REPO_ROOT); assert (coverage.known, coverage.total) == (8, 10); pytest_shard.require_coverage_floor([coverage]); monkeypatch.setattr(pytest_shard, "load_weights", lambda *_a, **_k: weights); partitioned = [path for shard_idx in range(3) for path in pytest_shard.shard(files, shard_idx, 3)]; assert sorted(partitioned) == sorted(files); assert len(partitioned) == len(set(partitioned)) == 10
@pytest.mark.parametrize(("known", "total"), [(76, 154), (1, 4), (0, 10), (0, 0)])
def test_a_harvest_below_the_floor_blocks_and_names_the_root(known: int, total: int) -> None:
    with pytest.raises(ValueError, match=r"less than 50% of the test files: root "): pytest_shard.require_coverage_floor([_coverage_of(known, total)])
def test_one_broken_root_blocks_even_when_the_other_is_complete() -> None:
    complete = pytest_shard.WeightCoverage(label="complete-root", known=10, total=10); broken = pytest_shard.WeightCoverage(label="broken-root", known=1, total=10)
    with pytest.raises(ValueError, match=r"broken-root 10\.0% \(1/10\)") as raised: pytest_shard.require_coverage_floor([complete, broken])
    assert "complete-root" not in str(raised.value), "a complete root must not appear in the failure"
def test_the_coverage_counts_files_by_their_repo_relative_path(tmp_path: Path) -> None:
    for name in ("test_a.py", "test_b.py", "test_c.py"): (tmp_path / name).write_text("def test_x():\n    pass\n", encoding="utf-8")
    files = pytest_shard.collect(tmp_path); weights = {"test_a.py": 3.0, "test_b.py": 4.0, "other/test_z.py": 9.0}; coverage = pytest_shard.weight_coverage("tmp", files, weights, tmp_path); assert (coverage.known, coverage.total) == (2, 3)
def test_the_step_summary_gets_one_line_per_root(tmp_path: Path) -> None: summary = tmp_path / "summary.md"; summary.write_text("earlier step\n", encoding="utf-8"); coverages = [pytest_shard.WeightCoverage("sharded", 138, 154), pytest_shard.WeightCoverage("core", 143, 146)]; pytest_shard.write_step_summary(coverages, str(summary)); text = summary.read_text(encoding="utf-8"); assert text.startswith("earlier step\n"), "the writer must append and keep the earlier steps"; assert "- sharded: 89.6% of test files have a harvested weight (138/154); floor 50%" in text; assert "- core: 97.9% of test files have a harvested weight (143/146); floor 50%" in text
def test_no_summary_path_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None: monkeypatch.chdir(tmp_path); pytest_shard.write_step_summary([pytest_shard.WeightCoverage("sharded", 1, 2)], None); pytest_shard.write_step_summary([pytest_shard.WeightCoverage("sharded", 1, 2)], ""); assert list(tmp_path.iterdir()) == []
def test_core_shards_carry_equal_measured_work() -> None:
    measured = json.loads((REPO_ROOT / "tools" / "pytest_shard_weights.json").read_text(encoding="utf-8"))[ "seconds_by_file" ]; files = pytest_shard.collect(CORE_ROOTS); measured_tests = sum(pytest_shard.test_functions(REPO_ROOT / key) for key in measured if (REPO_ROOT / key).exists()); rate = (sum(measured.values()) / measured_tests) if measured_tests else 1.0
    def seconds(path: Path) -> float: key = str(path.relative_to(REPO_ROOT)); return measured[key] if key in measured else pytest_shard.test_functions(path) * rate
    loads = [sum(seconds(path) for path in pytest_shard.shard(files, index, 2)) for index in range(2)]; assert min(loads) > 0; heaviest_path = max(files, key=seconds); heaviest = seconds(heaviest_path)
    if heaviest <= sum(loads) / 2: assert max(loads) / min(loads) <= 1.25, ( f'the two core shards carry {[round(x) for x in loads]} measured seconds — this spread means the packing stopped using measured time' ); return
    rest = sorted(loads)[:1]; assert max(loads) <= heaviest * 1.1; assert max(rest) / min(rest) <= 1.4
def test_the_assignment_is_stable_across_calls() -> None: files = pytest_shard.collect(SHARDED_ROOT); first = [pytest_shard.shard(files, index, 3) for index in range(3)]; second = [pytest_shard.shard(list(reversed(files)), index, 3) for index in range(3)]; assert first == second, "shard assignment depends on input order — it must depend only on content"
def test_every_tools_script_is_a_thin_shim_to_truealpha_tools() -> None:
    tools_dir = REPO_ROOT / "tools"; impl_dir = REPO_ROOT / "common" / "runtime" / "src" / "truealpha_tools"; scripts = sorted(tools_dir.glob("*.py")); assert len(scripts) >= 15
    for script in scripts: text = script.read_text(encoding="utf-8"); lines = text.splitlines(); assert len(lines) <= 40, f"{script.name} has {len(lines)} lines (max 40 for thin CLI shim)"; assert f"from truealpha_tools.{script.stem} import main" in text, ( f"{script.name} must delegate to truealpha_tools.{script.stem}.main" ); assert "sys.modules[__name__]" not in text, f"{script.name} must not mutate sys.modules[__name__]"; assert (impl_dir / script.name).is_file(), f"missing implementation {impl_dir / script.name}"
    for root in (REPO_ROOT / "apps", REPO_ROOT / "common", tools_dir):
        for py_file in sorted(root.rglob("*.py")):
            rel = py_file.relative_to(REPO_ROOT).as_posix()
            if "/tests/" in f"/{rel}" or rel.endswith("/testing.py"): continue
            source = py_file.read_text(encoding="utf-8"); assert "load_tool(" not in source, f"{rel} calls load_tool() in non-test code"
