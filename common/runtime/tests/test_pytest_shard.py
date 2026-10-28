"""The sharded CI lanes must cover the tree they claim to run — A4 B1 (#673).

`apps/data-engine/tests` runs as three parallel shards. The danger is not a
wrong split, it is a split that quietly stops covering something: pytest exits
0 over a shard whose files all vanished from the pattern, and three green jobs
report a suite nobody ran (#527's green-while-empty, #472's coverage-on-paper).

Two properties, proven here so the workflow can be trusted:

- the shards PARTITION the collected files — union equals the whole tree and no
  file is counted twice, for every shard count;
- the collection itself misses nothing — checked against file CONTENT (files
  defining test functions), not against the same glob, which would be circular.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from truealpha_runtime.testing import load_tool

pytest_shard = load_tool("pytest_shard")

REPO_ROOT = Path(__file__).resolve().parents[3]
SHARDED_ROOT = REPO_ROOT / "apps" / "data-engine" / "tests"
CORE_ROOTS = [
    REPO_ROOT / "common" / "contracts" / "tests",
    REPO_ROOT / "common" / "factors" / "tests",
    REPO_ROOT / "common" / "runtime" / "tests",
    REPO_ROOT / "apps" / "llm-service" / "tests",
]


@pytest.mark.parametrize("total", [1, 2, 3, 4, 7])
def test_shards_partition_the_tree(total: int) -> None:
    """Union of every shard == the whole collection, with no overlap."""
    files = pytest_shard.collect(SHARDED_ROOT)
    assert files, f"{SHARDED_ROOT} collected nothing — the pattern or the path is wrong"

    seen: list[Path] = []
    for index in range(total):
        seen.extend(pytest_shard.shard(files, index, total))

    assert sorted(seen) == files, (
        f"{total} shards do not cover {SHARDED_ROOT} exactly: "
        f"{len(files) - len(set(seen))} file(s) would run in no CI job at all"
    )
    assert len(seen) == len(set(seen)), "a file lands in more than one shard — wasted CI, not a defect"


def test_shards_partition_multiple_roots() -> None:
    """Multi-root collection partitions all roots exactly across 2 shards (#1173)."""
    files = pytest_shard.collect(CORE_ROOTS)
    assert len(files) >= 100, f"expected at least 100 files across core roots, got {len(files)}"

    seen: list[Path] = []
    for index in range(2):
        seen.extend(pytest_shard.shard(files, index, 2))

    assert sorted(seen) == files, "multi-root 2-way sharding dropped files"
    assert len(seen) == len(set(seen)), "a file lands in more than one shard"


def test_collection_misses_no_file_that_defines_tests() -> None:
    """Content-based, so it cannot agree with the glob by construction.

    A file holding `def test_` that the shard patterns do not match is a file
    pytest never collects — in the sharded lanes AND in a plain local run. It
    is a real coverage hole either way, and this is where it surfaces.
    """
    collected = set(pytest_shard.collect(SHARDED_ROOT))
    orphans = [
        path for path in SHARDED_ROOT.rglob("*.py") if path not in collected and pytest_shard.test_functions(path) > 0
    ]
    assert not orphans, (
        f"these files define test functions but match no pytest_shard pattern, so no shard "
        f"runs them: {[str(p.relative_to(REPO_ROOT)) for p in orphans]}"
    )


def test_a_bad_shard_request_fails_instead_of_running_nothing() -> None:
    files = pytest_shard.collect(SHARDED_ROOT)
    with pytest.raises(ValueError, match="out of range"):
        pytest_shard.shard(files, 3, 3)
    with pytest.raises(ValueError, match="must be >= 1"):
        pytest_shard.shard(files, 0, 0)


def test_more_shards_than_files_exits_nonzero() -> None:
    """An empty shard must not reach pytest: `pytest` with no path arguments
    runs the WHOLE testpaths set, so an empty lane would silently re-run
    everything and hide the very imbalance it was meant to expose."""
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "tools" / "pytest_shard.py"),
            str(SHARDED_ROOT),
            "--shard",
            str(len(pytest_shard.collect(SHARDED_ROOT))),
            "--of",
            str(len(pytest_shard.collect(SHARDED_ROOT)) + 1),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2, result.stdout
    assert "is empty" in result.stderr or "out of range" in result.stderr


def test_a_process_substitution_hides_the_failure_a_shard_lane_must_see() -> None:
    """The shell semantics the CI lane's shape depends on, executed rather than
    remembered: a process substitution's exit status is invisible to `set -e`
    and pipefail, a command substitution's is not. The workflow-shape half of
    this property lives in test_ci_workflows.py (#583); this is the WHY it
    asserts against, and it fails here if bash ever changes."""

    def run_snippet(capture: str) -> int:
        script = f"set -euo pipefail\nFAILING='exit 2'\n{capture}\necho reached-pytest\n"
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True).returncode

    assert run_snippet('mapfile -t FILES < <(sh -c "$FAILING")') == 0, (
        "a process substitution now propagates failure — if bash changed this, the "
        "workflow's defensive shape can be simplified"
    )
    assert run_snippet('LIST=$(sh -c "$FAILING")') != 0, "command substitution must abort under set -e"


