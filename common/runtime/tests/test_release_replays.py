"""Tests for tools/release_replays.py and the declaration it reads (#1412 R4).

`test_cut_release.py` runs the check end to end through `cut_release.sh` against a throwaway
origin. This file checks the real declaration of this repository: it must watch every parser
and mapping version constant of the data engine, and each watched identity must exist. A
version constant that the declaration does not watch is a change that no release must declare.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest
from truealpha_runtime.testing import load_tool

REPO_ROOT = Path(__file__).resolve().parents[3]
_module = load_tool("release_replays")
DATA_ENGINE_SRC = REPO_ROOT / "apps" / "data-engine" / "src"
VERSION_SUFFIXES = ("PARSER_VERSION", "PARSER_VERSION_HISTORY", "MAPPING_VERSION", "MAPPING_VERSION_HISTORY")


def _working_tree(_rev: str, path: str) -> str | None:
    target = REPO_ROOT / path
    return target.read_text(encoding="utf-8") if target.is_file() else None


def _declaration() -> dict:
    return _module.parse_declaration(_working_tree("HEAD", _module.DECLARATION), "HEAD")


def _literal(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    return isinstance(node, ast.Tuple) and all(_literal(element) for element in node.elts)


def _version_constants() -> set[tuple[str, str]]:
    """Every module-level `*_PARSER_VERSION[_HISTORY]` or `*_MAPPING_VERSION[_HISTORY]` that holds
    a literal. An alias such as `PARSER_VERSION = PARSER_VERSION_HISTORY[-1]` is not a source."""
    found: set[tuple[str, str]] = set()
    for path in sorted(DATA_ENGINE_SRC.rglob("*.py")):
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.Assign) and _literal(node.value):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id.endswith(VERSION_SUFFIXES):
                        found.add((path.relative_to(REPO_ROOT).as_posix(), target.id))
    return found


def test_the_declaration_watches_every_parser_and_mapping_version_constant() -> None:
    watched = {(spec["path"], spec["name"]) for spec in _declaration()["identities"].values() if "name" in spec}
    constants = _version_constants()

    assert constants, "the scan found no version constant, so it guards nothing"
    missing = sorted(constants - watched)
    assert not missing, (
        f"{_module.DECLARATION} does not watch {missing}. A release could change these versions with no "
        f"declared replay (#1270). Add each one to 'identities'."
    )


def test_every_watched_identity_exists_in_the_tree() -> None:
    for key, spec in _declaration()["identities"].items():
        assert _module.identity_value("HEAD", spec, _working_tree) is not None, f"{key} names {spec}, which is absent"


def test_the_projector_identity_watches_the_history_projector() -> None:
    """#1270 was a projector fix that no replay followed. The projector has no version
    constant, so its code is the identity."""
    projector = _declaration()["identities"]["strategy-history-projector"]
    assert "apps/data-engine/src/data_engine/datahub/strategy_history.py" in projector["paths"]


@pytest.mark.parametrize(
    ("text", "refusal"),
    [
        (None, "is missing"),
        ("{", "not valid JSON"),
        (json.dumps({"identities": {}, "replays": []}), "non-empty 'identities'"),
        (json.dumps({"identities": {"Bad Key": {"path": "a.py", "name": "X"}}, "replays": []}), "lowercase key"),
        (json.dumps({"identities": {"p": {"path": "a.py"}}, "replays": []}), "lowercase key"),
        (
            json.dumps(
                {"identities": {"p": {"path": "a.py", "name": "X"}}, "replays": [{"identity": "q", "replay": "r"}]}
            ),
            "must name a watched identity",
        ),
        (
            json.dumps(
                {"identities": {"p": {"path": "a.py", "name": "X"}}, "replays": [{"identity": "p", "replay": "a\nb"}]}
            ),
            "one-line",
        ),
        (
            json.dumps(
                {"identities": {"p": {"path": "a.py", "name": "X"}}, "replays": [{"identity": "p", "replay": " "}]}
            ),
            "non-empty 'replay'",
        ),
    ],
)
def test_a_defective_declaration_is_a_refusal(text: str | None, refusal: str) -> None:
    with pytest.raises(_module.ReplayError, match=refusal):
        _module.parse_declaration(text, "0" * 40)


def test_a_moved_identity_is_a_refusal_not_a_silent_pass() -> None:
    files = {
        ("release", _module.DECLARATION): json.dumps(
            {"identities": {"p": {"path": "gone.py", "name": "X"}}, "replays": []}
        ),
    }

    def read(rev: str, path: str) -> str | None:
        return files.get((rev, path))

    with pytest.raises(_module.ReplayError, match="does not exist at"):
        _module.trailers("base", "v0.0.1", "release", read)


def test_the_check_runs_on_a_bare_python_without_site_packages() -> None:
    """`auto-release-staging.yml` runs `cut_release.sh --auto` with no `uv sync`, so the check
    must import nothing outside the standard library."""
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(REPO_ROOT / "tools" / "release_replays.py"), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
