"""The infra2-sdk pin the canary asserts in the deployed image is the repository's pin.

`PINNED_INFRA2_SDK` read 1.2.0 while pyproject pinned 1.3.2: the oracle would have called
every correctly built image a failure, because the constant was updated by hand and the pin
was not. This is the check that runs again (AGENTS.md rule 7).
"""

from __future__ import annotations

import importlib.metadata
import re
import tomllib
from pathlib import Path

from data_engine.datahub.canary_oracles import PINNED_INFRA2_SDK

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
_WHEEL_PIN = re.compile(r"/releases/download/v(?P<version>\d+\.\d+\.\d+)/infra2_sdk-(?P=version)-py3-none-any\.whl$")


def _pyproject_pin() -> str:
    pyproject = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    pins = [item for item in pyproject["dependency-groups"]["dev"] if item.startswith("infra2-sdk")]
    assert len(pins) == 1, pins
    match = _WHEEL_PIN.search(pins[0])
    assert match, f"the infra2-sdk pin is not a release wheel URL: {pins[0]}"
    return match.group("version")


def test_the_canary_asserts_the_version_the_repository_pins() -> None:
    assert PINNED_INFRA2_SDK == _pyproject_pin()


def test_the_installed_sdk_is_the_pinned_one() -> None:
    """What the oracle checks inside the image, checked here against the workspace."""
    assert importlib.metadata.version("infra2-sdk") == PINNED_INFRA2_SDK