def test_the_cli_prints_one_existing_path_per_line() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "tools" / "pytest_shard.py"),
            str(SHARDED_ROOT),
            "--shard",
            "0",
            "--of",
            "3",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    lines = result.stdout.splitlines()
    assert lines, "shard 0 of 3 printed nothing"
    for line in lines:
        assert Path(line).is_file(), f"shard printed {line!r}, which is not a file"


def test_the_shards_carry_equal_measured_work() -> None:
    """Balanced in the unit that was actually measured: seconds from the harvest.

    Deliberately NOT computed through `pytest_shard.weight_of`. The first
    version did, reasoning that "it cannot drift from what the packer
    optimises, because it is the same number" — which is exactly why it could
    not detect the packer optimising the WRONG number. Degrading weight_of back
    to a test count degraded both sides together and the assertion still
    passed: an INERT verdict from redprove, and the fourth instance that day of
    a guard measuring a stand-in while claiming the property.

    Reading the harvest directly means the packer and the check disagree the
    moment the packer stops using measured time.

    Three weights have been tried against reality: file position gave
    57/107/137 s, test count gave 74/155/84 s at counts 177/177/176, and
    measured seconds give 70/70/70. Two files carry 42% of the suite's 210 s,
    which is why nothing derivable from the source could ever have predicted it.
    """
    measured = json.loads((REPO_ROOT / "tools" / "pytest_shard_weights.json").read_text(encoding="utf-8"))[
        "seconds_by_file"
    ]
    files = pytest_shard.collect(SHARDED_ROOT)
    # A file missing from the harvest must NOT count as 0 s (review on #718,
    # which arrived 35 s before I merged it). Zeroing does not reliably flatter
    # or exaggerate — measured on a truncated harvest it did both, 4.06x under
    # zeroing against 2.64x under estimation — and that is the actual defect:
    # the verdict then depends on WHICH files happen to be missing rather than
    # on how the work is distributed. It is
    # estimated at the harvest's own seconds-per-test — computed here from the
    # harvest, never through pytest_shard.weight_of, so the check still
    # disagrees with a packer that stopped using measured time.
    measured_tests = sum(pytest_shard.test_functions(REPO_ROOT / key) for key in measured if (REPO_ROOT / key).exists())
    rate = (sum(measured.values()) / measured_tests) if measured_tests else 1.0

    def seconds(path: Path) -> float:
        key = str(path.relative_to(REPO_ROOT))
        return measured[key] if key in measured else pytest_shard.test_functions(path) * rate

    loads = [sum(seconds(path) for path in pytest_shard.shard(files, index, 3)) for index in range(3)]
    assert min(loads) > 0
    # The floor is the heaviest single FILE, which cannot be split. While it stays under a
    # fair share (42 s inside a 210 s suite when this was written), 1.4 is loose enough to
    # survive files moving and tight enough that position packing (2.4x) or count packing
    # (2.1x) fails.
    heaviest_path = max(files, key=seconds)
    heaviest = seconds(heaviest_path)
    if heaviest <= sum(loads) / 3:
        assert max(loads) / min(loads) <= 1.25, (
            f"the three shards carry {[round(x) for x in loads]} measured seconds — the slowest lane "
            f"sets the CI wall, and this spread means the packing stopped using measured time"
        )
        return
    # 2026-09-16 harvest (run 35089913590): one file, 77 s of a 182 s suite, is heavier than a
    # fair share, so no file-level packing can beat 77 s. The best a packer can do is give
    # that file a lane of its own and balance the rest; that is what is asserted, and the
    # file is named so its owner can split it (the only thing that lowers this wall).
    rest = sorted(loads)[:2]
    assert max(loads) <= heaviest * 1.1, (
        f"the three shards carry {[round(x) for x in loads]} measured seconds; "
        f"{heaviest_path.relative_to(REPO_ROOT)} alone is {heaviest:.0f} s, and the packing put more "
        f"beside it instead of isolating it"
    )
    assert max(rest) / min(rest) <= 1.4, (
        f"the two lanes beside {heaviest_path.name} carry {[round(x) for x in rest]} measured seconds — "
        f"the packing stopped using measured time"
    )


