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
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tools"))

import pytest_shard  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
SHARDED_ROOT = REPO_ROOT / "apps" / "data-engine" / "tests"


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
    # The floor is the heaviest single FILE, which cannot be split: 42 s inside
    # a 210 s suite. 1.4 is loose enough to survive files moving and tight
    # enough that position packing (2.4x) or count packing (2.1x) fails.
    assert max(loads) / min(loads) <= 1.4, (
        f"the three shards carry {[round(x) for x in loads]} measured seconds — the slowest lane "
        f"sets the CI wall, and this spread means the packing stopped using measured time"
    )


def test_the_weights_still_describe_the_tree_they_weigh() -> None:
    """Staleness has no symptom. A harvest taken before a directory was added
    stays valid-looking forever: every lane green, the balance quietly drifting
    back to a count-based estimate as the unmeasured share grows. The
    observable is coverage, so that is what is asserted.
    """
    weights = pytest_shard.load_weights()
    if not weights:
        pytest.skip("no harvest committed; the tool falls back to test counts by design")
    files = pytest_shard.collect(SHARDED_ROOT)
    known = [path for path in files if str(path.relative_to(REPO_ROOT)) in weights]
    coverage = len(known) / len(files)
    assert coverage >= 0.9, (
        f"the harvest covers {coverage:.0%} of {SHARDED_ROOT.name} ({len(known)}/{len(files)} "
        f"files) — refresh it with tools/harvest_shard_weights.py <run-id> from any green run, "
        f"or the packing is guessing for the rest"
    )


def test_the_assignment_is_stable_across_calls() -> None:
    """A lane whose contents churn between calls makes a flaky failure
    impossible to attribute to a shard."""
    files = pytest_shard.collect(SHARDED_ROOT)
    first = [pytest_shard.shard(files, index, 3) for index in range(3)]
    second = [pytest_shard.shard(list(reversed(files)), index, 3) for index in range(3)]
    assert first == second, "shard assignment depends on input order — it must depend only on content"
