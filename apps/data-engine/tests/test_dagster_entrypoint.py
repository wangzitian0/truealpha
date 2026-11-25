"""Boot validation of `apps/data-engine/dagster-entrypoint.sh` (#1210 in infra2).

These tests run the real entrypoint with `sh` and the real `infra2_sdk` validator that
`uv.lock` pins. A fake `infra2_sdk` module hid an escape in infra2 (#1915 there): the
entrypoint imported a module that the image of the pinned release did not have.

The tests came from infra2 `common/tests/test_truealpha_data_engine.py`. infra2 deletes its
copy of this script, so the tests live next to the script here.

### Mutation evidence

- Remove the `python -c "...assert_environment(...)"` line from the script: 3 tests fail.
  These are `test_entrypoint_refuses_to_start_when_a_declared_variable_is_missing`,
  `test_entrypoint_requires_injected_variables_not_only_required_ones` and
  `test_entrypoint_reads_the_manifest_at_the_path_the_image_ships`. The script runs
  `exec` without a check.
- Change the manifest path in the script to `apps/data-engine/other-env.json`: 4 tests
  fail. These are the three above and
  `test_entrypoint_execs_the_command_when_every_declared_variable_is_present`. The
  validator does not find the manifest at the path that the image ships.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from data_engine.release_identity import ENV_MANIFEST_PATH

pytestmark = pytest.mark.contract

REPO_ROOT = Path(__file__).resolve().parents[3]
ENTRYPOINT = REPO_ROOT / "apps/data-engine/dagster-entrypoint.sh"

# The manifest path that the image reads. The Dockerfile copies the file to this path
# under WORKDIR /app (`test_release_identity.py` checks the COPY line).
EXPECTED_MANIFEST_PATH = ENV_MANIFEST_PATH.as_posix()

# Names that the fake secrets file defines. The values are fake.
SECRET_VALUES = {
    "DATABASE_URL": "postgresql://fake-user:fake-db-secret@127.0.0.1:5432/truealpha",
    "SEC_USER_AGENT": "fake-sec-user-agent-secret",
    "LLM_MODEL": "fake-llm-model-secret",
}

TARGET_SCRIPT = """\
#!/bin/sh
{
  printf 'ran\\n'
  printf 'arg=%s\\n' "$@"
  printf 'DAGSTER_POSTGRES_URL=%s\\n' "${DAGSTER_POSTGRES_URL-unset}"
} > "$TARGET_LOG"
"""


def _write_manifest(root, declared, manifest_path=EXPECTED_MANIFEST_PATH):
    """Write a manifest in the real SDK format: each declared name must be injected."""
    from infra2_sdk.runtime.config_schema import EnvironmentField, EnvironmentManifest

    manifest = EnvironmentManifest(
        source="test",
        fields=tuple(EnvironmentField(field=name.lower(), env=name, injected=True) for name in declared),
    )
    path = root / manifest_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest.to_dict()), encoding="utf-8")


def _run_entrypoint(tmp_path, *, declared, values=None, manifest_path=EXPECTED_MANIFEST_PATH):
    """Run dagster-entrypoint.sh with the real SDK validator and a recording exec target."""
    workdir = tmp_path / "app"
    workdir.mkdir()
    _write_manifest(workdir, declared, manifest_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # `python` on PATH is the interpreter that runs the tests, as the image's venv is.
    python_shim = bin_dir / "python"
    python_shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
    python_shim.chmod(0o755)
    target = bin_dir / "target"
    target.write_text(TARGET_SCRIPT, encoding="utf-8")
    target.chmod(0o755)

    vault_file = tmp_path / "vault-rendered"
    vault_file.write_text(
        "".join(f"{name}={value}\n" for name, value in (SECRET_VALUES if values is None else values).items()),
        encoding="utf-8",
    )
    target_log = tmp_path / "target.log"
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "SECRETS_ENV_FILE": str(vault_file),
        "TARGET_LOG": str(target_log),
    }
    result = subprocess.run(
        ["sh", str(ENTRYPOINT), str(target), "dagster-daemon", "run"],
        cwd=workdir,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return result, target_log


def test_entrypoint_refuses_to_start_when_a_declared_variable_is_missing(tmp_path):
    result, target_log = _run_entrypoint(tmp_path, declared=[*SECRET_VALUES, "SEC_API_KEY"])

    assert result.returncode != 0
    # The validator ran and named the missing variable. A failed import fails differently.
    assert "environment lacks 1 value(s)" in result.stderr, result.stderr
    assert "SEC_API_KEY" in result.stderr
    assert "ModuleNotFoundError" not in result.stderr
    assert not target_log.exists(), "exec ran after the validator failed"
    for value in SECRET_VALUES.values():
        assert value not in result.stdout + result.stderr


def test_entrypoint_execs_the_command_when_every_declared_variable_is_present(tmp_path):
    # DAGSTER_POSTGRES_URL is declared too: the validator sees it only if the entrypoint
    # exported it before the check ran, and the other values only if it sourced and
    # exported them.
    result, target_log = _run_entrypoint(tmp_path, declared=[*SECRET_VALUES, "DAGSTER_POSTGRES_URL"])

    assert result.returncode == 0, result.stderr
    ran = target_log.read_text(encoding="utf-8").splitlines()
    assert ran[0] == "ran"
    assert ran[1:3] == ["arg=dagster-daemon", "arg=run"]
    expected_url = SECRET_VALUES["DATABASE_URL"] + "?options=-csearch_path%3Ddagster"
    assert ran[3] == f"DAGSTER_POSTGRES_URL={expected_url}"


def test_entrypoint_requires_injected_variables_not_only_required_ones(tmp_path):
    # `require_injected=True`: a variable the manifest marks injected must be present even
    # when the code has a default. The fields of `_write_manifest` are injected, not required.
    values = {k: v for k, v in SECRET_VALUES.items() if k != "LLM_MODEL"}
    result, target_log = _run_entrypoint(tmp_path, declared=list(SECRET_VALUES), values=values)

    assert result.returncode != 0
    assert "LLM_MODEL" in result.stderr, result.stderr
    assert not target_log.exists()


def test_entrypoint_reads_the_manifest_at_the_path_the_image_ships(tmp_path):
    result, target_log = _run_entrypoint(
        tmp_path,
        declared=list(SECRET_VALUES),
        manifest_path="elsewhere/required-env.generated.json",
    )

    assert result.returncode != 0
    assert "is not in the image" in result.stderr, result.stderr
    assert EXPECTED_MANIFEST_PATH in ENTRYPOINT.read_text(encoding="utf-8")
    assert not target_log.exists()


def test_entrypoint_keeps_the_vault_mount_as_default():
    # A deployment never sets SECRETS_ENV_FILE, so the default must stay the Vault mount.
    assert "${SECRETS_ENV_FILE:-/secrets/.env}" in ENTRYPOINT.read_text(encoding="utf-8")