def test_a_measured_file_weighs_its_measured_seconds(tmp_path: Path) -> None:
    """The packer weighs a harvested file by its measured seconds, not its test count.

    The balance check above reads today's tree. On that tree a count-based packer
    balances within 10 percent, so the check passes even when measured weights are
    removed. This test fixes the input instead: three tests and 500 measured seconds.
    A count-based weight returns 3.0 and fails the first assertion.
    """
    path = tmp_path / "test_fixture.py"
    path.write_text(
        "def test_a():\n    pass\n\n\ndef test_b():\n    pass\n\n\ndef test_c():\n    pass\n",
        encoding="utf-8",
    )
    assert pytest_shard.weight_of(path, {"test_fixture.py": 500.0}, tmp_path, rate=1.0) == 500.0
    # A file the harvest does not know converts its test count at the measured rate.
    assert pytest_shard.weight_of(path, {}, tmp_path, rate=2.0) == 6.0


def test_the_weights_still_describe_the_tree_they_weigh() -> None:
    """Report the harvest coverage of both roots. Fail only when the harvest is broken.

    A harvest taken before a directory was added stays valid-looking: every lane
    is green and the packing drifts to a test-count estimate as the unweighted
    share grows. That drift is a speed statistic, so it goes to the CI step
    summary. It does not block a merge (#1503). Only a coverage below
    `WEIGHT_COVERAGE_FLOOR` fails, because then the packing is broken.
    """
    weights = pytest_shard.load_weights()
    if not weights:
        pytest.skip("no harvest committed; the tool falls back to test counts by design")
    coverages = [
        pytest_shard.weight_coverage(
            str(SHARDED_ROOT.relative_to(REPO_ROOT)), pytest_shard.collect(SHARDED_ROOT), weights, REPO_ROOT
        ),
        pytest_shard.weight_coverage("core tests", pytest_shard.collect(CORE_ROOTS), weights, REPO_ROOT),
    ]
    pytest_shard.write_step_summary(coverages, os.environ.get("GITHUB_STEP_SUMMARY"))
    pytest_shard.require_coverage_floor(coverages)


def _coverage_of(known: int, total: int) -> pytest_shard.WeightCoverage:
    """Build the coverage of a fake root of `total` files, of which `known` have a weight."""
    files = [Path(f"root/test_{index}.py") for index in range(total)]
    weights = {f"root/test_{index}.py": 1.0 for index in range(known)}
    return pytest_shard.weight_coverage("root", files, weights, Path("."))


@pytest.mark.parametrize(
    ("known", "total"),
    [
        (138, 154),  # 89.6%: the exact count that blocked PR #1540 with the old 90% assertion
        (119, 133),  # 89.5%: the exact count that blocked PR #1490
        (77, 154),  # 50.0%: exactly the floor passes
        (1, 1),
    ],
)
def test_a_harvest_at_or_above_the_floor_does_not_block(known: int, total: int) -> None:
    pytest_shard.require_coverage_floor([_coverage_of(known, total)])


