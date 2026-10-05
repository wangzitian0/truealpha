"""The Dagster code location exports OTLP telemetry to infra2's shared SigNoz (#1034).

`data_engine.dagster_defs` is the module every deployed Dagster process loads (`dagster api grpc -m
...` for the code server, and each run worker the daemon launches), so it is the entrypoint asserted
here, in a fresh interpreter, the way `dagster` imports it. The deployed identity comes from infra2's
compose (`OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES`); this proves the code location consumes it,
exports under it, and refuses to load without it once an endpoint is configured. infra2 issues no
endpoint to these roles today (they run on the host network and cannot reach the collector), so in
production this module loads with telemetry off; the enabled paths below are what an endpoint
reachable from the host would switch on.
"""

from __future__ import annotations

import json

import pytest
from truealpha_runtime.testing import OtlpCollectorStub, run_python_probe

IAC_REF = "0123456789abcdef0123456789abcdef01234567"
IDENTITY = {
    "deployment.environment.name": "production",
    "infra.service.id": "truealpha/data_engine",
    "service.version": "v1.2.3",
    "infra.iac.ref": IAC_REF,
}
DEPLOYED = {
    "OTEL_SERVICE_NAME": "truealpha-dagster",
    "APP_ENV": "production",
    "OTEL_RESOURCE_ATTRIBUTES": ",".join(f"{key}={value}" for key, value in IDENTITY.items()),
}

STATE_PROBE = """
import json, logging
import data_engine.dagster_defs
from opentelemetry import trace

provider = trace.get_tracer_provider()
resource = getattr(provider, "resource", None)
print(json.dumps({
    "sdk_provider": hasattr(provider, "resource"),
    "service_name": resource.attributes.get("service.name") if resource else None,
    "otlp_log_handlers": sum(
        type(h).__name__ == "LoggingHandler" and type(h).__module__.startswith("opentelemetry")
        for h in logging.getLogger().handlers
    ),
}))
"""

EXPORT_PROBE = """
import logging
import data_engine.dagster_defs
from opentelemetry import _logs, metrics, trace

logging.getLogger("data_engine.probe").error("data-engine probe failure")
trace.get_tracer_provider().force_flush()
metrics.get_meter_provider().force_flush()
_logs.get_logger_provider().force_flush()
"""


def test_without_an_endpoint_the_code_location_loads_with_telemetry_off() -> None:
    result = run_python_probe(STATE_PROBE, {})
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.splitlines()[-1]) == {
        "sdk_provider": False,
        "service_name": None,
        "otlp_log_handlers": 0,
    }


def test_the_host_network_roles_get_no_endpoint_and_stay_cleanly_disabled() -> None:
    """The Dagster roles run with `network_mode: host`, where the collector's Docker DNS name does
    not resolve, so infra2 issues them an identity but NO `OTEL_EXPORTER_OTLP_ENDPOINT`. That shape
    must load exactly like local development: no providers, no exporter thread, no log handler."""
    result = run_python_probe(STATE_PROBE, DEPLOYED)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.splitlines()[-1]) == {
        "sdk_provider": False,
        "service_name": None,
        "otlp_log_handlers": 0,
    }


def test_with_an_endpoint_and_the_rendered_identity_the_code_location_configures_telemetry() -> None:
    result = run_python_probe(STATE_PROBE, {**DEPLOYED, "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:9"})
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.splitlines()[-1]) == {
        "sdk_provider": True,
        "service_name": "truealpha-dagster",
        "otlp_log_handlers": 1,
    }


@pytest.mark.parametrize("missing", ["OTEL_SERVICE_NAME", "OTEL_RESOURCE_ATTRIBUTES"])
def test_with_an_endpoint_but_without_the_identity_the_code_location_refuses_to_load(missing: str) -> None:
    environ = {key: value for key, value in DEPLOYED.items() if key != missing}
    result = run_python_probe(STATE_PROBE, {**environ, "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:9"})
    assert result.returncode != 0, "the code location loaded and would have exported untagged telemetry"
    assert "TelemetryConfigError" in result.stderr
    assert missing in result.stderr
    assert IAC_REF not in result.stderr, "names only, never values"


def test_an_error_log_reaches_the_collector_under_the_rendered_identity() -> None:
    with OtlpCollectorStub() as collector:
        result = run_python_probe(EXPORT_PROBE, {**DEPLOYED, "OTEL_EXPORTER_OTLP_ENDPOINT": collector.endpoint})
    assert result.returncode == 0, result.stderr

    assert "data-engine probe failure" in collector.log_bodies()
    expected = {**IDENTITY, "service.name": "truealpha-dagster"}
    resources = collector.resources("logs")
    assert resources, "no logs reached the collector"
    for attributes in resources:
        for key, value in expected.items():
            assert attributes.get(key) == value, f"{key} is {attributes.get(key)!r}"
