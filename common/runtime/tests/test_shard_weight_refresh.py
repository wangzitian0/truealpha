"""The shard-weight refresh job opens one pull request, only when a refresh adds weights (#1503).

`shard_weight_refresh` decides; the `shard-weight-refresh` workflow triggers it. The workflow
shape is asserted in `test_ci_workflows.py`. These tests drive the decision with real files in a
temporary tree.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from truealpha_runtime.testing import REPO_ROOT, load_tool

refresh = load_tool("shard_weight_refresh")


def _tree(root: Path, names: list[str]) -> None:
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("def test_x():\n    pass\n", encoding="utf-8")


def _weights(path: Path, run_id: str, seconds: dict[str, float]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"harvested_from_run": run_id, "seconds_by_file": seconds}), encoding="utf-8")
    return path


def _cli(tmp_path: Path, weights: Path, harvest: Path, open_prs: int) -> subprocess.CompletedProcess[str]:
    """Run the tool as the workflow runs it: `python3 tools/shard_weight_refresh.py ...`."""
    return subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "tools" / "shard_weight_refresh.py"),
            "--harvest",
            str(harvest),
            "--open-prs",
            str(open_prs),
            "--weights",
            str(weights),
            "--repo-root",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def _run(tmp_path: Path, *, old: dict[str, float], harvest: dict[str, float], open_prs: int) -> tuple[str, Path]:
    """Run the CLI over a weights file and a harvest file. Return its stdout and the weights path."""
    weights = _weights(tmp_path / "tools" / "pytest_shard_weights.json", "100", old)
    harvest_file = _weights(tmp_path / "harvest.json", "200", harvest)
    result = _cli(tmp_path, weights, harvest_file, open_prs)
    assert result.returncode == 0, result.stderr
    return result.stdout, weights


def test_a_new_weighted_file_with_no_open_pr_opens_a_pr_and_writes_the_merge(tmp_path: Path) -> None:
    _tree(tmp_path, ["tests/test_old.py", "tests/test_new.py"])
    stdout, weights = _run(
        tmp_path,
        old={"tests/test_old.py": 1.0},
        harvest={"tests/test_old.py": 2.0, "tests/test_new.py": 3.0},
        open_prs=0,
    )
    assert "open_pr=true" in stdout
    written = json.loads(weights.read_text(encoding="utf-8"))
    assert written["seconds_by_file"] == {"tests/test_new.py": 3.0, "tests/test_old.py": 2.0}
    assert written["harvested_from_run"] == "200"
    assert written["files"] == 2


def test_an_open_refresh_pr_blocks_another_even_when_files_are_new(tmp_path: Path) -> None:
    _tree(tmp_path, ["tests/test_new.py"])
    stdout, weights = _run(tmp_path, old={}, harvest={"tests/test_new.py": 3.0}, open_prs=1)
    assert "open_pr=false" in stdout
    assert "open refresh pull request exists" in stdout
    assert json.loads(weights.read_text(encoding="utf-8"))["seconds_by_file"] == {}, "a skip must not write"


def test_a_harvest_that_adds_no_file_opens_no_pr_and_writes_nothing(tmp_path: Path) -> None:
    _tree(tmp_path, ["tests/test_old.py"])
    stdout, weights = _run(tmp_path, old={"tests/test_old.py": 1.0}, harvest={"tests/test_old.py": 9.0}, open_prs=0)
    assert "open_pr=false" in stdout
    assert json.loads(weights.read_text(encoding="utf-8"))["seconds_by_file"] == {"tests/test_old.py": 1.0}


def test_a_harvested_file_that_no_longer_exists_does_not_count_as_a_gain(tmp_path: Path) -> None:
    _tree(tmp_path, ["tests/test_old.py"])
    stdout, _ = _run(
        tmp_path,
        old={"tests/test_old.py": 1.0},
        harvest={"tests/test_old.py": 1.0, "tests/test_deleted.py": 5.0},
        open_prs=0,
    )
    assert "open_pr=false" in stdout


def test_a_partial_harvest_keeps_the_old_weight_and_drops_only_the_deleted_file(tmp_path: Path) -> None:
    """The harvest lacks `test_kept.py` (a skipped lane). It must keep its weight.
    `test_deleted.py` is gone from the tree. Its weight must go."""
    _tree(tmp_path, ["tests/test_kept.py", "tests/test_new.py"])
    _, weights = _run(
        tmp_path,
        old={"tests/test_kept.py": 4.0, "tests/test_deleted.py": 8.0},
        harvest={"tests/test_new.py": 1.0},
        open_prs=0,
    )
    written = json.loads(weights.read_text(encoding="utf-8"))["seconds_by_file"]
    assert written == {"tests/test_kept.py": 4.0, "tests/test_new.py": 1.0}


def test_an_empty_harvest_fails_closed_instead_of_reading_as_nothing_to_refresh(tmp_path: Path) -> None:
    weights = _weights(tmp_path / "tools" / "pytest_shard_weights.json", "100", {"tests/test_old.py": 1.0})
    harvest = _weights(tmp_path / "harvest.json", "200", {})
    result = _cli(tmp_path, weights, harvest, open_prs=0)
    assert result.returncode != 0
    assert "holds no weights" in result.stderr


def test_the_decision_is_written_to_github_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _tree(tmp_path, ["tests/test_new.py"])
    weights = _weights(tmp_path / "tools" / "w.json", "100", {})
    harvest = _weights(tmp_path / "harvest.json", "200", {"tests/test_new.py": 3.0})
    output = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    code = refresh.main(
        [
            "--harvest",
            str(harvest),
            "--open-prs",
            "0",
            "--weights",
            str(weights),
            "--repo-root",
            str(tmp_path),
        ]
    )
    assert code == 0
    lines = output.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "open_pr=true"
    assert lines[1].startswith("reason=")


@pytest.mark.parametrize(
    ("old", "merged", "open_prs", "expected"),
    [
        ({}, {"a": 1.0}, 0, True),
        ({"a": 1.0}, {"a": 1.0, "b": 2.0}, 0, True),
        ({"a": 1.0}, {"a": 5.0}, 0, False),
        ({"a": 1.0, "b": 2.0}, {"a": 1.0}, 0, False),
        ({}, {"a": 1.0}, 1, False),
        ({}, {"a": 1.0}, 3, False),
    ],
)
def test_the_decision_table(old: dict[str, float], merged: dict[str, float], open_prs: int, expected: bool) -> None:
    assert refresh.decide(old, merged, open_prs).open_pr is expected
