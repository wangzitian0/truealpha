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
        path
        for path in SHARDED_ROOT.rglob("*.py")
        if path not in collected and "def test_" in path.read_text(encoding="utf-8")
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
            "999",
            "--of",
            "1000",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2, result.stdout
    assert "is empty" in result.stderr or "out of range" in result.stderr


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
    lines = result.stdout.split()
    assert lines, "shard 0 of 3 printed nothing"
    for line in lines:
        assert Path(line).is_file(), f"shard printed {line!r}, which is not a file"
