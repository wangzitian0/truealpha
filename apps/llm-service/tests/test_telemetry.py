"""llm-service exports OTLP telemetry to infra2's shared SigNoz (#1034).

Asserted through the deployed entrypoint, `llm_service.main`, imported in a fresh interpreter the
way uvicorn imports it: a test that built providers by hand would prove the settings and still
pass if `main` never called `init_telemetry`. The decisive test points a real import of the app at
a localhost OTLP receiver and reads back what arrived: the server span of a real request, a metric
batch, and an ERROR log record, each under the identity infra2's deploy renders. The service name is whatever `OTEL_SERVICE_NAME` says (infra2 issues
`truealpha-app` for this stack); nothing in the app names it.
"""

from __future__ import annotations

import json

import pytest
from truealpha_runtime.testing import OtlpCollectorStub, run_python_probe

IAC_REF = "0123456789abcdef0123456789abcdef01234567"
IDENTITY = {
    "deployment.environment.name": "staging",
    "infra.service.id": "truealpha/app",
    "service.version": "v1.2.3",
    "infra.iac.ref": IAC_REF,
}
DEPLOYED = {
    "OTEL_SERVICE_NAME": "truealpha-app",
    "APP_ENV": "staging",
    "OTEL_RESOURCE_ATTRIBUTES": ",".join(f"{key}={value}" for key, value in IDENTITY.items()),
}

STATE_PROBE = """
import json, logging
from llm_service import main
from opentelemetry import trace

provider = trace.get_tracer_provider()
resource = getattr(provider, "resource", None)
print(json.dumps({
    "instrumented": bool(getattr(main.app, "_is_instrumented_by_opentelemetry", False)),
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
from fastapi.testclient import TestClient
from llm_service import main
from opentelemetry import _logs, metrics, trace

# /mcp answers 307 from the app's own route: a real request through the instrumented stack that
# needs no database.
assert TestClient(main.app, follow_redirects=False).get("/mcp").status_code == 307
logging.getLogger("llm_service.probe").error("llm-service probe failure")
trace.get_tracer_provider().force_flush()
metrics.get_meter_provider().force_flush()
_logs.get_logger_provider().force_flush()
"""


def test_without_an_endpoint_the_app_imports_with_telemetry_off() -> None:
    result = run_python_probe(STATE_PROBE, {})
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.splitlines()[-1]) == {
        "instrumented": False,
        "sdk_provider": False,
        "service_name": None,
        "otlp_log_handlers": 0,
    }


def test_with_an_endpoint_and_the_rendered_identity_the_app_is_instrumented() -> None:
    result = run_python_probe(STATE_PROBE, {**DEPLOYED, "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:9"})
    assert result.returncode == 0, result.stderr
    state = json.loads(result.stdout.splitlines()[-1])
    assert state == {
        "instrumented": True,
        "sdk_provider": True,
        "service_name": "truealpha-app",
        "otlp_log_handlers": 1,
    }


@pytest.mark.parametrize("missing", ["OTEL_SERVICE_NAME", "OTEL_RESOURCE_ATTRIBUTES"])
def test_with_an_endpoint_but_without_the_identity_the_app_refuses_to_import(missing: str) -> None:
    environ = {key: value for key, value in DEPLOYED.items() if key != missing}
    result = run_python_probe(STATE_PROBE, {**environ, "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:9"})
    assert result.returncode != 0, "the service started and would have exported untagged telemetry"
    assert "TelemetryConfigError" in result.stderr
    assert missing in result.stderr
    assert IAC_REF not in result.stderr, "names only, never values"


def test_a_request_and_an_error_log_reach_the_collector_under_the_rendered_identity() -> None:
    with OtlpCollectorStub() as collector:
        result = run_python_probe(EXPORT_PROBE, {**DEPLOYED, "OTEL_EXPORTER_OTLP_ENDPOINT": collector.endpoint})
    assert result.returncode == 0, result.stderr

    assert any("/mcp" in name for name in collector.span_names()), collector.span_names()
    assert "llm-service probe failure" in collector.log_bodies()
    expected = {**IDENTITY, "service.name": "truealpha-app"}
    for signal in ("traces", "metrics", "logs"):
        resources = collector.resources(signal)
        assert resources, f"no {signal} reached the collector"
        for attributes in resources:
            for key, value in expected.items():
                assert attributes.get(key) == value, f"{signal}: {key} is {attributes.get(key)!r}"
