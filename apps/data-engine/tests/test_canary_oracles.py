"""The canary's oracles about the deployed IMAGE itself.

The infra2-sdk pin the canary asserts in the deployed image is the repository's pin.

`PINNED_INFRA2_SDK` read 1.2.0 while pyproject pinned 1.3.2: the oracle would have called
every correctly built image a failure, because the constant was updated by hand and the pin
was not. This is the check that runs again (AGENTS.md rule 7).
"""

from __future__ import annotations

import importlib.metadata
import os
import re
import tomllib
from pathlib import Path

import psycopg
import pytest
from data_engine import release_identity
from data_engine.config import settings
from data_engine.datahub.canary_oracles import PINNED_INFRA2_SDK, failures_for_run, image_content_failures

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


# --- what the deployed image contains (#784) --------------------------------------------
#
# Only a process inside the container can see this. A GitHub runner inspects the repository,
# a health probe inspects an answer; neither can tell whether the image shipped the
# environment contract infra2's entrypoint validates against — and until #784 it did not,
# with every gate green, because the validation simply never ran.


def test_the_image_content_oracles_hold_for_this_working_tree() -> None:
    assert image_content_failures() == []


def test_an_image_without_its_environment_manifest_is_a_red_canary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(release_identity, "ENV_MANIFEST_PATH", tmp_path / "required-env.generated.json")
    failures = image_content_failures()
    assert any("does not ship" in line and "boot-validate" in line for line in failures), failures
    # ... and it cannot say what release it is either, which is the same missing file.
    assert any("cannot measure its release identity" in line for line in failures), failures


def test_an_image_without_its_migration_set_is_a_red_canary(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(release_identity, "MIGRATIONS_PATH", tmp_path / "migrations")
    assert any("cannot measure its release identity" in line for line in image_content_failures())


def test_the_run_verdict_carries_the_image_verdict(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Through `failures_for_run`, which is what the deploy lane calls: a wrong image is
    named even when the run it was asked about does not exist, because the image is the
    cause and "no capture status" is only the symptom."""
    try:
        connection = psycopg.connect(settings.database_url, connect_timeout=3)
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs the required integration coverage")
    monkeypatch.setattr(release_identity, "ENV_MANIFEST_PATH", tmp_path / "required-env.generated.json")
    try:
        failures = failures_for_run(connection, "capture-run:no-such-run")
    finally:
        connection.close()
    assert any("does not ship" in line for line in failures), failures
    assert any("no capture status" in line for line in failures), failures
