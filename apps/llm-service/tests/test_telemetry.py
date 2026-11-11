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


def _attributes(deployment_environment: str) -> str:
    issued = {**IDENTITY, "deployment.environment.name": deployment_environment}
    return ",".join(f"{key}={value}" for key, value in issued.items())


DEPLOYED = {
    "OTEL_SERVICE_NAME": "truealpha-app",
    "APP_ENV": "staging",
    "OTEL_RESOURCE_ATTRIBUTES": _attributes(IDENTITY["deployment.environment.name"]),
}

STATE_PROBE = """
import json, logging
from llm_service import main
from opentelemetry import trace

provider = trace.get_tracer_provider()
resource = getattr(provider, "resource", None)
print(json.dumps({
    "contrib_instrumented": bool(getattr(main.app, "_is_instrumented_by_opentelemetry", False)),
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
        "contrib_instrumented": False,
        "sdk_provider": False,
        "service_name": None,
        "otlp_log_handlers": 0,
    }


@pytest.mark.parametrize("deployment_environment", ["staging", "pr-12", "branch-main", "canary-preview"])
def test_with_an_endpoint_and_the_rendered_identity_the_app_exports_without_the_contrib_instrumentor(
    deployment_environment: str,
) -> None:
    """Preview stacks run with `APP_ENV=staging` and carry their own alias as the issued name.
    No contrib instrumentor is attached (#1660): it would replace the stable HTTP metric."""
    environ = {
        **DEPLOYED,
        "APP_ENV": "staging",
        "OTEL_RESOURCE_ATTRIBUTES": _attributes(deployment_environment),
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:9",
    }
    result = run_python_probe(STATE_PROBE, environ)
    assert result.returncode == 0, result.stderr
    state = json.loads(result.stdout.splitlines()[-1])
    assert state == {
        "contrib_instrumented": False,
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
    """On the wire, in the shape infra2's preview compose renders: staging behaviour, own alias."""
    preview = {**DEPLOYED, "APP_ENV": "staging", "OTEL_RESOURCE_ATTRIBUTES": _attributes("pr-12")}
    with OtlpCollectorStub() as collector:
        result = run_python_probe(EXPORT_PROBE, {**preview, "OTEL_EXPORTER_OTLP_ENDPOINT": collector.endpoint})
    assert result.returncode == 0, result.stderr

    assert any("/mcp" in name for name in collector.span_names()), collector.span_names()
    # The stable HTTP server metric of FastAPI itself (#1660): seconds, with the route and the
    # status code as labels. The legacy `http.server.duration` (milliseconds) of the contrib
    # instrumentor must not appear: infra2's alert rules read one convention.
    series = collector.metric_series()
    durations = [(unit, attributes) for name, unit, attributes in series if name == "http.server.request.duration"]
    assert durations, sorted({name for name, _unit, _attributes in series})
    assert {unit for unit, _ in durations} == {"s"}
    assert any(
        attributes.get("http.route") == "/mcp" and attributes.get("http.response.status_code") == "307"
        for _unit, attributes in durations
    ), durations
    legacy = sorted({name for name, _unit, _attributes in series if name.startswith("http.server.duration")})
    assert legacy == [], legacy
    assert "llm-service probe failure" in collector.log_bodies()
    expected = {**IDENTITY, "deployment.environment.name": "pr-12", "service.name": "truealpha-app"}
    for signal in ("traces", "metrics", "logs"):
        resources = collector.resources(signal)
        assert resources, f"no {signal} reached the collector"
        for attributes in resources:
            for key, value in expected.items():
                assert attributes.get(key) == value, f"{signal}: {key} is {attributes.get(key)!r}"


METRIC_PROBE = """
from truealpha_runtime.telemetry import init_telemetry

providers = init_telemetry()
meter = providers.meter_provider.get_meter("probe")
meter.create_histogram("demo.duration", unit="s").record(0.25, {"http.route": "/x", "http.response.status_code": 307})
meter.create_counter("demo.count", unit="1").add(3, {"outcome": "ok"})
providers.meter_provider.force_flush()
"""


def test_the_collector_stub_reads_the_name_unit_and_attributes_of_each_metric_point() -> None:
    """`OtlpCollectorStub.metric_series` backs the HTTP metric assertion above (#1660): a name,
    a unit or a label that it misreports would let a wrong metric pass."""
    with OtlpCollectorStub() as collector:
        result = run_python_probe(METRIC_PROBE, {**DEPLOYED, "OTEL_EXPORTER_OTLP_ENDPOINT": collector.endpoint})
    assert result.returncode == 0, result.stderr
    series = collector.metric_series()
    assert ("demo.duration", "s", {"http.route": "/x", "http.response.status_code": "307"}) in series
    assert ("demo.count", "1", {"outcome": "ok"}) in series