def test_a_harvest_of_80_percent_still_partitions_every_file_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1523 S6 (G8): an 80% harvest passes the coverage floor and partitions every file once across shards."""
    files = pytest_shard.collect(SHARDED_ROOT)[:10]
    weights = {str(path.relative_to(REPO_ROOT)): float(idx + 1) for idx, path in enumerate(files[:8])}
    coverage = pytest_shard.weight_coverage("sharded", files, weights, REPO_ROOT)
    assert (coverage.known, coverage.total) == (8, 10)
    pytest_shard.require_coverage_floor([coverage])

    monkeypatch.setattr(pytest_shard, "load_weights", lambda *_a, **_k: weights)
    partitioned = [path for shard_idx in range(3) for path in pytest_shard.shard(files, shard_idx, 3)]
    assert sorted(partitioned) == sorted(files)
    assert len(partitioned) == len(set(partitioned)) == 10


@pytest.mark.parametrize(("known", "total"), [(76, 154), (1, 4), (0, 10), (0, 0)])
def test_a_harvest_below_the_floor_blocks_and_names_the_root(known: int, total: int) -> None:
    with pytest.raises(ValueError, match=r"less than 50% of the test files: root "):
        pytest_shard.require_coverage_floor([_coverage_of(known, total)])


def test_one_broken_root_blocks_even_when_the_other_is_complete() -> None:
    complete = pytest_shard.WeightCoverage(label="complete-root", known=10, total=10)
    broken = pytest_shard.WeightCoverage(label="broken-root", known=1, total=10)
    with pytest.raises(ValueError, match=r"broken-root 10\.0% \(1/10\)") as raised:
        pytest_shard.require_coverage_floor([complete, broken])
    assert "complete-root" not in str(raised.value), "a complete root must not appear in the failure"


def test_the_coverage_counts_files_by_their_repo_relative_path(tmp_path: Path) -> None:
    """Three real files on disk. Two have a weight under their repo-relative key."""
    for name in ("test_a.py", "test_b.py", "test_c.py"):
        (tmp_path / name).write_text("def test_x():\n    pass\n", encoding="utf-8")
    files = pytest_shard.collect(tmp_path)
    weights = {"test_a.py": 3.0, "test_b.py": 4.0, "other/test_z.py": 9.0}
    coverage = pytest_shard.weight_coverage("tmp", files, weights, tmp_path)
    assert (coverage.known, coverage.total) == (2, 3)


def test_the_step_summary_gets_one_line_per_root(tmp_path: Path) -> None:
    summary = tmp_path / "summary.md"
    summary.write_text("earlier step\n", encoding="utf-8")
    coverages = [pytest_shard.WeightCoverage("sharded", 138, 154), pytest_shard.WeightCoverage("core", 143, 146)]
    pytest_shard.write_step_summary(coverages, str(summary))
    text = summary.read_text(encoding="utf-8")
    assert text.startswith("earlier step\n"), "the writer must append and keep the earlier steps"
    assert "- sharded: 89.6% of test files have a harvested weight (138/154); floor 50%" in text
    assert "- core: 97.9% of test files have a harvested weight (143/146); floor 50%" in text


def test_no_summary_path_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    pytest_shard.write_step_summary([pytest_shard.WeightCoverage("sharded", 1, 2)], None)
    pytest_shard.write_step_summary([pytest_shard.WeightCoverage("sharded", 1, 2)], "")
    assert list(tmp_path.iterdir()) == []


def test_core_shards_carry_equal_measured_work() -> None:
    """Core tests partitioned across 2 shards carry equal measured work (#1186)."""
    measured = json.loads((REPO_ROOT / "tools" / "pytest_shard_weights.json").read_text(encoding="utf-8"))[
        "seconds_by_file"
    ]
    files = pytest_shard.collect(CORE_ROOTS)
    measured_tests = sum(pytest_shard.test_functions(REPO_ROOT / key) for key in measured if (REPO_ROOT / key).exists())
    rate = (sum(measured.values()) / measured_tests) if measured_tests else 1.0

    def seconds(path: Path) -> float:
        key = str(path.relative_to(REPO_ROOT))
        return measured[key] if key in measured else pytest_shard.test_functions(path) * rate

    loads = [sum(seconds(path) for path in pytest_shard.shard(files, index, 2)) for index in range(2)]
    assert min(loads) > 0
    heaviest_path = max(files, key=seconds)
    heaviest = seconds(heaviest_path)
    if heaviest <= sum(loads) / 2:
        assert max(loads) / min(loads) <= 1.25, (
            f"the two core shards carry {[round(x) for x in loads]} measured seconds — "
            f"this spread means the packing stopped using measured time"
        )
        return
    rest = sorted(loads)[:1]
    assert max(loads) <= heaviest * 1.1
    assert max(rest) / min(rest) <= 1.4


def test_the_assignment_is_stable_across_calls() -> None:
    """A lane whose contents churn between calls makes a flaky failure
    impossible to attribute to a shard."""
    files = pytest_shard.collect(SHARDED_ROOT)
    first = [pytest_shard.shard(files, index, 3) for index in range(3)]
    second = [pytest_shard.shard(list(reversed(files)), index, 3) for index in range(3)]
    assert first == second, "shard assignment depends on input order — it must depend only on content"


def test_every_tools_script_is_a_thin_shim_to_truealpha_tools() -> None:
    """3-Tier reuse contract (#1340): every `tools/*.py` file is a <= 40-line thin CLI shim
    that delegates to `common/runtime/src/truealpha_tools/<name>.py`, and no non-test module
    calls `load_tool(...)` or imports `from tools...`."""
    tools_dir = REPO_ROOT / "tools"
    impl_dir = REPO_ROOT / "common" / "runtime" / "src" / "truealpha_tools"
    scripts = sorted(tools_dir.glob("*.py"))
    assert len(scripts) >= 25
    for script in scripts:
        text = script.read_text(encoding="utf-8")
        lines = text.splitlines()
        assert len(lines) <= 40, f"{script.name} has {len(lines)} lines (max 40 for thin CLI shim)"
        assert f"from truealpha_tools.{script.stem} import main" in text, (
            f"{script.name} must delegate to truealpha_tools.{script.stem}.main"
        )
        assert "sys.modules[__name__]" not in text, f"{script.name} must not mutate sys.modules[__name__]"
        assert (impl_dir / script.name).is_file(), f"missing implementation {impl_dir / script.name}"

    for root in (REPO_ROOT / "apps", REPO_ROOT / "common", tools_dir):
        for py_file in sorted(root.rglob("*.py")):
            rel = py_file.relative_to(REPO_ROOT).as_posix()
            if "/tests/" in f"/{rel}" or rel.endswith("/testing.py"):
                continue
            source = py_file.read_text(encoding="utf-8")
            assert "load_tool(" not in source, f"{rel} calls load_tool() in non-test code"
