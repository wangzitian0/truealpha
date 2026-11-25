"""The data-engine manifest declares each variable that a deployed container receives (infra2#1823 criterion A).

infra2 gives the Dagster roles the OTel identity and the webserver port. The SDK and the
compose read these names, not the Settings model. The manifest must still declare them.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from data_engine.config import Settings
from infra2_sdk.runtime.environment import RuntimeEnvKey

pytestmark = pytest.mark.contract

APP_DIR = Path(__file__).resolve().parents[1]
MANIFEST = APP_DIR / "required-env.generated.json"

#: The names that `infra2_sdk.runtime.otel.init_worker` reads in `data_engine.dagster_defs`.
OTEL_NAMES = (
    RuntimeEnvKey.OTEL_EXPORTER_OTLP_ENDPOINT.value,
    RuntimeEnvKey.SERVICE_NAME.value,
    RuntimeEnvKey.OTEL_RESOURCE_ATTRIBUTES.value,
)


def _fields() -> dict[str, dict[str, object]]:
    return {entry["env"]: entry for entry in json.loads(MANIFEST.read_text(encoding="utf-8"))["fields"]}


@pytest.mark.parametrize("name", [*OTEL_NAMES, "DAGSTER_WEBSERVER_PORT"])
def test_the_manifest_declares_the_name_as_a_code_default(name: str) -> None:
    entry = _fields().get(name)
    assert entry is not None, f"{name} reaches the container but the manifest does not declare it"
    assert entry["source"] == "code"
    assert entry["injected"] is False, f"{name} has a code default; a deployment does not have to inject it"
    assert entry["has_default"] is True


def test_the_otel_endpoint_default_keeps_telemetry_off() -> None:
    """`init_worker` exports only when an endpoint is set. An empty default is the off state."""
    assert Settings.model_fields["otel_exporter_otlp_endpoint"].default == ""


def test_the_webserver_port_default_is_the_image_port() -> None:
    cmd = re.search(r"^CMD (\[.*\])$", (APP_DIR / "Dockerfile").read_text(encoding="utf-8"), re.MULTILINE)
    assert cmd is not None, "the Dockerfile has no exec-form CMD"
    argv = json.loads(cmd.group(1))
    port = int(argv[argv.index("--port") + 1])
    assert Settings.model_fields["dagster_webserver_port"].default == port
